"""Queue semantics — the core invariants of the whole system.

Priority order under test:

    1 human intervention
    2 explicitly addressed participant
    3 explicit model-to-model handoff target
    4 existing queued participants
    5 optional participants that have not yet responded
"""

from __future__ import annotations

import pytest

from synchri.errors import StateError, TurnError
from synchri.models.envelope import MessageDraft

from helpers import make_room


# ----------------------------------------------------------------------
# ordinary messaging
# ----------------------------------------------------------------------


def test_untargeted_message_when_room_is_idle_is_allowed(room):
    result = room.send("claude", "starting work on the retry path")
    assert result["message"]["content"] == "starting work on the retry path"
    assert result["message"]["was_targeted"] is False
    assert room.active_speaker() is None, "an untargeted message releases the floor"


def test_message_is_visible_to_every_participant(room):
    room.send("claude", "hello room")
    for reader in ("human", "claude", "codex"):
        messages = room.broker.read(room.room_id, credential=room.credential(reader))["messages"]
        assert [m["content"] for m in messages] == ["hello room"]


# ----------------------------------------------------------------------
# direct address, blocking turns
# ----------------------------------------------------------------------


def test_direct_address_promotes_the_target_to_the_head(room):
    room.add_agent("gemini")
    # claude takes the floor and holds it, so gemini can only queue behind it.
    room.send("claude", "starting the review", hold_floor=True)
    room.queue_up("gemini")
    assert room.queue_names() == ["gemini"]

    room.send("claude", "codex, take a look", target="codex", message_type="task")

    # codex was addressed after gemini queued, but direct address outranks it.
    assert room.active_speaker() == "codex"
    assert room.queue_names() == ["gemini"]


def test_targeted_turn_is_blocking_for_other_agents(room):
    room.add_agent("gemini")
    room.send("claude", "review commit abc123", target="codex", message_type="task")

    for bystander in ("claude", "gemini"):
        with pytest.raises(TurnError) as exc:
            room.send(bystander, "let me jump in")
        assert exc.value.code == "blocked_targeted_turn"

    assert room.state("gemini") == "blocked"
    assert room.state("codex") == "your_turn"


def test_blocking_turn_reports_the_originating_request(room):
    room.send(
        "claude",
        "Adversarially review commit abc123 for race conditions.",
        target="codex",
        message_type="task",
        artifact_references=["git:abc123"],
        constraints=["Preserve the existing runtime contract"],
    )
    status = room.turn("codex")
    assert status["state"] == "your_turn"
    assert status["blocking"] is True
    assert status["request"]["content"].startswith("Adversarially review")
    assert status["request"]["artifact_references"] == ["git:abc123"]
    assert status["request"]["constraints"] == ["Preserve the existing runtime contract"]
    assert status["task_id"] == status["request"]["task_id"]


def test_queue_resumes_normal_order_after_an_untargeted_response(room):
    room.add_agent("gemini")
    room.send("claude", "starting", hold_floor=True)
    room.queue_up("gemini")

    room.send("claude", "codex, review this", target="codex", message_type="task")
    assert room.active_speaker() == "codex"

    # codex answers without addressing anyone: the pre-existing queue resumes.
    room.send("codex", "no issues found", message_type="response", response_status="complete")
    assert room.active_speaker() == "gemini"
    assert room.queue_names() == []


def test_target_must_exist_in_this_room(room):
    with pytest.raises(Exception):
        room.send("claude", "hi", target="nobody")


# ----------------------------------------------------------------------
# handoffs
# ----------------------------------------------------------------------


def test_handoff_reprioritizes_the_named_participant(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review this", target="codex", message_type="task")

    room.send(
        "codex",
        "Reviewed. Gemini should check the docs.",
        message_type="response",
        response_status="complete",
        handoff_target="gemini",
    )

    assert room.active_speaker() == "gemini"
    handoff_message = room.messages()[-1]
    assert handoff_message["handoff_target"] == "gemini"
    assert handoff_message["resulted_in_handoff"] is True


def test_direct_address_outranks_a_handoff(room):
    room.add_agent("gemini")
    room.add_agent("qwen")

    # codex holds the floor and hands off to gemini...
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("codex", "done, gemini next", handoff_target="gemini", message_type="response")
    assert room.active_speaker() == "gemini"

    # ...while gemini speaks, it directly addresses qwen. Direct address is a
    # stronger claim on the queue than the pending handoff bucket.
    room.send("gemini", "qwen, your turn on the tests", target="qwen", message_type="task")
    assert room.active_speaker() == "qwen"


def test_handoff_is_recorded_in_the_memory_ledger(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("codex", "done, over to gemini", handoff_target="gemini", message_type="response")

    memory = room.broker.memory_show(room.room_id, credential=room.credential("human"))
    handoffs = memory["memory"]["recent_handoffs"]
    assert len(handoffs) == 1
    assert "codex → gemini" in handoffs[0]


def test_a_message_cannot_both_target_and_hand_off(room):
    with pytest.raises(Exception):
        room.send("claude", "both", target="codex", handoff_target="codex")


# ----------------------------------------------------------------------
# pass
# ----------------------------------------------------------------------


def test_pass_releases_a_blocking_turn_and_resumes_the_queue(room):
    room.add_agent("gemini")
    room.send("claude", "starting", hold_floor=True)
    room.queue_up("gemini")

    room.send("claude", "codex, anything to add?", target="codex", message_type="task")
    result = room.pass_turn("codex", reason="nothing material to add")

    assert result["held_floor"] is True
    assert room.active_speaker() == "gemini"
    assert room.messages()[-1]["message_type"] == "pass"
    assert room.messages()[-1]["content"] == "nothing material to add"


def test_pass_is_recorded_as_an_attributed_message(room):
    room.send("claude", "codex?", target="codex", message_type="task")
    room.pass_turn("codex")
    passed = room.messages()[-1]
    assert passed["sender"] == "codex"
    assert passed["message_type"] == "pass"
    assert passed["response_status"] == "declined"


def test_human_direction_forces_reviewer_for_an_external_agent(room):
    room.send(
        "human",
        "Prioritize auth.",
        target="claude",
        metadata={"human_direction": {"lead": "claude", "reviewer": "codex"}},
    )

    result = room.send(
        "claude",
        "I will handle auth first.",
        target="human",
        message_type="task",
    )

    assert result["required_reviewer"] == "codex"
    assert result["next_speaker"] == "codex"
    response = room.messages()[-1]
    assert response["target"] is None
    assert response["handoff_target"] == "codex"
    assert response["metadata"]["human_direction_review"] == {"reviewer": "codex"}


def test_pass_gives_up_a_queued_slot_without_the_floor(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.broker.request_floor(room.room_id, credential=room.credential("gemini"))
    assert room.queue_names() == ["gemini"]

    result = room.pass_turn("gemini", reason="not my area")
    assert result["held_floor"] is False
    assert room.queue_names() == []
    assert room.active_speaker() == "codex", "the blocking turn is untouched"


def test_pass_with_nothing_to_pass_is_an_error(room):
    with pytest.raises(StateError) as exc:
        room.pass_turn("codex")
    assert exc.value.code == "nothing_to_pass"


def test_pass_closes_the_associated_task(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    task_id = room.status()["open_tasks"][0]["task_id"]
    room.pass_turn("codex")
    assert room.status()["open_tasks"] == []
    task = room.broker.provenance(
        room.room_id, room.messages()[-1]["message_id"], credential=room.credential("human")
    )
    assert task["message"]["task_id"] == task_id


# ----------------------------------------------------------------------
# human priority
# ----------------------------------------------------------------------


def test_human_can_speak_while_an_agent_holds_a_blocking_turn(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    result = room.send("human", "hold on, look at the retry path first")
    assert result["message"]["sender"] == "human"
    assert result["interrupted"] is not None
    assert result["interrupted"]["participant_name"] == "codex"


def test_untargeted_human_comment_lets_the_interrupted_agent_resume(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("human", "one more thing to keep in mind")
    assert room.active_speaker() == "codex", "codex resumes after a human comment"


def test_targeted_human_message_redirects_and_clears_the_queue(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.broker.request_floor(room.room_id, credential=room.credential("gemini"))
    assert room.queue_names() == ["gemini"]

    result = room.send("human", "forget that, claude do this instead", target="claude")
    assert result["cleared_queue_entries"] >= 1
    assert room.active_speaker() == "claude"
    assert room.queue_names() == []


def test_human_message_marks_provenance_of_the_override(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("human", "stop and check the retry path", target="codex")
    human_message = room.messages()[-1]
    assert human_message["human_override"] is True
    assert human_message["was_targeted"] is True


def test_human_interrupt_resets_the_autonomy_budget(room):
    harness = room
    harness.send("claude", "one")
    harness.send("codex", "two")
    assert harness.status()["autonomy"]["consecutive_agent_turns"] == 2

    harness.send("human", "carry on")
    assert harness.status()["autonomy"]["consecutive_agent_turns"] == 0


def test_interrupted_turn_is_recorded_as_interrupted(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.send("human", "actually, wait", target="claude")

    turns = room.broker.conn.execute(
        "SELECT status FROM turns WHERE room_id = ? ORDER BY started_at", (room.room_id,)
    ).fetchall()
    assert "interrupted" in [t["status"] for t in turns]


def test_human_always_reports_your_turn(room):
    room.send("claude", "codex, review", target="codex", message_type="task")
    assert room.state("human") == "your_turn"


# ----------------------------------------------------------------------
# autonomy limit
# ----------------------------------------------------------------------


def test_agents_stop_after_the_maximum_consecutive_turns(broker):
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=3)

    harness.send("claude", "one")
    harness.send("codex", "two")
    result = harness.send("claude", "three")
    assert result["awaiting_human"] is True

    with pytest.raises(StateError) as exc:
        harness.send("codex", "four")
    assert exc.value.code == "awaiting_human"
    assert harness.state("codex") == "awaiting_human"


def test_ping_pong_between_agents_terminates(broker):
    """Two agents addressing each other must not loop forever."""
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=4)

    speakers = ["claude", "codex"]
    sent = 0
    for index in range(20):
        speaker = speakers[index % 2]
        other = speakers[(index + 1) % 2]
        try:
            harness.send(speaker, f"turn {index}", target=other, message_type="task")
            sent += 1
        except StateError as exc:
            assert exc.code == "awaiting_human"
            break
    else:  # pragma: no cover - the loop must break
        pytest.fail("agent ping-pong never yielded the room back to the human")

    assert sent == 4
    assert harness.status()["autonomy"]["awaiting_human"] is True


def test_passes_count_against_the_autonomy_budget(broker):
    """Otherwise two agents could pass at each other indefinitely."""
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=2)
    harness.send("claude", "codex?", target="codex", message_type="task")
    harness.pass_turn("codex")
    assert harness.status()["autonomy"]["awaiting_human"] is True


def test_human_unblocks_an_exhausted_room(broker):
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=1)
    harness.send("claude", "one")
    assert harness.status()["autonomy"]["awaiting_human"] is True

    harness.send("human", "keep going")
    assert harness.status()["autonomy"]["awaiting_human"] is False
    assert harness.send("codex", "two")["message"]["sender"] == "codex"


def test_autonomy_limit_is_configurable_at_runtime(broker):
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=1)
    harness.send("claude", "one")
    assert harness.status()["autonomy"]["awaiting_human"] is True

    harness.broker.set_room_config(
        harness.room_id, credential=harness.credential("human"), max_consecutive_agent_turns=5
    )
    assert harness.status()["autonomy"]["awaiting_human"] is False
    assert harness.send("codex", "two")["message"]["sender"] == "codex"


def test_queued_target_survives_the_autonomy_limit(broker):
    """Hitting the limit pauses agents; it must not lose the pending ask."""
    harness = make_room(broker, "claude", "codex", max_consecutive_agent_turns=1)
    harness.send("claude", "codex, review this", target="codex", message_type="task")

    assert harness.status()["autonomy"]["awaiting_human"] is True
    assert harness.active_speaker() is None
    assert harness.queue_names() == ["codex"]

    harness.send("human", "go ahead")
    assert harness.active_speaker() == "codex"


# ----------------------------------------------------------------------
# floor discipline
# ----------------------------------------------------------------------


def test_an_agent_cannot_jump_the_queue(room):
    room.add_agent("gemini")
    room.broker.request_floor(room.room_id, credential=room.credential("codex"))
    room.broker.request_floor(room.room_id, credential=room.credential("gemini"))
    assert room.active_speaker() == "codex"

    with pytest.raises(TurnError):
        room.send("gemini", "me first")


def test_hold_keeps_the_floor_for_a_follow_up(room):
    result = room.broker.send(
        room.room_id,
        credential=room.credential("claude"),
        draft=MessageDraft(content="part one"),
        hold_floor=True,
    )
    assert result["floor_held"] is True
    assert room.active_speaker() == "claude"

    with pytest.raises(TurnError):
        room.send("codex", "interrupting")

    room.send("claude", "part two")
    assert room.active_speaker() is None


def test_request_floor_optional_ranks_below_queued(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")

    room.broker.request_floor(
        room.room_id, credential=room.credential("gemini"), priority="optional"
    )
    room.broker.request_floor(
        room.room_id, credential=room.credential("claude"), priority="queued"
    )
    assert room.queue_names() == ["claude", "gemini"]


def test_queue_positions_are_first_come_first_served_within_a_bucket(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.broker.request_floor(room.room_id, credential=room.credential("gemini"))
    room.broker.request_floor(room.room_id, credential=room.credential("claude"))
    assert room.queue_names() == ["gemini", "claude"]


def test_repeated_direct_address_does_not_duplicate_a_queue_entry(room):
    room.add_agent("gemini")
    room.send("claude", "codex, review", target="codex", message_type="task")
    room.pass_turn("codex")
    room.send("gemini", "codex, and this too", target="codex", message_type="task")
    assert room.queue_names() == [] and room.active_speaker() == "codex"

    live = room.broker.conn.execute(
        "SELECT COUNT(*) AS n FROM queue_entries WHERE room_id = ? AND status IN "
        "('waiting','active') AND participant_id = (SELECT participant_id FROM participants "
        "WHERE room_id = ? AND name = 'codex')",
        (room.room_id, room.room_id),
    ).fetchone()["n"]
    assert live == 1


def test_a_human_cannot_hand_off_or_hold(room):
    """Flags that only make sense for an agent turn are rejected, not ignored."""
    with pytest.raises(Exception) as exc:
        room.send("human", "over to you", handoff_target="codex")
    assert "hand off" in str(exc.value)

    with pytest.raises(Exception):
        room.broker.send(
            room.room_id,
            credential=room.credential("human"),
            draft=MessageDraft(content="holding"),
            hold_floor=True,
        )
