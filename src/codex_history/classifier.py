"""Versioned, content-minimizing classification rules for Codex rollouts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Optional, Tuple


RULESET_VERSION = "2"

THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
FILENAME_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4,5}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
REPORTABLE_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4,5}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
CLI_VERSION_RE = re.compile(
    r"^\d{1,6}(?:\.\d{1,6}){1,3}(?:-(?:alpha|beta|rc)(?:\.\d{1,6})?)?(?:\+[0-9A-Za-z.-]{1,32})?$"
)

KNOWN_STRING_SOURCES = {"cli", "exec", "vscode"}
KNOWN_ORIGINATORS = {
    "codex desktop": "Codex Desktop",
    "codex-tui": "codex-tui",
    "codex_cli_rs": "codex_cli_rs",
    "codex_exec": "codex_exec",
    "codex_vscode": "codex_vscode",
}
OFFICIAL_INTERACTIVE_ORIGINATORS = frozenset(
    {"codex desktop", "codex-tui", "codex_cli_rs", "codex_vscode"}
)


@dataclass(frozen=True)
class OriginClassification:
    origin_class: str
    surface: str
    rule_code: str
    confidence: str


def _is_known_thread_spawn(source: Dict[str, Any]) -> bool:
    """Accept only the exact object schema observed in current Codex rollouts."""
    if set(source) != {"subagent"} or not isinstance(source.get("subagent"), dict):
        return False
    subagent = source["subagent"]
    if set(subagent) != {"thread_spawn"} or not isinstance(
        subagent.get("thread_spawn"), dict
    ):
        return False
    spawn = subagent["thread_spawn"]
    if set(spawn) != {
        "agent_nickname",
        "agent_path",
        "agent_role",
        "depth",
        "parent_thread_id",
    }:
        return False
    return (
        isinstance(spawn.get("agent_nickname"), str)
        and (spawn.get("agent_path") is None or isinstance(spawn.get("agent_path"), str))
        and (spawn.get("agent_role") is None or isinstance(spawn.get("agent_role"), str))
        and type(spawn.get("depth")) is int
        and spawn["depth"] >= 0
        and isinstance(spawn.get("parent_thread_id"), str)
        and bool(spawn["parent_thread_id"])
    )


def _is_known_guardian(source: Dict[str, Any]) -> bool:
    if set(source) != {"subagent"} or not isinstance(source.get("subagent"), dict):
        return False
    return source["subagent"] == {"other": "guardian"}


def normalize_source(source: Any) -> Dict[str, str]:
    """Return a safe structural description without copying arbitrary metadata."""
    if isinstance(source, str):
        raw_kind = source.strip().lower()
        kind = raw_kind if raw_kind in KNOWN_STRING_SOURCES else "other"
        return {"type": "string", "kind": kind}

    if isinstance(source, dict):
        if _is_known_thread_spawn(source):
            kind = "subagent_thread_spawn"
        elif _is_known_guardian(source):
            kind = "subagent_guardian"
        elif set(source) == {"subagent"}:
            kind = "subagent_unknown"
        else:
            kind = "object_unknown"
        return {"type": "object", "kind": kind}

    if source is None:
        return {"type": "null", "kind": "null"}
    return {"type": type(source).__name__, "kind": "unknown"}


def normalize_automated_originators(originators: Iterable[str] = ()) -> Tuple[str, ...]:
    """Canonicalize an explicit custom-automation allow-list.

    Matching is by complete normalized originator name, never by substring. Official
    interactive Codex originators are reserved so a configuration mistake cannot
    reclassify ordinary interactive sessions as automation.
    """
    if isinstance(originators, str):
        originators = (originators,)
    normalized = set()
    for originator in originators:
        if not isinstance(originator, str):
            raise ValueError("automated originator names must be strings")
        name = originator.strip().casefold()
        if not name:
            raise ValueError("automated originator names cannot be empty")
        if name in OFFICIAL_INTERACTIVE_ORIGINATORS:
            raise ValueError(
                "official interactive Codex originators cannot be configured as automation"
            )
        normalized.add(name)
    return tuple(sorted(normalized))


def classify_origin(
    session_meta: Dict[str, Any], automated_originators: Iterable[str] = ()
) -> OriginClassification:
    raw_originator = session_meta.get("originator")
    originator = raw_originator.strip() if isinstance(raw_originator, str) else ""
    originator_normalized = originator.casefold()
    configured_automation = frozenset(
        normalize_automated_originators(automated_originators)
    )
    source = normalize_source(session_meta.get("source"))
    source_type = source["type"]
    source_kind = source["kind"]

    # Object-valued sources take precedence so children are never mistaken for roots.
    if source_type == "object" and source_kind == "subagent_thread_spawn":
        return OriginClassification(
            "automated", "subagent", "AUTOMATED_SUBAGENT_THREAD_SPAWN", "high"
        )
    if source_type == "object" and source_kind == "subagent_guardian":
        return OriginClassification(
            "automated", "guardian", "AUTOMATED_SUBAGENT_GUARDIAN", "high"
        )
    if source_type == "object":
        return OriginClassification(
            "ambiguous", "unknown", "AMBIGUOUS_UNKNOWN_SOURCE_OBJECT", "low"
        )

    if source_type == "string" and source_kind == "exec":
        return OriginClassification("automated", "exec", "AUTOMATED_EXEC", "high")

    if originator_normalized in configured_automation:
        return OriginClassification(
            "automated",
            "custom_automation",
            "AUTOMATED_CONFIGURED_ORIGINATOR",
            "high",
        )

    if originator_normalized == "codex desktop" and source_kind == "vscode":
        return OriginClassification(
            "interactive", "codex_app", "INTERACTIVE_CODEX_DESKTOP", "high"
        )
    if originator_normalized == "codex_vscode" and source_kind == "vscode":
        return OriginClassification(
            "interactive", "vscode", "INTERACTIVE_VSCODE", "high"
        )
    if originator_normalized == "codex_cli_rs" and source_kind == "cli":
        return OriginClassification(
            "interactive", "cli", "INTERACTIVE_CLI", "high"
        )
    if originator_normalized == "codex-tui" and source_kind == "cli":
        return OriginClassification(
            "interactive", "tui", "INTERACTIVE_TUI", "high"
        )

    return OriginClassification(
        "ambiguous", "unknown", "AMBIGUOUS_UNMATCHED_ORIGIN", "low"
    )


def is_user_message_event(record: Any) -> bool:
    """Detect a genuine persisted user turn without inspecting its contents."""
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return False
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "user_message"


def is_valid_thread_id(thread_id: Optional[str]) -> bool:
    return isinstance(thread_id, str) and bool(THREAD_ID_RE.fullmatch(thread_id))


def thread_id_for_report(thread_id: Any) -> Optional[str]:
    """Keep known opaque ID shapes; replace arbitrary custom values with a digest."""
    if thread_id is None:
        return None
    text = str(thread_id)
    if REPORTABLE_THREAD_ID_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return "invalid:%s" % digest


def originator_for_report(
    originator: Any, automated_originators: Iterable[str] = ()
) -> str:
    if not isinstance(originator, str) or not originator.strip():
        return "unknown"
    normalized = originator.strip().casefold()
    if normalized in frozenset(normalize_automated_originators(automated_originators)):
        return "custom_automation"
    return KNOWN_ORIGINATORS.get(normalized, "other")


def thread_source_for_report(thread_source: Any) -> Optional[str]:
    if thread_source is None or thread_source == "":
        return None
    if isinstance(thread_source, str) and thread_source.lower() in {"subagent", "user"}:
        return thread_source.lower()
    return "other"


def model_provider_for_report(model_provider: Any) -> Optional[str]:
    if model_provider is None or model_provider == "":
        return None
    if isinstance(model_provider, str) and model_provider.lower() in {"openai", "proxy"}:
        return model_provider.lower()
    return "other"


def cli_version_for_report(cli_version: Any) -> Optional[str]:
    if cli_version is None or cli_version == "":
        return None
    if isinstance(cli_version, str) and CLI_VERSION_RE.fullmatch(cli_version):
        return cli_version
    return "other"


def timestamp_for_report(timestamp: Any) -> Optional[str]:
    if not isinstance(timestamp, str) or not ISO_TIMESTAMP_RE.fullmatch(timestamp):
        return None
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp


def filename_thread_id(filename: str) -> Optional[str]:
    match = FILENAME_ID_RE.search(filename)
    return match.group(1) if match else None


def source_for_report(source: Any) -> str:
    normalized = normalize_source(source)
    return "%s:%s" % (normalized["type"], normalized["kind"])


def parse_database_source(source: Any) -> Any:
    """Decode SQLite's compact JSON object sources for structural comparison."""
    if not isinstance(source, str):
        return source
    stripped = source.strip()
    if not stripped.startswith(("{", "[")):
        return source
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return source
