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

A page name must never reach an observation record or entity metadata. `ObservationMutation` is a
deliberately low-information signal, and AgentGraph publishes it to every installed feed connector,
so any URL arriving from a payload or a caller goes through `urls.canonical_url` — which rebuilds it
from what `parse_url` keeps, i.e. `…/wiki/spaces/<KEY>/pages/<id>`. That covers
`confluence.page_to_batch` (`twg confluence content get` returns Confluence's `_links.webui` shape,
which ends in the title) and `_target()` in `__init__.py` (`agentgraph fetch --meta`, or a stale
stored `fetch_meta`). Core then prefers `fetch_meta["web_url"]`, then `entity_url()`, over the
browsed URL. `EntityUpsertMutation` still carries the title and body by design — that is the point
of a snapshot; this closes the leak on the observation signal and on `metadata["web_url"]`.

## Work Item Context

A work item's relationship context comes from `twg context get <key> --type jira-workitem --types
<node types> --detail full` (`jira.CONTEXT_TARGET_TYPES`), not from `twg context jira workitem`.
The per-product command never reports `project_links_to_entity`, so the Atlas project tracking the
same work never reaches the graph, and it returns one referenced Confluence page where the
type-selected traversal returns eleven. `--type jira-workitem` is not optional: a bare key is
ambiguous with `atlas-goal` and the call fails.

`--types` is a traversal instruction, not a post-filter. Unfiltered, `content_referenced_entity`
reports `n=1` — the relationship's own count, not a truncated slice — and ten of eighteen
relationship types come back `omittedByBudget`; naming the node types re-plans the traversal, so
the same relationship on the same anchor reports `n=11` with nothing omitted. `twg` silently
ignores type names it does not know, so the list is safe to extend as graph coverage appears.
Leave `--first` at its default: raising it to the 200 maximum recovers none of those pages and
spends the allocation on Bitbucket deployment URLs this connector cannot classify, at 3.4× the
payload.

`jira.context_to_batch` filters nothing itself — it walks every group, relationship, and target,
keeping whatever `stubs.stub_for_url` can classify — and honours the direction `twg` reports, so
both useful relationships, which come back `inbound`, produce edges running target → work item.

## Entity Types

This connector targets AgentGraph 0.7.0 through 0.7.x (`pyproject.toml`), which is what provides
the `Task` and `Video` entity types it emits. Do not emit an entity type outside that release's
vocabulary.

## Local Setup

Machine-specific paths, server lifecycle, and editable-install state are in `AGENTS.local.md`
(gitignored) when present.
