"""Atlassian and Loom URL parsing, plus the connector's identifier scheme.

Identifiers are site-qualified so a tenant with several Atlassian sites cannot
collide:

    jira/<site>/<KEY>                        -> Task
    jira/<site>/project/<KEY>                -> Folder
    confluence/<site>/<pageId>               -> Document
    confluence/<site>/space/<KEY>            -> Folder
    loom/<videoId>                           -> Video
    atlas/<orgId>/<cloudId>/goal/<KEY>       -> Task
    atlas/<orgId>/<cloudId>/project/<KEY>    -> Task

Atlas (Atlassian Home) is org-scoped rather than site-scoped, so its
identifiers carry both the org and the cloud id from the URL. That keeps them
lossless: `entity_url()` rebuilds the browse URL with no cache and no config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import parse_qs, urlsplit

from agentgraph.connectors.base import ResourceType, SourceReference

SOURCE: Final[str] = "twg"

TargetKind = Literal[
    "work-item",
    "page",
    "space",
    "project",
    "video",
    "atlas-goal",
    "atlas-project",
]
TwgResourceType = ResourceType | Literal["work-item", "video"]
"""Resource kinds emitted by this connector in addition to AgentGraph's base kinds."""

_ISSUE_KEY = r"[A-Z][A-Z0-9_]+-\d+"

SITE_LABEL: Final[str] = r"[A-Za-z0-9][A-Za-z0-9-]*"
"""Pattern a bare site name has to match, shared with config validation."""

_SITE_DOMAIN_PATHS: Final[dict[str, tuple[str, ...]]] = {
    # A tenant serves every product from `<site>.atlassian.net`, so narrow that
    # host to the paths this connector understands. Product domains are already
    # scoped to one product; Atlassian moves tenants onto them one product at a
    # time and keeps the paths (and an `atlassian.net` redirect) unchanged, so
    # both spellings have to be recognised.
    "atlassian.net": ("browse", "jira", "wiki"),
    "jira.atlassian.cloud": ("browse", "jira"),
    "confluence.atlassian.cloud": ("wiki",),
}
_SITE_HOST = re.compile(
    "^(?P<site>{})\\.(?:{})$".format(
        SITE_LABEL, "|".join(re.escape(domain) for domain in _SITE_DOMAIN_PATHS)
    )
)

_BROWSE_PATH = re.compile(rf"^/browse/(?P<key>{_ISSUE_KEY})/?$", re.IGNORECASE)
_JIRA_PROJECT_PATH = re.compile(
    r"^/jira/(?:software|core|servicedesk|polaris|product-discovery|discovery)"
    r"(?:/c)?/projects/(?P<project>[A-Za-z0-9_]+)(?:/.*)?$"
)
_WIKI_PAGE_PATH = re.compile(
    r"^/wiki/spaces/(?P<space>[^/]+)/(?:pages|blog)/(?P<page_id>\d+)(?:/.*)?$"
)
_WIKI_SPACE_PATH = re.compile(r"^/wiki/spaces/(?P<space>[^/]+)(?:/(?:overview|pages))?/?$")
_WIKI_TINY_PATH = re.compile(r"^/wiki/x/(?P<tiny>[A-Za-z0-9_-]+)/?$")
_LOOM_PATH = re.compile(r"^/(?:share|embed)/(?P<video_id>[A-Za-z0-9]+)/?$")
_LOOM_HOSTS: Final[frozenset[str]] = frozenset({"loom.com", "www.loom.com"})

_ATLAS_HOST: Final[str] = "home.atlassian.com"
# Atlas keys share the Jira key shape. Trailing segments (`/updates`, `/about`)
# are absorbed, but the `/o/<org>/project/<KEY>` form that `twg`'s own help text
# shows is deliberately not matched: without a cloud id it cannot produce the
# same identifier, so accepting it would give one resource two graph keys.
_ATLAS_PATH = re.compile(
    rf"^/o/(?P<org>[0-9a-f-]+)/s/(?P<cloud>[0-9a-f-]+)/(?P<kind>project|goal)/(?P<key>{_ISSUE_KEY})(?:/.*)?$",
    re.IGNORECASE,
)

_RESOURCE_TYPES: Final[dict[TargetKind, TwgResourceType]] = {
    "work-item": "work-item",
    "page": "document",
    "space": "folder",
    "project": "folder",
    "video": "video",
    # Atlas goals and projects are units of work, so they reuse the work-item
    # resource type and become Task entities.
    "atlas-goal": "work-item",
    "atlas-project": "work-item",
}


@dataclass(frozen=True)
class TwgTarget:
    """A resource the connector can fetch, decoded from a URL or an identifier."""

    kind: TargetKind
    entity_id: str
    site: str | None = None
    """Site name, or — for Atlas — the cloud id, which `--site` also accepts."""
    key: str | None = None
    """Issue key, page id, space key, project key, video id, or Atlas key."""
    space_key: str | None = None
    web_url: str | None = None
    org_id: str | None = None
    """Atlassian org id, carried only by Atlas targets."""

    @property
    def resource_type(self) -> TwgResourceType:
        return _RESOURCE_TYPES[self.kind]

    def to_reference(self) -> SourceReference:
        fetch_meta = {
            key: value
            for key, value in (
                ("site", self.site),
                ("space_key", self.space_key),
                ("web_url", self.web_url),
                ("org_id", self.org_id),
            )
            if value is not None
        }
        return SourceReference(
            source=SOURCE,
            # AgentGraph 0.7 accepts connector-defined resource kinds at runtime,
            # although its public type alias does not yet declare these two values.
            resource_type=self.resource_type,
            resource_id=self.entity_id,
            fetch_meta=fetch_meta or None,
        )


def workitem_entity_id(site: str, key: str) -> str:
    return f"jira/{site}/{key.upper()}"


def project_entity_id(site: str, project_key: str) -> str:
    return f"jira/{site}/project/{project_key.upper()}"


def page_entity_id(site: str, page_id: str) -> str:
    return f"confluence/{site}/{page_id}"


def space_entity_id(site: str, space_key: str) -> str:
    return f"confluence/{site}/space/{space_key.upper()}"


def video_entity_id(video_id: str) -> str:
    return f"loom/{video_id}"


def goal_entity_id(org_id: str, cloud_id: str, key: str) -> str:
    return f"atlas/{org_id}/{cloud_id}/goal/{key.upper()}"


def atlas_project_entity_id(org_id: str, cloud_id: str, key: str) -> str:
    return f"atlas/{org_id}/{cloud_id}/project/{key.upper()}"


def workitem_web_url(site: str, key: str) -> str:
    return f"https://{site}.atlassian.net/browse/{key.upper()}"


def project_web_url(site: str, project_key: str) -> str:
    return f"https://{site}.atlassian.net/jira/software/projects/{project_key.upper()}/summary"


def page_web_url(site: str, page_id: str, space_key: str | None = None) -> str:
    if space_key:
        return f"https://{site}.atlassian.net/wiki/spaces/{space_key}/pages/{page_id}"
    return f"https://{site}.atlassian.net/wiki/pages/viewpage.action?pageId={page_id}"


def space_web_url(site: str, space_key: str) -> str:
    return f"https://{site}.atlassian.net/wiki/spaces/{space_key.upper()}/overview"


def video_web_url(video_id: str) -> str:
    return f"https://www.loom.com/share/{video_id}"


def goal_web_url(org_id: str, cloud_id: str, key: str) -> str:
    return f"https://{_ATLAS_HOST}/o/{org_id}/s/{cloud_id}/goal/{key.upper()}"


def atlas_project_web_url(org_id: str, cloud_id: str, key: str) -> str:
    return f"https://{_ATLAS_HOST}/o/{org_id}/s/{cloud_id}/project/{key.upper()}"


def parse_url(url: str) -> TwgTarget | None:
    """Return the resource behind an Atlassian or Loom URL, or None.

    Purely local: no `twg` call and no network. Confluence tiny links
    (`/wiki/x/<tiny>`) cannot be decoded offline and are handled by the
    connector's async observation resolver.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower().split(":")[0]

    if host in _LOOM_HOSTS:
        loom_match = _LOOM_PATH.match(parsed.path)
        if loom_match is None:
            return None
        video_id = loom_match.group("video_id")
        return TwgTarget(
            kind="video",
            entity_id=video_entity_id(video_id),
            key=video_id,
            web_url=video_web_url(video_id),
        )

    if host == _ATLAS_HOST:
        atlas_match = _ATLAS_PATH.match(parsed.path)
        if atlas_match is None:
            return None
        return _atlas_target(
            atlas_match.group("org"),
            atlas_match.group("cloud"),
            atlas_match.group("kind").lower(),
            atlas_match.group("key"),
        )

    site_match = _SITE_HOST.match(host)
    if site_match is None:
        return None
    site = site_match.group("site")
    path = parsed.path
    query = parse_qs(parsed.query)

    browse_match = _BROWSE_PATH.match(path)
    if browse_match is not None:
        return _workitem_target(site, browse_match.group("key"))

    selected = _first(query.get("selectedIssue")) or _first(query.get("selectedIssueId"))
    if selected and re.fullmatch(_ISSUE_KEY, selected, re.IGNORECASE):
        return _workitem_target(site, selected)

    page_match = _WIKI_PAGE_PATH.match(path)
    if page_match is not None:
        space_key = page_match.group("space")
        page_id = page_match.group("page_id")
        return TwgTarget(
            kind="page",
            entity_id=page_entity_id(site, page_id),
            site=site,
            key=page_id,
            space_key=space_key,
            web_url=page_web_url(site, page_id, space_key),
        )

    page_id_param = _first(query.get("pageId"))
    if page_id_param and page_id_param.isdigit():
        return TwgTarget(
            kind="page",
            entity_id=page_entity_id(site, page_id_param),
            site=site,
            key=page_id_param,
            web_url=page_web_url(site, page_id_param),
        )

    space_match = _WIKI_SPACE_PATH.match(path)
    if space_match is not None:
        space_key = space_match.group("space")
        return TwgTarget(
            kind="space",
            entity_id=space_entity_id(site, space_key),
            site=site,
            key=space_key,
            space_key=space_key,
            web_url=space_web_url(site, space_key),
        )

    project_match = _JIRA_PROJECT_PATH.match(path)
    if project_match is not None:
        project_key = project_match.group("project")
        return TwgTarget(
            kind="project",
            entity_id=project_entity_id(site, project_key),
            site=site,
            key=project_key,
            web_url=project_web_url(site, project_key),
        )

    return None


def canonical_url(url: str | None) -> str | None:
    """Return the identifier-derived form of a URL this connector owns, or None.

    A Confluence page URL ends in the page title, so a URL that arrives from a
    payload or a caller is rebuilt from what `parse_url` keeps rather than stored
    as given. A page name must never reach an observation record or entity
    metadata.

    This also normalises the host to `<site>.atlassian.net`, which the connector
    is already committed to: `page_web_url()` — and therefore `entity_url()` —
    emits only that spelling, and per `_SITE_DOMAIN_PATHS` a migrated tenant
    keeps an `atlassian.net` redirect. Returns None for anything `parse_url`
    cannot decode, including tiny wiki links.
    """
    if url is None:
        return None
    target = parse_url(url)
    return target.web_url if target is not None else None


def is_tiny_wiki_link(url: str) -> bool:
    """True for Confluence short links, which need a `twg resolve` round trip."""
    parsed = urlsplit(url)
    host = parsed.netloc.lower().split(":")[0]
    return _SITE_HOST.match(host) is not None and _WIKI_TINY_PATH.match(parsed.path) is not None


def parse_entity_id(entity_id: str) -> TwgTarget | None:
    """Decode a stored `platform_entity_id` back into a fetchable target."""
    parts = entity_id.split("/")
    if parts[0] == "loom" and len(parts) == 2:
        return TwgTarget(
            kind="video",
            entity_id=entity_id,
            key=parts[1],
            web_url=video_web_url(parts[1]),
        )
    if parts[0] == "atlas" and len(parts) == 5 and parts[3] in {"goal", "project"}:
        return _atlas_target(parts[1], parts[2], parts[3], parts[4])
    if parts[0] == "jira" and len(parts) == 3:
        return _workitem_target(parts[1], parts[2])
    if parts[0] == "jira" and len(parts) == 4 and parts[2] == "project":
        return TwgTarget(
            kind="project",
            entity_id=entity_id,
            site=parts[1],
            key=parts[3],
            web_url=project_web_url(parts[1], parts[3]),
        )
    if parts[0] == "confluence" and len(parts) == 4 and parts[2] == "space":
        return TwgTarget(
            kind="space",
            entity_id=entity_id,
            site=parts[1],
            key=parts[3],
            space_key=parts[3],
            web_url=space_web_url(parts[1], parts[3]),
        )
    if parts[0] == "confluence" and len(parts) == 3:
        return TwgTarget(
            kind="page",
            entity_id=entity_id,
            site=parts[1],
            key=parts[2],
            web_url=page_web_url(parts[1], parts[2]),
        )
    return None


def parse_ari(ari: str, *, site: str | None = None) -> TwgTarget | None:
    """Decode the Atlassian ARIs the connector can act on.

    `twg resolve` and the context commands return ARIs such as
    `ari:cloud:confluence:<cloudId>:page/12345` or
    `ari:cloud:jira:<cloudId>:issue/PROJ-1`.

    Atlas is deliberately absent: `ari:cloud:townsquare:<cloudId>:goal/<uuid>`
    identifies the goal by UUID, and `twg goals get` only accepts the
    `ATLAS-nnnnnn` key, so such an ARI is not fetchable.
    """
    if not ari.startswith("ari:"):
        return None
    segments = ari.split(":")
    if len(segments) < 5:
        return None
    product = segments[2]
    resource = segments[-1]
    kind, _, identifier = resource.partition("/")
    if not identifier:
        return None
    if product == "confluence" and kind in {"page", "blogpost"} and site:
        return TwgTarget(
            kind="page",
            entity_id=page_entity_id(site, identifier),
            site=site,
            key=identifier,
            web_url=page_web_url(site, identifier),
        )
    if (
        product == "jira"
        and kind == "issue"
        and site
        and re.fullmatch(_ISSUE_KEY, identifier, re.IGNORECASE)
    ):
        return _workitem_target(site, identifier)
    if product == "loom" and kind in {"video", "recording"}:
        return TwgTarget(
            kind="video",
            entity_id=video_entity_id(identifier),
            key=identifier,
            web_url=video_web_url(identifier),
        )
    return None


def site_from_url(url: str) -> str | None:
    """Return the Atlassian site name in a URL host, if present."""
    return site_from_host(urlsplit(url).netloc)


def site_from_host(host: str) -> str | None:
    """Return the site name in an Atlassian host, or None if it is not one.

    Recognises `hello.atlassian.net` and the per-product domains a migrated
    tenant is served from, such as `hello.jira.atlassian.cloud`.
    """
    match = _SITE_HOST.match(host.strip().lower().split(":")[0])
    return match.group("site") if match else None


def site_host_examples(site: str) -> tuple[str, ...]:
    """Host spellings for a site, for use in error messages and documentation."""
    return tuple(f"{site}.{domain}" for domain in _SITE_DOMAIN_PATHS)


def site_url_patterns(site: str) -> list[str]:
    """Observation URL patterns covering every host spelling for a site.

    `site` may be `*` to match any tenant.
    """
    return [
        f"https://{site}.{domain}/{path}/*"
        for domain, paths in _SITE_DOMAIN_PATHS.items()
        for path in paths
    ]


def _workitem_target(site: str, key: str) -> TwgTarget:
    key = key.upper()
    return TwgTarget(
        kind="work-item",
        entity_id=workitem_entity_id(site, key),
        site=site,
        key=key,
        web_url=workitem_web_url(site, key),
    )


def _atlas_target(org_id: str, cloud_id: str, kind: str, key: str) -> TwgTarget:
    key = key.upper()
    if kind == "goal":
        return TwgTarget(
            kind="atlas-goal",
            entity_id=goal_entity_id(org_id, cloud_id, key),
            site=cloud_id,
            key=key,
            web_url=goal_web_url(org_id, cloud_id, key),
            org_id=org_id,
        )
    return TwgTarget(
        kind="atlas-project",
        entity_id=atlas_project_entity_id(org_id, cloud_id, key),
        site=cloud_id,
        key=key,
        web_url=atlas_project_web_url(org_id, cloud_id, key),
        org_id=org_id,
    )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0].strip()
    return value or None
