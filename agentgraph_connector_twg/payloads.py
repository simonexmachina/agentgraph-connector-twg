"""Tolerant accessors for `twg` payloads.

`twg` normalises several Atlassian APIs, so the same logical field can arrive
under more than one name (`url` / `webUrl`, `created` / `createdAt`). These
helpers read the documented field first and fall back to the known aliases
rather than raising, which keeps mapping code linear and upgrade-tolerant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from agentgraph.connectors.base import PersonRecord

MetadataValue = str | int | float | bool | None


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return None


def as_sequence(value: Any) -> list[Any]:
    if isinstance(value, list):
        return cast(list[Any], value)
    return []


def pick(payload: Mapping[str, Any] | None, *names: str) -> Any:
    """Return the first present, non-empty value among `names`."""
    if payload is None:
        return None
    for name in names:
        if name in payload:
            value = payload[name]
            if value not in (None, "", [], {}):
                return value
    return None


def pick_str(payload: Mapping[str, Any] | None, *names: str) -> str | None:
    value = pick(payload, *names)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def pick_int(payload: Mapping[str, Any] | None, *names: str) -> int | None:
    value = pick(payload, *names)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def pick_mapping(payload: Mapping[str, Any] | None, *names: str) -> Mapping[str, Any] | None:
    return as_mapping(pick(payload, *names))


def nested_str(payload: Mapping[str, Any] | None, path: Sequence[str], *names: str) -> str | None:
    """Read a string from a nested mapping path, e.g. `("status", "name")`."""
    current: Mapping[str, Any] | None = payload
    for key in path[:-1]:
        current = pick_mapping(current, key)
        if current is None:
            return None
    return pick_str(current, path[-1], *names)


def parse_datetime(value: Any) -> datetime | None:
    """Parse the timestamp forms `twg` emits: ISO 8601, `Z` suffix, or epoch millis."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def flatten_rich_text(value: Any) -> str:
    """Flatten an Atlassian Document Format body (or plain string) into text.

    ADF nests inline `text` nodes inside block nodes; block boundaries become
    blank lines and list items keep a leading marker so the shape survives in
    search results.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    blocks: list[str] = []
    _walk_adf(value, blocks)
    return "\n\n".join(block for block in (block.strip() for block in blocks) if block)


_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "panel",
        "listItem",
        "tableRow",
        "taskItem",
        "decisionItem",
        "mediaSingle",
    }
)


def _walk_adf(node: Any, blocks: list[str]) -> None:
    if isinstance(node, str):
        if node.strip():
            blocks.append(node)
        return
    if isinstance(node, list):
        for child in node:  # pyright: ignore[reportUnknownVariableType]
            _walk_adf(child, blocks)
        return
    mapping = as_mapping(node)
    if mapping is None:
        return

    node_type = pick_str(mapping, "type") or ""
    if node_type == "text":
        text = mapping.get("text")
        if isinstance(text, str) and text:
            _append_inline(blocks, text)
        return
    if node_type == "mention":
        label = nested_str(mapping, ("attrs", "text")) or nested_str(mapping, ("attrs", "id"))
        if label:
            _append_inline(blocks, label if label.startswith("@") else f"@{label}")
        return
    if node_type == "inlineCard":
        url = nested_str(mapping, ("attrs", "url"))
        if url:
            _append_inline(blocks, url)
        return
    if node_type == "hardBreak":
        _append_inline(blocks, "\n")
        return

    if node_type in _BLOCK_TYPES or node_type in {"bulletList", "orderedList", "table"}:
        blocks.append("")
    _walk_adf(mapping.get("content"), blocks)
    if node_type in _BLOCK_TYPES:
        blocks.append("")


def _append_inline(blocks: list[str], text: str) -> None:
    if blocks and blocks[-1] != "":
        blocks[-1] = f"{blocks[-1]}{text}"
    else:
        blocks.append(text)


def collect_mentions(values: Any) -> list[tuple[str, str | None]]:
    """Return `(account_id, label)` pairs for ADF mention nodes, in document order.

    Mentions identify people by account id, so they become `mentions` edges to a
    Person even when the surrounding text is all that a reader sees.
    """
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    _walk_mentions(values, found, seen)
    return found


def _walk_mentions(node: Any, found: list[tuple[str, str | None]], seen: set[str]) -> None:
    if isinstance(node, list):
        for child in node:  # pyright: ignore[reportUnknownVariableType]
            _walk_mentions(child, found, seen)
        return
    mapping = as_mapping(node)
    if mapping is None:
        return
    if pick_str(mapping, "type") == "mention":
        attrs = pick_mapping(mapping, "attrs")
        account_id = pick_str(attrs, "id", "accountId")
        if account_id is not None and account_id not in seen:
            seen.add(account_id)
            label = pick_str(attrs, "text")
            if label is not None:
                label = label.lstrip("@")
            found.append((account_id, label))
        return
    for value in mapping.values():
        if isinstance(value, (list, dict)):
            _walk_mentions(value, found, seen)


def person_from_payload(
    payload: Any,
    *,
    platform: str,
) -> PersonRecord | None:
    """Map a `twg` user object onto a PersonRecord, or None when unidentifiable."""
    mapping = as_mapping(payload)
    if mapping is None:
        if isinstance(payload, str) and payload.strip():
            return PersonRecord(
                platform=platform,
                platform_user_id=payload.strip(),
            )
        return None

    account_id = pick_str(mapping, "accountId", "account_id", "id", "ari")
    email = pick_str(mapping, "emailAddress", "email")
    display_name = pick_str(mapping, "displayName", "fullName", "name", "publicName", "nickname")
    user_id = account_id or email or display_name
    if user_id is None:
        return None
    return PersonRecord(
        platform=platform,
        platform_user_id=user_id,
        canonical_email=email.lower() if email else None,
        display_name=display_name,
        metadata=_person_metadata(mapping),
    )


def _person_metadata(mapping: Mapping[str, Any]) -> dict[str, MetadataValue]:
    profile = pick_mapping(mapping, "extendedProfile") or {}
    metadata: dict[str, MetadataValue] = {
        "job_title": pick_str(profile, "jobTitle"),
        "department": pick_str(profile, "department"),
        "account_ari": pick_str(mapping, "userAri", "ari"),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def person_canonical_id(person: PersonRecord) -> str:
    """Return the identifier core uses as a Person's `platform_entity_id`."""
    return person.canonical_email or f"{person.platform}:{person.platform_user_id}"


def clean_metadata(values: Mapping[str, Any]) -> dict[str, MetadataValue]:
    """Drop empty values and coerce the rest to metadata-safe scalars."""
    cleaned: dict[str, MetadataValue] = {}
    for key, value in values.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, (str, bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple, set)):
            cleaned[key] = ", ".join(
                str(item) for item in value if item not in (None, "")  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            ) or None
        elif isinstance(value, datetime):
            cleaned[key] = value.isoformat()
        else:
            cleaned[key] = str(value)
    return {key: value for key, value in cleaned.items() if value not in (None, "")}
