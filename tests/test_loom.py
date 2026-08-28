"""Loom video and transcript mapping."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import classify_atlassian_urls, video_payload

from agentgraph_connector_twg import loom, urls
from agentgraph_connector_twg.stubs import stub_for_url


def _target() -> urls.TwgTarget:
    target = urls.parse_url("https://www.loom.com/share/abc123def456")
    assert target is not None
    return target


@pytest.mark.parametrize(
    "payload",
    [
        [{"text": "Hello there."}, {"text": "This is the retry path."}],
        {"phrases": [{"text": "Hello there."}, {"text": "This is the retry path."}]},
        {"transcript": {"phrases": [{"value": "Hello there."}, {"value": "This is the retry path."}]}},
        {"data": {"segments": [{"phrase": "Hello there."}, {"phrase": "This is the retry path."}]}},
        ["Hello there.", "This is the retry path."],
    ],
)
def test_flatten_transcript_handles_known_shapes(payload: Any) -> None:
    transcript = loom.flatten_transcript(payload)

    assert transcript.text == "Hello there. This is the retry path."
    assert transcript.phrase_count == 2
    assert transcript.complete is True
    assert transcript.available is True


def test_flatten_transcript_preserves_phrase_order_with_timestamps() -> None:
    payload = [
        {"startTime": 0.0, "text": "First."},
        {"startTime": 1.5, "text": "Second."},
        {"startTime": 3.0, "text": "Third."},
    ]

    assert loom.flatten_transcript(payload).text == "First. Second. Third."


def test_flatten_transcript_marks_previews_incomplete() -> None:
    transcript = loom.flatten_transcript([{"text": "Partial."}], complete=False)

    assert transcript.complete is False
    assert transcript.available is True


def test_flatten_transcript_of_empty_payloads() -> None:
    empty: list[Any] = [None, [], {}, {"phrases": []}]
    for payload in empty:
        transcript = loom.flatten_transcript(payload)
        assert transcript.available is False
        assert transcript.phrase_count == 0


def test_video_maps_to_video_entity_with_transcript_content() -> None:
    transcript = loom.flatten_transcript([{"text": "Atlas sync retries every fifteen minutes."}])

    batch = loom.video_to_batch(video_payload(), target=_target(), transcript=transcript)

    video = next(entity for entity in batch.entities if entity.entity_type == "Video")
    assert video.platform == "twg"
    assert video.platform_entity_id == "loom/abc123def456"
    assert video.title == "Atlas sync walkthrough"
    assert video.content is not None
    assert "Transcript:\nAtlas sync retries every fifteen minutes." in video.content
    assert "Five minute tour of the retry path." in video.content
    assert video.metadata["web_url"] == "https://www.loom.com/share/abc123def456"
    assert video.metadata["duration_seconds"] == 312
    assert video.metadata["duration_label"] == "5m12s"
    assert video.metadata["view_count"] == 34
    assert video.metadata["transcript_available"] is True
    assert video.metadata["transcript_complete"] is True
    assert video.metadata["transcript_phrase_count"] == 1
    assert video.source_created_at is not None


def test_video_url_creates_video_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)

    stub = stub_for_url("https://www.loom.com/share/abc123def456")

    assert stub is not None
    assert stub.entity_type == "Video"
    assert stub.platform_entity_id == "loom/abc123def456"


def test_video_uses_target_id_when_payload_id_is_an_ari(monkeypatch: pytest.MonkeyPatch) -> None:
    classify_atlassian_urls(monkeypatch)
    raw_ari = "ari:cloud:loom:cloud-1:video/activation/recording-1/abc123def456"

    batch = loom.video_to_batch(video_payload(id=raw_ari), target=_target())

    videos = [entity for entity in batch.entities if entity.entity_type == "Video"]
    assert [(video.platform_entity_id, video.is_stub) for video in videos] == [
        ("loom/abc123def456", False)
    ]
    assert videos[0].metadata["video_id"] == "abc123def456"
    assert not any(edge.edge_type == "references" for edge in batch.edges)


def test_video_without_transcript_keeps_its_watch_url_in_metadata() -> None:
    batch = loom.video_to_batch(video_payload(), target=_target())

    video = next(entity for entity in batch.entities if entity.entity_type == "Video")
    assert video.metadata["transcript_available"] is False
    assert video.metadata["web_url"] == "https://www.loom.com/share/abc123def456"
    assert video.content is not None
    assert "Transcript" not in video.content
    assert "https://www.loom.com/share/abc123def456" not in video.content


def test_preview_transcript_is_labelled_in_content() -> None:
    transcript = loom.flatten_transcript([{"text": "Only the opening."}], complete=False)

    batch = loom.video_to_batch(video_payload(), target=_target(), transcript=transcript)
    video = next(entity for entity in batch.entities if entity.entity_type == "Video")

    assert video.content is not None
    assert "Transcript (preview):" in video.content


def test_video_maps_owner_space_and_commenters() -> None:
    comments = [
        {"author": {"accountId": "acct-dev", "displayName": "Dev Patel"}, "text": "Useful, thanks."},
        {"author": {"accountId": "acct-dev", "displayName": "Dev Patel"}, "text": "Duplicate author."},
    ]

    batch = loom.video_to_batch(video_payload(), target=_target(), comments=comments)

    assert {person.platform_user_id for person in batch.persons} == {"acct-maya", "acct-dev"}
    assert any(
        edge.edge_type == "authored"
        and edge.source_platform_user_id == "acct-maya"
        and edge.properties.get("role") == "owner"
        for edge in batch.edges
    )
    commenter_edges = [
        edge
        for edge in batch.edges
        if edge.edge_type == "participated_in" and edge.properties.get("role") == "commenter"
    ]
    assert len(commenter_edges) == 1

    folder = next(entity for entity in batch.entities if entity.entity_type == "Folder")
    assert folder.platform_entity_id == "loom/space/space-9"
    assert folder.title == "Engineering recordings"


def test_video_falls_back_to_target_url_and_id() -> None:
    payload = {"name": "Untitled recording"}

    batch = loom.video_to_batch(payload, target=_target())
    video = next(entity for entity in batch.entities if entity.entity_type == "Video")

    assert video.platform_entity_id == "loom/abc123def456"
    assert video.metadata["web_url"] == "https://www.loom.com/share/abc123def456"


def test_long_durations_use_hours_in_the_label() -> None:
    batch = loom.video_to_batch(video_payload(duration=3725), target=_target())
    video = next(entity for entity in batch.entities if entity.entity_type == "Video")

    assert video.metadata["duration_label"] == "1h02m05s"
