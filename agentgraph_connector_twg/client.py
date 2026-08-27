"""Subprocess client for the Atlassian Teamwork Graph (`twg`) CLI.

Every call into `twg` goes through this module: binary resolution, JSON parsing,
timeouts, output caps, and failure classification live here so the mapping
modules only ever see decoded payloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final[float] = 45.0
"""Seconds allowed for a single `twg` invocation."""

MAX_OUTPUT_BYTES: Final[int] = 5_000_000
"""Upper bound on captured stdout; larger responses are refused rather than parsed."""

MAX_CONCURRENCY: Final[int] = 4
"""Concurrent `twg` processes allowed per event loop."""

BINARY_ENV_VAR: Final[str] = "AGENTGRAPH_TWG_BIN"

# `agentgraph serve` runs under a LaunchAgent with a minimal PATH, so the usual
# install locations are probed directly when `which` comes up empty.
_FALLBACK_BINARIES: Final[tuple[Path, ...]] = (
    Path.home() / ".local" / "bin" / "twg",
    Path("/opt/homebrew/bin/twg"),
    Path("/usr/local/bin/twg"),
)

_AUTH_MARKERS: Final[tuple[str, ...]] = (
    "oauth refresh is due",
    "twg auth refresh",
    "twg login",
    "not logged in",
    "unauthorized",
    "401",
    "403",
)

_INSTALL_HINT: Final[str] = (
    "The twg CLI was not found. Install it, or point AGENTGRAPH_TWG_BIN at the binary "
    "(commonly ~/.local/bin/twg)."
)

_AUTH_HINT: Final[str] = (
    "The twg CLI is not authenticated. Run `twg auth refresh` in a terminal — it needs write "
    "access to ~/.config/twg, which a sandboxed agent cannot provide."
)


class TwgError(RuntimeError):
    """Base class for every `twg` invocation failure."""


class TwgNotInstalledError(TwgError):
    """The `twg` binary could not be located."""


class TwgAuthError(TwgError):
    """`twg` ran but its Atlassian session is missing, expired, or refused."""


class TwgTimeoutError(TwgError):
    """`twg` did not finish within the allotted time."""


class TwgCommandError(TwgError):
    """`twg` failed for a reason unrelated to authentication."""


_binary_cache: str | None = None
_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def twg_binary() -> str:
    """Return the path to the `twg` executable, raising if it cannot be found."""
    global _binary_cache
    if _binary_cache is not None:
        return _binary_cache

    override = os.environ.get(BINARY_ENV_VAR)
    if override:
        if not Path(override).exists():
            raise TwgNotInstalledError(f"{BINARY_ENV_VAR} is set to {override}, which does not exist")
        _binary_cache = override
        return _binary_cache

    found = shutil.which("twg")
    if found is None:
        found = next((str(path) for path in _FALLBACK_BINARIES if path.exists()), None)
    if found is None:
        raise TwgNotInstalledError(_INSTALL_HINT)
    _binary_cache = found
    return _binary_cache


def reset_binary_cache() -> None:
    """Forget the resolved binary path. Used by tests and after configuration changes."""
    global _binary_cache
    _binary_cache = None


def build_argv(
    args: Sequence[str],
    *,
    site: str | None = None,
    json_output: bool = True,
) -> list[str]:
    """Return the full argument vector for a `twg` invocation."""
    argv = [twg_binary(), *args]
    if site and "--site" not in argv and "-s" not in argv:
        argv.extend(["--site", site])
    if json_output and "--output" not in argv and "-o" not in argv:
        argv.extend(["--output", "json"])
    return argv


def run_twg_sync(
    args: Sequence[str],
    *,
    site: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    json_output: bool = True,
) -> dict[str, Any]:
    """Run `twg` and return its decoded JSON envelope.

    Raises a `TwgError` subclass for every failure mode so callers can react to
    authentication problems separately from command or transport errors.
    """
    argv = build_argv(args, site=site, json_output=json_output)
    printable = " ".join(args)
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built from a resolved binary, never a shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        reset_binary_cache()
        raise TwgNotInstalledError(_INSTALL_HINT) from exc
    except subprocess.TimeoutExpired as exc:
        raise TwgTimeoutError(f"twg {printable} timed out after {timeout:.0f}s") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout.encode("utf-8", errors="ignore")) > MAX_OUTPUT_BYTES:
        raise TwgCommandError(
            f"twg {printable} returned more than {MAX_OUTPUT_BYTES} bytes; narrow the request"
        )

    if completed.returncode != 0:
        raise _failure(printable, completed.returncode, stdout, stderr)

    envelope = _decode(stdout)
    if envelope is None:
        # A zero exit code with unparsable output usually means twg printed a
        # remediation notice instead of data (the auth-refresh path does this).
        raise _failure(printable, completed.returncode, stdout, stderr)
    return envelope


async def run_twg(
    args: Sequence[str],
    *,
    site: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    json_output: bool = True,
) -> dict[str, Any]:
    """Async wrapper around `run_twg_sync` with a per-loop concurrency bound."""
    async with _semaphore():
        return await asyncio.to_thread(
            run_twg_sync,
            args,
            site=site,
            timeout=timeout,
            json_output=json_output,
        )


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        _semaphores[loop] = semaphore
    return semaphore


def _failure(command: str, returncode: int, stdout: str, stderr: str) -> TwgError:
    detail = (stderr.strip() or stdout.strip() or "no output").splitlines()
    message = " / ".join(line.strip() for line in detail[:4] if line.strip())
    haystack = f"{stderr}\n{stdout}".lower()
    if any(marker in haystack for marker in _AUTH_MARKERS):
        return TwgAuthError(f"{_AUTH_HINT} (twg {command}: {message})")
    return TwgCommandError(f"twg {command} failed with exit code {returncode}: {message}")


def _decode(stdout: str) -> dict[str, Any] | None:
    """Decode a `twg` JSON envelope, tolerating trailing progress lines."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        parsed = _decode_last_json_line(text)
    if isinstance(parsed, dict):
        return dict(parsed)  # pyright: ignore[reportUnknownArgumentType]
    if parsed is None:
        return None
    return {"data": parsed}


def _decode_last_json_line(text: str) -> Any:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith(("{", "[")):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def payload_data(envelope: Mapping[str, Any]) -> Any:
    """Return the `data` member of a `twg` envelope, or the envelope itself."""
    if "data" in envelope:
        return envelope["data"]
    return envelope


def batch_items(data: Any) -> list[Any]:
    """Normalise single, array, and batched (`data.items[].data`) payloads to a list."""
    mapping = _mapping(data)
    if mapping is not None:
        items = mapping.get("items")
        if isinstance(items, list):
            unwrapped: list[Any] = []
            for item in cast(list[Any], items):
                item_mapping = _mapping(item)
                if item_mapping is None:
                    unwrapped.append(item)
                    continue
                if item_mapping.get("ok") is False:
                    continue
                unwrapped.append(item_mapping.get("data"))
            return [item for item in unwrapped if item is not None]
        return [mapping]
    if isinstance(data, list):
        return cast(list[Any], data)
    return []


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return None


def twg_version() -> str | None:
    """Return the installed `twg` version, or None when it cannot be determined."""
    try:
        completed = subprocess.run(  # noqa: S603 - resolved binary, no shell
            [twg_binary(), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (TwgNotInstalledError, OSError, subprocess.TimeoutExpired):
        return None
    version = (completed.stdout or "").strip().splitlines()
    return version[0].strip() if version else None


_whoami_cache: dict[str, Any] | None = None
_whoami_failed = False


def whoami(*, refresh: bool = False) -> dict[str, Any] | None:
    """Return the authenticated Atlassian user record, or None when unavailable.

    The result is cached for the process lifetime because `get_authenticated_user()`
    is called on every CLI invocation and status render.
    """
    global _whoami_cache, _whoami_failed
    if refresh:
        _whoami_cache, _whoami_failed = None, False
    if _whoami_cache is not None:
        return _whoami_cache
    if _whoami_failed:
        return None

    try:
        envelope = run_twg_sync(["whoami"], timeout=20)
    except TwgError as exc:
        logger.debug("twg whoami failed: %s", exc)
        _whoami_failed = True
        return None

    data = _mapping(payload_data(envelope))
    if data is not None:
        record = _mapping(data.get("user")) or data
        _whoami_cache = dict(record)
        return _whoami_cache
    _whoami_failed = True
    return None


def reset_caches() -> None:
    """Clear the binary and identity caches. Used by tests and auth flows."""
    global _whoami_cache, _whoami_failed
    _whoami_cache, _whoami_failed = None, False
    reset_binary_cache()
