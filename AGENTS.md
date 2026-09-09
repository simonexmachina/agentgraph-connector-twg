## Quality Gates

Run these three, and only through the venv interpreter, matching `bitbucket-pipelines.yml`:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

Do not use `uv run` or `uv sync` for these. A contributor may have AgentGraph installed editable
from a local checkout, and both commands sync exactly against `uv.lock` and silently revert it.
See README.md § Development.

## twg Fixtures

Tests must never invoke the real `twg`. The subprocess layer is patched and every payload fixture
lives in `tests/conftest.py`, following the field paths from `twg help describe "<command>"`.
When adding a code path, add its fixture there rather than reaching for the live CLI.

Parsing stays tolerant: `payloads.py` reads the documented field first, then known aliases. Keep
that shape when handling a new field.

## Entity Types

This connector targets AgentGraph 0.6.1 through 0.6.x (`pyproject.toml`), which is what provides
the `Task` and `Video` entity types it emits. Do not emit an entity type outside that release's
vocabulary.

## Local Setup

Machine-specific paths, server lifecycle, and editable-install state are in `AGENTS.local.md`
(gitignored) when present.
