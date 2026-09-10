"""Shared fixtures. No test in this suite runs the real `twg` binary."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentgraph.connectors.base import SourceReference  # noqa: E402

from agentgraph_connector_twg import client, urls  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point AgentGraph's config directory at a temporary path."""
    config_dir = tmp_path / "agentgraph-config"
    config_dir.mkdir()
    monkeypatch.setenv("AGENTGRAPH_CONFIG_DIR", str(config_dir))
    client.reset_caches()
    yield config_dir
    client.reset_caches()


def _no_classification(url: str) -> SourceReference | None:
    _ = url
    return None


def _classify_twg_urls(url: str) -> SourceReference | None:
    target = urls.parse_url(url)
    return target.to_reference() if target is not None else None


@pytest.fixture(autouse=True)
def no_url_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `add_stubs_from` inert unless a test opts into URL classification."""
    monkeypatch.setattr("agentgraph.server.router.classify_url", _no_classification)


def classify_atlassian_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route URL classification through this connector's own resolver."""
    monkeypatch.setattr("agentgraph.server.router.classify_url", _classify_twg_urls)


@pytest.fixture()
def fake_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide a resolvable `twg` path without touching the real installation."""
    binary = tmp_path / "twg"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv(client.BINARY_ENV_VAR, str(binary))
    client.reset_binary_cache()
    return binary


ATLAS_ORG_ID = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
ATLAS_CLOUD_ID = "1f2e3d4c-5b6a-7988-9a0b-1c2d3e4f5a6b"


def atlas_url(kind: str, key: str) -> str:
    return f"https://home.atlassian.com/o/{ATLAS_ORG_ID}/s/{ATLAS_CLOUD_ID}/{kind}/{key}"


def workitem_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg jira workitem get --full` payload with the documented field shape."""
    payload: dict[str, Any] = {
        "key": "ENG-42",
        "summary": "Atlas sync drops updates",
        "url": "https://acme.atlassian.net/browse/ENG-42",
        "ari": "ari:cloud:jira:cloud-1:issue/10042",
        "created": "2026-08-01T09:30:00.000Z",
        "updated": "2026-08-20T14:00:00.000Z",
        "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
        "issueType": {"name": "Bug"},
        "priority": {"name": "High"},
        "project": {"key": "ENG", "name": "Engineering"},
        "labels": ["atlas", "sync"],
        "reporter": {
            "accountId": "acct-maya",
            "displayName": "Maya Chen",
            "emailAddress": "Maya@Acme.test",
        },
        "assignee": {"accountId": "acct-sam", "displayName": "Sam Ito"},
        "description": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Updates are lost when the sync retries. "},
                        {"type": "mention", "attrs": {"id": "acct-sam", "text": "@Sam Ito"}},
                        {"type": "text", "text": " please confirm."},
                    ],
                }
            ],
        },
        "comments": [
            {
                "author": {"accountId": "acct-dev", "displayName": "Dev Patel"},
                "created": "2026-08-19T11:00:00.000Z",
                "body": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Reproduced on staging."}]}
                    ],
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


def idea_payload(**overrides: Any) -> dict[str, Any]:
    """A JPD idea: a Jira work item in a `product_discovery` project with delivery links."""
    payload = workitem_payload(
        key="TIN-2284",
        summary="Land intent capture",
        url="https://acme.atlassian.net/browse/TIN-2284",
        project={"key": "TIN", "name": "Tinker", "projectTypeKey": "product_discovery"},
        issueType={"name": "Idea"},
        issuelinks=[
            {
                "id": "3939188",
                "type": {
                    "id": "10802",
                    "name": "Polaris work item link",
                    "inward": "is implemented by",
                    "outward": "implements",
                },
                "inwardIssue": {
                    "id": "13179506",
                    "key": "SQA-4374",
                    "fields": {
                        "summary": "[Experiment] Revised intent capture",
                        "status": {"name": "To Do"},
                        "issuetype": {"name": "Epic"},
                    },
                },
            },
            {
                "id": "3924752",
                "type": {
                    "id": "12180",
                    "name": "Discovery - Connected",
                    "inward": "is connected to",
                    "outward": "connects to",
                },
                "outwardIssue": {
                    "id": "13026108",
                    "key": "TIN-585",
                    "fields": {"summary": "Validating Land", "issuetype": {"name": "Opportunity"}},
                },
            },
        ],
    )
    payload.update(overrides)
    return payload


PAGE_BODY_MARKDOWN = f"""## Overview

Atlas sync runs every 15 minutes. @Sam Ito owns the retry path.

Tracked in [ENG-42](https://acme.atlassian.net/browse/ENG-42), described in
[the retry design](https://acme.atlassian.net/wiki/spaces/ENG/pages/884737/Retry+design),
delivered under [{atlas_url("project", "ATLAS-133324")}]({atlas_url("project", "ATLAS-133324")}?xpis=smart-link),
and demonstrated in [the walkthrough](https://www.loom.com/share/abc123def456).

Everything else lives in the [Engineering space](https://acme.atlassian.net/wiki/spaces/ENG)."""
"""A markdown body as `--format md` renders it.

The conversion is lossy in two ways this connector has to cope with: a mention
survives only as the plain text `@Sam Ito`, with no account id, and every link is
markdown, so a naive URL scan keeps the closing `)` — and, for the Atlas smart
link, a whole second URL after `](`.
"""


def page_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg confluence content get --detail full --format md` payload."""
    payload: dict[str, Any] = {
        "id": "884736",
        "type": "page",
        "title": "Atlas sync plan",
        "status": "current",
        "spaceId": "65539",
        # Confluence's own `_links.webui` shape: the page title is part of the URL.
        "url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Atlas+sync+plan",
        "detail": "full",
        "snapshotToken": "token-1",
        "createdAt": "2026-07-02T08:00:00Z",
        "summary": {"excerpt": "How Atlas sync works", "wordCount": 640, "sectionCount": 4},
        "outline": [
            {"level": 1, "text": "Overview", "anchor": "overview"},
            {"level": 2, "text": "Retry policy", "anchor": "retry"},
        ],
        "body": {"format": "md", "value": PAGE_BODY_MARKDOWN, "lossyConversion": True},
        "metadata": {
            "authorId": {"accountId": "acct-maya", "displayName": "Maya Chen"},
            "totalViews": 128,
            "version": {
                "number": 7,
                "message": "Clarify retries",
                "createdAt": "2026-08-18T10:15:00Z",
                "authorId": {"accountId": "acct-sam", "displayName": "Sam Ito"},
            },
        },
    }
    payload.update(overrides)
    return payload


def page_context_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg context confluence page <id> --detail full` payload.

    Every relationship comes back `inbound` with `AtlassianAccountUser` targets;
    `--detail full` is what supplies `accountId` and `email` on each one.
    """
    payload: dict[str, Any] = {
        "object": {
            "ari": "ari:cloud:confluence:cloud-1:page/884736",
            "type": "ConfluencePage",
            "name": "Atlas sync plan",
        },
        "relationships": [
            {
                "relationshipName": "atlassian_user_mentioned_in_confluence_page",
                "direction": "inbound",
                "targetType": "AtlassianAccountUser",
                "targets": [
                    {
                        "ari": "ari:cloud:identity::user/acct-sam",
                        "type": "AtlassianAccountUser",
                        "name": "Sam Ito",
                        "accountId": "acct-sam",
                        "email": "Sam@Acme.test",
                        "zoneinfo": "Australia/Sydney",
                    },
                    {
                        "ari": "ari:cloud:identity::user/acct-lee",
                        "type": "AtlassianAccountUser",
                        "name": "Lee Kim",
                        "accountId": "acct-lee",
                    },
                    # Confluence reports deactivated and anonymous mentions this
                    # way, with no account id to key a Person on.
                    {
                        "ari": "ari:cloud:identity::user/unidentified",
                        "type": "AtlassianAccountUser",
                    },
                ],
            },
            {
                "relationshipName": "atlassian_user_contributed_to_confluence_page",
                "direction": "inbound",
                "targetType": "AtlassianAccountUser",
                "targets": [
                    {
                        "ari": "ari:cloud:identity::user/acct-dev",
                        "type": "AtlassianAccountUser",
                        "name": "Dev Patel",
                        "accountId": "acct-dev",
                        "email": "dev@acme.test",
                    }
                ],
            },
            {
                "relationshipName": "atlassian_user_watches_confluence_page",
                "direction": "inbound",
                "targetType": "AtlassianAccountUser",
                "targets": [
                    {
                        "ari": "ari:cloud:identity::user/acct-maya",
                        "type": "AtlassianAccountUser",
                        "name": "Maya Chen",
                        "accountId": "acct-maya",
                        "email": "maya@acme.test",
                    }
                ],
            },
            {
                "relationshipName": "atlassian_user_viewed_confluence_page",
                "direction": "inbound",
                "targetType": "AtlassianAccountUser",
                "targets": [
                    {
                        "ari": "ari:cloud:identity::user/acct-viewer",
                        "type": "AtlassianAccountUser",
                        "name": "Casual Reader",
                        "accountId": "acct-viewer",
                    }
                ],
            },
        ],
        "pagination": {"first": 50, "hasMore": False},
    }
    payload.update(overrides)
    return payload


def goal_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg goals get --include-description` payload.

    A goal carries both `state` and `status`, a `targetDate`, a plain-string
    description, and tags as an edge/node graph.
    """
    payload: dict[str, Any] = {
        "id": f"ari:cloud:townsquare:{ATLAS_CLOUD_ID}:goal/b9b16695-a93f-447f-8131-f01e60f34829",
        "key": "ATLAS-131327",
        "name": "Prove Land is a useful JSM acquisition channel",
        "state": {"label": "On track - 0.7", "value": "on_track"},
        "status": {"label": "", "value": "on_track"},
        "owner": {"accountId": "acct-maya", "name": "Maya Chen"},
        "description": "Determine whether Land can become a reliable net-new acquisition channel.",
        "targetDate": {
            "label": "Jun 2027",
            "dateRange": {"start": "2027-06-01T00:00:00Z", "end": "2027-06-30T00:00:00Z"},
        },
        "parentGoal": {"key": "ATLAS-120001", "name": "Grow JSM"},
        "contributingProjects": [
            {
                "key": "ATLAS-133324",
                "name": "Land E2E journey",
                "url": atlas_url("project", "ATLAS-133324"),
            }
        ],
        "latestUserUpdate": {
            "ari": f"ari:cloud:townsquare:{ATLAS_CLOUD_ID}:goal-update/5e17f76c",
            "creationDate": "2026-08-14T03:35:22.191277Z",
            "summary": "July actuals landed at 883 against a target of 746.",
            "url": f"{atlas_url('goal', 'ATLAS-131327')}/updates/5e17f76c",
            "creator": {"name": "Anurag Datta Roy"},
            "newState": {"label": "On track - 0.7", "value": "on_track"},
        },
        "tags": {"edges": [{"node": {"name": "servco-fy27-goals"}}, {"node": {"name": "l3"}}]},
        "url": atlas_url("goal", "ATLAS-131327"),
        "isArchived": False,
    }
    payload.update(overrides)
    return payload


def atlas_project_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg projects get --include-description --include-linked-goals` payload.

    A project carries `state` only, a `dueDate`, a `startDate`, a
    `{what, why, measurement}` description, and tags as plain strings.
    """
    payload: dict[str, Any] = {
        "id": f"ari:cloud:townsquare:{ATLAS_CLOUD_ID}:project/74a23f0f-4a07-47b9-8367-571ebef1c114",
        "key": "ATLAS-133324",
        "name": "[Feature] Land E2E journey for IT Support",
        "state": {"label": "On track", "value": "on_track"},
        "owner": {"accountId": "acct-maya", "name": "Maya Chen"},
        "startDate": "2026-09-07",
        "latestUpdateDate": "2026-09-07T06:29:16.529933Z",
        "latestUserUpdate": {
            "ari": f"ari:cloud:townsquare:{ATLAS_CLOUD_ID}:project-update/1db40689",
            "creationDate": "2026-09-07T06:29:16.529933Z",
            "summaryText": "Kicking off officially. This will have three sub-projects.",
            "url": f"{atlas_url('project', 'ATLAS-133324')}/updates/1db40689",
            "creator": {"name": "Anurag Datta Roy"},
            "newState": {"label": "On track", "value": "on_track"},
            "notes": [],
        },
        "dueDate": {
            "label": "October",
            "dateRange": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
        },
        "description": {
            "what": "Build one coherent end-to-end Land journey for IT Support.",
            "why": "Intent capture alone creates little customer value on its own.",
            "measurement": "Improve Signup to D1T6AI conversion for the targeted journeys.",
        },
        "tags": ["sequoia-quality-signups", "itsm-experiment"],
        "linkedGoals": [
            {
                "id": f"ari:cloud:townsquare:{ATLAS_CLOUD_ID}:goal/b9b16695",
                "key": "ATLAS-131327",
                "name": "Prove Land is a useful JSM acquisition channel",
                "state": {"label": "On track - 0.7", "value": "on_track"},
                "owner": {"accountId": "acct-maya", "name": "Maya Chen"},
                "url": atlas_url("goal", "ATLAS-131327"),
            }
        ],
        "url": atlas_url("project", "ATLAS-133324"),
        "isArchived": False,
    }
    payload.update(overrides)
    return payload


def video_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg loom get` video payload."""
    payload: dict[str, Any] = {
        "id": "abc123def456",
        "name": "Atlas sync walkthrough",
        "description": "Five minute tour of the retry path.",
        "url": "https://www.loom.com/share/abc123def456",
        "createdAt": "2026-08-10T16:20:00Z",
        "duration": 312,
        "viewCount": 34,
        "privacy": "team",
        "owner": {"accountId": "acct-maya", "displayName": "Maya Chen", "email": "maya@acme.test"},
        "space": {"id": "space-9", "name": "Engineering recordings"},
    }
    payload.update(overrides)
    return payload
