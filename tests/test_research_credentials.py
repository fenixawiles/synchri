"""Credentials are loaded only from an explicit, owner-only, dedicated file (ISO-06)."""

from __future__ import annotations

import json
import os
import pickle

import pytest

from synchri.research.credentials import CredentialVault, Secret
from synchri.research.errors import CredentialError


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_loads_a_dedicated_owner_only_file(tmp_path):
    path = _write(tmp_path / "keys" / "research.json", {"anthropic": "dummy-key-123456"})
    vault = CredentialVault.load(path, environ={"HOME": str(tmp_path / "home")})
    assert vault.providers() == ["anthropic"]
    assert vault.get("anthropic").reveal() == "dummy-key-123456"
    assert "dummy-key" not in repr(vault) and "dummy-key" not in repr(vault.get("anthropic"))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_refuses_files_readable_or_writable_by_others(tmp_path, mode):
    path = _write(tmp_path / "research.json", {"anthropic": "dummy-key-123456"}, mode)
    with pytest.raises(CredentialError, match="chmod 600"):
        CredentialVault.load(path, environ={"HOME": str(tmp_path / "home")})


def test_refuses_symlinks_relative_paths_and_missing_files(tmp_path):
    real = _write(tmp_path / "real.json", {"anthropic": "dummy-key-123456"})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    env = {"HOME": str(tmp_path / "home")}
    with pytest.raises(CredentialError, match="symbolic link"):
        CredentialVault.load(link, environ=env)
    with pytest.raises(CredentialError, match="absolute"):
        CredentialVault.load("research.json", environ=env)
    with pytest.raises(CredentialError, match="cannot read"):
        CredentialVault.load(tmp_path / "absent.json", environ=env)
    with pytest.raises(CredentialError, match="explicit"):
        CredentialVault.load(None, environ=env)


@pytest.mark.parametrize(
    "relative",
    [".claude/research.json", ".codex/research.json", ".synchri/keys.json",
     ".config/anthropic/k.json", ".ccr/k.json"],
)
def test_refuses_provider_session_and_synchri_locations(tmp_path, relative):
    home = tmp_path / "home"
    path = _write(home / relative, {"anthropic": "dummy-key-123456"})
    with pytest.raises(CredentialError, match="dedicated"):
        CredentialVault.load(path, environ={"HOME": str(home)})


def test_refuses_env_named_session_locations(tmp_path):
    ingress = tmp_path / "session"
    path = _write(ingress / "token.json", {"anthropic": "dummy-key-123456"})
    with pytest.raises(CredentialError, match="dedicated"):
        CredentialVault.load(path, environ={"HOME": str(tmp_path / "home"),
                                            "CLAUDE_SESSION_INGRESS_TOKEN_FILE": str(ingress)})


@pytest.mark.parametrize(
    "data, message",
    [({}, "map provider"), ({"gemini": "dummy-key-123456"}, "unknown provider"),
     ({"anthropic": ""}, "empty or malformed"), ({"anthropic": 5}, "empty or malformed")],
)
def test_refuses_malformed_contents(tmp_path, data, message):
    path = _write(tmp_path / "research.json", data)
    with pytest.raises(CredentialError, match=message):
        CredentialVault.load(path, environ={"HOME": str(tmp_path / "home")})


def test_secrets_do_not_print_or_pickle():
    secret = Secret("dummy-key-123456")
    assert "dummy" not in str(secret) and "dummy" not in f"{secret!r}"
    with pytest.raises(TypeError):
        pickle.dumps(secret)
