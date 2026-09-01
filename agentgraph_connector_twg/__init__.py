"""Atlassian Teamwork Graph connector for AgentGraph.

Jira work items become `Task` entities, Confluence pages become `Document`
entities, and Loom videos become `Video` entities whose content is the
transcript. Every upstream read goes through the `twg` CLI, which already holds
the user's Atlassian session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from agentgraph.connectors.base import (
    BaseConnector,
    ConnectorAccount,
    ConnectorCommandEffects,
    EntityBatch,
    EntityRecord,
    FetchPolicy,
    PersonRecord,
    ResourceUnavailableError,
    SourceReference,
)

from agentgraph_connector_twg import confluence, jira, loom, urls
from agentgraph_connector_twg.client import (
    TwgAuthError,
    TwgError,
    TwgNotInstalledError,
    batch_items,
    payload_data,
    reset_caches,
    run_twg,
    run_twg_sync,
    twg_binary,
    twg_version,
    whoami,
)
from agentgraph_connector_twg.config import (
    TwgSettings,
    add_values,
    config_path,
    load_settings,
    normalise_site,
    remove_values,
    save_settings,
)
from agentgraph_connector_twg.payloads import (
    as_mapping,
    as_sequence,
    nested_str,
    person_from_payload,
    pick,
    pick_mapping,
    pick_str,
)

logger = logging.getLogger(__name__)

_STALE_AFTER_SECONDS = 15 * 60
_POLL_INTERVAL = timedelta(minutes=30)
_INGEST_ITEM_LIMIT = 400
_MAX_TRANSCRIPT_PREVIEW_PHRASES = 400
_TRANSCRIPT_TIMEOUT = 120.0
_WORK_QUERY_SECTIONS = {
    "issues": "work-item",
    "pages": "page",
    "blogPosts": "page",
    "videos": "video",
}

__all__ = ["TwgConnector"]


class TwgConnector(BaseConnector):
    source = urls.SOURCE
    fetch_policy = FetchPolicy(stale_after_seconds=_STALE_AFTER_SECONDS)
    poll_interval: timedelta | None = _POLL_INTERVAL  # type: ignore[assignment]
    url_patterns = [
        *urls.site_url_patterns("*"),
        "https://www.loom.com/share/*",
        "https://www.loom.com/embed/*",
    ]
    auth_label = "twg"
    auth_description = (
        "Atlassian Teamwork Graph via the twg CLI: Jira work items as Task entities, Confluence "
        "pages as Documents, and Loom videos as Video entities with transcripts."
    )
    onboard_prompt = "Connect Atlassian Teamwork Graph (requires the twg CLI)?"

    def __init__(self) -> None:
        self._space_keys: dict[str, tuple[str, str | None]] = {}
        self._person_lookup_cache: dict[str, PersonRecord] = {}

    # ------------------------------------------------------------------
    # Auth and identity
    # ------------------------------------------------------------------

    @classmethod
    def run_auth_flow(
        cls,
        account_id: str | None = None,
        add: bool = False,
        args: list[str] | None = None,
    ) -> None:
        """Configure sites and report session state.

        The connector never authenticates on the user's behalf: `twg` owns the
        Atlassian OAuth session, and refreshing it needs an interactive terminal.
        """
        _ = (account_id, add)
        sites = _parse_site_args(args or [])
        if sites:
            settings = load_settings()
            settings.sites = list(dict.fromkeys([*sites, *settings.sites]))
            save_settings(settings)
            print(f"Configured Atlassian sites: {', '.join(settings.sites)}")

        reset_caches()
        try:
            binary = twg_binary()
        except TwgNotInstalledError as exc:
            print(str(exc))
            return
        print(f"Using twg at {binary}" + (f" (version {twg_version()})" if twg_version() else ""))

        user = whoami(refresh=True)
        if user is None:
            print(
                "twg is installed but not authenticated. Run `twg login` (or `twg auth refresh` if "
                "your session has expired) in a terminal, then re-run this command."
            )
            return
        print(f"Authenticated as {pick_str(user, 'email') or pick_str(user, 'name') or 'unknown'}")

    @classmethod
    def run_auth_flow_with_args(
        cls,
        args: list[str],
        account_id: str | None = None,
        add: bool = False,
    ) -> None:
        cls.run_auth_flow(account_id=account_id, add=add, args=args)

    @classmethod
    def get_authenticated_user(cls) -> str | None:
        user = whoami()
        if user is None:
            return None
        return pick_str(user, "email") or pick_str(user, "name") or pick_str(user, "accountId")

    @classmethod
    def list_accounts(cls) -> list[ConnectorAccount]:
        user = whoami()
        if user is None:
            return []
        email = pick_str(user, "email")
        label = email or pick_str(user, "name") or "twg"
        return [
            ConnectorAccount(
                account_id=cls.source,
                label=label,
                auth_group=cls.auth_label or cls.source,
                source=cls.source,
                user_id=pick_str(user, "accountId"),
                email=email,
                auth_method="twg cli session",
                metadata={"sites": ", ".join(load_settings().sites)},
            )
        ]

    @classmethod
    async def verify_auth(cls, account_id: str | None = None) -> tuple[str, str | None]:
        _ = account_id
        try:
            envelope = await asyncio.to_thread(run_twg_sync, ["whoami"], timeout=20)
        except TwgNotInstalledError as exc:
            return ("missing", str(exc))
        except TwgError as exc:
            # Auth, command, and transport failures all mean the stored session
            # cannot be used right now; the message carries the remediation.
            return ("invalid", str(exc))
        data = payload_data(envelope)
        record = as_mapping(data)
        user = pick_mapping(record, "user") or record
        return ("ok", pick_str(user, "email") or pick_str(user, "name"))

    @classmethod
    def current_user_id(cls) -> str | None:
        user = whoami()
        if user is None:
            return None
        email = pick_str(user, "email")
        if email:
            return email.lower()
        account_id = pick_str(user, "accountId")
        return f"{cls.source}:{account_id}" if account_id else None

    # ------------------------------------------------------------------
    # URL ownership
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        return urls.parse_url(url) is not None or urls.is_tiny_wiki_link(url)

    def resolve_url(self, url: str) -> SourceReference | None:
        target = urls.parse_url(url)
        return target.to_reference() if target is not None else None

    async def resolve_observation_url(
        self,
        url: str,
        meta: dict[str, str] | None = None,
    ) -> SourceReference | None:
        _ = meta
        target = urls.parse_url(url)
        if target is not None:
            return target.to_reference()
        if not urls.is_tiny_wiki_link(url):
            return None
        resolved = await self._resolve_via_twg(url)
        return resolved.to_reference() if resolved is not None else None

    async def observation_url_patterns(self) -> list[str]:
        """Narrow observation to the configured Atlassian sites.

        Falling back to the any-tenant wildcards keeps observation working before
        any site is configured, but once one is, browsing an unrelated tenant (a
        customer's or partner's site) should not be observed.
        """
        sites = load_settings().sites
        if not sites:
            return type(self).url_patterns
        wildcards = set(urls.site_url_patterns("*"))
        return [
            *(pattern for site in sites for pattern in urls.site_url_patterns(site)),
            *(pattern for pattern in type(self).url_patterns if pattern not in wildcards),
        ]

    async def _resolve_via_twg(self, url: str) -> urls.TwgTarget | None:
        """Resolve a Confluence short link through `twg resolve`."""
        site = urls.site_from_url(url)
        try:
            envelope = await run_twg(["resolve", url], site=site, timeout=30)
        except TwgError as exc:
            logger.debug("twg resolve failed for %s: %s", url, exc)
            return None
        data = as_mapping(payload_data(envelope))
        ari = pick_str(data, "response", "ari")
        if ari is None:
            return None
        return urls.parse_ari(ari, site=site)

    def normalise_fetch_id(self, resource_id: str, entity_type: str) -> tuple[str, urls.TwgResourceType]:
        target = urls.parse_entity_id(resource_id)
        if target is not None:
            return resource_id, target.resource_type
        return super().normalise_fetch_id(resource_id, entity_type)

    def entity_url(self, platform_entity_id: str) -> str | None:
        target = urls.parse_entity_id(platform_entity_id)
        return target.web_url if target is not None else None

    def fetch_error_hint(
        self,
        resource_id: str,
        error: Exception,
        audience: Literal["cli", "mcp"],
    ) -> str | None:
        _ = (resource_id, audience)
        if isinstance(error, TwgNotInstalledError):
            return (
                "Install the twg CLI, or set AGENTGRAPH_TWG_BIN to its path, then retry. "
                "`agentgraph connector twg status` reports what was found."
            )
        if isinstance(error, TwgAuthError):
            return (
                "Run `twg auth refresh` in a terminal to renew the Atlassian session (it needs write "
                "access to ~/.config/twg), then retry."
            )
        return None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        resource_type: urls.TwgResourceType,
        resource_id: str,
        meta: dict[str, str] | None = None,
        account_id: str | None = None,
    ) -> EntityBatch:
        _ = account_id
        settings = load_settings()
        target = self._target(resource_id, resource_type, meta)
        if target is None:
            raise ResourceUnavailableError(f"{resource_id} is not a twg resource identifier")

        if target.kind == "work-item":
            batch = await self._fetch_workitem(target, settings)
        elif target.kind == "page":
            batch = await self._fetch_page(target, settings)
        elif target.kind == "space":
            batch = await self._fetch_space(target, settings)
        elif target.kind == "project":
            batch = self._project_batch(target)
        elif target.kind == "video":
            batch = await self._fetch_video(target, settings)
        else:
            raise ResourceUnavailableError(f"twg cannot fetch {resource_id}")
        return await self._enrich_people(batch, site=self._site(target, settings))

    async def _enrich_people(self, batch: EntityBatch, *, site: str | None) -> EntityBatch:
        """Fill display names for people emitted with only an Atlassian account ID."""
        unresolved_ids = {
            person.platform_user_id
            for person in batch.persons
            if person.platform == self.source and person.display_name is None
        }
        if not unresolved_ids:
            return batch

        missing_ids = unresolved_ids.difference(self._person_lookup_cache)
        if missing_ids:
            args = ["user", "bulk-lookup"]
            for account_id in sorted(missing_ids):
                args.extend(["--account-id", account_id])
            try:
                envelope = await run_twg(args, site=site)
            except TwgError as exc:
                logger.debug("twg user lookup failed for %d people: %s", len(missing_ids), exc)
            else:
                for payload in batch_items(payload_data(envelope)):
                    person = person_from_payload(payload, platform=self.source)
                    if person is not None:
                        self._person_lookup_cache[person.platform_user_id] = person

        batch.persons = [
            self._with_person_identity(person, self._person_lookup_cache.get(person.platform_user_id))
            if person.platform == self.source and person.display_name is None
            else person
            for person in batch.persons
        ]
        return batch

    @staticmethod
    def _with_person_identity(person: PersonRecord, identity: PersonRecord | None) -> PersonRecord:
        if identity is None or identity.display_name is None:
            return person
        return person.model_copy(
            update={
                "canonical_email": identity.canonical_email or person.canonical_email,
                "display_name": identity.display_name,
                "metadata": {**person.metadata, **identity.metadata},
            }
        )

    def _target(
        self,
        resource_id: str,
        resource_type: urls.TwgResourceType,
        meta: Mapping[str, str] | None,
    ) -> urls.TwgTarget | None:
        _ = resource_type
        target = urls.parse_entity_id(resource_id)
        if target is None and resource_id.startswith(("http://", "https://")):
            target = urls.parse_url(resource_id)
        if target is None:
            return None
        space_key = (meta or {}).get("space_key") or target.space_key
        web_url = (meta or {}).get("web_url") or target.web_url
        site = (meta or {}).get("site") or target.site
        return urls.TwgTarget(
            kind=target.kind,
            entity_id=target.entity_id,
            site=site,
            key=target.key,
            space_key=space_key,
            web_url=web_url,
        )

    def _site(self, target: urls.TwgTarget, settings: TwgSettings) -> str | None:
        return target.site or settings.default_site

    async def _fetch_workitem(self, target: urls.TwgTarget, settings: TwgSettings) -> EntityBatch:
        key = target.key
        if key is None:
            raise ResourceUnavailableError(f"{target.entity_id} has no Jira key")
        site = self._site(target, settings)
        envelope = await run_twg(["jira", "workitem", "get", key, "--full"], site=site)
        payload = _first_mapping(payload_data(envelope))
        if payload is None:
            raise ResourceUnavailableError(f"Jira work item {key} returned no data")

        batch = jira.workitem_to_batch(payload, target=target)
        context = await self._workitem_context(key, target.entity_id, site)
        _merge(batch, context)
        return batch

    async def _workitem_context(
        self,
        key: str,
        entity_id: str,
        site: str | None,
    ) -> EntityBatch | None:
        """Relationship context is best-effort: a work item is still worth indexing without it."""
        try:
            envelope = await run_twg(
                ["context", "jira", "workitem", key, "--detail", "summary"],
                site=site,
            )
        except TwgError as exc:
            logger.debug("twg context for %s unavailable: %s", key, exc)
            return None
        payload = _first_mapping(payload_data(envelope))
        if payload is None:
            return None
        return jira.context_to_batch(payload, source_entity_id=entity_id)

    async def _fetch_page(self, target: urls.TwgTarget, settings: TwgSettings) -> EntityBatch:
        page_id = target.key
        if page_id is None:
            raise ResourceUnavailableError(f"{target.entity_id} has no Confluence page id")
        site = self._site(target, settings)
        envelope = await run_twg(
            [
                "confluence",
                "content",
                "get",
                page_id,
                "--detail",
                "full",
                "--format",
                "md",
                "--include-metadata",
            ],
            site=site,
        )
        payload = _first_mapping(payload_data(envelope))
        if payload is None:
            raise ResourceUnavailableError(f"Confluence page {page_id} returned no data")

        space_key = target.space_key
        space_name: str | None = None
        if space_key is None:
            space_id = pick_str(payload, "spaceId")
            if space_id is not None and site is not None:
                space_key, space_name = await self._space_identity(site, space_id)

        return confluence.page_to_batch(
            payload,
            target=urls.TwgTarget(
                kind="page",
                entity_id=target.entity_id,
                site=site,
                key=page_id,
                space_key=space_key,
                web_url=target.web_url,
            ),
            space_key=space_key,
            space_name=space_name,
        )

    async def _space_identity(self, site: str, space_id: str) -> tuple[str | None, str | None]:
        """Resolve a numeric space id to its key and name, caching the answer."""
        cache_key = f"{site}/{space_id}"
        cached = self._space_keys.get(cache_key)
        if cached is not None:
            return cached
        try:
            envelope = await run_twg(["confluence", "space", "get", space_id], site=site)
        except TwgError as exc:
            logger.debug("twg confluence space get %s failed: %s", space_id, exc)
            return (None, None)
        payload = _first_mapping(payload_data(envelope))
        if payload is None:
            return (None, None)
        identity = (confluence.space_key_from_payload(payload), pick_str(payload, "name"))
        if identity[0] is not None:
            self._space_keys[cache_key] = (identity[0], identity[1])
        return identity

    async def _fetch_space(self, target: urls.TwgTarget, settings: TwgSettings) -> EntityBatch:
        key = target.key
        site = self._site(target, settings)
        if key is None or site is None:
            raise ResourceUnavailableError(f"{target.entity_id} has no Confluence space key")
        envelope = await run_twg(["confluence", "space", "get", key], site=site)
        payload = _first_mapping(payload_data(envelope))
        if payload is None:
            raise ResourceUnavailableError(f"Confluence space {key} returned no data")
        return EntityBatch(
            entities=[confluence.space_to_entity(payload, site=site, space_key=key)]
        )

    def _project_batch(self, target: urls.TwgTarget) -> EntityBatch:
        """Jira projects are containers only; the Folder is derived from the identifier."""
        if target.site is None or target.key is None:
            raise ResourceUnavailableError(f"{target.entity_id} has no Jira project key")
        return EntityBatch(
            entities=[
                EntityRecord(
                    entity_type="Folder",
                    platform=self.source,
                    platform_entity_id=target.entity_id,
                    title=target.key,
                    content=f"Jira project {target.key}",
                    metadata={
                        "site": target.site,
                        "project_key": target.key,
                        "web_url": urls.project_web_url(target.site, target.key),
                    },
                )
            ]
        )

    async def _fetch_video(self, target: urls.TwgTarget, settings: TwgSettings) -> EntityBatch:
        video_id = target.key
        if video_id is None:
            raise ResourceUnavailableError(f"{target.entity_id} has no Loom video id")
        payload, transcript = await self._video_with_transcript(video_id)
        if payload is None:
            raise ResourceUnavailableError(f"Loom video {video_id} returned no data")
        comments = await self._video_comments(video_id)
        return loom.video_to_batch(
            payload,
            target=target,
            transcript=transcript,
            comments=comments,
        )

    async def _video_with_transcript(
        self,
        video_id: str,
    ) -> tuple[Mapping[str, Any] | None, loom.Transcript]:
        """Fetch video metadata plus the best transcript twg will give us.

        `--transcript full` only writes to a file, so a temporary path is supplied
        and read back; a preview is used when no full transcript exists.
        """
        with tempfile.TemporaryDirectory(prefix="agentgraph-twg-") as directory:
            transcript_path = Path(directory) / f"{video_id}.json"
            try:
                envelope = await run_twg(
                    [
                        "loom",
                        "get",
                        video_id,
                        "--transcript",
                        "full",
                        "--transcript-output-file",
                        str(transcript_path),
                    ],
                    timeout=_TRANSCRIPT_TIMEOUT,
                )
            except TwgError as exc:
                logger.debug("Full transcript unavailable for %s: %s", video_id, exc)
                return await self._video_transcript_preview(video_id)

            payload = _first_mapping(payload_data(envelope))
            transcript = _read_transcript_file(transcript_path)
            if not transcript.available:
                preview_payload, preview = await self._video_transcript_preview(video_id)
                return (payload or preview_payload, preview)
            return (payload, transcript)

    async def _video_transcript_preview(
        self,
        video_id: str,
    ) -> tuple[Mapping[str, Any] | None, loom.Transcript]:
        try:
            envelope = await run_twg(
                [
                    "loom",
                    "get",
                    video_id,
                    "--transcript",
                    "preview",
                    "--transcript-preview-phrases",
                    str(_MAX_TRANSCRIPT_PREVIEW_PHRASES),
                ],
                timeout=_TRANSCRIPT_TIMEOUT,
            )
        except TwgError as exc:
            logger.debug("Transcript preview unavailable for %s: %s", video_id, exc)
            return (await self._video_metadata(video_id), loom.EMPTY_TRANSCRIPT)

        payload = _first_mapping(payload_data(envelope))
        raw_transcript = pick(payload, "transcript", "transcriptPreview", "phrases")
        transcript = loom.flatten_transcript(raw_transcript, complete=False)
        return (payload, transcript)

    async def _video_metadata(self, video_id: str) -> Mapping[str, Any] | None:
        envelope = await run_twg(["loom", "video", "get", video_id])
        return _first_mapping(payload_data(envelope))

    async def _video_comments(self, video_id: str) -> list[Any]:
        try:
            envelope = await run_twg(["loom", "video", "comments", video_id, "--limit", "50"])
        except TwgError as exc:
            logger.debug("Loom comments unavailable for %s: %s", video_id, exc)
            return []
        data = payload_data(envelope)
        mapping = as_mapping(data)
        if mapping is not None:
            return as_sequence(pick(mapping, "comments", "results", "items"))
        return as_sequence(data)

    # ------------------------------------------------------------------
    # Background refresh
    # ------------------------------------------------------------------

    async def poll(
        self,
        cursor: dict[str, Any],
        account_id: str | None = None,
    ) -> tuple[EntityBatch, dict[str, Any]]:
        _ = account_id
        settings = load_settings()
        since = _poll_window(cursor)
        targets = await self._recent_activity_targets(settings, since)
        batch, hydrated = await self._hydrate(targets, limit=settings.poll_item_limit)
        return batch, {
            "last_polled_at": datetime.now(UTC).isoformat(),
            "candidates": len(targets),
            "hydrated": hydrated,
        }

    async def ingest(self, account_id: str | None = None) -> EntityBatch:
        _ = account_id
        settings = load_settings()
        targets = await self._recent_activity_targets(settings, settings.ingest_since)
        targets.extend(await self._jql_targets(settings))
        targets.extend(await self._space_targets(settings))
        batch, _hydrated = await self._hydrate(targets, limit=_INGEST_ITEM_LIMIT)
        return batch

    async def _recent_activity_targets(
        self,
        settings: TwgSettings,
        since: str,
    ) -> list[urls.TwgTarget]:
        """Return the user's own recently touched work items, pages, and videos."""
        types = ["jira", "docs", "videos"]
        try:
            envelope = await run_twg(
                [
                    "work",
                    "query",
                    "--scope",
                    "me",
                    "--since",
                    since,
                    "--types",
                    ",".join(types),
                    "--hydrate",
                    "none",
                ],
                site=settings.default_site,
                timeout=90.0,
            )
        except TwgError as exc:
            logger.warning("twg work query failed: %s", exc)
            return []
        return _targets_from_work_query(payload_data(envelope), settings.default_site)

    async def _jql_targets(self, settings: TwgSettings) -> list[urls.TwgTarget]:
        targets: list[urls.TwgTarget] = []
        for query in settings.jql:
            try:
                envelope = await run_twg(
                    ["jira", "workitem", "query", "--jql", query, "--first", "50"],
                    site=settings.default_site,
                    timeout=90.0,
                )
            except TwgError as exc:
                logger.warning("twg jira workitem query failed for %r: %s", query, exc)
                continue
            data = as_mapping(payload_data(envelope))
            for issue in as_sequence(pick(data, "issues", "results")):
                mapping = as_mapping(issue)
                target = _issue_target(mapping, settings.default_site)
                if target is not None:
                    targets.append(target)
        return targets

    async def _space_targets(self, settings: TwgSettings) -> list[urls.TwgTarget]:
        targets: list[urls.TwgTarget] = []
        for space_key in settings.spaces:
            site, _, key = space_key.rpartition(":")
            site = site or (settings.default_site or "")
            if not site:
                logger.warning("Skipping space %r: no Atlassian site configured", space_key)
                continue
            targets.append(
                urls.TwgTarget(
                    kind="space",
                    entity_id=urls.space_entity_id(site, key),
                    site=site,
                    key=key,
                    space_key=key,
                    web_url=urls.space_web_url(site, key),
                )
            )
            try:
                envelope = await run_twg(
                    [
                        "confluence",
                        "search",
                        "query",
                        "--cql",
                        f'space = "{key}" AND type = page ORDER BY lastmodified DESC',
                        "--limit",
                        "50",
                    ],
                    site=site,
                    timeout=90.0,
                )
            except TwgError as exc:
                logger.warning("twg confluence search failed for space %s: %s", key, exc)
                continue
            data = as_mapping(payload_data(envelope))
            for result in as_sequence(pick(data, "results")):
                target = _page_target_from_search(as_mapping(result), site)
                if target is not None:
                    targets.append(target)
        return targets

    async def _hydrate(
        self,
        targets: Sequence[urls.TwgTarget],
        *,
        limit: int,
    ) -> tuple[EntityBatch, int]:
        """Fetch each target that is not already fresh, up to `limit` resources."""
        combined = EntityBatch()
        seen: set[str] = set()
        hydrated = 0
        skipped = 0
        for target in targets:
            if hydrated >= limit:
                skipped += 1
                continue
            if target.entity_id in seen:
                continue
            seen.add(target.entity_id)
            if await self._is_fresh(target.entity_id):
                continue
            try:
                batch = await self.fetch(
                    target.resource_type,
                    target.entity_id,
                    meta=_fetch_meta(target),
                )
            except Exception as exc:
                logger.warning(
                    "Skipping twg resource %s (%s: %s)",
                    target.entity_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            hydrated += 1
            _merge(combined, batch)
        if skipped:
            logger.info(
                "twg refresh stopped at %d resources; %d candidates were not hydrated this run",
                limit,
                skipped,
            )
        return combined, hydrated

    async def _is_fresh(self, entity_id: str) -> bool:
        try:
            last_synced_at = await self.last_synced_at(entity_id)
        except Exception:  # a missing backend must not stop a refresh
            return False
        return type(self).fetch_policy.decide(last_synced_at) == FetchPolicy.FRESH

    # ------------------------------------------------------------------
    # Connector-owned CLI
    # ------------------------------------------------------------------

    @classmethod
    def run_cli_command(cls, args: list[str]) -> dict[str, Any]:
        if not args:
            raise ValueError(_usage())
        command, *rest = args

        if command == "status":
            return _status_result()
        field = {
            "add-site": "sites",
            "remove-site": "sites",
            "add-jql": "jql",
            "remove-jql": "jql",
            "add-space": "spaces",
            "remove-space": "spaces",
        }.get(command)
        if field is None:
            raise ValueError(
                f"Unknown twg connector command '{command}'. Available: status, add-site, "
                "remove-site, add-jql, remove-jql, add-space, remove-space"
            )
        values = _parse_values(rest, command=command, field=field)
        if command.startswith("add-"):
            settings, changed = add_values(field, values)
            key = "added"
        else:
            settings, changed = remove_values(field, values)
            key = "removed"
        return {
            "status": "ok",
            "source": cls.source,
            "field": field,
            key: changed,
            field: list(getattr(settings, field)),
        }

    @classmethod
    def command_effects(
        cls,
        args: list[str],
        result: dict[str, Any],
    ) -> ConnectorCommandEffects:
        _ = result
        command = args[0] if args else None
        return ConnectorCommandEffects(
            ingest=command in {"add-jql", "add-space"},
            ingest_account_id=cls.source if command in {"add-jql", "add-space"} else None,
            poll=command == "add-site",
        )

    @classmethod
    def cli_help(cls) -> str:
        return "\n".join(
            [
                "twg connector commands:",
                "",
                _usage(),
                "",
                "Commands:",
                "  status",
                "      Report the twg binary, version, session, and configured scopes.",
                "  add-site <site> / remove-site <site>",
                "      Manage Atlassian sites (the <site> in https://<site>.atlassian.net or",
                "      https://<site>.jira.atlassian.cloud). A site URL is accepted too.",
                "      The first configured site is the default for twg calls.",
                "  add-jql <jql> / remove-jql <jql>",
                "      Manage JQL queries swept by ingest, then queue an ingest.",
                "  add-space <KEY> / remove-space <KEY>",
                "      Manage Confluence spaces swept by ingest, then queue an ingest.",
                "Notes:",
                "  Authentication belongs to the twg CLI. Run `twg auth refresh` in a terminal",
                "  when the session expires; it needs write access to ~/.config/twg.",
            ]
        )

    @classmethod
    def format_cli_result(cls, result: dict[str, Any]) -> str:
        if "binary" in result:
            return _format_status(result)

        lines: list[str] = []
        for key in ("added", "removed"):
            values = as_sequence(result.get(key))
            if values:
                lines.append(f"{key.capitalize()} {len(values)}:")
                lines.extend(f"  - {value}" for value in values)
            elif key in result:
                lines.append(f"Nothing {key}.")
        field = result.get("field")
        if isinstance(field, str):
            configured = as_sequence(result.get(field))
            rendered = ", ".join(str(item) for item in configured) or "none"
            lines.append(f"Configured {field}: {rendered}")
        for effect in ("poll", "ingest"):
            payload = as_mapping(result.get(effect))
            if payload is not None:
                status = str(payload.get("status") or "unknown").replace("_", " ")
                lines.append(f"{effect.capitalize()}: {status}.")
        return "\n".join(lines) or json.dumps(result, indent=2, default=str)


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _usage() -> str:
    return (
        "Usage: agentgraph connector twg status\n"
        "   or: agentgraph connector twg add-site <site> [site...]\n"
        "   or: agentgraph connector twg remove-site <site> [site...]\n"
        "   or: agentgraph connector twg add-jql <jql>\n"
        "   or: agentgraph connector twg remove-jql <jql>\n"
        "   or: agentgraph connector twg add-space <SPACEKEY> [SPACEKEY...]\n"
        "   or: agentgraph connector twg remove-space <SPACEKEY> [SPACEKEY...]"
    )


def _status_result() -> dict[str, Any]:
    settings = load_settings()
    try:
        binary = twg_binary()
    except TwgNotInstalledError as exc:
        binary = None
        session = str(exc)
    else:
        user = whoami(refresh=True)
        session = (
            f"authenticated as {pick_str(user, 'email') or pick_str(user, 'name')}"
            if user is not None
            else "not authenticated — run `twg auth refresh` in a terminal"
        )
    return {
        "status": "ok",
        "source": TwgConnector.source,
        "binary": binary,
        "version": twg_version() if binary else None,
        "session": session,
        "sites": settings.sites,
        "jql": settings.jql,
        "spaces": settings.spaces,
        "config_path": str(config_path()),
    }


def _format_status(result: Mapping[str, Any]) -> str:
    def _join(key: str) -> str:
        values = as_sequence(result.get(key))
        return ", ".join(str(value) for value in values) if values else "none"

    return "\n".join(
        [
            f"twg binary:      {result.get('binary') or 'not found'}",
            f"twg version:     {result.get('version') or 'unknown'}",
            f"session:         {result.get('session')}",
            f"sites:           {_join('sites')}",
            f"watched JQL:     {_join('jql')}",
            f"watched spaces:  {_join('spaces')}",
            f"config:          {result.get('config_path')}",
        ]
    )


def _parse_values(args: list[str], *, command: str, field: str) -> list[str]:
    values = [arg.strip() for arg in args if arg.strip()]
    for value in values:
        if value.startswith("--"):
            raise ValueError(f"Unknown {command} option: {value}")
    if not values:
        raise ValueError(f"{command} requires at least one value\n\n{_usage()}")
    if field == "spaces":
        return [value.upper() for value in values]
    if field == "sites":
        return [normalise_site(value) for value in values]
    return values


def _parse_site_args(args: list[str]) -> list[str]:
    sites: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--site":
            if index + 1 >= len(args):
                raise ValueError("--site requires a value")
            sites.append(normalise_site(args[index + 1]))
            index += 2
            continue
        raise ValueError(f"Unknown twg auth option: {arg}")
    return sites


def _first_mapping(data: Any) -> Mapping[str, Any] | None:
    for item in batch_items(data):
        mapping = as_mapping(item)
        if mapping is not None:
            return mapping
    return None


def _merge(target: EntityBatch, extra: EntityBatch | None) -> None:
    if extra is None:
        return
    target.entities.extend(extra.entities)
    target.edges.extend(extra.edges)
    target.persons.extend(extra.persons)


def _fetch_meta(target: urls.TwgTarget) -> dict[str, str]:
    return {
        key: value
        for key, value in (
            ("site", target.site),
            ("space_key", target.space_key),
            ("web_url", target.web_url),
        )
        if value is not None
    }


def _poll_window(cursor: Mapping[str, Any]) -> str:
    """Return a `--since` window covering everything since the last successful poll.

    Emitted as an absolute ISO timestamp: `twg work query --since` accepts
    `YYYY-MM-DD`, an ISO datetime, or `7d`/`2w`/`1m` units, but not hours, so a
    sub-day window can only be expressed as a datetime.
    """
    now = datetime.now(UTC)
    cutoff = now - (_POLL_INTERVAL + timedelta(hours=1))
    last_polled_at = cursor.get("last_polled_at")
    if isinstance(last_polled_at, str):
        parsed = _parse_iso(last_polled_at)
        if parsed is not None:
            # Overlap the previous poll by an hour so edits landing mid-poll
            # are not skipped, and clamp long outages to 30 days.
            cutoff = max(parsed - timedelta(hours=1), now - timedelta(days=30))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_transcript_file(path: Path) -> loom.Transcript:
    if not path.exists():
        return loom.EMPTY_TRANSCRIPT
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Unreadable transcript file %s: %s", path, exc)
        return loom.EMPTY_TRANSCRIPT
    return loom.flatten_transcript(raw, complete=True)


def _targets_from_work_query(data: Any, default_site: str | None) -> list[urls.TwgTarget]:
    """Extract fetchable targets from `twg work query` section payloads."""
    targets: list[urls.TwgTarget] = []
    for item in batch_items(data):
        mapping = as_mapping(item)
        sections = pick_mapping(mapping, "sections")
        if sections is None:
            continue
        for section, kind in _WORK_QUERY_SECTIONS.items():
            for raw_entry in as_sequence(sections.get(section)):
                entry = as_mapping(raw_entry)
                if entry is None:
                    continue
                target = _target_from_entry(entry, kind, default_site)
                if target is not None:
                    targets.append(target)
    return targets


def _target_from_entry(
    entry: Mapping[str, Any],
    kind: str,
    default_site: str | None,
) -> urls.TwgTarget | None:
    url = pick_str(entry, "webUrl", "url", "link")
    if url is not None:
        target = urls.parse_url(url)
        if target is not None:
            return target
    if kind == "work-item":
        return _issue_target(entry, default_site)
    if kind == "video":
        video_id = pick_str(entry, "id", "videoId")
        if video_id is None:
            return None
        return urls.TwgTarget(
            kind="video",
            entity_id=urls.video_entity_id(video_id),
            key=video_id,
            web_url=urls.video_web_url(video_id),
        )
    if kind == "page" and default_site is not None:
        page_id = pick_str(entry, "id", "pageId")
        if page_id is None or not page_id.isdigit():
            return None
        return urls.TwgTarget(
            kind="page",
            entity_id=urls.page_entity_id(default_site, page_id),
            site=default_site,
            key=page_id,
            web_url=urls.page_web_url(default_site, page_id),
        )
    return None


def _issue_target(
    entry: Mapping[str, Any] | None,
    default_site: str | None,
) -> urls.TwgTarget | None:
    if entry is None:
        return None
    url = pick_str(entry, "url", "webUrl")
    if url is not None:
        target = urls.parse_url(url)
        if target is not None:
            return target
    key = pick_str(entry, "key", "issueKey")
    if key is None or default_site is None:
        return None
    return urls.TwgTarget(
        kind="work-item",
        entity_id=urls.workitem_entity_id(default_site, key),
        site=default_site,
        key=key.upper(),
        web_url=urls.workitem_web_url(default_site, key),
    )


def _page_target_from_search(
    result: Mapping[str, Any] | None,
    site: str,
) -> urls.TwgTarget | None:
    """Map one `confluence search query` result onto a page target."""
    content = pick_mapping(result, "content")
    page_id = pick_str(content, "id")
    if page_id is None:
        return None
    webui = nested_str(content, ("_links", "webui"))
    if webui is not None:
        target = urls.parse_url(f"https://{site}.atlassian.net/wiki{webui}")
        if target is not None:
            return target
    return urls.TwgTarget(
        kind="page",
        entity_id=urls.page_entity_id(site, page_id),
        site=site,
        key=page_id,
        web_url=urls.page_web_url(site, page_id),
    )
