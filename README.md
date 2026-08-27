# agentgraph-connector-twg

An [AgentGraph](https://github.com/simonexmachina/agent-graph) connector for the Atlassian
Teamwork Graph, built on the `twg` CLI.

| Resource | Entity | Content indexed |
| --- | --- | --- |
| Jira work item | `Task` | Summary, description, comments, status, assignee, labels |
| Jira project | `Folder` | Container for its work items |
| Confluence page | `Document` | Heading outline plus the page body as markdown |
| Confluence space | `Folder` | Container for its pages |
| Loom video | `Video` | Title, description, and the **transcript**; `metadata.web_url` is the watch link |

People become `Person` entities from reporters, assignees, commenters, authors, editors, video
owners, and `@` mentions. Relationships from `twg context jira workitem` become `references` edges,
with stub entities for linked resources that have not been fetched yet.

This package is kept outside the AgentGraph repository because `twg` is internal Atlassian tooling.

## Requirements

- AgentGraph 0.6 or newer (it provides the `Task` and `Video` entity types this connector emits).
- The `twg` CLI, authenticated. The connector never authenticates on your behalf.

## Install

```bash
uv pip install -e /path/to/agentgraph-connector-twg --no-deps
```

`--no-deps` keeps uv from re-resolving the AgentGraph packages already installed in the target
environment. Re-run the command after `uv sync` in the AgentGraph repo, which can prune editable
installs that are not workspace members.

Confirm registration and configure your site:

```bash
agentgraph list-connectors
```

```bash
agentgraph connector twg add-site <site>
```

```bash
agentgraph connector twg status
```

## Authentication

`twg` owns the Atlassian OAuth session. When it expires, the connector reports it and every fetch
returns the same remediation:

```bash
twg auth refresh
```

Run that in a real terminal. `twg` needs write access to `~/.config/twg`, which a sandboxed agent
cannot grant. Under the `com.agentgraph.server` LaunchAgent the server can refresh the session
itself.

If `twg` is not on `PATH` — likely under launchd — the connector probes `~/.local/bin/twg`,
`/opt/homebrew/bin/twg` and `/usr/local/bin/twg`. Set `AGENTGRAPH_TWG_BIN` to override.

## Configuration

Settings live in `<agentgraph-config-dir>/twg.json` (usually `~/.agentgraph/twg.json`) and are
managed through the CLI:

| Command | Effect |
| --- | --- |
| `agentgraph connector twg add-site <site>` / `remove-site` | Atlassian sites; the first is the default `--site` |
| `agentgraph connector twg add-jql "<jql>"` / `remove-jql` | JQL swept by ingest, then queues an ingest |
| `agentgraph connector twg add-space <KEY>` / `remove-space` | Confluence spaces swept by ingest, then queues an ingest |
| `agentgraph connector twg videos <on\|off>` | Index Loom videos and transcripts |
| `agentgraph connector twg status` | Binary, version, session, and configured scopes |

## Refresh behaviour

- **Observe / fetch:** browsing or fetching a supported URL fetches that resource. Confluence short
  links (`/wiki/x/<tiny>`) are resolved through `twg resolve`.
- **Poll (every 30 minutes):** `twg work query --scope me` covers your own recently touched work
  items, pages, and videos, hydrating at most `poll_item_limit` (default 50) resources per run and
  skipping anything fetched within the last 15 minutes.
- **Ingest:** the same sweep over `ingest_since` (default 90 days), plus every configured JQL query
  and Confluence space.

Only your own activity and explicitly configured scopes are indexed.

## Data locality

Indexed Jira, Confluence, and Loom content — including transcripts — is stored in the local
AgentGraph SQLite database and becomes readable by any MCP client you connect. This is internal
work data; choose your polled scopes deliberately.

## Development

```bash
uv run --with pytest --with pytest-asyncio pytest -q
```

Tests never invoke the real `twg`: the subprocess layer is patched and payload fixtures live in
`tests/conftest.py`. Those fixtures follow the field paths reported by
`twg help describe "<command>"`. Re-record them from live output when a `twg` upgrade changes a
payload, and keep parsing tolerant — `payloads.py` reads the documented field first and falls back
to known aliases.
