"""Shared semantic memory ledger: storage, parsing, provenance, hand edits."""

from __future__ import annotations

import pytest

from synchri.errors import AuthError, ValidationError
from synchri.memory import ledger as ledger_module


def test_memory_file_is_created_with_the_room(room, workspace):
    path = workspace.memory_path(room.room_id)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    for section in ledger_module.SECTIONS:
        assert f"## {section}" in text


def test_set_and_read_back_the_goal(room):
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("claude"),
        section="goal",
        value="Eliminate the race in the retry path",
        mode="set",
    )
    memory = room.broker.memory_show(room.room_id, credential=room.credential("human"))
    assert memory["memory"]["goal"].startswith("Eliminate the race in the retry path")


def test_entries_carry_provenance(room):
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("codex"),
        section="decisions",
        value="Keep the existing runtime contract",
        mode="add",
        message_id="msg_abcdefghijklmnop",
    )
    decisions = room.broker.memory_show(room.room_id, credential=room.credential("human"))[
        "memory"
    ]["decisions"]
    assert len(decisions) == 1
    assert "Keep the existing runtime contract" in decisions[0]
    assert "codex" in decisions[0], "the ledger entry must name its author"
    assert "msg_abcdefghijklmnop" in decisions[0], "and the message it came from"


def test_memory_survives_a_broker_restart(workspace):
    from synchri.broker import Broker

    from helpers import make_room

    with Broker(workspace) as first:
        harness = make_room(first, "claude")
        first.memory_write(
            harness.room_id,
            credential=harness.credential("claude"),
            section="constraints",
            value="No new runtime dependencies",
            mode="add",
        )
        room_id, human = harness.room_id, harness.credential("human")

    with Broker(workspace) as second:
        constraints = second.memory_show(room_id, credential=human)["memory"]["constraints"]
        assert any("No new runtime dependencies" in c for c in constraints)


def test_memory_is_separate_from_the_transcript(room):
    room.send("claude", "this is chat, not memory")
    memory = room.broker.memory_show(room.room_id, credential=room.credential("human"))
    assert "this is chat, not memory" not in memory["markdown"]
    assert [m["content"] for m in room.messages()] == ["this is chat, not memory"]


def test_memory_is_separate_from_the_queue(room):
    """The ledger must never be consulted for scheduling decisions."""
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("human"),
        section="current_task",
        value="codex reviews commit abc123",
        mode="set",
    )
    assert room.active_speaker() is None
    assert room.queue_names() == []


def test_hand_edits_are_preserved_across_agent_writes(room, workspace):
    path = workspace.memory_path(room.room_id)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text + "\n## Human Scratchpad\n\nremember to check the flaky test\n", encoding="utf-8"
    )

    room.broker.memory_write(
        room.room_id,
        credential=room.credential("claude"),
        section="open_issues",
        value="retry path may double-fire",
        mode="add",
    )

    updated = path.read_text(encoding="utf-8")
    assert "## Human Scratchpad" in updated
    assert "remember to check the flaky test" in updated
    assert "retry path may double-fire" in updated


def test_hand_written_entries_are_readable_by_agents(room, workspace):
    path = workspace.memory_path(room.room_id)
    path.write_text(
        "# memory\n\n## Goal\n\nship the fix\n\n## Constraints\n\n- no new deps\n- keep the API\n",
        encoding="utf-8",
    )
    memory = room.broker.memory_show(room.room_id, credential=room.credential("codex"))["memory"]
    assert memory["goal"] == "ship the fix"
    assert memory["constraints"] == ["no new deps", "keep the API"]


def test_remove_an_entry_by_index(room):
    for value in ("first", "second", "third"):
        room.broker.memory_write(
            room.room_id,
            credential=room.credential("human"),
            section="open_issues",
            value=value,
            mode="add",
        )
    room.broker.memory_remove(
        room.room_id, credential=room.credential("human"), section="open_issues", index=2
    )
    issues = room.broker.memory_show(room.room_id, credential=room.credential("human"))["memory"][
        "open_issues"
    ]
    assert len(issues) == 2
    assert "second" not in " ".join(issues)


def test_removing_a_missing_entry_is_rejected(room):
    with pytest.raises(ValidationError):
        room.broker.memory_remove(
            room.room_id, credential=room.credential("human"), section="open_issues", index=1
        )


def test_unknown_section_is_rejected(room):
    with pytest.raises(ValidationError):
        room.broker.memory_write(
            room.room_id,
            credential=room.credential("human"),
            section="nonsense",
            value="x",
            mode="add",
        )


def test_set_on_a_list_section_is_rejected(room):
    with pytest.raises(ValidationError):
        room.broker.memory_write(
            room.room_id,
            credential=room.credential("human"),
            section="decisions",
            value="x",
            mode="set",
        )


def test_add_on_a_value_section_is_rejected(room):
    with pytest.raises(ValidationError):
        room.broker.memory_write(
            room.room_id, credential=room.credential("human"), section="goal", value="x", mode="add"
        )


def test_memory_writes_require_a_participant_identity(room):
    from synchri.broker import Credential

    with pytest.raises(AuthError):
        room.broker.memory_write(
            room.room_id,
            credential=Credential(room_token=room.observer_token),
            section="decisions",
            value="anonymous decision",
            mode="add",
        )


def test_memory_writes_are_audited(room):
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("claude"),
        section="decisions",
        value="use a mutex",
        mode="add",
    )
    updates = room.events("memory.updated")
    assert len(updates) == 1
    assert updates[0]["actor_name"] == "claude"
    assert updates[0]["payload"]["section"] == "decisions"


def test_recent_handoffs_is_a_bounded_window(room):
    store = room.broker.ledger
    overflow = 5
    for index in range(ledger_module.MAX_HANDOFFS + overflow):
        store.add_entry(room.room_id, "Recent Handoffs", f"handoff {index}", actor="claude")

    entries = store.load(room.room_id).entries("Recent Handoffs")
    assert len(entries) == ledger_module.MAX_HANDOFFS
    assert entries[0].startswith(f"handoff {overflow}"), "the oldest handoffs roll off"
    assert entries[-1].startswith(f"handoff {ledger_module.MAX_HANDOFFS + overflow - 1}")


def test_ledger_round_trips_through_render_and_parse():
    original = ledger_module.default_ledger("room_abcdefghijklmnop", "Round trip")
    original.sections["Goal"] = ["ship it"]
    original.sections["Decisions"] = ["decided a", "decided b"]
    original.extra["Notes"] = ["freeform"]

    reparsed = ledger_module.parse(
        ledger_module.render(original), "room_abcdefghijklmnop", "Round trip"
    )
    assert reparsed.get_value("Goal") == "ship it"
    assert reparsed.entries("Decisions") == ["decided a", "decided b"]
    assert reparsed.extra["Notes"] == ["freeform"]


def test_multiline_entries_are_flattened_to_keep_the_list_structure(room):
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("human"),
        section="open_issues",
        value="line one\nline two",
        mode="add",
    )
    issues = room.broker.memory_show(room.room_id, credential=room.credential("human"))["memory"][
        "open_issues"
    ]
    assert issues[0].startswith("line one line two")


def test_empty_entry_is_rejected(room):
    with pytest.raises(ValidationError):
        room.broker.memory_write(
            room.room_id,
            credential=room.credential("human"),
            section="open_issues",
            value="   ",
            mode="add",
        )
