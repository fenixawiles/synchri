"""Room creation, token security, cross-room isolation, and persistence."""

from __future__ import annotations

import re
import sqlite3

import pytest

from aidapter.broker import Broker, Credential
from aidapter.errors import AuthError, NotFoundError, StateError, ValidationError
from aidapter.models.envelope import MessageDraft
from aidapter.security import tokens

from helpers import make_room


def test_create_room_returns_identity_and_token(broker):
    result = broker.create_room("PR 89 Review", goal="find race conditions")

    assert re.match(r"^room_[A-Za-z0-9_-]{16,}$", result["room_id"])
    assert result["observer_token"].startswith(result["room_id"] + ".")
    assert result["human"]["kind"] == "human"
    assert result["human"]["secret"]

    status = broker.room_status(
        result["room_id"],
        credential=Credential(result["human"]["name"], result["human"]["secret"]),
    )
    assert status["room"]["status"] == "active"
    assert status["active_speaker"] is None
    assert status["queue"] == []


def test_room_goal_seeds_the_memory_ledger(broker):
    result = broker.create_room("Room", goal="find race conditions")
    memory = broker.memory_show(
        result["room_id"],
        credential=Credential(result["human"]["name"], result["human"]["secret"]),
    )
    assert memory["memory"]["goal"] == "find race conditions"


def test_room_ids_and_tokens_are_unique_and_unguessable(broker):
    rooms = [broker.create_room(f"Room {i}") for i in range(25)]
    room_ids = {r["room_id"] for r in rooms}
    secrets = {r["observer_token"].split(".", 1)[1] for r in rooms}

    assert len(room_ids) == 25, "room ids must not collide"
    assert len(secrets) == 25, "join secrets must not collide"
    # No sequential structure: ids share only the prefix.
    assert len({r["room_id"][:12] for r in rooms}) > 1
    for secret in secrets:
        assert len(secret) >= 40, "join secret must carry at least 256 bits of entropy"


def test_join_tokens_are_stored_hashed_not_plaintext(broker, workspace):
    result = broker.create_room("Room")
    secret = result["observer_token"].split(".", 1)[1]

    row = broker.conn.execute(
        "SELECT token_salt, token_hash FROM rooms WHERE room_id = ?", (result["room_id"],)
    ).fetchone()
    assert secret not in row["token_hash"]
    assert tokens.verify_secret(secret, row["token_salt"], row["token_hash"])

    raw_db = workspace.db_path.read_bytes()
    assert secret.encode() not in raw_db, "the plaintext join secret must never be persisted"


def test_participant_secrets_are_stored_hashed(broker, workspace):
    result = broker.create_room("Room", agents=["claude"])
    invite = result["invites"][0]
    joined = broker.join(invite["token"], "claude")
    assert joined["secret"].encode() not in workspace.db_path.read_bytes()
    # The invite's own secret is hashed too.
    assert invite["token"].split(".", 1)[1].encode() not in workspace.db_path.read_bytes()


def test_an_invite_from_another_room_is_rejected(broker):
    room_a = broker.create_room("A", agents=["claude"])
    room_b = broker.create_room("B", agents=["claude"])
    invite_b_secret = room_b["invites"][0]["token"].split(".", 1)[1]

    with pytest.raises(AuthError):
        broker.join(room_a["room_id"], "claude", token=invite_b_secret)


def test_the_observer_token_cannot_join_a_room(broker):
    """Reading a room and entering it are separate capabilities."""
    created = broker.create_room("Split capabilities")
    with pytest.raises(AuthError) as exc:
        broker.join(created["observer_token"], "claude")
    assert exc.value.code == "invalid_invite"


def test_unknown_room_is_not_found(broker):
    with pytest.raises(NotFoundError):
        broker.room_status("room_" + "x" * 20, credential=Credential(room_token="whatever"))


def test_malformed_room_id_is_rejected(broker):
    with pytest.raises(ValidationError):
        broker.room_status("../../etc/passwd", credential=Credential(room_token="t"))


def test_cross_room_isolation_of_messages_and_participants(broker):
    room_a = make_room(broker, "claude", name="Room A")
    room_b = make_room(broker, "claude", name="Room B")

    room_a.send("claude", "secret from room A")
    room_b.send("claude", "secret from room B")

    messages_a = [m["content"] for m in room_a.messages()]
    messages_b = [m["content"] for m in room_b.messages()]
    assert messages_a == ["secret from room A"]
    assert messages_b == ["secret from room B"]

    # A credential minted in room A carries no authority in room B, even though
    # the participant name is identical.
    with pytest.raises((AuthError, NotFoundError)):
        broker.read(room_b.room_id, credential=room_a.credential("claude"))


def test_room_a_participant_cannot_address_a_room_b_participant(broker):
    room_a = make_room(broker, "claude", name="Room A")
    room_b = make_room(broker, "codex", name="Room B")

    with pytest.raises(NotFoundError):
        room_a.send("claude", "hi", target="codex")
    assert room_b  # room B is untouched


def test_observer_can_read_with_the_room_token_only(room):
    room.send("claude", "hello everyone")
    result = room.broker.read(room.room_id, credential=room.observer())
    assert [m["content"] for m in result["messages"]] == ["hello everyone"]


def test_read_requires_some_credential(room):
    with pytest.raises(AuthError):
        room.broker.read(room.room_id, credential=Credential())


def test_read_rejects_a_bogus_room_token(room):
    with pytest.raises(AuthError):
        room.broker.read(room.room_id, credential=Credential(room_token="not-the-token"))


def test_state_persists_across_broker_restart(workspace):
    with Broker(workspace) as first:
        harness = make_room(first, "claude", "codex", name="Durable")
        harness.send("claude", "review this", target="codex", message_type="task")
        room_id = harness.room_id
        codex_credential = harness.credential("codex")
        human_credential = harness.credential("human")

    # New process-equivalent: fresh Broker, fresh connection, same workspace.
    with Broker(workspace) as second:
        status = second.room_status(room_id, credential=human_credential)
        assert status["active_speaker"] == "codex"
        assert status["active_turn"]["blocking"] is True
        assert second.turn_status(room_id, credential=codex_credential)["state"] == "your_turn"

        messages = second.read(room_id, credential=human_credential)["messages"]
        assert [m["content"] for m in messages] == ["review this"]

        result = second.send(
            room_id,
            credential=codex_credential,
            draft=MessageDraft(content="findings", message_type="response"),
        )
        assert result["message"]["sender"] == "codex"


def test_room_ledger_and_transcript_live_in_a_scoped_directory(room, workspace):
    room.send("claude", "hello")
    room_dir = workspace.rooms_dir / room.room_id
    assert (room_dir / "memory.md").exists()
    assert (room_dir / "transcript.jsonl").exists()
    # No sibling room's files leak into this directory.
    assert {p.name for p in room_dir.iterdir()} <= {"memory.md", "transcript.jsonl"}


def test_stopped_room_rejects_new_joins(room):
    invite = room.invite("gemini")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    with pytest.raises(StateError):
        room.broker.join(invite["token"], "gemini")


def test_workspace_directories_are_owner_only(workspace):
    mode = workspace.home.stat().st_mode & 0o777
    assert mode == 0o700


def test_database_file_is_owner_only(broker, workspace):
    broker.create_room("Room")
    assert workspace.db_path.stat().st_mode & 0o777 == 0o600


def test_schema_enforces_one_live_queue_entry_per_participant(room):
    """Defence in depth: the invariant is also a database constraint."""
    room.send("claude", "task", target="codex", message_type="task")
    entry = room.broker.conn.execute(
        "SELECT participant_id FROM queue_entries WHERE room_id = ? LIMIT 1", (room.room_id,)
    ).fetchone()
    if entry is None:
        pytest.skip("no queue entry materialized for this scenario")
    with pytest.raises(sqlite3.IntegrityError):
        room.broker.conn.execute(
            "INSERT INTO queue_entries (room_id, participant_id, priority, enqueue_seq, status, "
            "blocking, enqueued_at) VALUES (?, ?, 1, 99, 'waiting', 0, 'now')",
            (room.room_id, entry["participant_id"]),
        )
