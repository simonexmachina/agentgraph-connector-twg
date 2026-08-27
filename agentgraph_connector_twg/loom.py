"""Map `twg loom` payloads onto Video entities.

A Loom is only useful in a graph if its words are searchable, so the transcript
becomes the entity's content and `metadata.web_url` carries the watch link.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
    flatten_rich_text,
    nested_str,
    parse_datetime,
    person_from_payload,
    pick,
    pick_int,
    pick_mapping,
    pick_str,
)

_TRANSCRIPT_TEXT_KEYS = ("text", "phrase", "value", "content", "transcription")
_TRANSCRIPT_CONTAINER_KEYS = (
    "phrases",
    "transcript",
    "transcriptPhrases",
    "segments",
    "sentences",
    "captions",
    "items",
    "data",
)


@dataclass(frozen=True)
class Transcript:
    """A flattened Loom transcript."""

    text: str
    phrase_count: int
    complete: bool
    """False when only a bounded preview was available."""

    @property
    def available(self) -> bool:
        return bool(self.text)


EMPTY_TRANSCRIPT = Transcript(text="", phrase_count=0, complete=False)


def flatten_transcript(payload: Any, *, complete: bool = True) -> Transcript:
    """Flatten a Loom transcript payload into ordered text.

    `twg` exposes transcripts as phrase lists whose exact nesting differs between
    the preview and full forms, so every string under a recognised text key is
    collected in document order.
    """
    phrases: list[str] = []
    _walk_transcript(payload, phrases)
    text = " ".join(phrase for phrase in (phrase.strip() for phrase in phrases) if phrase)
    return Transcript(text=text, phrase_count=len(phrases), complete=complete and bool(text))


def _walk_transcript(node: Any, phrases: list[str]) -> None:
    if isinstance(node, str):
        if node.strip():
            phrases.append(node)
        return
    if isinstance(node, list):
        for child in node:  # pyright: ignore[reportUnknownVariableType]
            _walk_transcript(child, phrases)
        return
    mapping = as_mapping(node)
    if mapping is None:
        return
    for key in _TRANSCRIPT_TEXT_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            phrases.append(value)
            return
    for key in _TRANSCRIPT_CONTAINER_KEYS:
        if key in mapping:
            _walk_transcript(mapping[key], phrases)
            return
    for value in mapping.values():
        if isinstance(value, (list, dict)):
            _walk_transcript(value, phrases)


def video_to_batch(
    payload: Mapping[str, Any],
    *,
    target: urls.TwgTarget,
    transcript: Transcript = EMPTY_TRANSCRIPT,
    comments: list[Any] | None = None,
) -> EntityBatch:
    """Build the Video entity, its owner, commenters, and space edge."""
    video_id = pick_str(payload, "id", "videoId") or target.key or ""
    entity_id = urls.video_entity_id(video_id) if video_id else target.entity_id
    title = pick_str(payload, "name", "title") or f"Loom video {video_id}"
    description = flatten_rich_text(pick(payload, "description", "summary"))
    web_url = (
        pick_str(payload, "url", "webUrl", "shareUrl", "sharedUrl")
        or target.web_url
        or (urls.video_web_url(video_id) if video_id else None)
    )

    batch = EntityBatch()
    edges: list[EdgeRecord] = []
    persons: dict[str, PersonRecord] = {}

    entity = EntityRecord(
        entity_type="Video",
        platform=urls.SOURCE,
        platform_entity_id=entity_id,
        title=title,
        content="\n\n".join(
            section
            for section in (
                title,
                description,
                _transcript_section(transcript),
                web_url,
            )
            if section
        ),
        source_created_at=parse_datetime(
            pick(payload, "createdAt", "recordedAt", "created", "recordedDate")
        ),
        source_updated_at=parse_datetime(pick(payload, "updatedAt", "modifiedAt", "updated")),
        metadata=_metadata(
            payload=payload,
            video_id=video_id,
            web_url=web_url,
            transcript=transcript,
        ),
    )
    batch.entities.append(entity)

    owner = person_from_payload(
        pick(payload, "owner", "author", "createdBy", "user"),
        platform=urls.SOURCE,
    )
    if owner is not None:
        persons[owner.platform_user_id] = owner
        edges.append(
            EdgeRecord(
                edge_type="authored",
                source_platform_user_id=owner.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": "owner"},
            )
        )

    for raw_comment in comments or []:
        comment = as_mapping(raw_comment)
        if comment is None:
            continue
        author = person_from_payload(
            pick(comment, "author", "user", "createdBy"),
            platform=urls.SOURCE,
        )
        if author is None or author.platform_user_id in persons:
            continue
        persons[author.platform_user_id] = author
        edges.append(
            EdgeRecord(
                edge_type="participated_in",
                source_platform_user_id=author.platform_user_id,
                target_platform_entity_id=entity_id,
                platform=urls.SOURCE,
                properties={"role": "commenter"},
            )
        )

    space = pick_mapping(payload, "space", "folder")
    space_id = pick_str(space, "id")
    if space_id is not None:
        folder_id = f"loom/space/{space_id}"
        batch.entities.append(
            EntityRecord(
                entity_type="Folder",
                platform=urls.SOURCE,
                platform_entity_id=folder_id,
                title=pick_str(space, "name") or f"Loom space {space_id}",
                content=f"Loom space {pick_str(space, 'name') or space_id}",
                metadata=clean_metadata(
                    {"space_id": space_id, "web_url": pick_str(space, "url", "webUrl")}
                ),
            )
        )
        edges.append(
            EdgeRecord(
                edge_type="posted_in",
                source_platform_entity_id=entity_id,
                target_platform_entity_id=folder_id,
                platform=urls.SOURCE,
            )
        )

    batch.persons.extend(persons.values())
    batch.edges.extend(edges)
    batch.add_stubs_from(entity)
    return batch


def _transcript_section(transcript: Transcript) -> str | None:
    if not transcript.available:
        return None
    label = "Transcript" if transcript.complete else "Transcript (preview)"
    return f"{label}:\n{transcript.text}"


def _metadata(
    *,
    payload: Mapping[str, Any],
    video_id: str,
    web_url: str | None,
    transcript: Transcript,
) -> dict[str, MetadataValue]:
    duration = pick_int(payload, "duration", "durationSeconds", "videoDuration")
    return clean_metadata(
        {
            "video_id": video_id,
            "web_url": web_url,
            "ari": pick_str(payload, "ari"),
            "owner": nested_str(payload, ("owner", "displayName")) or nested_str(payload, ("owner", "name")),
            "duration_seconds": duration,
            "duration_label": _duration_label(duration),
            "view_count": pick_int(payload, "viewCount", "views", "totalViews"),
            "privacy": pick_str(payload, "privacy", "visibility"),
            "recorded_at": pick_str(payload, "recordedAt", "recordedDate", "createdAt"),
            "transcript_available": transcript.available,
            "transcript_complete": transcript.complete,
            "transcript_phrase_count": transcript.phrase_count,
            "folders": [
                name
                for name in (
                    pick_str(as_mapping(folder), "name") for folder in as_sequence(pick(payload, "folders"))
                )
                if name
            ],
        }
    )


def _duration_label(duration_seconds: int | None) -> str | None:
    if duration_seconds is None or duration_seconds <= 0:
        return None
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"
