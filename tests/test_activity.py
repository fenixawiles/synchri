"""Live work notes are visible, but never become room conversation."""

from __future__ import annotations

import pytest

from synchri.errors import TurnError


def test_live_work_note_is_separate_from_transcript_and_turns(room):
    room.send("claude", "Please inspect the startup flow.", target="codex", message_type="task")
    before = room.messages()

    published = room.broker.publish_activity(
        room.room_id,
        credential=room.credential("codex"),
        summary="Tracing the startup flow and its persisted state.",
    )

    assert published["activity"]["name"] == "codex"
    assert room.active_speaker() == "codex"
    assert room.messages() == before
    assert room.status()["activities"] == [
        {
            "participant_id": published["activity"]["participant_id"],
            "name": "codex",
            "summary": "Tracing the startup flow and its persisted state.",
            "updated_at": published["activity"]["updated_at"],
            "expires_at": published["activity"]["expires_at"],
        }
    ]

    room.send("codex", "The state is durable.", message_type="response", response_status="complete")
    assert room.status()["activities"] == []


def test_only_the_agent_with_the_floor_can_publish_live_work(room):
    room.send("claude", "Review this.", target="codex", message_type="task")

    with pytest.raises(TurnError) as exc:
        room.broker.publish_activity(
            room.room_id,
            credential=room.credential("claude"),
            summary="Trying to jump in.",
        )

    assert exc.value.code == "not_your_turn"


def test_human_redirect_clears_an_interrupted_agent_work_note(room):
    room.send("claude", "Start the implementation.", hold_floor=True)
    room.broker.publish_activity(
        room.room_id,
        credential=room.credential("claude"),
        summary="Inspecting the existing implementation.",
    )

    room.send("human", "Please review the plan first.", target="codex", message_type="interrupt")

    assert room.active_speaker() == "codex"
    assert room.status()["activities"] == []
