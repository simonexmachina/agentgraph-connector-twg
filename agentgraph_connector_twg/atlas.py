"""Map `twg goals` and `twg projects` payloads onto Task entities.

Atlas (Atlassian Home) goals and projects are both units of work, so both become
`Task` entities and `metadata.kind` tells them apart. The two commands return
overlapping but not identical shapes — a goal carries `state` *and* `status`, a
`targetDate` and a plain-string description; a project carries `state` only, a
`dueDate` and a `{what, why, measurement}` description — so each field is read
from the union of both spellings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

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
    flatten_encoded_rich_text,
    flatten_rich_text,
    nested_str,
    parse_datetime,
    person_from_payload,
    pick,
    pick_mapping,
    pick_str,
)

AtlasKind = Literal["goal", "project"]

_DESCRIPTION_SECTIONS: tuple[tuple[str, str], ...] = (
    ("what", "What"),
    ("why", "Why"),
    ("measurement", "Measurement"),
)

_RELATIONS: dict[AtlasKind, tuple[tuple[str, AtlasKind, str], ...]] = {
    # field in the payload -> kind of the other end, and the relationship name
    # carried by the `references` edge.
    "goal": (
        ("parentGoal", "goal", "parent_goal"),
        ("contributingProjects", "project", "contributing_project"),
    ),
    "project": (("linkedGoals", "goal", "linked_goal"),),
}
_RELATION_METADATA_KEYS: dict[str, str] = {
    "parent_goal": "parent_goal_key",
    "contributing_project": "contributing_project_keys",
    "linked_goal": "linked_goal_keys",
}


def goal_to_batch(payload: Mapping[str, Any], *, target: urls.TwgTarget) -> EntityBatch:
    """Build the Task entity, people, and linked-project edges for one Atlas goal."""
    return _to_batch(payload, target=target, kind="goal")


def project_to_batch(payload: Mapping[str, Any], *, target: urls.TwgTarget) -> EntityBatch:
    """Build the Task entity, people, and linked-goal edges for one Atlas project."""
    return _to_batch(payload, target=target, kind="project")


def _to_batch(
    payload: Mapping[str, Any],
    *,
    target: urls.TwgTarget,
    kind: AtlasKind,
) -> EntityBatch:
    org_id = target.org_id
    cloud_id = target.site
    key = pick_str(payload, "key") or target.key or ""
    entity_id = (
        _entity_id(kind, org_id, cloud_id, key)
        if org_id and cloud_id and key
        else target.entity_id
    )
    name = pick_str(payload, "name") or f"Atlas {kind} {key}".strip()
    state = _label(payload, "state")
    status = _label(payload, "status")
    owner = person_from_payload(pick(payload, "owner"), platform=urls.SOURCE)
    dates = pick_mapping(payload, "targetDate", "dueDate")
    target_date = pick_str(dates, "label") or nested_str(dates, ("dateRange", "start"))
    update = pick_mapping(payload, "latestUserUpdate")
    update_author = person_from_payload(pick(update, "creator"), platform=urls.SOURCE)
    tags = _tags(payload)
    web_url = (
        pick_str(payload, "url", "webUrl")
        or target.web_url
        or (_web_url(kind, org_id, cloud_id, key) if org_id and cloud_id and key else None)
    )

    links, link_keys = _related(payload, target=target, kind=kind, entity_id=entity_id)

    batch = EntityBatch()
    persons: dict[str, PersonRecord] = {}
    edges: list[EdgeRecord] = []

    entity = EntityRecord(
        entity_type="Task",
        platform=urls.SOURCE,
        platform_entity_id=entity_id,
        title=f"{key}: {name}" if key else name,
        content=_content(
            name=name,
            status=state or status,
            owner=owner,
            target_date=target_date,
            description=_description(pick(payload, "description")),
            update=_update_section(update, update_author),
            web_url=web_url,
        ),
        # Only a project reports when it started; a goal has no created date.
        source_created_at=parse_datetime(pick(payload, "startDate")),
        source_updated_at=parse_datetime(
            pick(payload, "latestUpdateDate") or pick(update, "creationDate")
        ),
        metadata=_metadata(
            payload=payload,
            kind=kind,
            org_id=org_id,
            cloud_id=cloud_id,
            key=key,
            web_url=web_url,
            state=state,
            status=status,
            owner=owner,
            target_date=target_date,
            tags=tags,
            update=update,
            link_keys=link_keys,
        ),
    )
    batch.entities.append(entity)

    for stub, edge in links:
        batch.entities.append(stub)
        edges.append(edge)

    for person, edge_type, role in (
        (owner, "authored", "owner"),
        (update_author, "participated_in", "update-author"),
    ):
        if person is None or person.platform_user_id in persons:
            continue
        persons[person.platform_user_id] = person
        edges.append(
            EdgeRecord(
                edge_type=edge_type,
                source_platform_user_id=person.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": role},
            )
        )

    batch.persons.extend(persons.values())
    batch.edges.extend(edges)
    batch.add_stubs_from(entity)
    return batch


def _entity_id(kind: AtlasKind, org_id: str, cloud_id: str, key: str) -> str:
    if kind == "goal":
        return urls.goal_entity_id(org_id, cloud_id, key)
    return urls.atlas_project_entity_id(org_id, cloud_id, key)


def _web_url(kind: AtlasKind, org_id: str, cloud_id: str, key: str) -> str:
    if kind == "goal":
        return urls.goal_web_url(org_id, cloud_id, key)
    return urls.atlas_project_web_url(org_id, cloud_id, key)


def _label(payload: Mapping[str, Any], name: str) -> str | None:
    """Read a `{label, value}` health field, preferring the human-readable label."""
    return nested_str(payload, (name, "label")) or nested_str(payload, (name, "value"))


def _tags(payload: Mapping[str, Any]) -> list[str]:
    """Read tags from either the project's plain list or the goal's edge/node graph."""
    raw = pick(payload, "tags")
    names = [tag for tag in as_sequence(raw) if isinstance(tag, str) and tag.strip()]
    if names:
        return names
    for edge in as_sequence(pick(as_mapping(raw), "edges")):
        name = nested_str(as_mapping(edge), ("node", "name"))
        if name is not None:
            names.append(name)
    return names


def _description(value: Any) -> str:
    """Render a description, which a project splits into three labelled sections."""
    mapping = as_mapping(value)
    if mapping is None:
        return flatten_rich_text(value)
    sections = [
        f"{label}: {text}"
        for label, text in (
            (label, flatten_rich_text(pick(mapping, field)))
            for field, label in _DESCRIPTION_SECTIONS
        )
        if text
    ]
    return "\n\n".join(sections) or flatten_rich_text(value)


def _update_section(
    update: Mapping[str, Any] | None,
    author: PersonRecord | None,
) -> str | None:
    if update is None:
        return None
    # `summaryText` is sometimes a JSON-encoded ADF document rather than text.
    text = flatten_encoded_rich_text(pick(update, "summaryText", "summary"))
    if not text:
        return None
    created = pick_str(update, "creationDate")
    who = (author.display_name or author.platform_user_id) if author is not None else "Unknown"
    return f"Latest update:\n— {who}{f' ({created})' if created else ''}: {text}"


def _content(
    *,
    name: str,
    status: str | None,
    owner: PersonRecord | None,
    target_date: str | None,
    description: str,
    update: str | None,
    web_url: str | None,
) -> str:
    header = " | ".join(
        part
        for part in (
            f"Status: {status}" if status else None,
            f"Owner: {owner.display_name or owner.platform_user_id}" if owner is not None else None,
            f"Target: {target_date}" if target_date else None,
        )
        if part
    )
    return "\n\n".join(
        section for section in (name, header, description, update, web_url) if section
    )


def _related(
    payload: Mapping[str, Any],
    *,
    target: urls.TwgTarget,
    kind: AtlasKind,
    entity_id: str,
) -> tuple[list[tuple[EntityRecord, EdgeRecord]], dict[str, list[str]]]:
    """Turn the goal/project cross-links into stubs and `references` edges.

    The org and cloud ids come from `target` rather than each nested payload's
    own `url`: every link is inside the same Atlas instance, so reusing the
    parent's ids keeps the identifiers consistent even when a nested entry is
    trimmed down to a key and a name.
    """
    links: list[tuple[EntityRecord, EdgeRecord]] = []
    keys_by_relationship: dict[str, list[str]] = {}
    org_id = target.org_id
    cloud_id = target.site
    seen: set[str] = {entity_id}
    for field, other_kind, relationship in _RELATIONS[kind]:
        for item in _mappings(pick(payload, field)):
            key = pick_str(item, "key")
            if key is None:
                continue
            keys_by_relationship.setdefault(relationship, []).append(key)
            if not (org_id and cloud_id):
                continue
            stub_id = _entity_id(other_kind, org_id, cloud_id, key)
            if stub_id in seen:
                continue
            seen.add(stub_id)
            links.append(
                (
                    EntityRecord(
                        entity_type="Task",
                        platform=urls.SOURCE,
                        platform_entity_id=stub_id,
                        is_stub=True,
                    ),
                    EdgeRecord(
                        edge_type="references",
                        source_platform_entity_id=entity_id,
                        target_platform_entity_id=stub_id,
                        platform="cross",
                        properties={"relationship": relationship},
                    ),
                )
            )
    return links, keys_by_relationship


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """Normalise a relation field that may be a single object or a list of them."""
    single = as_mapping(value)
    if single is not None:
        return [single]
    entries = (as_mapping(entry) for entry in as_sequence(value))
    return [entry for entry in entries if entry is not None]


def _metadata(
    *,
    payload: Mapping[str, Any],
    kind: AtlasKind,
    org_id: str | None,
    cloud_id: str | None,
    key: str,
    web_url: str | None,
    state: str | None,
    status: str | None,
    owner: PersonRecord | None,
    target_date: str | None,
    tags: list[str],
    update: Mapping[str, Any] | None,
    link_keys: Mapping[str, list[str]],
) -> dict[str, MetadataValue]:
    metadata: dict[str, Any] = {
        "kind": kind,
        "org_id": org_id,
        "cloud_id": cloud_id,
        "atlas_key": key,
        "web_url": web_url,
        "ari": pick_str(payload, "ari", "id"),
        "state": state,
        "status": status,
        "owner": owner.display_name if owner is not None else None,
        "target_date": target_date,
        "start_date": pick_str(payload, "startDate"),
        "tags": tags,
        "latest_update_at": pick_str(update, "creationDate"),
        "latest_update_url": pick_str(update, "url", "webUrl"),
        "is_archived": pick(payload, "isArchived"),
    }
    for relationship, keys in link_keys.items():
        metadata[_RELATION_METADATA_KEYS[relationship]] = keys
    return clean_metadata(metadata)
