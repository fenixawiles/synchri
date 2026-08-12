"""Local security primitives: token handling and path sanitization."""

from __future__ import annotations

import pytest

from aidapter.config import resolve_workspace
from aidapter.errors import ValidationError
from aidapter.ids import is_valid_id, new_id, new_secret
from aidapter.security import tokens


def test_generated_ids_are_prefixed_and_high_entropy():
    ids = {new_id("room") for _ in range(500)}
    assert len(ids) == 500, "identifier collision"
    for value in list(ids)[:10]:
        assert is_valid_id("room", value)
        assert not is_valid_id("part", value), "prefixes must not be interchangeable"


def test_secret_verification_is_hash_based():
    secret = new_secret()
    salt = tokens.new_salt()
    digest = tokens.hash_secret(secret, salt)

    assert tokens.verify_secret(secret, salt, digest)
    assert not tokens.verify_secret(secret + "x", salt, digest)
    assert not tokens.verify_secret(secret, tokens.new_salt(), digest)
    assert not tokens.verify_secret("", salt, digest)
    assert not tokens.verify_secret(None, salt, digest)


def test_the_same_secret_under_different_salts_yields_different_hashes():
    secret = new_secret()
    assert tokens.hash_secret(secret, "salt-a") != tokens.hash_secret(secret, "salt-b")


def test_join_token_round_trip():
    room_id = new_id("room")
    secret = new_secret()
    token = tokens.compose_join_token(room_id, secret)

    assert tokens.split_join_token(token) == (room_id, secret)
    assert tokens.room_id_from_reference(token) == room_id
    assert tokens.room_id_from_reference(room_id) == room_id


def test_a_bare_secret_carries_no_room_claim():
    assert tokens.split_join_token("just-a-secret") == (None, "just-a-secret")


def test_empty_join_token_is_rejected():
    with pytest.raises(ValidationError):
        tokens.split_join_token("   ")


@pytest.mark.parametrize(
    "room_id",
    [
        "../../etc/passwd",
        "room_../escape",
        "/absolute/path",
        "room_",
        "",
        "room_short",
        "roomabcdefghijklmnop",
        "room_ok/../../escape",
    ],
)
def test_room_paths_reject_traversal_and_malformed_ids(tmp_path, room_id):
    workspace = resolve_workspace(tmp_path / "home")
    with pytest.raises(ValidationError):
        workspace.room_dir(room_id)
    with pytest.raises(ValidationError):
        workspace.memory_path(room_id)


def test_room_paths_stay_inside_the_workspace(tmp_path):
    workspace = resolve_workspace(tmp_path / "home")
    path = workspace.memory_path(new_id("room"))
    assert workspace.home in path.parents


@pytest.mark.parametrize("name", ["../evil", "a/b", "with space", "", "x" * 200])
def test_session_paths_reject_malformed_participant_names(tmp_path, name):
    workspace = resolve_workspace(tmp_path / "home")
    with pytest.raises(ValidationError):
        workspace.session_path(new_id("room"), name)


def test_room_name_is_never_used_as_a_path_component(broker, workspace):
    """A hostile room name must not escape the workspace."""
    created = broker.create_room("../../../etc/evil")
    memory_path = workspace.memory_path(created["room_id"])
    assert memory_path.exists()
    assert workspace.rooms_dir in memory_path.parents
    assert created["room_id"] in str(memory_path)


def test_credential_repr_does_not_leak_the_secret():
    from aidapter.broker import Credential

    text = repr(Credential(participant="claude", secret="super-secret-value"))
    assert "super-secret-value" not in text
    assert "redacted" in text


def test_no_network_listener_is_opened_by_the_broker(room):
    """v0.1 binds nothing: the broker is a library over a local file."""
    import socket

    original_bind = socket.socket.bind

    def refuse(self, address):  # pragma: no cover - only runs on a regression
        raise AssertionError(f"the broker must not bind a socket (attempted {address!r})")

    socket.socket.bind = refuse
    try:
        room.send("claude", "no sockets here", target="codex", message_type="task")
        room.pass_turn("codex")
        room.status()
    finally:
        socket.socket.bind = original_bind
