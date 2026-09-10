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

## Confluence People and Links

A Confluence body is fetched as `--format md`, and that conversion drops account ids: a mention
survives only as the plain text `@Name`. So `payloads.collect_mentions` — which walks ADF `mention`
nodes and works for Jira — finds nothing in a page. The people a page names come from a second
call, `twg context confluence page <id> --detail full`, mapped by `confluence.context_to_batch`.
Keep `--detail full`: the default summary omits each target's `accountId` and `email` and truncates
the lists.

Markdown links also mean `EntityBatch.add_stubs_from` cannot be used on a page — its URL pattern
keeps the trailing `)` — so `stubs.references_in_text` normalises first. `jira`, `atlas` and `loom`
stay on `add_stubs_from` because their bodies are ADF, where URLs are whitespace-delimited.

## Entity Types

This connector targets AgentGraph 0.7.0 through 0.7.x (`pyproject.toml`), which is what provides
the `Task` and `Video` entity types it emits. Do not emit an entity type outside that release's
vocabulary.

## Local Setup

Machine-specific paths, server lifecycle, and editable-install state are in `AGENTS.local.md`
(gitignored) when present.
