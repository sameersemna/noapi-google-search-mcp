## What this changes

<!-- One paragraph. If it fixes a bug, describe the symptom and the mechanism. -->

## Why

<!-- The motivation. What breaks or what is impossible without this? -->

## Checklist

- [ ] `pytest -m "not live" -q` passes (offline suite — no network, no browser)
- [ ] `pytest -m "not live" -q --cov=google_search_mcp.core` still meets the 80% gate
- [ ] `ruff check` passes on the gated paths (`core/`, `errors.py`,
      `tools/annotations.py`, `__init__.py`, `__main__.py`)
- [ ] `python -m google_search_mcp --check` exits 0

## If this touches a tool's signature or output

- [ ] `tools/annotations.py` `_SPECS` updated
- [ ] `core/cache.py` `PER_TOOL_TTL` updated (read-only tools only)
- [ ] `tests/unit/test_tool_registry.py` `EXPECTED` + `EXPECTED_TOOL_COUNT` updated
- [ ] Tool count strings in `pyproject.toml`, `__init__.py`, `README.md` updated
- [ ] `CHANGELOG.md` updated

## If this touches dependency metadata

- [ ] Core install works **without** the optional extras (`pip install -e . --no-deps`)
- [ ] Python versions installed against: <!-- e.g. 3.11, 3.13 -->
- [ ] New direct imports are declared in `[project] dependencies` — not left to a
      transitive dependency

## Notes for the reviewer

<!-- Anything you are unsure about, trade-offs, or follow-up work you deliberately left out. -->
