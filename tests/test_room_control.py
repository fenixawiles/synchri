"""Human control of the room: pause, resume, and hard stop."""

from __future__ import annotations

import pytest

from aidapter.errors import AuthError, StateError
from aidapter.models.envelope import MessageDraft

from helpers import make_room


def test_pause_blocks_agents_but_not_the_human(room):
    room.broker.pause_room(room.room_id, credential=room.credential("human"))

    with pytest.raises(StateError) as exc:
        room.send("claude", "still going")
    assert exc.value.code == "room_paused"
    assert room.state("claude") == "paused"

    assert room.send("human", "thinking about it")["message"]["sender"] == "human"


def test_resume_restores_agent_messaging(room):
    room.broker.pause_room(room.room_id, credential=room.credential("human"))
    room.broker.resume_room(room.room_id, credential=room.credential("human"))
    assert room.send("claude", "back to work")["message"]["sender"] == "claude"


def test_pause_preserves_the_active_turn(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.broker.pause_room(room.room_id, credential=room.credential("human"))
    assert room.active_speaker() == "codex"

    room.broker.resume_room(room.room_id, credential=room.credential("human"))
    assert room.active_speaker() == "codex"
    assert room.send("codex", "findings", message_type="response")["message"]["sender"] == "codex"


def test_a_paused_room_queues_but_does_not_promote(room):
    """The human can still line up the next speaker while the room is held."""
    room.broker.pause_room(room.room_id, credential=room.credential("human"))
    room.send("human", "codex, pick this up when we resume", target="codex")

    assert room.active_speaker() is None, "a paused room must not start a turn"
    assert room.queue_names() == ["codex"]

    room.broker.resume_room(room.room_id, credential=room.credential("human"))
    assert room.active_speaker() == "codex"


def test_only_a_human_may_pause(room):
    with pytest.raises(AuthError):
        room.broker.pause_room(room.room_id, credential=room.credential("claude"))


def test_stop_is_authoritative_for_every_participant(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))

    for actor in ("claude", "codex", "human"):
        with pytest.raises(StateError) as exc:
            room.send(actor, "anyone there?")
        assert exc.value.code == "room_stopped"


def test_stop_clears_the_queue_and_the_floor(room):
    room.send("claude", "starting", hold_floor=True)
    room.queue_up("codex")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))

    status = room.status()
    assert status["room"]["status"] == "stopped"
    assert status["active_speaker"] is None
    assert status["queue"] == []
    assert status["room"]["stopped_at"] is not None


def test_stop_cancels_open_tasks(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    assert room.status()["open_tasks"]
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    assert room.status()["open_tasks"] == []


def test_stopped_room_is_still_readable(room):
    room.send("claude", "a durable thought")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    assert [m["content"] for m in room.messages()] == ["a durable thought"]


def test_a_stopped_room_cannot_be_resumed(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    with pytest.raises(StateError):
        room.broker.resume_room(room.room_id, credential=room.credential("human"))


def test_stop_is_idempotent(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    status = room.broker.stop_room(room.room_id, credential=room.credential("human"))
    assert status["room"]["status"] == "stopped"


def test_stop_survives_a_restart(workspace):
    from aidapter.broker import Broker

    with Broker(workspace) as first:
        harness = make_room(first, "claude")
        first.stop_room(harness.room_id, credential=harness.credential("human"))
        room_id, claude = harness.room_id, harness.credential("claude")

    with Broker(workspace) as second:
        with pytest.raises(StateError):
            second.send(room_id, credential=claude, draft=MessageDraft(content="hello?"))


def test_stopped_room_reports_stopped_to_a_waiting_agent(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    assert room.state("codex") == "stopped"


def test_memory_is_frozen_after_stop(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    with pytest.raises(StateError):
        room.broker.memory_write(
            room.room_id,
            credential=room.credential("human"),
            section="decisions",
            value="too late",
        )
