"""Connector behaviour: fetch dispatch, refresh, and connector-owned CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agentgraph.connectors.base import EntityBatch, ResourceUnavailableError
from conftest import ATLAS_CLOUD_ID as CLOUD
from conftest import ATLAS_ORG_ID as ORG
from conftest import (
    atlas_project_payload,
    atlas_url,
    classify_atlassian_urls,
    goal_payload,
    page_context_payload,
    page_payload,
    video_payload,
    workitem_payload,
)

import agentgraph_connector_twg as twg_connector
from agentgraph_connector_twg import config
from agentgraph_connector_twg.client import TwgAuthError, TwgCommandError, TwgNotInstalledError


class _FakeTwg:
    """Records `twg` invocations and replays canned envelopes."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.sites: list[str | None] = []

    async def __call__(
        self,
        args: Sequence[str],
        *,
        site: str | None = None,
        timeout: float = 45.0,
        json_output: bool = True,
    ) -> dict[str, Any]:
        _ = (timeout, json_output)
        argv = list(args)
        self.calls.append(argv)
        self.sites.append(site)
        for prefix, response in self.responses.items():
            if " ".join(argv).startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(argv)  # pyright: ignore[reportUnknownVariableType, reportReturnType]
                return {"data": response}
        raise TwgCommandError(f"unexpected call: {' '.join(argv)}")

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


@pytest.fixture()
def connector(monkeypatch: pytest.MonkeyPatch) -> twg_connector.TwgConnector:
    monkeypatch.setattr(twg_connector.TwgConnector, "_is_fresh", _never_fresh)
    return twg_connector.TwgConnector()


async def _never_fresh(self: Any, entity_id: str) -> bool:
    _ = (self, entity_id)
    return False


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeTwg) -> None:
    monkeypatch.setattr(twg_connector, "run_twg", fake)


def _patch_whoami(monkeypatch: pytest.MonkeyPatch, user: dict[str, str] | None) -> None:
    def _whoami(*, refresh: bool = False) -> dict[str, str] | None:
        _ = refresh
        return user

    monkeypatch.setattr(twg_connector, "whoami", _whoami)


def _patch_version(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    def _version() -> str | None:
        return version

    monkeypatch.setattr(twg_connector, "twg_version", _version)


# ----------------------------------------------------------------------
# URL ownership
# ----------------------------------------------------------------------


def test_can_handle_claims_atlassian_and_loom_urls(connector: twg_connector.TwgConnector) -> None:
    assert connector.can_handle("https://acme.atlassian.net/browse/ENG-42") is True
    assert connector.can_handle("https://acme.atlassian.net/wiki/x/AbCd") is True
    assert connector.can_handle("https://www.loom.com/share/abc123") is True
    assert connector.can_handle("https://example.com/") is False


def test_normalise_fetch_id_maps_stored_ids_to_resource_types(
    connector: twg_connector.TwgConnector,
) -> None:
    assert connector.normalise_fetch_id("jira/acme/ENG-42", "Task") == ("jira/acme/ENG-42", "work-item")
    assert connector.normalise_fetch_id("loom/abc123", "Video") == ("loom/abc123", "video")
    assert connector.normalise_fetch_id("confluence/acme/884736", "Document") == (
        "confluence/acme/884736",
        "document",
    )


def test_entity_url_rebuilds_watch_and_browse_links(
    connector: twg_connector.TwgConnector,
) -> None:
    assert connector.entity_url("jira/acme/ENG-42") == "https://acme.atlassian.net/browse/ENG-42"
    assert connector.entity_url("loom/abc123") == "https://www.loom.com/share/abc123"
    assert connector.entity_url("gmail/thread") is None


async def test_tiny_wiki_links_resolve_through_twg(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"resolve": {"response": "ari:cloud:confluence:cloud-1:page/884736"}})
    _install(monkeypatch, fake)

    reference = await connector.resolve_observation_url("https://acme.atlassian.net/wiki/x/AbCd")

    assert reference is not None
    assert reference.resource_id == "confluence/acme/884736"


async def test_unresolvable_tiny_link_returns_none(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeTwg({"resolve": TwgCommandError("no match")}))

    assert await connector.resolve_observation_url("https://acme.atlassian.net/wiki/x/AbCd") is None


async def test_observation_patterns_scope_to_configured_sites(
    connector: twg_connector.TwgConnector,
) -> None:
    config.save_settings(config.TwgSettings(sites=["hello", "acme"]))

    patterns = await connector.observation_url_patterns()

    assert "https://hello.atlassian.net/wiki/*" in patterns
    assert "https://hello.atlassian.net/browse/*" in patterns
    assert "https://hello.jira.atlassian.cloud/browse/*" in patterns
    assert "https://acme.atlassian.net/jira/*" in patterns
    assert not any("https://*." in pattern for pattern in patterns)
    assert "https://www.loom.com/share/*" in patterns


async def test_observation_patterns_fall_back_to_the_wildcard(
    connector: twg_connector.TwgConnector,
) -> None:
    patterns = await connector.observation_url_patterns()

    assert patterns == twg_connector.TwgConnector.url_patterns


def test_personal_space_pages_resolve(connector: twg_connector.TwgConnector) -> None:
    url = (
        "https://hello.atlassian.net/wiki/spaces/"
        "~71202099c5dd7aaf6d47bbbc210f883d8259bb/pages/7650323840/Team+Brain"
    )

    reference = connector.resolve_url(url)

    assert reference is not None
    assert reference.resource_id == "confluence/hello/7650323840"
    assert reference.fetch_meta is not None
    assert reference.fetch_meta["space_key"] == "~71202099c5dd7aaf6d47bbbc210f883d8259bb"


# ----------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------


async def test_fetch_workitem_includes_context_relationships(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "jira workitem get": workitem_payload(),
            "context jira workitem": {
                "relationshipSummary": [
                    {
                        "relationshipName": "documented-by",
                        "targets": [
                            {"url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Plan"}
                        ],
                    }
                ]
            },
        }
    )
    _install(monkeypatch, fake)
    classify_atlassian_urls(monkeypatch)

    batch = await connector.fetch("work-item", "jira/acme/ENG-42")

    assert any(entity.entity_type == "Task" for entity in batch.entities)
    assert any(entity.platform_entity_id == "confluence/acme/884736" for entity in batch.entities)
    assert "jira workitem get ENG-42 --full" in fake.commands()


async def test_fetch_workitem_survives_missing_context(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _FakeTwg(
            {
                "jira workitem get": workitem_payload(),
                "context jira workitem": TwgCommandError("context unavailable"),
            }
        ),
    )

    batch = await connector.fetch("work-item", "jira/acme/ENG-42")

    assert [entity.entity_type for entity in batch.entities] == ["Task", "Folder"]


async def test_fetch_workitem_raises_when_no_data(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeTwg({"jira workitem get": None}))

    with pytest.raises(ResourceUnavailableError):
        await connector.fetch("work-item", "jira/acme/ENG-42")


async def test_fetch_page_requests_markdown_and_resolves_space(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "confluence content get": page_payload(),
            "confluence space get": {"id": "65539", "key": "ENG", "name": "Engineering"},
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch("document", "confluence/acme/884736")

    page = next(entity for entity in batch.entities if entity.entity_type == "Document")
    assert page.metadata["space_key"] == "ENG"
    folder = next(entity for entity in batch.entities if entity.entity_type == "Folder")
    assert folder.title == "Engineering"
    assert any("--format md" in command for command in fake.commands())
    assert any("confluence space get 65539" in command for command in fake.commands())
    assert not any(command.startswith("user bulk-lookup") for command in fake.commands())


async def test_fetch_page_strips_the_title_from_a_caller_supplied_url(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = page_payload()
    # Leave the caller's `web_url` as the only candidate, so it is what is tested.
    del payload["url"]
    fake = _FakeTwg(
        {
            "confluence content get": payload,
            "confluence space get": {"id": "65539", "key": "ENG", "name": "Engineering"},
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch(
        "document",
        "confluence/acme/884736",
        meta={
            "space_key": "ENG",
            "web_url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Atlas+sync+plan",
        },
    )

    page = next(entity for entity in batch.entities if entity.entity_type == "Document")
    assert page.metadata["web_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736"


async def test_fetch_page_includes_context_people_and_edges(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "confluence content get": page_payload(),
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "context confluence page": page_context_payload(),
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch("document", "confluence/acme/884736")

    assert "context confluence page 884736 --detail full" in fake.commands()
    assert {
        edge.target_platform_user_id for edge in batch.edges if edge.edge_type == "mentions"
    } == {"acct-sam", "acct-lee"}
    # `--detail full` supplies emails, so mentioned people need no directory lookup.
    assert not any(command.startswith("user bulk-lookup") for command in fake.commands())


async def test_fetch_page_survives_missing_context(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "confluence content get": page_payload(),
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "context confluence page": TwgCommandError("context unavailable"),
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch("document", "confluence/acme/884736")

    assert any(entity.entity_type == "Document" for entity in batch.entities)
    assert not any(edge.edge_type == "mentions" for edge in batch.edges)


async def test_page_tiny_links_resolve_once_and_are_cached(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = page_payload(
        body={
            "format": "md",
            "value": (
                "See [the plan](https://acme.atlassian.net/wiki/x/AbCd) and "
                "[the retro](https://acme.atlassian.net/wiki/x/EfGh)."
            ),
        }
    )

    def _resolve(argv: list[str]) -> dict[str, Any]:
        page_id = "884737" if argv[-1].endswith("AbCd") else "884738"
        return {"data": {"response": f"ari:cloud:confluence:cloud-1:page/{page_id}"}}

    fake = _FakeTwg(
        {
            "confluence content get": payload,
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "context confluence page": page_context_payload(relationships=[]),
            "resolve": _resolve,
        }
    )
    _install(monkeypatch, fake)

    first = await connector.fetch("document", "confluence/acme/884736")
    await connector.fetch("document", "confluence/acme/884736")

    referenced = {
        edge.target_platform_entity_id
        for edge in first.edges
        if edge.edge_type == "references"
    }
    assert referenced == {"confluence/acme/884737", "confluence/acme/884738"}
    assert len([command for command in fake.commands() if command.startswith("resolve")]) == 2


async def test_page_tiny_link_resolution_is_bounded(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiny = " ".join(
        f"[link {index}](https://acme.atlassian.net/wiki/x/Tiny{index})" for index in range(9)
    )
    fake = _FakeTwg(
        {
            "confluence content get": page_payload(body={"format": "md", "value": tiny}),
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "context confluence page": page_context_payload(relationships=[]),
            "resolve": TwgCommandError("no match"),
        }
    )
    _install(monkeypatch, fake)

    await connector.fetch("document", "confluence/acme/884736")

    resolutions = [command for command in fake.commands() if command.startswith("resolve")]
    assert len(resolutions) == twg_connector._MAX_TINY_LINK_RESOLUTIONS  # pyright: ignore[reportPrivateUsage]


async def test_fetch_page_enriches_id_only_people_and_caches_them(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = page_payload()
    payload["metadata"]["authorId"] = "acct-simon"
    payload["metadata"]["version"]["authorId"] = "acct-simon"
    fake = _FakeTwg(
        {
            "confluence content get": payload,
            "user bulk-lookup": {
                "items": [
                    {
                        "input": "acct-simon",
                        "ok": True,
                        "data": {
                            "accountId": "acct-simon",
                            "displayName": "Simon Wade",
                            "fullName": "Simon Wade",
                            "userAri": "ari:cloud:identity::user/acct-simon",
                        },
                    }
                ]
            },
        }
    )
    _install(monkeypatch, fake)

    first = await connector.fetch(
        "document",
        "confluence/acme/884736",
        meta={"space_key": "ENG"},
    )
    second = await connector.fetch(
        "document",
        "confluence/acme/884736",
        meta={"space_key": "ENG"},
    )

    assert {person.display_name for person in first.persons} == {"Simon Wade"}
    assert {person.display_name for person in second.persons} == {"Simon Wade"}
    assert any(
        edge.edge_type == "authored"
        and edge.source_platform_user_id == "acct-simon"
        for edge in first.edges
    )
    lookups = [command for command in fake.commands() if command.startswith("user bulk-lookup")]
    assert lookups == ["user bulk-lookup --account-id acct-simon"]


async def test_fetch_keeps_id_only_people_when_lookup_fails(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = page_payload()
    payload["metadata"]["authorId"] = "acct-unavailable"
    fake = _FakeTwg(
        {
            "confluence content get": payload,
            "user bulk-lookup": TwgCommandError("directory unavailable"),
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch(
        "document",
        "confluence/acme/884736",
        meta={"space_key": "ENG"},
    )

    person = next(person for person in batch.persons if person.platform_user_id == "acct-unavailable")
    assert person.display_name is None
    assert any(
        edge.edge_type == "authored"
        and edge.source_platform_user_id == "acct-unavailable"
        for edge in batch.edges
    )


async def test_fetch_workitem_enriches_id_only_people_and_mentions(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = workitem_payload(reporter="acct-simon")
    mention_attrs = payload["description"]["content"][0]["content"][1]["attrs"]
    mention_attrs.pop("text")
    mention_attrs["id"] = "acct-lee"
    fake = _FakeTwg(
        {
            "jira workitem get": payload,
            "context jira workitem": {"relationshipSummary": []},
            "user bulk-lookup": {
                "items": [
                    {
                        "input": "acct-lee",
                        "ok": True,
                        "data": {"accountId": "acct-lee", "displayName": "Lee Kim"},
                    },
                    {
                        "input": "acct-simon",
                        "ok": True,
                        "data": {"accountId": "acct-simon", "displayName": "Simon Wade"},
                    },
                ]
            },
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch("work-item", "jira/acme/ENG-42")

    people = {person.platform_user_id: person.display_name for person in batch.persons}
    assert people["acct-simon"] == "Simon Wade"
    assert people["acct-lee"] == "Lee Kim"
    assert any(
        edge.edge_type == "mentions" and edge.target_platform_user_id == "acct-lee"
        for edge in batch.edges
    )
    assert "user bulk-lookup --account-id acct-lee --account-id acct-simon" in fake.commands()


async def test_fetch_video_enriches_an_id_only_owner(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "loom get": TwgCommandError("transcript unsupported"),
            "loom video get": video_payload(owner="acct-simon"),
            "loom video comments": {"comments": []},
            "user bulk-lookup": {
                "items": [
                    {
                        "input": "acct-simon",
                        "ok": True,
                        "data": {"accountId": "acct-simon", "displayName": "Simon Wade"},
                    }
                ]
            },
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.fetch("video", "loom/abc123def456")

    person = next(person for person in batch.persons if person.platform_user_id == "acct-simon")
    assert person.display_name == "Simon Wade"
    assert any(
        edge.edge_type == "authored" and edge.source_platform_user_id == "acct-simon"
        for edge in batch.edges
    )


async def test_page_space_lookup_is_cached(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "confluence content get": page_payload(),
            "confluence space get": {"id": "65539", "key": "ENG", "name": "Engineering"},
        }
    )
    _install(monkeypatch, fake)

    await connector.fetch("document", "confluence/acme/884736")
    await connector.fetch("document", "confluence/acme/884736")

    space_lookups = [command for command in fake.commands() if command.startswith("confluence space get")]
    assert len(space_lookups) == 1


async def test_fetch_video_reads_the_full_transcript_file(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _write_transcript(argv: list[str]) -> dict[str, Any]:
        path = Path(argv[argv.index("--transcript-output-file") + 1])
        path.write_text(
            json.dumps({"phrases": [{"text": "Atlas sync retries."}, {"text": "Every fifteen minutes."}]}),
            encoding="utf-8",
        )
        return {"data": video_payload()}

    fake = _FakeTwg({"loom get": _write_transcript, "loom video comments": {"comments": []}})
    _install(monkeypatch, fake)

    batch = await connector.fetch("video", "loom/abc123def456")

    video = next(entity for entity in batch.entities if entity.entity_type == "Video")
    assert video.content is not None
    assert "Atlas sync retries. Every fifteen minutes." in video.content
    assert video.metadata["transcript_complete"] is True


async def test_fetch_video_falls_back_to_preview_transcript(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(argv: list[str]) -> dict[str, Any]:
        if "full" in argv:
            # Full transcripts are unavailable for this video; no file is written.
            return {"data": video_payload()}
        return {"data": video_payload(transcript={"phrases": [{"text": "Opening remarks."}]})}

    fake = _FakeTwg({"loom get": _respond, "loom video comments": {"comments": []}})
    _install(monkeypatch, fake)

    batch = await connector.fetch("video", "loom/abc123def456")

    video = next(entity for entity in batch.entities if entity.entity_type == "Video")
    assert video.content is not None
    assert "Transcript (preview):\nOpening remarks." in video.content
    assert video.metadata["transcript_complete"] is False


async def test_fetch_video_without_any_transcript_still_indexes_metadata(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _FakeTwg(
            {
                "loom get": TwgCommandError("transcript unsupported"),
                "loom video get": video_payload(),
                "loom video comments": TwgCommandError("no comments"),
            }
        ),
    )

    batch = await connector.fetch("video", "loom/abc123def456")

    video = next(entity for entity in batch.entities if entity.entity_type == "Video")
    assert video.metadata["transcript_available"] is False
    assert video.metadata["web_url"] == "https://www.loom.com/share/abc123def456"


async def test_fetch_goal_asks_for_the_description_on_the_atlas_cloud_id(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"goals get": goal_payload()})
    _install(monkeypatch, fake)

    batch = await connector.fetch("work-item", f"atlas/{ORG}/{CLOUD}/goal/ATLAS-131327")

    task = next(entity for entity in batch.entities if not entity.is_stub)
    assert task.entity_type == "Task"
    assert task.metadata["kind"] == "goal"
    assert task.metadata["web_url"] == atlas_url("goal", "ATLAS-131327")
    assert fake.commands() == ["goals get ATLAS-131327 --include-description"]
    # Atlas is org-scoped, so `--site` carries the cloud id from the identifier.
    assert fake.sites == [CLOUD]


async def test_fetch_atlas_project_includes_linked_goals(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"projects get": atlas_project_payload()})
    _install(monkeypatch, fake)

    batch = await connector.fetch("work-item", f"atlas/{ORG}/{CLOUD}/project/ATLAS-133324")

    assert fake.commands() == [
        "projects get ATLAS-133324 --include-description --include-linked-goals"
    ]
    assert fake.sites == [CLOUD]
    assert any(
        entity.is_stub and entity.platform_entity_id == f"atlas/{ORG}/{CLOUD}/goal/ATLAS-131327"
        for entity in batch.entities
    )


async def test_fetch_atlas_raises_when_no_data(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeTwg({"goals get": None}))

    with pytest.raises(ResourceUnavailableError):
        await connector.fetch("work-item", f"atlas/{ORG}/{CLOUD}/goal/ATLAS-131327")


def test_entity_url_round_trips_atlas_identifiers(
    connector: twg_connector.TwgConnector,
) -> None:
    for kind, key in (("goal", "ATLAS-131327"), ("project", "ATLAS-133324")):
        entity_id = f"atlas/{ORG}/{CLOUD}/{kind}/{key}"
        assert connector.entity_url(entity_id) == atlas_url(kind, key)
        assert connector.resolve_url(atlas_url(kind, key)) is not None
        assert connector.normalise_fetch_id(entity_id, "Task") == (entity_id, "work-item")


async def test_observation_patterns_keep_the_atlas_host(
    connector: twg_connector.TwgConnector,
) -> None:
    """`home.atlassian.com` is not site-scoped, so it is never narrowed."""
    config.save_settings(config.TwgSettings(sites=["acme"]))

    patterns = await connector.observation_url_patterns()

    assert "https://home.atlassian.com/o/*/s/*/goal/*" in patterns
    assert "https://home.atlassian.com/o/*/s/*/project/*" in patterns


async def test_fetch_rejects_foreign_identifiers(
    connector: twg_connector.TwgConnector,
) -> None:
    with pytest.raises(ResourceUnavailableError):
        await connector.fetch("document", "gmail/thread-1")


async def test_fetch_project_derives_folder_without_calling_twg(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({})
    _install(monkeypatch, fake)

    batch = await connector.fetch("folder", "jira/acme/project/ENG")

    assert [entity.entity_type for entity in batch.entities] == ["Folder"]
    assert fake.calls == []


# ----------------------------------------------------------------------
# Refresh
# ----------------------------------------------------------------------


def _work_query_response() -> dict[str, Any]:
    return {
        "sections": {
            "issues": [{"key": "ENG-42", "webUrl": "https://acme.atlassian.net/browse/ENG-42"}],
            "pages": [
                {
                    "id": "884736",
                    "webUrl": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Plan",
                }
            ],
            "videos": [{"id": "abc123def456", "url": "https://www.loom.com/share/abc123def456"}],
        }
    }


async def test_poll_hydrates_recent_activity_across_types(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "work query": _work_query_response(),
            "jira workitem get": workitem_payload(),
            "context jira workitem": {"relationshipSummary": []},
            "confluence content get": page_payload(),
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "loom get": video_payload(),
            "loom video comments": {"comments": []},
        }
    )
    _install(monkeypatch, fake)

    batch, cursor = await connector.poll({})

    types = {entity.entity_type for entity in batch.entities}
    assert {"Task", "Document", "Video"} <= types
    assert cursor["hydrated"] == 3
    assert cursor["candidates"] == 3
    assert "last_polled_at" in cursor
    assert any("--types jira,docs,videos" in command for command in fake.commands())


def _since_value(fake: _FakeTwg) -> str:
    for call in fake.calls:
        if "--since" in call:
            return call[call.index("--since") + 1]
    raise AssertionError(f"no --since in {fake.commands()}")


def _parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_poll_window_covers_the_gap_since_the_last_run(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)
    last_polled_at = (datetime.now(UTC) - timedelta(hours=9)).isoformat()

    await connector.poll({"last_polled_at": last_polled_at})

    # An hour of overlap either side of the last poll, so a 9h gap looks back 10h.
    lookback = datetime.now(UTC) - _parse_since(_since_value(fake))
    assert timedelta(hours=9, minutes=55) < lookback < timedelta(hours=10, minutes=5)


async def test_poll_window_defaults_without_a_cursor(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)

    await connector.poll({})

    since = _parse_since(_since_value(fake))
    assert timedelta(minutes=85) < datetime.now(UTC) - since < timedelta(minutes=95)


async def test_poll_window_clamps_a_long_outage_to_thirty_days(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)
    last_polled_at = (datetime.now(UTC) - timedelta(days=400)).isoformat()

    await connector.poll({"last_polled_at": last_polled_at})

    since = _parse_since(_since_value(fake))
    assert datetime.now(UTC) - since < timedelta(days=30, minutes=5)


async def test_poll_window_uses_a_since_format_twg_accepts(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`twg work query --since` rejects hour units — only dates, ISO datetimes, d/w/m."""
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)

    await connector.poll({"last_polled_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()})

    since = _since_value(fake)
    assert not since.endswith("h")
    assert _parse_since(since).tzinfo is not None


async def test_poll_respects_the_item_limit_and_deduplicates(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save_settings(config.TwgSettings(poll_item_limit=1))
    duplicated = _work_query_response()
    duplicated["sections"]["issues"] = duplicated["sections"]["issues"] * 2
    fake = _FakeTwg(
        {
            "work query": duplicated,
            "jira workitem get": workitem_payload(),
            "context jira workitem": {"relationshipSummary": []},
        }
    )
    _install(monkeypatch, fake)

    _batch, cursor = await connector.poll({})

    assert cursor["hydrated"] == 1
    assert len([call for call in fake.commands() if call.startswith("jira workitem get")]) == 1


async def test_poll_skips_fresh_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = twg_connector.TwgConnector()
    monkeypatch.setattr(twg_connector.TwgConnector, "last_synced_at", _synced_just_now)
    fake = _FakeTwg({"work query": _work_query_response()})
    _install(monkeypatch, fake)

    _batch, cursor = await connector.poll({})

    assert cursor["hydrated"] == 0


async def _synced_just_now(self: Any, resource_id: str) -> datetime:
    _ = (self, resource_id)
    return datetime.now(UTC)


async def test_poll_reports_no_candidates_when_work_query_fails(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeTwg({"work query": TwgAuthError("session expired")}))

    batch, cursor = await connector.poll({})

    assert batch.entities == []
    assert cursor["candidates"] == 0


async def test_failed_resource_does_not_abort_the_poll(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg(
        {
            "work query": _work_query_response(),
            "jira workitem get": TwgCommandError("gone"),
            "confluence content get": page_payload(),
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "loom get": video_payload(),
            "loom video comments": {"comments": []},
        }
    )
    _install(monkeypatch, fake)

    batch, cursor = await connector.poll({})

    assert cursor["hydrated"] == 2
    assert any(entity.entity_type == "Document" for entity in batch.entities)


async def test_ingest_sweeps_configured_jql_and_spaces(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save_settings(
        config.TwgSettings(
            sites=["acme"],
            jql=["assignee = currentUser()"],
            spaces=["ENG"],
        )
    )
    fake = _FakeTwg(
        {
            "work query": {"sections": {}},
            "jira workitem query": {
                "issues": [{"key": "ENG-9", "url": "https://acme.atlassian.net/browse/ENG-9"}]
            },
            "confluence search query": {
                "results": [
                    {
                        "content": {
                            "id": "884736",
                            "title": "Atlas sync plan",
                            "_links": {"webui": "/spaces/ENG/pages/884736/Atlas+sync+plan"},
                        }
                    }
                ]
            },
            "jira workitem get": workitem_payload(key="ENG-9", url="https://acme.atlassian.net/browse/ENG-9"),
            "context jira workitem": {"relationshipSummary": []},
            "confluence space get": {"key": "ENG", "name": "Engineering"},
            "confluence content get": page_payload(),
        }
    )
    _install(monkeypatch, fake)

    batch = await connector.ingest()

    assert any(entity.platform_entity_id == "jira/acme/ENG-9" for entity in batch.entities)
    assert any(entity.platform_entity_id == "confluence/acme/884736" for entity in batch.entities)
    assert any(entity.platform_entity_id == "confluence/acme/space/ENG" for entity in batch.entities)
    assert any("--since 90d" in command for command in fake.commands())
    assert any("--types jira,docs,videos" in command for command in fake.commands())


# ----------------------------------------------------------------------
# Auth surface
# ----------------------------------------------------------------------


async def test_verify_auth_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        twg_connector,
        "run_twg_sync",
        _raise(TwgNotInstalledError("twg not found")),
    )

    status, detail = await twg_connector.TwgConnector.verify_auth()

    assert status == "missing"
    assert detail is not None


async def test_verify_auth_reports_expired_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(twg_connector, "run_twg_sync", _raise(TwgAuthError("run twg auth refresh")))

    status, detail = await twg_connector.TwgConnector.verify_auth()

    assert status == "invalid"
    assert detail is not None
    assert "twg auth refresh" in detail


async def test_verify_auth_reports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def _whoami_envelope(args: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        _ = (args, kwargs)
        return {"data": {"user": {"email": "maya@acme.test"}}}

    monkeypatch.setattr(twg_connector, "run_twg_sync", _whoami_envelope)

    assert await twg_connector.TwgConnector.verify_auth() == ("ok", "maya@acme.test")


def test_current_user_id_prefers_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_whoami(monkeypatch, {"email": "Maya@Acme.test", "accountId": "acct-maya"})

    assert twg_connector.TwgConnector.current_user_id() == "maya@acme.test"


def test_current_user_id_falls_back_to_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_whoami(monkeypatch, {"accountId": "acct-maya"})

    assert twg_connector.TwgConnector.current_user_id() == "twg:acct-maya"


def test_list_accounts_is_empty_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_whoami(monkeypatch, None)

    assert twg_connector.TwgConnector.list_accounts() == []


def test_fetch_error_hints_are_actionable(connector: twg_connector.TwgConnector) -> None:
    assert "AGENTGRAPH_TWG_BIN" in (
        connector.fetch_error_hint("jira/acme/ENG-1", TwgNotInstalledError("x"), "cli") or ""
    )
    assert "twg auth refresh" in (
        connector.fetch_error_hint("jira/acme/ENG-1", TwgAuthError("x"), "mcp") or ""
    )
    assert connector.fetch_error_hint("jira/acme/ENG-1", ValueError("x"), "cli") is None


# ----------------------------------------------------------------------
# Connector-owned CLI
# ----------------------------------------------------------------------


def test_status_reports_configuration(monkeypatch: pytest.MonkeyPatch, fake_binary: Path) -> None:
    _patch_whoami(monkeypatch, {"email": "maya@acme.test"})
    _patch_version(monkeypatch, "1.2.5")
    config.save_settings(config.TwgSettings(sites=["acme"], spaces=["ENG"]))

    result = twg_connector.TwgConnector.run_cli_command(["status"])
    rendered = twg_connector.TwgConnector.format_cli_result(result)

    assert result["sites"] == ["acme"]
    assert "authenticated as maya@acme.test" in str(result["session"])
    assert "twg version:     1.2.5" in rendered
    assert "watched spaces:  ENG" in rendered


def test_scope_commands_persist_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    added = twg_connector.TwgConnector.run_cli_command(["add-jql", "assignee = currentUser()"])
    assert added["added"] == ["assignee = currentUser()"]
    assert config.load_settings().jql == ["assignee = currentUser()"]

    duplicate = twg_connector.TwgConnector.run_cli_command(["add-jql", "assignee = currentUser()"])
    assert duplicate["added"] == []

    removed = twg_connector.TwgConnector.run_cli_command(["remove-jql", "assignee = currentUser()"])
    assert removed["removed"] == ["assignee = currentUser()"]
    assert config.load_settings().jql == []


@pytest.mark.parametrize(
    "given",
    [
        "hello",
        "hello.atlassian.net",
        "https://hello.atlassian.net/jira/software",
        "HELLO",
        "hello.jira.atlassian.cloud",
        "https://hello.jira.atlassian.cloud/browse/SQA-703",
        "https://hello.confluence.atlassian.cloud/wiki/spaces/ENG/overview",
    ],
)
def test_site_values_are_reduced_to_the_bare_site_name(given: str) -> None:
    result = twg_connector.TwgConnector.run_cli_command(["add-site", given])

    assert result["sites"] == ["hello"]
    assert config.load_settings().default_site == "hello"


@pytest.mark.parametrize(
    "given",
    ["https://hello.jira.atlassian.com", "hello.example.com", "https://example.com/browse/SQA-1"],
)
def test_add_site_rejects_hosts_that_are_not_atlassian_sites(given: str) -> None:
    with pytest.raises(ValueError, match="not an Atlassian site"):
        twg_connector.TwgConnector.run_cli_command(["add-site", given])

    assert config.load_settings().sites == []


def test_stored_full_hostnames_are_normalised_on_read(isolated_config: Path) -> None:
    (isolated_config / config.CONFIG_FILENAME).write_text(
        json.dumps({"sites": ["hello.atlassian.net", "hello.jira.atlassian.cloud", "hello"]}),
        encoding="utf-8",
    )

    assert config.load_settings().sites == ["hello"]


def test_legacy_video_setting_is_ignored(isolated_config: Path) -> None:
    (isolated_config / config.CONFIG_FILENAME).write_text(
        json.dumps({"include_videos": False}),
        encoding="utf-8",
    )

    assert config.load_settings().model_dump() == config.TwgSettings().model_dump()


def test_stored_sites_that_are_not_atlassian_hosts_are_dropped(isolated_config: Path) -> None:
    """A config written before site references were validated must still load."""
    path = isolated_config / config.CONFIG_FILENAME
    path.write_text(json.dumps({"sites": ["hello.jira.atlassian.com", "hello"]}), encoding="utf-8")

    assert config.load_settings().sites == ["hello"]
    # Persisted, or the drop is re-derived and re-warned on every load.
    assert json.loads(path.read_text(encoding="utf-8"))["sites"] == ["hello"]


def test_normalisation_is_not_written_back_when_nothing_changed(isolated_config: Path) -> None:
    path = isolated_config / config.CONFIG_FILENAME
    config.save_settings(config.TwgSettings(sites=["hello"]))
    before = path.stat().st_mtime_ns

    assert config.load_settings().sites == ["hello"]
    assert path.stat().st_mtime_ns == before


def test_remove_site_accepts_either_spelling() -> None:
    twg_connector.TwgConnector.run_cli_command(["add-site", "hello"])

    result = twg_connector.TwgConnector.run_cli_command(["remove-site", "hello.atlassian.net"])

    assert result["removed"] == ["hello"]
    assert config.load_settings().sites == []


def test_space_keys_are_normalised_to_upper_case() -> None:
    result = twg_connector.TwgConnector.run_cli_command(["add-space", "eng"])

    assert result["added"] == ["ENG"]
    assert config.load_settings().spaces == ["ENG"]


def test_scope_additions_queue_an_ingest() -> None:
    effects = twg_connector.TwgConnector.command_effects(["add-jql", "x"], {})
    assert effects.ingest is True

    site_effects = twg_connector.TwgConnector.command_effects(["add-site", "acme"], {})
    assert site_effects.poll is True
    assert site_effects.ingest is False


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["add-jql"],
        ["videos"],
        ["videos", "on"],
        ["videos", "off"],
        ["add-site", "--flag"],
    ],
)
def test_invalid_commands_are_rejected(args: list[str]) -> None:
    with pytest.raises(ValueError):
        twg_connector.TwgConnector.run_cli_command(args)


def test_cli_help_documents_every_command() -> None:
    help_text = twg_connector.TwgConnector.cli_help()

    for command in ("status", "add-site", "add-jql", "add-space"):
        assert command in help_text
    assert "twg auth refresh" in help_text


def test_auth_flow_stores_sites_and_reports_state(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_whoami(monkeypatch, {"email": "maya@acme.test"})
    _patch_version(monkeypatch, "1.2.5")

    twg_connector.TwgConnector.run_auth_flow_with_args(["--site", "acme"])

    assert config.load_settings().sites == ["acme"]
    output = capsys.readouterr().out
    assert "Configured Atlassian sites: acme" in output
    assert "Authenticated as maya@acme.test" in output


def test_auth_flow_explains_an_unauthenticated_session(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_whoami(monkeypatch, None)
    _patch_version(monkeypatch, None)

    twg_connector.TwgConnector.run_auth_flow()

    assert "twg auth refresh" in capsys.readouterr().out


def test_config_is_written_to_the_agentgraph_config_dir(isolated_config: Path) -> None:
    config.save_settings(config.TwgSettings(sites=["acme"]))

    path = isolated_config / config.CONFIG_FILENAME
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["sites"] == ["acme"]


def test_unreadable_config_falls_back_to_defaults(isolated_config: Path) -> None:
    (isolated_config / config.CONFIG_FILENAME).write_text("not json", encoding="utf-8")

    assert config.load_settings().sites == []


def _raise(error: Exception) -> Any:
    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return _fail


def test_batch_merge_preserves_all_members() -> None:
    first = EntityBatch()
    second = EntityBatch()
    second.entities.append(
        twg_connector.EntityRecord(
            entity_type="Task",
            platform="twg",
            platform_entity_id="jira/acme/ENG-1",
        )
    )

    twg_connector._merge(first, second)  # pyright: ignore[reportPrivateUsage]

    assert len(first.entities) == 1
