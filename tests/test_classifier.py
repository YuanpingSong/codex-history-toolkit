import unittest

from codex_history.classifier import (
    classify_origin,
    filename_thread_id,
    is_user_message_event,
    is_valid_thread_id,
    normalize_automated_originators,
    normalize_source,
    originator_for_report,
)


class ClassifierTests(unittest.TestCase):
    def test_known_interactive_origins(self):
        cases = [
            ("Codex Desktop", "vscode", "codex_app"),
            ("codex_vscode", "vscode", "vscode"),
            ("codex_cli_rs", "cli", "cli"),
            ("codex-tui", "cli", "tui"),
        ]
        for originator, source, surface in cases:
            with self.subTest(originator=originator):
                result = classify_origin({"originator": originator, "source": source})
                self.assertEqual(result.origin_class, "interactive")
                self.assertEqual(result.surface, surface)
                self.assertEqual(result.confidence, "high")

    def test_known_automated_origins(self):
        cases = [
            ({"originator": "codex_exec", "source": "exec"}, "exec"),
            (
                {
                    "originator": "Codex Desktop",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "agent_nickname": "reviewer",
                                "agent_path": None,
                                "agent_role": "review",
                                "depth": 1,
                                "parent_thread_id": "019e0000-0000-7000-8000-000000000001",
                            }
                        }
                    },
                },
                "subagent",
            ),
            (
                {
                    "originator": "Codex Desktop",
                    "source": {"subagent": {"other": "guardian"}},
                },
                "guardian",
            ),
        ]
        for meta, surface in cases:
            with self.subTest(surface=surface):
                result = classify_origin(meta)
                self.assertEqual(result.origin_class, "automated")
                self.assertEqual(result.surface, surface)

    def test_explicit_custom_originators_are_exact_matched_and_redacted(self):
        configured = normalize_automated_originators(
            ["  Example Orchestrator  ", "example orchestrator", "Build Bot"]
        )
        self.assertEqual(configured, ("build bot", "example orchestrator"))

        result = classify_origin(
            {"originator": "EXAMPLE ORCHESTRATOR", "source": "vscode"},
            configured,
        )
        self.assertEqual(result.origin_class, "automated")
        self.assertEqual(result.surface, "custom_automation")
        self.assertEqual(result.rule_code, "AUTOMATED_CONFIGURED_ORIGINATOR")
        self.assertEqual(result.confidence, "high")
        self.assertEqual(
            originator_for_report("Example Orchestrator", configured),
            "custom_automation",
        )

        near_match = classify_origin(
            {"originator": "Example Orchestrator Child", "source": "vscode"},
            configured,
        )
        self.assertEqual(near_match.origin_class, "ambiguous")
        self.assertEqual(originator_for_report("unconfigured product"), "other")

    def test_official_interactive_originators_cannot_be_configured_as_automation(self):
        with self.assertRaises(ValueError):
            normalize_automated_originators(["Codex Desktop"])

    def test_object_source_precedes_human_originator(self):
        result = classify_origin(
            {
                "originator": "Codex Desktop",
                "source": {"subagent": {"other": "guardian"}},
            }
        )
        self.assertEqual(result.origin_class, "automated")

    def test_structural_sources_precede_configured_custom_originators(self):
        configured = ["example orchestrator"]
        guardian = classify_origin(
            {
                "originator": "example orchestrator",
                "source": {"subagent": {"other": "guardian"}},
            },
            configured,
        )
        execute = classify_origin(
            {"originator": "example orchestrator", "source": "exec"},
            configured,
        )
        self.assertEqual(guardian.surface, "guardian")
        self.assertEqual(execute.surface, "exec")

    def test_unknown_object_and_unmatched_origin_are_ambiguous(self):
        unknown_sources = [
            {"new": "shape"},
            {"subagent": {"thread_spawn": False}},
            {"subagent": {"thread_spawn": {"depth": 1}}},
            {"metadata": {"subagent": "guardian"}},
            {"subagent": {"other": "guardian", "future": True}},
        ]
        for source in unknown_sources:
            with self.subTest(source=source):
                self.assertEqual(
                    classify_origin({"originator": "future", "source": source}).origin_class,
                    "ambiguous",
                )
        self.assertEqual(
            classify_origin(
                {"originator": "unconfigured product", "source": "vscode"}
            ).origin_class,
            "ambiguous",
        )

    def test_user_message_requires_anchored_event_shape(self):
        self.assertTrue(
            is_user_message_event(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "secret"}}
            )
        )
        self.assertFalse(
            is_user_message_event(
                {"type": "response_item", "payload": {"type": "message", "role": "user"}}
            )
        )

    def test_thread_ids_are_opaque_but_uuid_shape_is_diagnostic(self):
        valid = "00000000-0000-4000-8000-000000000001"
        malformed = "00000000-0000-4000-80000-000000000001"
        self.assertTrue(is_valid_thread_id(valid))
        self.assertFalse(is_valid_thread_id(malformed))
        self.assertEqual(
            filename_thread_id("rollout-2026-04-29T21-00-32-%s.jsonl" % malformed),
            malformed,
        )

    def test_source_normalization_does_not_expose_object_values(self):
        normalized = normalize_source(
            {
                "subagent": {
                    "thread_spawn": {
                        "agent_nickname": "reviewer",
                        "agent_path": "/private/canary",
                        "agent_role": None,
                        "depth": 1,
                        "parent_thread_id": "019e0000-0000-7000-8000-000000000001",
                    }
                }
            }
        )
        self.assertEqual(normalized, {"type": "object", "kind": "subagent_thread_spawn"})
        self.assertNotIn("canary", str(normalized))


if __name__ == "__main__":
    unittest.main()
