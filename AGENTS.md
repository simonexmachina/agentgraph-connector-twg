## Quality Gates

Run these three, and only through the venv interpreter, matching `.github/workflows/ci.yml`:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

Locally, do not use `uv run` or `uv sync` for these. A contributor may have AgentGraph installed
editable from a local checkout, and both commands sync `.venv` exactly against `uv.lock` and
silently revert it. See README.md § Development. CI is unaffected — it runs `uv sync --locked`
from a clean checkout, where there is no editable install to lose.

## twg Fixtures

Tests must never invoke the real `twg`. The subprocess layer is patched and every payload fixture
lives in `tests/conftest.py`, following the field paths from `twg help describe "<command>"`.
When adding a code path, add its fixture there rather than reaching for the live CLI.

Parsing stays tolerant: `payloads.py` reads the documented field first, then known aliases. Keep
that shape when handling a new field.

## Entity Types

This connector targets AgentGraph 0.7.0 through 0.7.x (`pyproject.toml`), which is what provides
the `Task` and `Video` entity types it emits. Do not emit an entity type outside that release's
vocabulary.

## Local Setup

Machine-specific paths, server lifecycle, and editable-install state are in `AGENTS.local.md`
(gitignored) when present.
