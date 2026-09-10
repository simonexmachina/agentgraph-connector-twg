"""Map `twg confluence` payloads onto AgentGraph entities, people, and edges."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from agentgraph.connectors.base import (
    EdgeRecord,
    EntityBatch,
    EntityRecord,
    PersonRecord,
)

from agentgraph_connector_twg import urls
from agentgraph_connector_twg.payloads import (
    MetadataValue,
    as_mapping,
    as_sequence,
    clean_metadata,
    flatten_rich_text,
    nested_str,
    parse_datetime,
    person_from_payload,
    pick,
    pick_int,
    pick_mapping,
    pick_str,
)
from agentgraph_connector_twg.stubs import references_in_text

_BODY_FORMAT_LABELS = {"md": "markdown", "markdown": "markdown", "html": "html"}

MAX_CONTEXT_TARGETS = 50
"""Targets turned into edges per relationship; the CLI's own `--first` default is 50."""

_RELATIONSHIP_EDGES: Final[dict[str, tuple[str, str | None]]] = {
    # Keyed on the verb in the middle of a relationship name
    # (`atlassian_user_<verb>_confluence_page`) rather than the whole name, so a
    # blogpost (`…_confluence_blogpost`) and any future product-suffix rename
    # keep working — the same upgrade tolerance `payloads.py` documents.
    "mentioned_in": ("mentions", None),
    "owns": ("authored", "owner"),
    "created": ("authored", "author"),
    "updated": ("authored", "editor"),
    "contributed_to": ("participated_in", "contributor"),
    "watches": ("participated_in", "watcher"),
}

_IGNORED_RELATIONSHIPS: Final[frozenset[str]] = frozenset(
    {
        # A view is passive: it says nothing about the page beyond traffic, and a
        # popular page names half the org this way.
        "viewed",
        # twg's own snapshotting, not a human relationship to the content.
        "snapshotted",
    }
)


def page_to_batch(
    payload: Mapping[str, Any],
    *,
    target: urls.TwgTarget,
    space_key: str | None = None,
    space_name: str | None = None,
) -> EntityBatch:
    """Build the Document entity, its space Folder, people, and edges for one page."""
    site = target.site or ""
    page_id = pick_str(payload, "id") or target.key or ""
    entity_id = urls.page_entity_id(site, page_id) if site and page_id else target.entity_id
    space_key = space_key or target.space_key
    title = pick_str(payload, "title") or f"Confluence page {page_id}"

    body = pick_mapping(payload, "body")
    body_text = flatten_rich_text(pick(body, "value", "representation", "text"))
    summary_excerpt = nested_str(payload, ("summary", "excerpt"))
    outline = _outline(payload)
    # `twg confluence content get` returns Confluence's `_links.webui` shape, which
    # ends in the page title, so every candidate is rebuilt from its page id.
    web_url = (
        urls.canonical_url(pick_str(payload, "url", "webUrl"))
        or urls.canonical_url(target.web_url)
        or (urls.page_web_url(site, page_id, space_key) if site and page_id else None)
    )

    metadata_block = pick_mapping(payload, "metadata")
    version = pick_mapping(metadata_block, "version")

    batch = EntityBatch()
    edges: list[EdgeRecord] = []
    persons: dict[str, PersonRecord] = {}

    entity = EntityRecord(
        entity_type="Document",
        platform=urls.SOURCE,
        platform_entity_id=entity_id,
        title=title,
        content="\n\n".join(
            section
            for section in (title, outline, body_text or summary_excerpt, web_url)
            if section
        ),
        source_created_at=parse_datetime(pick(payload, "createdAt", "created")),
        source_updated_at=parse_datetime(
            pick(version, "createdAt") or pick(payload, "updatedAt", "updated")
        ),
        metadata=_metadata(
            payload=payload,
            body=body,
            version=version,
            metadata_block=metadata_block,
            site=site,
            page_id=page_id,
            space_key=space_key,
            web_url=web_url,
        ),
    )
    batch.entities.append(entity)

    if site and space_key:
        space_id = urls.space_entity_id(site, space_key)
        batch.entities.append(
            EntityRecord(
                entity_type="Folder",
                platform=urls.SOURCE,
                platform_entity_id=space_id,
                title=space_name or space_key,
                content=f"Confluence space {space_key}",
                metadata=clean_metadata(
                    {
                        "site": site,
                        "space_key": space_key,
                        "web_url": urls.space_web_url(site, space_key),
                    }
                ),
            )
        )
        edges.append(
            EdgeRecord(
                edge_type="posted_in",
                source_platform_entity_id=entity_id,
                target_platform_entity_id=space_id,
                platform=urls.SOURCE,
            )
        )

    for candidate, role in (
        (pick(payload, "authorId", "createdBy", "author"), "author"),
        (pick(metadata_block, "authorId"), "author"),
        (pick(version, "authorId", "author", "by"), "editor"),
        (pick(payload, "ownerId", "owner"), "owner"),
    ):
        person = person_from_payload(candidate, platform=urls.SOURCE)
        if person is None or person.platform_user_id in persons:
            continue
        persons[person.platform_user_id] = person
        edges.append(
            EdgeRecord(
                edge_type="authored",
                source_platform_user_id=person.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": role},
            )
        )

    batch.persons.extend(persons.values())
    batch.edges.extend(edges)
    stub_entities, reference_edges = references_in_text(
        entity.content or "",
        source_entity_id=entity_id,
    )
    # A body linking its own space would otherwise stub an entity this batch
    # already carries in full — the edge is still true, so only the stub is dropped.
    already_known = {(item.platform, item.platform_entity_id) for item in batch.entities}
    batch.entities.extend(
        stub
        for stub in stub_entities
        if (stub.platform, stub.platform_entity_id) not in already_known
    )
    batch.edges.extend(reference_edges)
    return batch


def context_to_batch(
    payload: Mapping[str, Any],
    *,
    source_entity_id: str,
) -> EntityBatch:
    """Turn `twg context confluence page` relationships into people and edges.

    A markdown body carries no account ids — the `--format md` conversion leaves
    a mention as the plain text `@William Dahl` — so this is the only place the
    people a page names reach the graph. Each target carries both an `accountId`
    and an `email`, so the people it yields need no `user bulk-lookup`.

    Edge direction follows the connector's own convention rather than twg's
    `direction` field, which reports every one of these as inbound.
    """
    batch = EntityBatch()
    persons: dict[str, PersonRecord] = {}
    edges: list[EdgeRecord] = []
    for item in as_sequence(pick(payload, "relationships", "relationshipSummary")):
        relationship = as_mapping(item)
        if relationship is None:
            continue
        mapped = _relationship_edge(pick_str(relationship, "relationshipName", "name") or "")
        if mapped is None:
            continue
        edge_type, role = mapped
        for raw_target in as_sequence(relationship.get("targets"))[:MAX_CONTEXT_TARGETS]:
            target = as_mapping(raw_target)
            # A target can be `ari:cloud:identity::user/unidentified`, which
            # carries no account id and would otherwise key a Person on that ARI.
            if target is None or pick_str(target, "accountId") is None:
                continue
            person = person_from_payload(target, platform=urls.SOURCE)
            if person is None:
                continue
            persons.setdefault(person.platform_user_id, person)
            if role is None:
                # A mention runs from the page to the person it names.
                edges.append(
                    EdgeRecord(
                        edge_type=edge_type,
                        source_platform_entity_id=source_entity_id,
                        target_platform_user_id=person.platform_user_id,
                        platform=urls.SOURCE,
                    )
                )
            else:
                edges.append(
                    EdgeRecord(
                        edge_type=edge_type,
                        source_platform_user_id=person.platform_user_id,
                        target_platform_entity_id=source_entity_id,
                        platform=urls.SOURCE,
                        properties={"role": role},
                    )
                )
    batch.persons.extend(persons.values())
    batch.edges.extend(edges)
    return batch


def _relationship_edge(relationship_name: str) -> tuple[str, str | None] | None:
    """Map a relationship name onto `(edge_type, role)`, or None to skip it."""
    if any(verb in relationship_name for verb in _IGNORED_RELATIONSHIPS):
        return None
    for verb, mapping in _RELATIONSHIP_EDGES.items():
        if verb in relationship_name:
            return mapping
    return None


def space_to_entity(
    payload: Mapping[str, Any],
    *,
    site: str,
    space_key: str | None = None,
) -> EntityRecord:
    """Build the Folder entity for a Confluence space."""
    key = pick_str(payload, "key", "spaceKey") or space_key or ""
    name = pick_str(payload, "name") or key
    description = flatten_rich_text(
        pick(pick_mapping(payload, "description") or {}, "value", "plain") or pick(payload, "description")
    )
    return EntityRecord(
        entity_type="Folder",
        platform=urls.SOURCE,
        platform_entity_id=urls.space_entity_id(site, key),
        title=name,
        content="\n\n".join(part for part in (f"Confluence space {key}", name, description) if part),
        metadata=clean_metadata(
            {
                "site": site,
                "space_key": key,
                "space_id": pick_str(payload, "id"),
                "space_type": pick_str(payload, "type"),
                "status": pick_str(payload, "status"),
                "web_url": urls.space_web_url(site, key),
            }
        ),
    )


def space_key_from_payload(payload: Mapping[str, Any]) -> str | None:
    """Read the space key from a space payload returned by `confluence space get`."""
    return pick_str(payload, "key", "spaceKey")


def _outline(payload: Mapping[str, Any]) -> str:
    headings: list[str] = []
    for item in as_sequence(pick(payload, "outline")):
        mapping = as_mapping(item)
        text = pick_str(mapping, "text")
        if text is None:
            continue
        level = pick_int(mapping, "level") or 1
        headings.append(f"{'#' * max(1, min(level, 6))} {text}")
    return "\n".join(headings)


def _metadata(
    *,
    payload: Mapping[str, Any],
    body: Mapping[str, Any] | None,
    version: Mapping[str, Any] | None,
    metadata_block: Mapping[str, Any] | None,
    site: str,
    page_id: str,
    space_key: str | None,
    web_url: str | None,
) -> dict[str, MetadataValue]:
    body_format = pick_str(body, "format")
    return clean_metadata(
        {
            "site": site,
            "page_id": page_id,
            "space_key": space_key,
            "space_id": pick_str(payload, "spaceId"),
            "content_type": pick_str(payload, "type"),
            "status": pick_str(payload, "status"),
            "web_url": web_url,
            "body_format": _BODY_FORMAT_LABELS.get(body_format or "", body_format),
            "lossy_conversion": pick(body, "lossyConversion"),
            "word_count": nested_str(payload, ("summary", "wordCount")),
            "version_number": pick_int(version, "number"),
            "version_message": pick_str(version, "message"),
            "total_views": pick_int(metadata_block, "totalViews"),
            "snapshot_token": pick_str(payload, "snapshotToken"),
        }
    )
