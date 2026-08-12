"""Provenance and the transcript layer.

Every message and state transition must be able to answer: who sent it, under
which identity, when, in which room, for which task, replying to what, whether
it was targeted, whether a human overrode it, whether it caused a handoff, and
which artifacts it referenced.
"""

from __future__ import annotations

import json

import pytest

from aidapter.errors import NotFoundError, ValidationError
from aidapter.models.envelope import MAX_CONTENT_CHARS, MessageDraft


def test_envelope_carries_every_provenance_field(room):
    first = room.send("claude", "opening statement")["message"]
    result = room.send(
        "claude",
        "Adversarially review commit abc123 for race conditions.",
        target="codex",
        message_type="task",
        goal="find race conditions before merge",
        request_type="review",
        confidence=0.8,
        claim="the retry path is racy",
        evidence="two interleavings in the log",
        artifact_references=["git:abc123", "file:retry.py#L40"],
        constraints=["Preserve the existing runtime contract"],
        in_reply_to=first["message_id"],
        metadata={"origin": "unit-test"},
    )
    message = result["message"]

    assert message["sender"] == "claude"
    assert message["sender_participant_id"].startswith("part_")
    assert message["target"] == "codex"
    assert message["target_participant_id"].startswith("part_")
    assert message["room_id"] == room.room_id
    assert message["timestamp"].endswith("Z")
    assert message["seq"] > first["seq"]
    assert message["task_id"].startswith("task_")
    assert message["in_reply_to"] == first["message_id"]
    assert message["turn_id"].startswith("turn_")
    assert message["was_targeted"] is True
    assert message["human_override"] is False
    assert message["resulted_in_handoff"] is False
    assert message["artifact_references"] == ["git:abc123", "file:retry.py#L40"]
    assert message["constraints"] == ["Preserve the existing runtime contract"]
    assert message["claim"] == "the retry path is racy"
    assert message["evidence"] == "two interleavings in the log"
    assert message["request_type"] == "review"
    assert message["confidence"] == 0.8
    assert message["goal"] == "find race conditions before merge"
    assert message["metadata"] == {"origin": "unit-test"}


def test_provenance_query_resolves_the_whole_chain(room):
    request = room.send(
        "claude", "codex, review", target="codex", message_type="task"
    )["message"]
    response = room.send(
        "codex",
        "found one",
        message_type="response",
        response_status="complete",
        task_id=request["task_id"],
        in_reply_to=request["message_id"],
    )["message"]

    record = room.broker.provenance(
        room.room_id, response["message_id"], credential=room.credential("human")
    )
    assert record["message"]["sender"] == "codex"
    assert record["in_reply_to"]["message_id"] == request["message_id"]
    assert record["turn"]["participant_name"] == "codex"
    assert record["turn"]["request_message_id"] == request["message_id"]
    assert record["task"]["created_by"] == "claude"
    assert record["task"]["assigned_to"] == "codex"
    assert record["task"]["status"] == "completed"


def test_handoff_provenance_is_recorded_on_the_message_and_the_turn(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    message = room.send(
        "codex", "done, gemini next", message_type="response", handoff_target="gemini"
    )["message"]

    assert message["resulted_in_handoff"] is True
    assert message["handoff_target"] == "gemini"
    record = room.broker.provenance(
        room.room_id, message["message_id"], credential=room.credential("human")
    )
    assert record["turn"]["handoff_target"] == "gemini"
    assert record["turn"]["status"] == "completed"


def test_state_transitions_are_audited_with_an_actor(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("human", "wait", target="claude")

    types = [e["event_type"] for e in room.events()]
    for expected in (
        "room.created",
        "participant.joined",
        "message.sent",
        "queue.promoted",
        "turn.started",
        "turn.interrupted",
        "human.interrupt",
        "task.opened",
    ):
        assert expected in types, f"missing audit event {expected}"

    for event in room.events():
        assert event["room_id"] == room.room_id
        assert event["created_at"].endswith("Z")
        assert isinstance(event["seq"], int)


def test_sequence_numbers_are_monotonic_across_messages_and_events(room):
    room.send("claude", "one")
    room.send("codex", "two")
    room.send("human", "three")

    seqs = [m["seq"] for m in room.messages()] + [e["seq"] for e in room.events()]
    assert len(seqs) == len(set(seqs)), "message and event sequence numbers must not collide"

    message_seqs = [m["seq"] for m in room.messages()]
    assert message_seqs == sorted(message_seqs)
    event_seqs = [e["seq"] for e in room.events()]
    assert event_seqs == sorted(event_seqs)


def test_transcript_jsonl_mirrors_the_database(room, workspace):
    room.send("claude", "first")
    room.send("codex", "second")

    path = workspace.transcript_path(room.room_id)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [line["content"] for line in lines] == ["first", "second"]
    assert lines[0]["sender_participant_id"].startswith("part_")
    assert path.stat().st_mode & 0o777 == 0o600


def test_export_rebuilds_the_transcript_from_authoritative_state(room, workspace):
    room.send("claude", "first")
    path = workspace.transcript_path(room.room_id)
    path.write_text("corrupted\n", encoding="utf-8")

    result = room.broker.export(room.room_id, credential=room.credential("human"))
    assert result["message_count"] == 1
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [line["content"] for line in lines] == ["first"]


def test_transcript_is_independent_of_the_memory_ledger(room, workspace):
    room.send("claude", "chat line")
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("claude"),
        section="decisions",
        value="ledger line",
        mode="add",
    )
    transcript_text = workspace.transcript_path(room.room_id).read_text(encoding="utf-8")
    memory_text = workspace.memory_path(room.room_id).read_text(encoding="utf-8")
    assert "chat line" in transcript_text and "chat line" not in memory_text
    assert "ledger line" in memory_text and "ledger line" not in transcript_text


def test_pass_messages_are_part_of_the_transcript(room):
    room.send("claude", "codex?", target="codex", message_type="task")
    room.pass_turn("codex", reason="nothing to add")
    assert [(m["sender"], m["message_type"]) for m in room.messages()] == [
        ("claude", "task"),
        ("codex", "pass"),
    ]


def test_unknown_message_id_is_not_found(room):
    with pytest.raises(NotFoundError):
        room.broker.provenance(
            room.room_id, "msg_" + "a" * 20, credential=room.credential("human")
        )


def test_reply_to_must_reference_a_message_in_this_room(room):
    with pytest.raises(NotFoundError):
        room.send("claude", "replying to nothing", in_reply_to="msg_" + "b" * 20)


# ----------------------------------------------------------------------
# envelope validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": ""},
        {"content": "x", "message_type": "not-a-type"},
        {"content": "x", "confidence": 1.5},
        {"content": "x", "confidence": "high"},
        {"content": "x", "response_status": "maybe"},
        {"content": "x", "task_id": "not-a-task-id"},
        {"content": "x", "in_reply_to": "nope"},
        {"content": "x", "artifact_references": [42]},
        {"content": "x", "metadata": ["not", "an", "object"]},
        {"content": "x", "target": "codex", "handoff_target": "gemini"},
        {"content": "x" * (MAX_CONTENT_CHARS + 1)},
    ],
)
def test_malformed_envelopes_are_rejected_before_touching_state(room, kwargs):
    with pytest.raises(ValidationError):
        MessageDraft(**kwargs).validate()
    assert room.messages() == [], "a rejected draft must not reach the transcript"


def test_valid_envelope_normalizes_lists(room):
    draft = MessageDraft(
        content="x", artifact_references=["  git:abc  ", ""], constraints="single"
    ).validate()
    assert draft.artifact_references == ["git:abc"]
    assert draft.constraints == ["single"]
