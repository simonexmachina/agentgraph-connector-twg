"""Confluence page and space mapping."""

from __future__ import annotations

import pytest
from conftest import (
    atlas_url,
    classify_atlassian_urls,
    page_context_payload,
    page_payload,
)

from agentgraph_connector_twg import confluence, urls

PAGE_ID = "confluence/acme/884736"


def _target() -> urls.TwgTarget:
    target = urls.parse_url("https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Atlas+sync+plan")
    assert target is not None
    return target


def test_page_maps_to_document_entity() -> None:
    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG", space_name="Engineering")

    page = next(entity for entity in batch.entities if entity.entity_type == "Document")
    assert page.platform_entity_id == "confluence/acme/884736"
    assert page.title == "Atlas sync plan"
    assert page.metadata["space_key"] == "ENG"
    assert page.metadata["content_type"] == "page"
    assert page.metadata["body_format"] == "markdown"
    assert page.metadata["lossy_conversion"] is True
    assert page.metadata["version_number"] == 7
    assert page.metadata["total_views"] == 128
    assert page.metadata["web_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736"


def test_page_url_from_payload_loses_the_page_title() -> None:
    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG")
    page = next(entity for entity in batch.entities if entity.entity_type == "Document")

    assert page.metadata["web_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736"
    assert page.content is not None
    assert "Atlas+sync+plan" not in page.content


def test_page_content_includes_outline_and_body() -> None:
    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG")
    page = next(entity for entity in batch.entities if entity.entity_type == "Document")

    assert page.content is not None
    assert "# Overview" in page.content
    assert "## Retry policy" in page.content
    assert "Atlas sync runs every 15 minutes." in page.content


def test_page_uses_version_timestamp_as_source_update() -> None:
    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG")
    page = next(entity for entity in batch.entities if entity.entity_type == "Document")

    assert page.source_created_at is not None
    assert page.source_updated_at is not None
    assert page.source_updated_at.month == 8
    assert page.source_updated_at.day == 18


def test_page_creates_space_folder_and_authors() -> None:
    batch = confluence.page_to_batch(
        page_payload(),
        target=_target(),
        space_key="ENG",
        space_name="Engineering",
    )

    folder = next(entity for entity in batch.entities if entity.entity_type == "Folder")
    assert folder.platform_entity_id == "confluence/acme/space/ENG"
    assert folder.title == "Engineering"
    assert any(
        edge.edge_type == "posted_in"
        and edge.source_platform_entity_id == "confluence/acme/884736"
        and edge.target_platform_entity_id == "confluence/acme/space/ENG"
        for edge in batch.edges
    )

    authored = {
        (edge.source_platform_user_id, edge.properties.get("role"))
        for edge in batch.edges
        if edge.edge_type == "authored"
    }
    assert ("acct-maya", "author") in authored
    assert ("acct-sam", "editor") in authored


def test_page_falls_back_to_excerpt_without_body() -> None:
    payload = page_payload()
    payload.pop("body")

    batch = confluence.page_to_batch(payload, target=_target(), space_key="ENG")
    page = next(entity for entity in batch.entities if entity.entity_type == "Document")

    assert page.content is not None
    assert "How Atlas sync works" in page.content


def test_page_without_space_key_has_no_folder() -> None:
    target = urls.parse_url("https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=884736")
    assert target is not None

    batch = confluence.page_to_batch(page_payload(), target=target)

    assert [entity.entity_type for entity in batch.entities] == ["Document"]


def test_context_mentions_become_edges_to_each_account() -> None:
    batch = confluence.context_to_batch(page_context_payload(), source_entity_id=PAGE_ID)

    mentioned = {
        edge.target_platform_user_id
        for edge in batch.edges
        if edge.edge_type == "mentions" and edge.source_platform_entity_id == PAGE_ID
    }
    assert mentioned == {"acct-sam", "acct-lee"}

    people = {person.platform_user_id: person for person in batch.persons}
    assert people["acct-sam"].canonical_email == "sam@acme.test"
    assert people["acct-sam"].display_name == "Sam Ito"
    # twg omits the email for some accounts; the mention is still worth an edge.
    assert people["acct-lee"].canonical_email is None


def test_context_skips_unidentified_targets_and_passive_relationships() -> None:
    batch = confluence.context_to_batch(page_context_payload(), source_entity_id=PAGE_ID)

    identifiers = {person.platform_user_id for person in batch.persons}
    assert not any("unidentified" in identifier for identifier in identifiers)
    assert "acct-viewer" not in identifiers
    assert not any(
        edge.source_platform_user_id == "acct-viewer" or edge.target_platform_user_id == "acct-viewer"
        for edge in batch.edges
    )


def test_context_roles_reach_contributors_and_watchers() -> None:
    batch = confluence.context_to_batch(page_context_payload(), source_entity_id=PAGE_ID)

    participated = {
        (edge.source_platform_user_id, edge.properties.get("role"))
        for edge in batch.edges
        if edge.edge_type == "participated_in" and edge.target_platform_entity_id == PAGE_ID
    }
    assert participated == {("acct-dev", "contributor"), ("acct-maya", "watcher")}


def test_context_maps_ownership_and_editing_to_authored() -> None:
    payload = page_context_payload(
        relationships=[
            {
                "relationshipName": name,
                "direction": "inbound",
                "targets": [{"name": "Maya Chen", "accountId": "acct-maya"}],
            }
            for name in (
                "atlassian_user_owns_confluence_page",
                "atlassian_user_created_confluence_page",
                "atlassian_user_updated_confluence_page",
            )
        ]
    )

    batch = confluence.context_to_batch(payload, source_entity_id=PAGE_ID)

    assert {edge.properties.get("role") for edge in batch.edges} == {"owner", "author", "editor"}
    assert {edge.edge_type for edge in batch.edges} == {"authored"}
    # One Person, three edges: a page's owner is usually its author too.
    assert len(batch.persons) == 1


def test_context_reads_a_blogpost_relationship_name() -> None:
    """The verb is matched, not the whole name, so a blogpost still yields edges."""
    payload = page_context_payload(
        relationships=[
            {
                "relationshipName": "atlassian_user_mentioned_in_confluence_blogpost",
                "targets": [{"name": "Sam Ito", "accountId": "acct-sam"}],
            }
        ]
    )

    batch = confluence.context_to_batch(payload, source_entity_id=PAGE_ID)

    assert [edge.target_platform_user_id for edge in batch.edges] == ["acct-sam"]


def test_context_reads_a_summary_shaped_payload() -> None:
    payload = {
        "relationshipSummary": [
            {
                "relationshipName": "atlassian_user_watches_confluence_page",
                "targets": [{"ari": "ari:cloud:identity::user/acct-sam", "accountId": "acct-sam"}],
            }
        ]
    }

    batch = confluence.context_to_batch(payload, source_entity_id=PAGE_ID)

    assert [edge.properties.get("role") for edge in batch.edges] == ["watcher"]


def test_context_caps_targets_per_relationship() -> None:
    payload = page_context_payload(
        relationships=[
            {
                "relationshipName": "atlassian_user_mentioned_in_confluence_page",
                "targets": [
                    {"accountId": f"acct-{index}"}
                    for index in range(confluence.MAX_CONTEXT_TARGETS + 5)
                ],
            }
        ]
    )

    batch = confluence.context_to_batch(payload, source_entity_id=PAGE_ID)

    assert len(batch.edges) == confluence.MAX_CONTEXT_TARGETS


def test_markdown_links_become_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """A markdown link's closing `)` must not reach the URL classifier."""
    classify_atlassian_urls(monkeypatch)

    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG")

    referenced = {
        edge.target_platform_entity_id
        for edge in batch.edges
        if edge.edge_type == "references" and edge.source_platform_entity_id == PAGE_ID
    }
    assert "jira/acme/ENG-42" in referenced
    assert "confluence/acme/884737" in referenced
    assert "loom/abc123def456" in referenced
    assert any(reference is not None and "/project/ATLAS-133324" in reference for reference in referenced)
    assert {entity.platform_entity_id for entity in batch.entities if entity.is_stub} <= referenced


def test_a_linked_space_is_not_keyed_on_its_bracket(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)

    batch = confluence.page_to_batch(page_payload(), target=_target(), space_key="ENG")

    identifiers = {entity.platform_entity_id for entity in batch.entities}
    assert "confluence/acme/space/ENG)" not in identifiers
    # The space it links is the one it lives in, already in this batch in full.
    assert [entity.platform_entity_id for entity in batch.entities].count(
        "confluence/acme/space/ENG"
    ) == 1
    assert atlas_url("project", "ATLAS-133324") not in identifiers


def test_space_maps_to_folder_entity() -> None:
    payload = {
        "id": "65539",
        "key": "ENG",
        "name": "Engineering",
        "type": "global",
        "status": "current",
        "description": {"plain": "Engineering space"},
    }

    entity = confluence.space_to_entity(payload, site="acme")

    assert entity.entity_type == "Folder"
    assert entity.platform_entity_id == "confluence/acme/space/ENG"
    assert entity.title == "Engineering"
    assert entity.metadata["space_id"] == "65539"
    assert entity.metadata["web_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/overview"
    assert entity.content is not None
    assert "Engineering space" in entity.content


def test_space_key_from_payload() -> None:
    assert confluence.space_key_from_payload({"key": "ENG"}) == "ENG"
    assert confluence.space_key_from_payload({"spaceKey": "OPS"}) == "OPS"
    assert confluence.space_key_from_payload({"id": "1"}) is None
