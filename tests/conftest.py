"""Shared fixtures.

Every test gets an isolated workspace under ``tmp_path`` so nothing touches the
developer's real ``~/.aidapter``.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aidapter.broker import Broker  # noqa: E402
from aidapter.config import resolve_workspace  # noqa: E402
from helpers import RoomHarness, make_room  # noqa: E402

ENV_VARS = ("AIDAPTER_ROOM", "AIDAPTER_PARTICIPANT", "AIDAPTER_SECRET", "AIDAPTER_ROOM_TOKEN")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "aidapter-home"
    monkeypatch.setenv("AIDAPTER_HOME", str(home))
    # Make sure no ambient credentials leak into a test run.
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return resolve_workspace(home).ensure()


@pytest.fixture
def broker(workspace):
    instance = Broker(workspace)
    yield instance
    instance.close()


@pytest.fixture
def room(broker) -> RoomHarness:
    """A room containing ``human``, ``claude``, and ``codex``."""
    return make_room(broker, "claude", "codex", name="PR 89 Review")
