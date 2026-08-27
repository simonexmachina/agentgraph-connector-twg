"""Subprocess client behaviour: argv construction, decoding, failure classification."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentgraph_connector_twg import client


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def test_binary_resolution_prefers_env_override(fake_binary: Path) -> None:
    assert client.twg_binary() == str(fake_binary)


def test_binary_resolution_reports_missing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.BINARY_ENV_VAR, "/nonexistent/twg")
    client.reset_binary_cache()

    with pytest.raises(client.TwgNotInstalledError):
        client.twg_binary()


def test_binary_resolution_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.BINARY_ENV_VAR, raising=False)
    def _which(cmd: str) -> str:
        _ = cmd
        return "/opt/tools/twg"

    monkeypatch.setattr(client.shutil, "which", _which)
    client.reset_binary_cache()

    assert client.twg_binary() == "/opt/tools/twg"


def test_build_argv_appends_site_and_json(fake_binary: Path) -> None:
    argv = client.build_argv(["jira", "workitem", "get", "ENG-1"], site="acme")

    assert argv == [
        str(fake_binary),
        "jira",
        "workitem",
        "get",
        "ENG-1",
        "--site",
        "acme",
        "--output",
        "json",
    ]


def test_build_argv_respects_explicit_flags(fake_binary: Path) -> None:
    argv = client.build_argv(["whoami", "-o", "json", "-s", "other"], site="acme")

    assert argv == [str(fake_binary), "whoami", "-o", "json", "-s", "other"]


def test_run_twg_sync_returns_decoded_envelope(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout='{"data": {"key": "ENG-1"}}'))

    envelope = client.run_twg_sync(["jira", "workitem", "get", "ENG-1"])

    assert client.payload_data(envelope) == {"key": "ENG-1"}


def test_run_twg_sync_decodes_trailing_json_line(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout='fetching...\n{"data": [1, 2]}\n'))

    assert client.run_twg_sync(["docs", "query"]) == {"data": [1, 2]}


def test_run_twg_sync_wraps_bare_json_documents(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout="[{\"key\": \"ENG-1\"}]"))

    assert client.run_twg_sync(["jira", "workitem", "query"]) == {"data": [{"key": "ENG-1"}]}


@pytest.mark.parametrize(
    "stderr",
    [
        "OAuth refresh is due, but TWG cannot safely write /Users/x/.config/twg.",
        "GraphQL request failed: 401",
        "Run `twg auth refresh` in a terminal outside the coding agent.",
    ],
)
def test_auth_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
    stderr: str,
) -> None:
    _patch_run(monkeypatch, _Completed(returncode=1, stderr=stderr))

    with pytest.raises(client.TwgAuthError) as excinfo:
        client.run_twg_sync(["whoami"])

    assert "twg auth refresh" in str(excinfo.value)


def test_zero_exit_with_unparsable_output_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout="OAuth refresh is due"))

    with pytest.raises(client.TwgAuthError):
        client.run_twg_sync(["whoami"])


def test_command_failures_are_not_auth_failures(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(returncode=2, stderr="Unknown command path"))

    with pytest.raises(client.TwgCommandError) as excinfo:
        client.run_twg_sync(["bogus"])

    assert "exit code 2" in str(excinfo.value)


def test_missing_binary_at_exec_time_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, FileNotFoundError("gone"))

    with pytest.raises(client.TwgNotInstalledError):
        client.run_twg_sync(["whoami"])


def test_timeouts_are_reported(monkeypatch: pytest.MonkeyPatch, fake_binary: Path) -> None:
    _patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="twg", timeout=1.0))

    with pytest.raises(client.TwgTimeoutError):
        client.run_twg_sync(["whoami"], timeout=1.0)


def test_oversized_output_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout="x" * (client.MAX_OUTPUT_BYTES + 1)))

    with pytest.raises(client.TwgCommandError) as excinfo:
        client.run_twg_sync(["docs", "query"])

    assert "narrow the request" in str(excinfo.value)


async def test_run_twg_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(stdout='{"data": {"ok": true}}'))

    assert await client.run_twg(["whoami"]) == {"data": {"ok": True}}


def test_whoami_unwraps_and_caches(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    calls = _patch_run(
        monkeypatch,
        _Completed(stdout='{"data": {"user": {"email": "maya@acme.test", "accountId": "acct-maya"}}}'),
    )

    first = client.whoami()
    second = client.whoami()

    assert first is not None
    assert first["email"] == "maya@acme.test"
    assert second == first
    assert len(calls) == 1


def test_whoami_returns_none_when_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    _patch_run(monkeypatch, _Completed(returncode=1, stderr="401"))

    assert client.whoami() is None


def test_batch_items_normalises_shapes() -> None:
    assert client.batch_items({"key": "ENG-1"}) == [{"key": "ENG-1"}]
    assert client.batch_items([{"key": "ENG-1"}]) == [{"key": "ENG-1"}]
    assert client.batch_items(
        {
            "items": [
                {"ok": True, "data": {"key": "ENG-1"}},
                {"ok": False, "error": {"message": "not found"}},
            ]
        }
    ) == [{"key": "ENG-1"}]
    assert client.batch_items(None) == []
