"""Jira work item and relationship-context mapping."""

from __future__ import annotations

from typing import Any

import pytest
from agentgraph.connectors.base import EntityRecord
from conftest import classify_atlassian_urls, idea_payload, workitem_payload

from agentgraph_connector_twg import jira, urls


def _target(url: str = "https://acme.atlassian.net/browse/ENG-42") -> urls.TwgTarget:
    target = urls.parse_url(url)
    assert target is not None
    return target


def _idea_target() -> urls.TwgTarget:
    return _target("https://acme.atlassian.net/browse/TIN-2284")


def test_workitem_maps_to_task_entity() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    task = next(entity for entity in batch.entities if entity.entity_type == "Task")
    assert task.platform == "twg"
    assert task.platform_entity_id == "jira/acme/ENG-42"
    assert task.title == "ENG-42: Atlas sync drops updates"
    assert task.metadata["issue_key"] == "ENG-42"
    assert task.metadata["status"] == "In Progress"
    assert task.metadata["status_category"] == "In Progress"
    assert task.metadata["issue_type"] == "Bug"
    assert task.metadata["priority"] == "High"
    assert task.metadata["labels"] == "atlas, sync"
    assert task.metadata["web_url"] == "https://acme.atlassian.net/browse/ENG-42"
    assert task.metadata["comment_count"] == 1
    assert task.source_created_at is not None
    assert task.source_updated_at is not None
    assert task.source_updated_at.year == 2026


def test_workitem_content_flattens_description_and_comments() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())
    task = next(entity for entity in batch.entities if entity.entity_type == "Task")

    assert task.content is not None
    assert "Updates are lost when the sync retries. @Sam Ito please confirm." in task.content
    assert "Bug ENG-42 | Status: In Progress | Assignee: Sam Ito" in task.content
    assert "— Dev Patel (2026-08-19T11:00:00.000Z): Reproduced on staging." in task.content


def test_workitem_creates_project_folder_and_containment_edge() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    folder = next(entity for entity in batch.entities if entity.entity_type == "Folder")
    assert folder.platform_entity_id == "jira/acme/project/ENG"
    assert folder.title == "Engineering"
    assert any(
        edge.edge_type == "posted_in"
        and edge.source_platform_entity_id == "jira/acme/ENG-42"
        and edge.target_platform_entity_id == "jira/acme/project/ENG"
        for edge in batch.edges
    )


def test_workitem_maps_people_and_roles() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    people = {person.platform_user_id: person for person in batch.persons}
    assert people["acct-maya"].canonical_email == "maya@acme.test"
    assert people["acct-sam"].display_name == "Sam Ito"
    assert people["acct-dev"].display_name == "Dev Patel"

    roles = {
        (edge.edge_type, edge.source_platform_user_id, edge.properties.get("role"))
        for edge in batch.edges
        if edge.source_platform_user_id
    }
    assert ("authored", "acct-maya", "reporter") in roles
    assert ("participated_in", "acct-sam", "assignee") in roles
    assert ("participated_in", "acct-dev", "commenter") in roles


def test_workitem_creates_mention_edges_from_description() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    assert any(
        edge.edge_type == "mentions"
        and edge.source_platform_entity_id == "jira/acme/ENG-42"
        and edge.target_platform_user_id == "acct-sam"
        for edge in batch.edges
    )


def test_workitem_tolerates_rest_style_fields_container() -> None:
    payload: dict[str, Any] = {
        "key": "ENG-7",
        "url": "https://acme.atlassian.net/browse/ENG-7",
        "fields": {
            "summary": "Nested summary",
            "description": "Plain text body",
            "status": {"name": "Done"},
            "issuetype": {"name": "Task"},
            "project": {"key": "ENG", "name": "Engineering"},
            "assignee": {"accountId": "acct-sam", "displayName": "Sam Ito"},
        },
    }
    target = urls.parse_url("https://acme.atlassian.net/browse/ENG-7")
    assert target is not None

    batch = jira.workitem_to_batch(payload, target=target)
    task = next(entity for entity in batch.entities if entity.entity_type == "Task")

    assert task.title == "ENG-7: Nested summary"
    assert task.metadata["status"] == "Done"
    assert task.metadata["issue_type"] == "Task"
    assert task.content is not None
    assert "Plain text body" in task.content


def test_workitem_derives_project_key_from_issue_key_when_absent() -> None:
    payload = workitem_payload()
    payload.pop("project")

    batch = jira.workitem_to_batch(payload, target=_target())

    folder = next(entity for entity in batch.entities if entity.entity_type == "Folder")
    assert folder.platform_entity_id == "jira/acme/project/ENG"
    assert folder.title == "ENG"


def test_workitem_stubs_referenced_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)
    payload = workitem_payload(description="See https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Plan")

    batch = jira.workitem_to_batch(payload, target=_target())

    stub = next(entity for entity in batch.entities if entity.is_stub)
    assert stub.entity_type == "Document"
    assert stub.platform_entity_id == "confluence/acme/884736"


def test_context_maps_relationships_to_reference_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)
    payload = {
        "object": {"key": "ENG-42"},
        "relationshipSummary": [
            {
                "relationshipName": "documented-by",
                "direction": "OUTBOUND",
                "targetType": "page",
                "count": 1,
                "targets": [
                    {
                        "ari": "ari:cloud:confluence:cloud-1:page/884736",
                        "name": "Atlas sync plan",
                        "url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Plan",
                    }
                ],
            },
            {
                "relationshipName": "blocked-by",
                "direction": "INBOUND",
                "targets": [
                    {"name": "ENG-9", "url": "https://acme.atlassian.net/browse/ENG-9"},
                ],
            },
        ],
    }

    batch = jira.context_to_batch(payload, source_entity_id="jira/acme/ENG-42")

    assert {entity.platform_entity_id for entity in batch.entities} == {
        "confluence/acme/884736",
        "jira/acme/ENG-9",
    }
    assert all(entity.is_stub for entity in batch.entities)

    outbound = next(edge for edge in batch.edges if edge.properties.get("relationship") == "documented-by")
    assert outbound.source_platform_entity_id == "jira/acme/ENG-42"
    assert outbound.target_platform_entity_id == "confluence/acme/884736"

    inbound = next(edge for edge in batch.edges if edge.properties.get("relationship") == "blocked-by")
    assert inbound.source_platform_entity_id == "jira/acme/ENG-9"
    assert inbound.target_platform_entity_id == "jira/acme/ENG-42"


def test_context_ignores_unclassifiable_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)
    payload = {
        "relationshipSummary": [
            {
                "relationshipName": "implemented-by",
                "targets": [{"name": "PR 12", "url": "https://bitbucket.org/acme/repo/pull-requests/12"}],
            }
        ]
    }

    batch = jira.context_to_batch(payload, source_entity_id="jira/acme/ENG-42")

    assert batch.entities == []
    assert batch.edges == []


def test_context_never_self_references(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)
    payload = {
        "relationshipSummary": [
            {
                "relationshipName": "self",
                "targets": [{"url": "https://acme.atlassian.net/browse/ENG-42"}],
            }
        ]
    }

    batch = jira.context_to_batch(payload, source_entity_id="jira/acme/ENG-42")

    assert batch.edges == []


def test_idea_records_its_project_type() -> None:
    batch = jira.workitem_to_batch(idea_payload(), target=_idea_target())

    task = next(entity for entity in batch.entities if entity.entity_type == "Task")
    assert task.metadata["project_type"] == "product_discovery"
    assert task.metadata["is_idea"] is True
    assert task.metadata["issue_type"] == "Idea"
    assert task.metadata["issue_link_count"] == 2
    assert task.content is not None
    assert "Idea TIN-2284" in task.content


def test_ordinary_workitem_is_not_an_idea() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    task = next(entity for entity in batch.entities if entity.entity_type == "Task")
    assert task.metadata["is_idea"] is False
    assert task.metadata["issue_link_count"] == 0
    assert "project_type" not in task.metadata


def test_idea_links_delivery_tickets_through_issue_links() -> None:
    batch = jira.workitem_to_batch(idea_payload(), target=_idea_target())

    stubs = {entity.platform_entity_id: entity for entity in batch.entities if entity.is_stub}
    assert set(stubs) == {"jira/acme/SQA-4374", "jira/acme/TIN-585"}
    assert all(stub.entity_type == "Task" for stub in stubs.values())

    edges = {
        (edge.source_platform_entity_id, edge.target_platform_entity_id): edge
        for edge in batch.edges
        if edge.edge_type == "references"
    }
    # The idea "is implemented by" SQA-4374, so the epic implements the idea.
    delivery = edges[("jira/acme/SQA-4374", "jira/acme/TIN-2284")]
    assert delivery.platform == "cross"
    assert delivery.properties["relationship"] == "implements"

    connected = edges[("jira/acme/TIN-2284", "jira/acme/TIN-585")]
    assert connected.properties["relationship"] == "connects to"


def test_issue_links_fall_back_to_the_link_type_name() -> None:
    payload = idea_payload(
        issuelinks=[{"type": {"name": "Mystery link"}, "outwardIssue": {"key": "TIN-9"}}]
    )

    batch = jira.workitem_to_batch(payload, target=_idea_target())

    edge = next(edge for edge in batch.edges if edge.edge_type == "references")
    assert edge.properties["relationship"] == "Mystery link"


def test_issue_links_skip_self_links_and_duplicates() -> None:
    payload = idea_payload(
        issuelinks=[
            {"type": {"outward": "relates to"}, "outwardIssue": {"key": "TIN-2284"}},
            {"type": {"outward": "relates to"}, "outwardIssue": {"key": "TIN-9"}},
            {"type": {"outward": "blocks"}, "inwardIssue": {"key": "TIN-9"}},
            {"type": {"outward": "relates to"}},
        ]
    )

    batch = jira.workitem_to_batch(payload, target=_idea_target())

    assert [entity.platform_entity_id for entity in batch.entities if entity.is_stub] == [
        "jira/acme/TIN-9"
    ]


def test_issue_links_are_truncated_at_the_cap() -> None:
    payload = idea_payload(
        issuelinks=[
            {"type": {"outward": "relates to"}, "outwardIssue": {"key": f"TIN-{index}"}}
            for index in range(jira.MAX_ISSUE_LINKS + 10)
        ]
    )

    batch = jira.workitem_to_batch(payload, target=_idea_target())
    task = next(entity for entity in batch.entities if entity.entity_type == "Task")

    assert len([entity for entity in batch.entities if entity.is_stub]) == jira.MAX_ISSUE_LINKS
    # The count reports every link, so the truncation is visible.
    assert task.metadata["issue_link_count"] == jira.MAX_ISSUE_LINKS + 10


def test_entity_records_are_valid_models() -> None:
    batch = jira.workitem_to_batch(workitem_payload(), target=_target())

    for entity in batch.entities:
        assert isinstance(entity, EntityRecord)
        assert entity.retention_policy == "observed"
