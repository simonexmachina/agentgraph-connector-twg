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


def page_payload(**overrides: Any) -> dict[str, Any]:
    """A `twg confluence content get --detail full --format md` payload."""
    payload: dict[str, Any] = {
        "id": "884736",
        "type": "page",
        "title": "Atlas sync plan",
        "status": "current",
        "spaceId": "65539",
        "detail": "full",
        "snapshotToken": "token-1",
        "createdAt": "2026-07-02T08:00:00Z",
        "summary": {"excerpt": "How Atlas sync works", "wordCount": 640, "sectionCount": 4},
        "outline": [
            {"level": 1, "text": "Overview", "anchor": "overview"},
            {"level": 2, "text": "Retry policy", "anchor": "retry"},
        ],
        "body": {"format": "md", "value": "## Overview\n\nAtlas sync runs every 15 minutes.", "lossyConversion": True},
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
