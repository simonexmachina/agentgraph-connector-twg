"""Stub creation for resources referenced by a fetched entity.

`EntityBatch.add_stubs_from()` covers URLs found in entity content. Relationship
payloads give the connector explicit target URLs instead, so this module applies
the same rule — classify through the connector registry, then create a
placeholder entity with `synced_at = NULL` — to those URLs.
"""

from __future__ import annotations

import logging

from agentgraph.connectors.base import RESOURCE_TYPE_TO_ENTITY_TYPE, EntityRecord

logger = logging.getLogger(__name__)

_TWG_RESOURCE_TYPE_TO_ENTITY_TYPE = {
    "work-item": "Task",
    "video": "Video",
}


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
