"""Confluence page and space mapping."""

from __future__ import annotations

from conftest import page_payload

from agentgraph_connector_twg import confluence, urls


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
