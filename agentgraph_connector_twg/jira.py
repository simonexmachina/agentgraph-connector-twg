"""Map `twg jira` payloads onto AgentGraph entities, people, and edges."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
    collect_mentions,
    flatten_rich_text,
    nested_str,
    parse_datetime,
    person_from_payload,
    pick,
    pick_int,
    pick_mapping,
    pick_str,
)
from agentgraph_connector_twg.stubs import stub_for_url

MAX_COMMENTS = 50
"""Comments rendered into content; older ones are summarised by a trailing count."""


def workitem_to_batch(payload: Mapping[str, Any], *, target: urls.TwgTarget) -> EntityBatch:
    """Build the Task entity, its project Folder, people, and edges for one work item."""
    site = target.site or urls.site_from_url(pick_str(payload, "url", "webUrl") or "") or ""
    key = pick_str(payload, "key", "issueKey") or target.key or ""
    entity_id = urls.workitem_entity_id(site, key) if site and key else target.entity_id

    fields = pick_mapping(payload, "fields") or payload
    summary = pick_str(payload, "summary") or pick_str(fields, "summary") or key
    description = flatten_rich_text(pick(payload, "description") or pick(fields, "description"))
    comments = _comments(payload, fields)
    web_url = (
        pick_str(payload, "url", "webUrl")
        or target.web_url
        or (urls.workitem_web_url(site, key) if site and key else None)
    )

    batch = EntityBatch()
    persons: dict[str, PersonRecord] = {}
    edges: list[EdgeRecord] = []

    reporter = _person(payload, fields, "reporter")
    creator = _person(payload, fields, "creator")
    assignee = _person(payload, fields, "assignee")

    entity = EntityRecord(
        entity_type="Task",
        platform=urls.SOURCE,
        platform_entity_id=entity_id,
        title=f"{key}: {summary}" if key else summary,
        content=_content(
            key=key,
            summary=summary,
            description=description,
            comments=comments,
            status=nested_str(payload, ("status", "name")) or nested_str(fields, ("status", "name")),
            issue_type=_issue_type(payload, fields),
            assignee=assignee,
            web_url=web_url,
        ),
        source_created_at=parse_datetime(pick(payload, "created", "createdAt") or pick(fields, "created")),
        source_updated_at=parse_datetime(pick(payload, "updated", "updatedAt") or pick(fields, "updated")),
        metadata=_metadata(
            payload=payload,
            fields=fields,
            site=site,
            key=key,
            web_url=web_url,
            comment_count=len(comments),
        ),
    )
    batch.entities.append(entity)

    project_key = _project_key(payload, fields, key)
    if site and project_key:
        project_id = urls.project_entity_id(site, project_key)
        batch.entities.append(
            EntityRecord(
                entity_type="Folder",
                platform=urls.SOURCE,
                platform_entity_id=project_id,
                title=_project_name(payload, fields) or project_key,
                content=f"Jira project {project_key}",
                metadata=clean_metadata(
                    {
                        "site": site,
                        "project_key": project_key,
                        "web_url": urls.project_web_url(site, project_key),
                    }
                ),
            )
        )
        edges.append(
            EdgeRecord(
                edge_type="posted_in",
                source_platform_entity_id=entity_id,
                target_platform_entity_id=project_id,
                platform=urls.SOURCE,
            )
        )

    for person, edge_type, role in (
        (reporter, "authored", "reporter"),
        (creator, "authored", "creator"),
        (assignee, "participated_in", "assignee"),
    ):
        if person is None:
            continue
        persons.setdefault(person.platform_user_id, person)
        edges.append(
            EdgeRecord(
                edge_type=edge_type,
                source_platform_user_id=person.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": role},
            )
        )

    for comment in comments:
        author = comment.author
        if author is None:
            continue
        persons.setdefault(author.platform_user_id, author)
        edges.append(
            EdgeRecord(
                edge_type="participated_in",
                source_platform_user_id=author.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": "commenter"},
            )
        )

    for mention_id, mention_label in collect_mentions(
        [pick(payload, "description"), pick(fields, "description"), *[c.raw_body for c in comments]]
    ):
        mention = PersonRecord(
            platform=urls.SOURCE,
            platform_user_id=mention_id,
            display_name=mention_label,
        )
        persons.setdefault(mention_id, mention)
        edges.append(
            EdgeRecord(
                edge_type="mentions",
                source_platform_entity_id=entity_id,
                target_platform_user_id=mention_id,
                platform=urls.SOURCE,
            )
        )

    batch.persons.extend(persons.values())
    batch.edges.extend(edges)
    batch.add_stubs_from(entity)
    return batch


def context_to_batch(
    payload: Mapping[str, Any],
    *,
    source_entity_id: str,
) -> EntityBatch:
    """Turn `twg context jira workitem` relationships into stubs and `references` edges."""
    batch = EntityBatch()
    seen: set[str] = set()
    for summary in as_sequence(pick(payload, "relationshipSummary", "relationships")):
        relationship = as_mapping(summary)
        if relationship is None:
            continue
        relationship_name = pick_str(relationship, "relationshipName", "name") or "related"
        outbound = (pick_str(relationship, "direction") or "OUTBOUND").upper() != "INBOUND"
        for raw_target in as_sequence(relationship.get("targets")):
            target = as_mapping(raw_target)
            if target is None:
                continue
            url = pick_str(target, "url", "webUrl")
            if url is None:
                continue
            stub = stub_for_url(url)
            if stub is None or stub.platform_entity_id in seen:
                continue
            seen.add(stub.platform_entity_id)
            if stub.platform_entity_id == source_entity_id:
                continue
            batch.entities.append(stub)
            batch.edges.append(
                EdgeRecord(
                    edge_type="references",
                    source_platform_entity_id=(
                        source_entity_id if outbound else stub.platform_entity_id
                    ),
                    target_platform_entity_id=(
                        stub.platform_entity_id if outbound else source_entity_id
                    ),
                    platform="cross",
                    properties={"relationship": relationship_name},
                )
            )
    return batch


class _Comment:
    __slots__ = ("author", "body", "created", "raw_body")

    def __init__(
        self,
        author: PersonRecord | None,
        body: str,
        created: str | None,
        raw_body: Any,
    ) -> None:
        self.author = author
        self.body = body
        self.created = created
        self.raw_body = raw_body


def _comments(payload: Mapping[str, Any], fields: Mapping[str, Any]) -> list[_Comment]:
    raw = pick(payload, "comments")
    if raw is None:
        container = pick_mapping(payload, "comment") or pick_mapping(fields, "comment")
        raw = pick(container, "comments") if container is not None else None
    if raw is None:
        raw = pick(fields, "comments")

    comments: list[_Comment] = []
    for item in as_sequence(raw):
        mapping = as_mapping(item)
        if mapping is None:
            continue
        raw_body = pick(mapping, "body", "renderedBody", "text")
        comments.append(
            _Comment(
                author=person_from_payload(
                    pick(mapping, "author", "updateAuthor", "createdBy"),
                    platform=urls.SOURCE,
                ),
                body=flatten_rich_text(raw_body),
                created=pick_str(mapping, "created", "createdAt", "updated"),
                raw_body=raw_body,
            )
        )
    return comments


def _person(
    payload: Mapping[str, Any],
    fields: Mapping[str, Any],
    name: str,
) -> PersonRecord | None:
    return person_from_payload(
        pick(payload, name) or pick(fields, name),
        platform=urls.SOURCE,
    )


def _issue_type(payload: Mapping[str, Any], fields: Mapping[str, Any]) -> str | None:
    return (
        nested_str(payload, ("issueType", "name"))
        or nested_str(payload, ("issuetype", "name"))
        or nested_str(fields, ("issuetype", "name"))
        or nested_str(fields, ("issueType", "name"))
        or pick_str(payload, "type")
    )


def _project_key(payload: Mapping[str, Any], fields: Mapping[str, Any], key: str) -> str | None:
    project_key = (
        nested_str(payload, ("project", "key"))
        or nested_str(fields, ("project", "key"))
        or pick_str(payload, "projectKey")
    )
    if project_key:
        return project_key
    return key.rsplit("-", 1)[0] if "-" in key else None


def _project_name(payload: Mapping[str, Any], fields: Mapping[str, Any]) -> str | None:
    return nested_str(payload, ("project", "name")) or nested_str(fields, ("project", "name"))


def _content(
    *,
    key: str,
    summary: str,
    description: str,
    comments: list[_Comment],
    status: str | None,
    issue_type: str | None,
    assignee: PersonRecord | None,
    web_url: str | None,
) -> str:
    header = " | ".join(
        part
        for part in (
            f"{issue_type} {key}" if issue_type and key else key or None,
            f"Status: {status}" if status else None,
            f"Assignee: {assignee.display_name or assignee.platform_user_id}"
            if assignee is not None
            else None,
        )
        if part
    )
    sections = [summary, header, description]
    if comments:
        rendered = [
            f"— {comment.author.display_name if comment.author else 'Unknown'}"
            f"{f' ({comment.created})' if comment.created else ''}: {comment.body}"
            for comment in comments[:MAX_COMMENTS]
            if comment.body
        ]
        if rendered:
            sections.append("Comments:\n" + "\n".join(rendered))
        if len(comments) > MAX_COMMENTS:
            sections.append(f"({len(comments) - MAX_COMMENTS} older comments not shown)")
    if web_url:
        sections.append(web_url)
    return "\n\n".join(section for section in sections if section)


def _metadata(
    *,
    payload: Mapping[str, Any],
    fields: Mapping[str, Any],
    site: str,
    key: str,
    web_url: str | None,
    comment_count: int,
) -> dict[str, MetadataValue]:
    labels = as_sequence(pick(payload, "labels") or pick(fields, "labels"))
    return clean_metadata(
        {
            "site": site,
            "issue_key": key,
            "web_url": web_url,
            "ari": pick_str(payload, "ari", "id"),
            "issue_type": _issue_type(payload, fields),
            "status": nested_str(payload, ("status", "name")) or nested_str(fields, ("status", "name")),
            "status_category": (
                nested_str(payload, ("status", "statusCategory", "name"))
                or nested_str(fields, ("status", "statusCategory", "name"))
            ),
            "priority": nested_str(payload, ("priority", "name")) or nested_str(fields, ("priority", "name")),
            "resolution": nested_str(payload, ("resolution", "name")) or nested_str(fields, ("resolution", "name")),
            "project_key": _project_key(payload, fields, key),
            "project_name": _project_name(payload, fields),
            "assignee": nested_str(payload, ("assignee", "displayName"))
            or nested_str(fields, ("assignee", "displayName")),
            "reporter": nested_str(payload, ("reporter", "displayName"))
            or nested_str(fields, ("reporter", "displayName")),
            "labels": labels,
            "due_date": pick_str(payload, "duedate", "dueDate") or pick_str(fields, "duedate"),
            "comment_count": comment_count,
            "vote_count": pick_int(payload, "votes") or pick_int(fields, "votes"),
        }
    )
