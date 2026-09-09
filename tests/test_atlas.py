"""Atlas (Atlassian Home) goal and project mapping."""

from __future__ import annotations

import json
from typing import Any

from conftest import ATLAS_CLOUD_ID as CLOUD
from conftest import ATLAS_ORG_ID as ORG
from conftest import atlas_project_payload, atlas_url, goal_payload

from agentgraph_connector_twg import atlas, urls

GOAL_ID = f"atlas/{ORG}/{CLOUD}/goal/ATLAS-131327"
PROJECT_ID = f"atlas/{ORG}/{CLOUD}/project/ATLAS-133324"


def _target(kind: str, key: str) -> urls.TwgTarget:
    target = urls.parse_url(atlas_url(kind, key))
    assert target is not None
    return target


def _goal_batch(**overrides: Any) -> Any:
    return atlas.goal_to_batch(goal_payload(**overrides), target=_target("goal", "ATLAS-131327"))


def _project_batch(**overrides: Any) -> Any:
    return atlas.project_to_batch(
        atlas_project_payload(**overrides),
        target=_target("project", "ATLAS-133324"),
    )


# ----------------------------------------------------------------------
# Goals
# ----------------------------------------------------------------------


def test_goal_maps_to_task_entity() -> None:
    batch = _goal_batch()

    task = next(entity for entity in batch.entities if not entity.is_stub)
    assert task.entity_type == "Task"
    assert task.platform == "twg"
    assert task.platform_entity_id == GOAL_ID
    assert task.title == "ATLAS-131327: Prove Land is a useful JSM acquisition channel"
    assert task.metadata["kind"] == "goal"
    assert task.metadata["org_id"] == ORG
    assert task.metadata["cloud_id"] == CLOUD
    assert task.metadata["atlas_key"] == "ATLAS-131327"
    assert task.metadata["web_url"] == atlas_url("goal", "ATLAS-131327")
    assert task.metadata["state"] == "On track - 0.7"
    # The goal's `status.label` is empty, so its value is used instead.
    assert task.metadata["status"] == "on_track"
    assert task.metadata["owner"] == "Maya Chen"
    assert task.metadata["target_date"] == "Jun 2027"
    assert task.metadata["tags"] == "servco-fy27-goals, l3"
    assert task.metadata["is_archived"] is False
    assert task.metadata["ari"].startswith("ari:cloud:townsquare:")


def test_goal_content_renders_header_description_and_update() -> None:
    task = next(entity for entity in _goal_batch().entities if not entity.is_stub)

    assert task.content is not None
    assert "Prove Land is a useful JSM acquisition channel" in task.content
    assert "Status: On track - 0.7 | Owner: Maya Chen | Target: Jun 2027" in task.content
    assert "Determine whether Land can become a reliable" in task.content
    assert (
        "Latest update:\n— Anurag Datta Roy (2026-08-14T03:35:22.191277Z): July actuals"
        in task.content
    )
    assert atlas_url("goal", "ATLAS-131327") in task.content


def test_goal_timestamps_come_from_the_latest_update() -> None:
    task = next(entity for entity in _goal_batch().entities if not entity.is_stub)

    # A goal has no created date; only a project reports when it started.
    assert task.source_created_at is None
    assert task.source_updated_at is not None
    assert task.source_updated_at.year == 2026
    assert task.source_updated_at.month == 8


def test_goal_maps_owner_and_update_author() -> None:
    batch = _goal_batch()

    people = {person.platform_user_id: person for person in batch.persons}
    assert people["acct-maya"].display_name == "Maya Chen"
    # An update creator often carries only a name, which becomes its identifier.
    assert people["Anurag Datta Roy"].display_name == "Anurag Datta Roy"
    roles = {
        (edge.edge_type, edge.source_platform_user_id, edge.properties.get("role"))
        for edge in batch.edges
        if edge.source_platform_user_id
    }
    assert ("authored", "acct-maya", "owner") in roles
    assert ("participated_in", "Anurag Datta Roy", "update-author") in roles


def test_goal_links_parent_goal_and_contributing_projects() -> None:
    batch = _goal_batch()

    stubs = {entity.platform_entity_id: entity for entity in batch.entities if entity.is_stub}
    assert set(stubs) == {
        f"atlas/{ORG}/{CLOUD}/goal/ATLAS-120001",
        PROJECT_ID,
    }
    assert all(stub.entity_type == "Task" for stub in stubs.values())

    edges = {
        edge.target_platform_entity_id: edge
        for edge in batch.edges
        if edge.edge_type == "references"
    }
    parent = edges[f"atlas/{ORG}/{CLOUD}/goal/ATLAS-120001"]
    assert parent.source_platform_entity_id == GOAL_ID
    assert parent.platform == "cross"
    assert parent.properties["relationship"] == "parent_goal"
    assert edges[PROJECT_ID].properties["relationship"] == "contributing_project"

    task = next(entity for entity in batch.entities if not entity.is_stub)
    assert task.metadata["parent_goal_key"] == "ATLAS-120001"
    assert task.metadata["contributing_project_keys"] == "ATLAS-133324"


def test_goal_update_flattens_an_adf_encoded_summary() -> None:
    adf = json.dumps(
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Rebaselining in September."}],
                }
            ],
        }
    )
    payload = goal_payload()
    payload["latestUserUpdate"] = {
        "creationDate": "2026-08-14T03:35:22.191277Z",
        "summaryText": adf,
        "creator": {"name": "Anurag Datta Roy"},
    }

    batch = atlas.goal_to_batch(payload, target=_target("goal", "ATLAS-131327"))
    task = next(entity for entity in batch.entities if not entity.is_stub)

    assert task.content is not None
    assert "Rebaselining in September." in task.content
    assert "{" not in task.content


def test_minimal_goal_maps_without_raising() -> None:
    payload = {"key": "ATLAS-131327", "name": "Bare goal"}

    batch = atlas.goal_to_batch(payload, target=_target("goal", "ATLAS-131327"))
    task = next(entity for entity in batch.entities if not entity.is_stub)

    assert task.platform_entity_id == GOAL_ID
    assert task.content is not None
    assert task.metadata["web_url"] == atlas_url("goal", "ATLAS-131327")
    assert task.source_created_at is None
    assert task.source_updated_at is None
    assert batch.persons == []
    assert batch.edges == []


# ----------------------------------------------------------------------
# Projects
# ----------------------------------------------------------------------


def test_project_maps_to_task_entity() -> None:
    task = next(entity for entity in _project_batch().entities if not entity.is_stub)

    assert task.entity_type == "Task"
    assert task.platform_entity_id == PROJECT_ID
    assert task.title == "ATLAS-133324: [Feature] Land E2E journey for IT Support"
    assert task.metadata["kind"] == "project"
    assert task.metadata["state"] == "On track"
    assert "status" not in task.metadata
    assert task.metadata["target_date"] == "October"
    assert task.metadata["start_date"] == "2026-09-07"
    assert task.metadata["tags"] == "sequoia-quality-signups, itsm-experiment"
    assert task.metadata["latest_update_at"] == "2026-09-07T06:29:16.529933Z"
    assert task.metadata["latest_update_url"].endswith("/updates/1db40689")


def test_project_renders_the_three_description_sections() -> None:
    task = next(entity for entity in _project_batch().entities if not entity.is_stub)

    assert task.content is not None
    assert "What: Build one coherent end-to-end Land journey for IT Support." in task.content
    assert "Why: Intent capture alone creates little customer value on its own." in task.content
    assert "Measurement: Improve Signup to D1T6AI conversion" in task.content


def test_project_timestamps_use_start_and_latest_update_dates() -> None:
    task = next(entity for entity in _project_batch().entities if not entity.is_stub)

    assert task.source_created_at is not None
    assert task.source_created_at.day == 7
    assert task.source_updated_at is not None
    assert task.source_updated_at.month == 9


def test_project_links_its_goals() -> None:
    batch = _project_batch()

    stub = next(entity for entity in batch.entities if entity.is_stub)
    assert stub.entity_type == "Task"
    assert stub.platform_entity_id == GOAL_ID

    edge = next(edge for edge in batch.edges if edge.edge_type == "references")
    assert edge.source_platform_entity_id == PROJECT_ID
    assert edge.target_platform_entity_id == GOAL_ID
    assert edge.platform == "cross"
    assert edge.properties["relationship"] == "linked_goal"

    task = next(entity for entity in batch.entities if not entity.is_stub)
    assert task.metadata["linked_goal_keys"] == "ATLAS-131327"


def test_minimal_project_maps_without_raising() -> None:
    payload = {"key": "ATLAS-133324"}

    batch = atlas.project_to_batch(payload, target=_target("project", "ATLAS-133324"))
    task = next(entity for entity in batch.entities if not entity.is_stub)

    assert task.title == "ATLAS-133324: Atlas project ATLAS-133324"
    assert task.metadata["kind"] == "project"
    assert batch.edges == []
