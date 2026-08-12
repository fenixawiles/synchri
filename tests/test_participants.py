"""Participant identity: join, duplicates, removal, and revocation."""

from __future__ import annotations

import pytest

from synchri.broker import Credential
from synchri.errors import AuthError, ConflictError, NotFoundError, ValidationError
from synchri.models.envelope import MessageDraft

from helpers import make_room


def test_join_creates_a_stable_identity(room):
    people = {p["name"]: p for p in room.broker.list_participants(
        room.room_id, credential=room.credential("human")
    )}
    assert set(people) == {"human", "claude", "codex"}
    assert people["claude"]["kind"] == "agent"
    assert people["human"]["kind"] == "human"
    assert people["claude"]["participant_id"].startswith("part_")


def test_participant_identity_is_stable_across_messages(room):
    first = room.send("claude", "one")["message"]
    second = room.send("claude", "two")["message"]
    assert first["sender_participant_id"] == second["sender_participant_id"]


def test_duplicate_join_without_secret_is_rejected(room):
    with pytest.raises(ConflictError) as exc:
        room.broker.join(room.room_id, "claude")
    assert exc.value.code == "duplicate_participant"


def test_duplicate_join_with_the_right_secret_reattaches(room):
    secret = room.credential("claude").secret
    result = room.broker.join(room.observer_token, "claude", rejoin_secret=secret)
    assert result["rejoined"] is True
    assert result["secret"] == secret

    people = room.broker.list_participants(room.room_id, credential=room.credential("human"))
    assert sum(1 for p in people if p["name"] == "claude") == 1


def test_rejoin_with_a_wrong_secret_is_rejected(room):
    with pytest.raises(AuthError):
        room.broker.join(room.observer_token, "claude", rejoin_secret="not-the-secret")


def test_join_requires_an_invite(room):
    with pytest.raises(AuthError):
        room.broker.join(room.room_id, "gemini")


def test_join_rejects_invalid_names(room):
    for bad in ["", "has space", "../escape", "a" * 100, "-leading"]:
        with pytest.raises(ValidationError):
            room.broker.join(room.room_id, bad)


def test_removal_revokes_the_participant_immediately(room):
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))

    with pytest.raises(AuthError) as exc:
        room.send("codex", "I am still here")
    assert exc.value.code == "participant_removed"


def test_removed_participant_holding_a_valid_secret_is_still_rejected(room):
    """Revocation is authoritative: the secret stays valid, the status does not."""
    secret = room.credential("codex").secret
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))

    stale = Credential(participant="codex", secret=secret)
    with pytest.raises(AuthError):
        room.broker.send(
            room.room_id, credential=stale, draft=MessageDraft(content="sneaking in")
        )
    with pytest.raises(AuthError):
        room.broker.pass_turn(room.room_id, credential=stale)
    with pytest.raises(AuthError):
        room.broker.request_floor(room.room_id, credential=stale)


def test_removed_participant_can_still_see_that_it_was_removed(room):
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))
    assert room.state("codex") == "removed"


def test_removing_the_active_speaker_releases_the_floor(room):
    room.send("claude", "review this", target="codex", message_type="task")
    assert room.active_speaker() == "codex"

    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))
    assert room.active_speaker() is None
    assert room.queue_names() == []
    # Another agent can now speak.
    assert room.send("claude", "carrying on")["message"]["sender"] == "claude"


def test_removed_participant_cannot_be_addressed(room):
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))
    with pytest.raises(ConflictError):
        room.send("claude", "hello", target="codex")


def test_removed_name_cannot_be_reclaimed_by_a_new_join(room):
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))
    with pytest.raises(ConflictError) as exc:
        room.broker.join(room.room_id, "codex")
    assert exc.value.code == "participant_removed"


def test_only_a_human_may_remove_participants(room):
    with pytest.raises(AuthError) as exc:
        room.broker.remove_participant(room.room_id, "codex", credential=room.credential("claude"))
    assert exc.value.code == "human_required"


def test_the_room_owner_cannot_be_removed(room):
    with pytest.raises(ValidationError):
        room.broker.remove_participant(room.room_id, "human", credential=room.credential("human"))


def test_removing_an_unknown_participant_is_not_found(room):
    with pytest.raises(NotFoundError):
        room.broker.remove_participant(room.room_id, "gemini", credential=room.credential("human"))


def test_a_wrong_secret_is_rejected(room):
    with pytest.raises(AuthError):
        room.broker.send(
            room.room_id,
            credential=Credential(participant="claude", secret="wrong"),
            draft=MessageDraft(content="hi"),
        )


def test_a_missing_secret_is_rejected(room):
    with pytest.raises(AuthError):
        room.broker.send(
            room.room_id,
            credential=Credential(participant="claude"),
            draft=MessageDraft(content="hi"),
        )


def test_participant_secrets_do_not_transfer_between_participants(room):
    swapped = Credential(participant="codex", secret=room.credential("claude").secret)
    with pytest.raises(AuthError):
        room.broker.send(room.room_id, credential=swapped, draft=MessageDraft(content="hi"))


def test_a_third_agent_can_join_an_existing_room(broker):
    harness = make_room(broker, "claude", "codex")
    harness.add_agent("gemini")
    names = [p["name"] for p in harness.broker.list_participants(
        harness.room_id, credential=harness.credential("human")
    )]
    assert names == ["human", "claude", "codex", "gemini"]


def test_cannot_address_yourself(room):
    with pytest.raises(ValidationError):
        room.send("claude", "talking to myself", target="claude")
