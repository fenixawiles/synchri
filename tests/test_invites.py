"""Invites: name-bound, single-use, expiring grants to enter a room.

The room's observer token can read a room but never join it. Entering requires
an invite, and an invite stops working once it is used, revoked, expired, or
the room ends.
"""

from __future__ import annotations

import pytest

from aidapter.broker import Broker, Credential
from aidapter.errors import (
    AidapterError,
    AuthError,
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
from aidapter.storage import dao

from helpers import make_room


def expire(broker: Broker, room_id: str, name: str, when: str = "2000-01-01T00:00:00.000Z") -> None:
    """Backdate an invite's expiry so expiry can be tested deterministically."""
    invite = dao.get_live_invite_by_name(broker.conn, room_id, name)
    assert invite is not None
    with broker.conn:
        dao.update_invite(broker.conn, room_id, invite.invite_id, expires_at=when)


# ----------------------------------------------------------------------
# provisioning at room creation
# ----------------------------------------------------------------------


def test_create_room_mints_one_invite_per_named_agent(broker):
    created = broker.create_room("PR 89", agents=["claude", "codex"])

    names = [i["participant_name"] for i in created["invites"]]
    assert names == ["claude", "codex"]
    for invite in created["invites"]:
        assert invite["command"] == f"aidapter join {invite['token']} --name {invite['participant_name']}"
        assert invite["token"].startswith(created["room_id"] + ".")
        assert invite["expires_at"] is not None


def test_agents_may_be_given_as_a_comma_separated_string(broker):
    created = broker.create_room("Room", agents=["claude,codex", "gemini"])
    assert [i["participant_name"] for i in created["invites"]] == ["claude", "codex", "gemini"]


def test_duplicate_agent_names_are_collapsed(broker):
    created = broker.create_room("Room", agents=["claude", "claude"])
    assert len(created["invites"]) == 1


def test_an_agent_cannot_be_named_after_the_human(broker):
    with pytest.raises(ValidationError):
        broker.create_room("Room", human_name="human", agents=["human"])


def test_invalid_agent_names_are_rejected_at_creation(broker):
    with pytest.raises(ValidationError):
        broker.create_room("Room", agents=["../escape"])


def test_a_room_can_be_created_with_no_agents(broker):
    created = broker.create_room("Room")
    assert created["invites"] == []


def test_each_invite_secret_is_distinct(broker):
    created = broker.create_room("Room", agents=["claude", "codex"])
    secrets = {i["token"].split(".", 1)[1] for i in created["invites"]}
    assert len(secrets) == 2
    assert created["observer_token"].split(".", 1)[1] not in secrets


def test_invite_secrets_are_stored_hashed(broker, workspace):
    created = broker.create_room("Room", agents=["claude"])
    secret = created["invites"][0]["token"].split(".", 1)[1]
    assert secret.encode() not in workspace.db_path.read_bytes()


# ----------------------------------------------------------------------
# redemption
# ----------------------------------------------------------------------


def test_an_invite_admits_exactly_the_name_it_was_minted_for(broker):
    created = broker.create_room("Room", agents=["codex"])
    token = created["invites"][0]["token"]

    with pytest.raises(AuthError) as exc:
        broker.join(token, "claude")
    assert exc.value.code == "invite_name_mismatch"

    assert broker.join(token, "codex")["name"] == "codex"


def test_an_invite_is_single_use(broker):
    created = broker.create_room("Room", agents=["codex"])
    token = created["invites"][0]["token"]
    broker.join(token, "codex")

    with pytest.raises(AidapterError) as exc:
        broker.join(token, "codex")
    # Re-presenting it for the same name trips the duplicate-participant check
    # first; either way the token buys nothing a second time.
    assert exc.value.code in {"invite_already_used", "duplicate_participant"}


def test_a_used_invite_cannot_admit_a_second_identity(broker):
    """The real single-use property: the token is dead even for a fresh name."""
    created = broker.create_room("Room", agents=["codex"])
    token = created["invites"][0]["token"]
    broker.join(token, "codex")
    broker.remove_participant(
        created["room_id"],
        "codex",
        credential=Credential(created["human"]["name"], created["human"]["secret"]),
    )

    with pytest.raises(AuthError) as exc:
        broker.join(token, "gemini")
    assert exc.value.code == "invite_already_used"


def test_redemption_is_recorded_against_the_participant(broker):
    created = broker.create_room("Room", agents=["codex"])
    joined = broker.join(created["invites"][0]["token"], "codex")

    human = Credential(created["human"]["name"], created["human"]["secret"])
    invite = broker.list_invites(created["room_id"], credential=human)[0]
    assert invite["status"] == "redeemed"
    assert invite["redeemed_participant_id"] == joined["participant_id"]
    assert invite["redeemed_at"] is not None


def test_a_garbage_token_is_rejected(broker):
    created = broker.create_room("Room", agents=["codex"])
    with pytest.raises(AuthError) as exc:
        broker.join(created["room_id"], "codex", token="not-a-real-secret")
    assert exc.value.code == "invalid_invite"


def test_joining_without_any_token_is_rejected(broker):
    created = broker.create_room("Room", agents=["codex"])
    with pytest.raises(AuthError):
        broker.join(created["room_id"], "codex")


# ----------------------------------------------------------------------
# expiry
# ----------------------------------------------------------------------


def test_an_expired_invite_is_refused(broker):
    created = broker.create_room("Room", agents=["codex"])
    expire(broker, created["room_id"], "codex")

    with pytest.raises(AuthError) as exc:
        broker.join(created["invites"][0]["token"], "codex")
    assert exc.value.code == "invite_expired"


def test_an_expired_invite_reports_as_expired_without_being_touched(broker):
    created = broker.create_room("Room", agents=["codex"])
    human = Credential(created["human"]["name"], created["human"]["secret"])
    expire(broker, created["room_id"], "codex")

    invite = broker.list_invites(created["room_id"], credential=human)[0]
    assert invite["status"] == "expired"
    assert invite["revoked_at"] is None, "expiry is derived from time, not a stored flag"


def test_a_fresh_invite_replaces_an_expired_one(broker):
    created = broker.create_room("Room", agents=["codex"])
    room_id = created["room_id"]
    human = Credential(created["human"]["name"], created["human"]["secret"])
    expire(broker, room_id, "codex")

    replacement = broker.create_invite(room_id, "codex", credential=human)
    assert broker.join(replacement["token"], "codex")["name"] == "codex"


def test_ttl_zero_means_no_time_limit(broker):
    created = broker.create_room("Room", agents=["codex"], invite_ttl_seconds=0)
    assert created["invites"][0]["expires_at"] is None
    assert broker.join(created["invites"][0]["token"], "codex")["name"] == "codex"


def test_negative_ttl_is_rejected(broker):
    with pytest.raises(ValidationError):
        broker.create_room("Room", agents=["codex"], invite_ttl_seconds=-5)


# ----------------------------------------------------------------------
# minting, superseding, revoking
# ----------------------------------------------------------------------


def test_minting_an_invite_after_the_fact(room):
    invite = room.invite("gemini")
    joined = room.broker.join(invite["token"], "gemini")
    assert joined["name"] == "gemini"
    assert joined["kind"] == "agent"


def test_an_invite_can_admit_a_second_human(room):
    invite = room.invite("reviewer", kind="human")
    joined = room.broker.join(invite["token"], "reviewer")
    assert joined["kind"] == "human"
    # ...and that human carries human authority.
    room.credentials["reviewer"] = Credential("reviewer", joined["secret"])
    assert room.broker.pause_room(room.room_id, credential=room.credential("reviewer"))


def test_the_invite_decides_the_kind_not_the_joiner(room):
    """A participant cannot promote itself to human by asking."""
    invite = room.invite("gemini", kind="agent")
    joined = room.broker.join(invite["token"], "gemini")
    assert joined["kind"] == "agent"


def test_minting_a_second_invite_supersedes_the_first(room):
    first = room.invite("gemini")
    second = room.invite("gemini")
    assert second["superseded_invite_id"] == first["invite_id"]

    with pytest.raises(AuthError) as exc:
        room.broker.join(first["token"], "gemini")
    assert exc.value.code == "invite_revoked"
    assert room.broker.join(second["token"], "gemini")["name"] == "gemini"


def test_revoked_invites_stop_working(room):
    invite = room.invite("gemini")
    room.broker.revoke_invite(room.room_id, "gemini", credential=room.credential("human"))

    with pytest.raises(AuthError) as exc:
        room.broker.join(invite["token"], "gemini")
    assert exc.value.code == "invite_revoked"


def test_revoking_a_missing_invite_is_not_found(room):
    with pytest.raises(NotFoundError):
        room.broker.revoke_invite(room.room_id, "nobody", credential=room.credential("human"))


def test_only_a_human_may_mint_or_revoke_invites(room):
    with pytest.raises(AuthError) as exc:
        room.broker.create_invite(room.room_id, "gemini", credential=room.credential("claude"))
    assert exc.value.code == "human_required"

    room.invite("gemini")
    with pytest.raises(AuthError):
        room.broker.revoke_invite(room.room_id, "gemini", credential=room.credential("claude"))


def test_cannot_invite_an_existing_participant(room):
    with pytest.raises(ConflictError) as exc:
        room.invite("codex")
    assert exc.value.code == "duplicate_participant"


def test_cannot_invite_a_removed_participant(room):
    room.broker.remove_participant(room.room_id, "codex", credential=room.credential("human"))
    with pytest.raises(ConflictError):
        room.invite("codex")


# ----------------------------------------------------------------------
# the room ending ends every grant
# ----------------------------------------------------------------------


def test_stopping_the_room_revokes_every_pending_invite(room):
    pending = room.invite("gemini")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))

    invites = {
        i["participant_name"]: i
        for i in room.broker.list_invites(room.room_id, credential=room.observer())
    }
    assert invites["gemini"]["status"] == "revoked"
    assert invites["gemini"]["revoked_reason"] == "room_stopped"

    with pytest.raises(StateError):
        room.broker.join(pending["token"], "gemini")


def test_a_stopped_room_cannot_mint_new_invites(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    with pytest.raises(StateError):
        room.invite("gemini")


def test_stopping_a_room_leaves_redeemed_invites_recorded(room):
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    invites = {
        i["participant_name"]: i["status"]
        for i in room.broker.list_invites(room.room_id, credential=room.observer())
    }
    assert invites["claude"] == "redeemed", "history must not be rewritten by the stop"


# ----------------------------------------------------------------------
# isolation, listing, provenance
# ----------------------------------------------------------------------


def test_invites_are_scoped_to_their_room(broker):
    room_a = make_room(broker, name="Room A")
    room_b = make_room(broker, name="Room B")
    invite = room_a.invite("codex")

    with pytest.raises(AuthError):
        broker.join(room_b.room_id, "codex", token=invite["token"].split(".", 1)[1])
    assert broker.join(invite["token"], "codex")["room_id"] == room_a.room_id


def test_listing_invites_never_exposes_a_token(room):
    room.invite("gemini")
    for invite in room.broker.list_invites(room.room_id, credential=room.observer()):
        assert "token" not in invite, "only the hash is stored; a token is shown once"


def test_invite_activity_is_audited(room):
    room.invite("gemini")
    room.broker.revoke_invite(room.room_id, "gemini", credential=room.credential("human"))

    created = room.events("invite.created")
    revoked = room.events("invite.revoked")
    assert any(e["payload"]["participant_name"] == "gemini" for e in created)
    assert revoked and revoked[0]["actor_name"] == "human"


def test_join_records_which_invite_was_redeemed(room):
    invite = room.invite("gemini")
    room.broker.join(invite["token"], "gemini")
    joins = [e for e in room.events("participant.joined") if e["actor_name"] == "gemini"]
    assert joins[0]["payload"]["invite_id"] == invite["invite_id"]


def test_invites_survive_a_broker_restart(workspace):
    with Broker(workspace) as first:
        created = first.create_room("Durable", agents=["codex"])
        room_id, token = created["room_id"], created["invites"][0]["token"]

    with Broker(workspace) as second:
        assert second.join(token, "codex")["room_id"] == room_id
