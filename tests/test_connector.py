"""Connector behaviour: fetch dispatch, refresh, and connector-owned CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agentgraph.connectors.base import EntityBatch, ResourceUnavailableError
from conftest import (
    classify_atlassian_urls,
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

    async def __call__(
        self,
        args: Sequence[str],
        *,
        site: str | None = None,
        timeout: float = 45.0,
        json_output: bool = True,
    ) -> dict[str, Any]:
        _ = (site, timeout, json_output)
        argv = list(args)
        self.calls.append(argv)
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


async def test_videos_can_be_disabled(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save_settings(config.TwgSettings(include_videos=False))
    _install(monkeypatch, _FakeTwg({}))

    batch = await connector.fetch("video", "loom/abc123def456")

    assert batch.entities == []


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


async def test_poll_window_covers_the_gap_since_the_last_run(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)
    last_polled_at = (datetime.now(UTC) - timedelta(hours=9)).isoformat()

    await connector.poll({"last_polled_at": last_polled_at})

    assert any("--since 10h" in command for command in fake.commands())


async def test_poll_window_defaults_without_a_cursor(
    connector: twg_connector.TwgConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTwg({"work query": {"sections": {}}})
    _install(monkeypatch, fake)

    await connector.poll({})

    assert any("--since 2h" in command for command in fake.commands())


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
            include_videos=False,
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
    assert not any("videos" in command for command in fake.commands())


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
    ["hello", "hello.atlassian.net", "https://hello.atlassian.net/jira/software", "HELLO"],
)
def test_site_values_are_reduced_to_the_bare_site_name(given: str) -> None:
    result = twg_connector.TwgConnector.run_cli_command(["add-site", given])

    assert result["sites"] == ["hello"]
    assert config.load_settings().default_site == "hello"


def test_stored_full_hostnames_are_normalised_on_read(isolated_config: Path) -> None:
    (isolated_config / config.CONFIG_FILENAME).write_text(
        json.dumps({"sites": ["hello.atlassian.net", "hello"]}), encoding="utf-8"
    )

    assert config.load_settings().sites == ["hello"]


def test_remove_site_accepts_either_spelling() -> None:
    twg_connector.TwgConnector.run_cli_command(["add-site", "hello"])

    result = twg_connector.TwgConnector.run_cli_command(["remove-site", "hello.atlassian.net"])

    assert result["removed"] == ["hello"]
    assert config.load_settings().sites == []


def test_space_keys_are_normalised_to_upper_case() -> None:
    result = twg_connector.TwgConnector.run_cli_command(["add-space", "eng"])

    assert result["added"] == ["ENG"]
    assert config.load_settings().spaces == ["ENG"]


def test_videos_command_toggles_indexing() -> None:
    twg_connector.TwgConnector.run_cli_command(["videos", "off"])
    assert config.load_settings().include_videos is False

    twg_connector.TwgConnector.run_cli_command(["videos", "on"])
    assert config.load_settings().include_videos is True


def test_scope_additions_queue_an_ingest() -> None:
    effects = twg_connector.TwgConnector.command_effects(["add-jql", "x"], {})
    assert effects.ingest is True

    site_effects = twg_connector.TwgConnector.command_effects(["add-site", "acme"], {})
    assert site_effects.poll is True
    assert site_effects.ingest is False


@pytest.mark.parametrize(
    "args",
    [[], ["bogus"], ["add-jql"], ["videos"], ["videos", "maybe"], ["add-site", "--flag"]],
)
def test_invalid_commands_are_rejected(args: list[str]) -> None:
    with pytest.raises(ValueError):
        twg_connector.TwgConnector.run_cli_command(args)


def test_cli_help_documents_every_command() -> None:
    help_text = twg_connector.TwgConnector.cli_help()

    for command in ("status", "add-site", "add-jql", "add-space", "videos"):
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
