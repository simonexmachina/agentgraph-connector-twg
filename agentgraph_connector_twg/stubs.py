"""Stub creation for resources referenced by a fetched entity.

`EntityBatch.add_stubs_from()` covers whitespace-delimited URLs in entity
content. Relationship payloads give the connector explicit target URLs instead,
and a markdown body wraps them in `[label](url)`, so this module applies the same
rule — classify through the connector registry, then create a placeholder entity
with `synced_at = NULL` — to both.
"""

from __future__ import annotations

import logging
import re

from agentgraph.connectors.base import (
    RESOURCE_TYPE_TO_ENTITY_TYPE,
    EdgeRecord,
    EntityRecord,
)

from agentgraph_connector_twg import urls

logger = logging.getLogger(__name__)

_TWG_RESOURCE_TYPE_TO_ENTITY_TYPE = {
    "work-item": "Task",
    "video": "Video",
}

_URL_RE = re.compile(r"https?://\S+")


def stub_for_url(url: str) -> EntityRecord | None:
    """Return a stub entity for a URL owned by any installed connector, or None."""
    from agentgraph.server.router import classify_url

    try:
        reference = classify_url(url)
    except Exception as exc:  # a third-party resolver must not break a fetch
        logger.debug("URL classification failed for %s: %s", url, exc)
        return None
    if reference is None:
        return None
    entity_type = RESOURCE_TYPE_TO_ENTITY_TYPE.get(reference.resource_type)
    if entity_type is None and reference.source == "twg":
        entity_type = _TWG_RESOURCE_TYPE_TO_ENTITY_TYPE.get(reference.resource_type)
    if entity_type is None:
        return None
    return EntityRecord(
        entity_type=entity_type,
        platform=reference.source,
        platform_entity_id=reference.resource_id,
        is_stub=True,
    )


def normalise_url(raw: str) -> str:
    """Trim the markdown and prose punctuation a URL collects in body text.

    A markdown link renders as `[label](url)`, so a URL scanned out of body text
    keeps the closing bracket, and a smart link twg renders as
    `…/project/ATLAS-129011](https://…?xpis=…)` keeps a whole second URL.
    """
    return raw.split("](")[0].rstrip(">).,;:!?\"'”’]")


def references_in_text(
    text: str,
    *,
    source_entity_id: str,
) -> tuple[list[EntityRecord], list[EdgeRecord]]:
    """Return stubs and `references` edges for the Atlassian URLs in body text.

    The same shape `EntityBatch.add_stubs_from` produces, but for text that is
    not whitespace-delimited: core's URL pattern has no trimming pass, so a
    markdown link's trailing `)` ends up in the URL and either fails to classify
    or — for `…/wiki/spaces/ENG)` — classifies as a space whose key is `ENG)`.
    `stub_for_url` additionally covers this connector's `work-item` and `video`
    resource kinds, which core's entity-type table does not.
    """
    entities: list[EntityRecord] = []
    edges: list[EdgeRecord] = []
    seen: set[str] = {source_entity_id}
    for raw_url in _URL_RE.findall(text):
        stub = stub_for_url(normalise_url(raw_url))
        if stub is None or stub.platform_entity_id in seen:
            continue
        seen.add(stub.platform_entity_id)
        entities.append(stub)
        edges.append(
            EdgeRecord(
                edge_type="references",
                source_platform_entity_id=source_entity_id,
                target_platform_entity_id=stub.platform_entity_id,
                platform="cross",
            )
        )
    return entities, edges


def tiny_links_in_text(text: str) -> list[str]:
    """Return the Confluence short links in body text, in document order.

    `/wiki/x/<tiny>` carries no page id, so unlike every other link in a body it
    cannot be classified offline — the caller has to spend a `twg resolve` on it.
    """
    found: list[str] = []
    for raw_url in _URL_RE.findall(text):
        url = normalise_url(raw_url)
        if urls.is_tiny_wiki_link(url) and url not in found:
            found.append(url)
    return found
