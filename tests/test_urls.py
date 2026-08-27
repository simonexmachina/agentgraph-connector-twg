"""URL and identifier handling."""

from __future__ import annotations

import pytest

from agentgraph_connector_twg import urls


@pytest.mark.parametrize(
    ("url", "kind", "entity_id"),
    [
        (
            "https://acme.atlassian.net/browse/ENG-42",
            "work-item",
            "jira/acme/ENG-42",
        ),
        (
            "https://acme.atlassian.net/browse/eng-42",
            "work-item",
            "jira/acme/ENG-42",
        ),
        (
            "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12?selectedIssue=ENG-42",
            "work-item",
            "jira/acme/ENG-42",
        ),
        (
            "https://acme.atlassian.net/jira/software/projects/ENG/boards/12",
            "project",
            "jira/acme/project/ENG",
        ),
        (
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Atlas+sync+plan",
            "page",
            "confluence/acme/884736",
        ),
        (
            "https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=884736",
            "page",
            "confluence/acme/884736",
        ),
        (
            "https://acme.atlassian.net/wiki/spaces/ENG/overview",
            "space",
            "confluence/acme/space/ENG",
        ),
        (
            "https://www.loom.com/share/abc123def456",
            "video",
            "loom/abc123def456",
        ),
        (
            "https://loom.com/embed/abc123def456?sid=1",
            "video",
            "loom/abc123def456",
        ),
    ],
)
def test_parse_url_identifies_supported_resources(url: str, kind: str, entity_id: str) -> None:
    target = urls.parse_url(url)

    assert target is not None
    assert target.kind == kind
    assert target.entity_id == entity_id


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/browse/ENG-42",
        "https://acme.atlassian.net/",
        "https://acme.atlassian.net/browse/not-an-issue",
        "https://www.loom.com/looks/abc123",
        "ftp://acme.atlassian.net/browse/ENG-42",
    ],
)
def test_parse_url_rejects_unowned_urls(url: str) -> None:
    assert urls.parse_url(url) is None


def test_page_target_keeps_space_key_and_web_url() -> None:
    target = urls.parse_url("https://acme.atlassian.net/wiki/spaces/ENG/pages/884736/Plan")

    assert target is not None
    assert target.space_key == "ENG"
    assert target.web_url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736"
    reference = target.to_reference()
    assert reference.source == "twg"
    assert reference.resource_type == "document"
    assert reference.fetch_meta == {
        "site": "acme",
        "space_key": "ENG",
        "web_url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/884736",
    }


def test_workitem_reference_uses_workitem_resource_type() -> None:
    target = urls.parse_url("https://acme.atlassian.net/browse/ENG-42")

    assert target is not None
    assert target.to_reference().resource_type == "work-item"


def test_video_reference_uses_video_resource_type() -> None:
    target = urls.parse_url("https://www.loom.com/share/abc123def456")

    assert target is not None
    assert target.to_reference().resource_type == "video"


def test_tiny_wiki_links_are_recognised_but_not_decoded() -> None:
    url = "https://acme.atlassian.net/wiki/x/AbCdEf"

    assert urls.parse_url(url) is None
    assert urls.is_tiny_wiki_link(url) is True
    assert urls.is_tiny_wiki_link("https://acme.atlassian.net/wiki/spaces/ENG") is False


@pytest.mark.parametrize(
    "entity_id",
    [
        "jira/acme/ENG-42",
        "jira/acme/project/ENG",
        "confluence/acme/884736",
        "confluence/acme/space/ENG",
        "loom/abc123def456",
    ],
)
def test_entity_ids_round_trip(entity_id: str) -> None:
    target = urls.parse_entity_id(entity_id)

    assert target is not None
    assert target.entity_id == entity_id
    assert target.web_url is not None


def test_parse_entity_id_rejects_foreign_ids() -> None:
    assert urls.parse_entity_id("gmail/thread-1") is None
    assert urls.parse_entity_id("jira") is None


@pytest.mark.parametrize(
    ("ari", "entity_id"),
    [
        ("ari:cloud:confluence:cloud-1:page/884736", "confluence/acme/884736"),
        ("ari:cloud:jira:cloud-1:issue/ENG-42", "jira/acme/ENG-42"),
        ("ari:cloud:loom::video/abc123def456", "loom/abc123def456"),
    ],
)
def test_parse_ari_decodes_supported_products(ari: str, entity_id: str) -> None:
    target = urls.parse_ari(ari, site="acme")

    assert target is not None
    assert target.entity_id == entity_id


def test_parse_ari_ignores_unsupported_products() -> None:
    assert urls.parse_ari("ari:cloud:bitbucket:cloud-1:repository/x", site="acme") is None
    assert urls.parse_ari("not-an-ari", site="acme") is None


def test_site_from_url() -> None:
    assert urls.site_from_url("https://acme.atlassian.net/browse/ENG-42") == "acme"
    assert urls.site_from_url("https://www.loom.com/share/abc") is None
