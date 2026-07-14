.PHONY: audit archive-help export-help test check release-check safety-check install-hooks

audit:
	./codex-history audit

archive-help:
	./codex-history archive --help

export-help:
	./codex-history export --help

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

check: safety-check
	PYTHONPATH=src python3 -m compileall -q src tests

release-check: test check
	git diff --check

safety-check:
	PYTHONPATH=src python3 -m codex_history.artifact_guard tracked
	PYTHONPATH=src python3 -m codex_history.artifact_guard history

install-hooks:
	git config core.hooksPath .githooks
