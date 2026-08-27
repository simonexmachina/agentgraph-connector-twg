"""Connector-owned configuration, stored beside AgentGraph's own config.

The file lives at `<agentgraph-config-dir>/twg.json` so the connector never has
to rewrite the user's shared `config.yaml`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agentgraph_connector_twg import urls

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "twg.json"

_BARE_SITE = re.compile(urls.SITE_LABEL)


def parse_site(value: str) -> str | None:
    """Reduce a site reference to the bare site name `twg --site` expects.

    Accepts what a user is likely to paste — `hello`, `hello.atlassian.net`,
    `hello.jira.atlassian.cloud`, or a URL on either host — and yields `hello`,
    which is also the value URL construction interpolates. Returns None for
    anything that is not an Atlassian site, rather than guessing: a host that is
    kept whole here reappears as `https://<host>.atlassian.net/...` in the
    observation patterns, which matches nothing.
    """
    site = value.strip()
    if "//" in site:
        site = site.split("//", 1)[1]
    site = site.split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    if "." in site:
        return urls.site_from_host(site)
    return site if _BARE_SITE.fullmatch(site) else None


def normalise_site(value: str) -> str:
    """Like `parse_site`, but reject an unusable site reference."""
    site = parse_site(value)
    if site is None:
        raise ValueError(
            f"{value!r} is not an Atlassian site. Pass the site name, or a URL on "
            f"one of {', '.join(urls.site_host_examples('<site>'))}."
        )
    return site


class TwgSettings(BaseModel):
    """Watched scopes and refresh preferences for the twg connector."""

    sites: list[str] = Field(default_factory=list)
    """Atlassian sites to pass as `--site`. The first entry is the default."""

    @field_validator("sites", mode="after")
    @classmethod
    def _normalise_sites(cls, value: list[str]) -> list[str]:
        """Normalise on read as well as write, so older config files keep working.

        Unusable entries are dropped rather than rejected: a config file written
        before site references were validated must still load.
        """
        sites: list[str] = []
        for entry in value:
            if not entry.strip():
                continue
            site = parse_site(entry)
            if site is None:
                logger.warning(
                    "Ignoring configured twg site %r: not an Atlassian site. "
                    "Re-add it with `agentgraph connector twg add-site <site>`.",
                    entry,
                )
                continue
            sites.append(site)
        return list(dict.fromkeys(sites))

    jql: list[str] = Field(default_factory=list)
    """JQL queries swept by `ingest()` in addition to the user's own activity."""

    spaces: list[str] = Field(default_factory=list)
    """Confluence space keys swept by `ingest()`."""

    include_videos: bool = True
    """Index Loom videos and their transcripts."""

    poll_item_limit: int = 50
    """Maximum resources hydrated per background poll."""

    ingest_since: str = "90d"
    """Activity window used by `ingest()`."""

    @property
    def default_site(self) -> str | None:
        return self.sites[0] if self.sites else None


def config_path() -> Path:
    from agentgraph.config import get_config_paths

    config_dir = get_config_paths()[0]
    return config_dir / CONFIG_FILENAME


def load_settings() -> TwgSettings:
    """Return stored settings, falling back to defaults when nothing is configured."""
    path = config_path()
    if not path.exists():
        return TwgSettings()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable twg connector config at %s: %s", path, exc)
        return TwgSettings()
    if not isinstance(raw, dict):
        return TwgSettings()
    return TwgSettings.model_validate(raw)


def save_settings(settings: TwgSettings) -> TwgSettings:
    """Persist settings atomically and return them."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(f"{payload}\n", encoding="utf-8")
    os.replace(temp_path, path)
    return settings


def add_values(field: str, values: list[str]) -> tuple[TwgSettings, list[str]]:
    """Append unique values to a list field. Returns the settings and what was added."""
    settings = load_settings()
    current: list[str] = list(getattr(settings, field))
    added = [value for value in values if value and value not in current]
    if added:
        setattr(settings, field, [*current, *added])
        save_settings(settings)
    return settings, added


def remove_values(field: str, values: list[str]) -> tuple[TwgSettings, list[str]]:
    """Drop values from a list field. Returns the settings and what was removed."""
    settings = load_settings()
    current: list[str] = list(getattr(settings, field))
    removed = [value for value in values if value in current]
    if removed:
        setattr(settings, field, [value for value in current if value not in removed])
        save_settings(settings)
    return settings, removed
