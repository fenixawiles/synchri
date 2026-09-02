"""Mixed teams: a managed subset plus externally-run participants, one room.

The durable ``managed_by_synchri`` split drives everything under test here:
subset launch and acknowledgment, activation held until every externally-run
participant agrees, supervision that stays alive through an external turn and
resumes by itself, and reconstruction of the same split from a fresh hydrate.
"""

from __future__ import annotations

import sys
import time

import pytest

from synchri.broker import Credential
from synchri.errors import ValidationError
from synchri.models.envelope import MessageDraft
from synchri.runner.managed import ManagedRunnerRegistry
from synchri.session.manager import SessionManager

from test_recovery import ACK_OK, _wait_phase
from test_ui import call, repo, ui  # noqa: F401 - fixtures by import


def _start_mixed_session(ui, repo, builder_command=None):
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [
            {"name": "builder", "runtime": "generic", "role": "primary_builder",
             "command": builder_command or "true {prompt}"},
            # No command: this participant is deliberately not launchable by
            # Synchri — it connects through its own setup prompt.
            {"name": "reviewer", "runtime": "generic", "role": "adversarial_reviewer"},
        ],
    })
    return started["session"]["session_id"]


def _wait(predicate, timeout=45, message="condition never held"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def test_readiness_distinguishes_any_from_all(ui, repo):
    session_id = _start_mixed_session(ui, repo)
    readiness = call(ui, f"/api/managed?session={session_id}")["readiness"]
    assert readiness["available"] is False, "the historical whole-roster meaning survives"
    assert readiness["any_available"] is True
    assert readiness["all_available"] is False


def test_the_managed_split_is_validated_and_durable(ui, repo):
    session_id = _start_mixed_session(ui, repo)
    manager = SessionManager(ui["broker"])

    # Absent flags are the legacy shape: everyone managed, nobody external.
    record = manager.get(session_id)
    assert [p.name for p in ManagedRunnerRegistry._managed_plans(record)] == [
        "builder", "reviewer",
    ]
    assert ManagedRunnerRegistry._external_names(record) == []

    with pytest.raises(ValidationError):
        manager.set_managed_participants(session_id, ["ghost"])
    with pytest.raises(ValidationError):
        manager.set_managed_participants(session_id, [])

    record = manager.set_managed_participants(session_id, ["builder"])
    assert [
        (p.name, bool((p.metadata or {}).get("managed_by_synchri")))
        for p in record.participants
    ] == [("builder", True), ("reviewer", False)]
    assert [p.name for p in ManagedRunnerRegistry._managed_plans(record)] == ["builder"]
    assert ManagedRunnerRegistry._external_names(record) == ["reviewer"]

    # A fresh hydrate reconstructs the same split — restart survival.
    assert [
        p.name for p in ManagedRunnerRegistry._managed_plans(manager.get(session_id))
    ] == ["builder"]

    # The split can widen back to everyone.
    record = manager.set_managed_participants(session_id, ["builder", "reviewer"])
    assert [p.name for p in ManagedRunnerRegistry._managed_plans(record)] == [
        "builder", "reviewer",
    ]
    assert ManagedRunnerRegistry._external_names(record) == []


def _mixed_builder(count_file):
    return (
        "import sys\nprompt = sys.argv[1]\n" + ACK_OK +
        f"with open({str(count_file)!r}, 'a') as handle:\n    handle.write('turn\\n')\n"
        f"turns = open({str(count_file)!r}).read().count('turn')\n"
        "if turns == 1:\n"
        "    print('First pass done; over to the reviewer.')\n"
        "    print('SYNCHRI-TO: reviewer')\n"
        "else:\n"
        "    print('Review received; wrapping up.')\n"
        "    print('SYNCHRI-PASS')\n"
    )


def test_a_mixed_team_launches_waits_and_resumes_around_external_turns(ui, repo, tmp_path):
    count = tmp_path / "turns.log"
    script = tmp_path / "mixed_builder.py"
    script.write_text(_mixed_builder(count), encoding="utf-8")
    session_id = _start_mixed_session(
        ui, repo, builder_command=f"{sys.executable} {script} {{prompt}}"
    )

    # Start only the managed subset; the split persists on the session.
    call(ui, "/api/managed/start", {"session": session_id, "participants": ["builder"]})
    managed = _wait_phase(ui, session_id, {"waiting_external_setup"})
    assert managed["reason"] == "awaiting_external"
    assert "reviewer" in managed["detail"]

    launch = call(ui, f"/api/launch?session={session_id}")["launch"]
    by_name = {agent["name"]: agent for agent in launch["agents"]}
    assert by_name["builder"]["managed_by_synchri"] is True
    assert by_name["builder"]["acknowledged"] is True, "the managed subset attached and agreed"
    assert by_name["reviewer"]["managed_by_synchri"] is False
    assert by_name["reviewer"]["joined"] is False
    assert launch["ready_to_activate"] is False, "externals gate activation"

    # The external reviewer joins through its own prompt and acknowledges.
    token = by_name["reviewer"]["join_command"].split(" join ", 1)[1].split(" --name", 1)[0]
    joined = ui["broker"].join(token, "reviewer")
    reviewer = Credential("reviewer", secret=joined["secret"])
    call(ui, "/api/ack", {"session": session_id, "participant": "reviewer", "reply": "UNDERSTOOD"})
    assert call(ui, f"/api/launch?session={session_id}")["launch"]["ready_to_activate"] is True

    # Begin collaboration: activation itself starts managed-subset supervision.
    call(ui, "/api/activate", {"session": session_id})

    # The builder speaks and hands to the external reviewer; supervision holds
    # as a live worker instead of ending the run.
    def waiting_external_turn():
        payload = call(ui, f"/api/managed?session={session_id}")["managed"]
        return payload if payload["phase"] == "waiting_external_turn" else None

    held = _wait(waiting_external_turn, message="supervision never held for the external turn")
    assert held["alive"] is True, "the keep-alive is a live worker, not an ended run"
    assert held["reason"] == "unmanaged_speaker"

    # The externally-run reviewer replies from its own terminal.
    room_id = call(ui, f"/api/session?session={session_id}")["room_id"]
    ui["broker"].send(room_id, credential=reviewer, draft=MessageDraft(
        content="Reviewed: proceed.", handoff_target="builder"))

    # Supervision resumes by itself: the builder's second turn runs without
    # any human press, passes, and the room idles.
    managed = _wait_phase(ui, session_id, {"waiting"})
    assert managed["reason"] == "idle"
    assert count.read_text().count("turn") >= 2, (
        "the managed agent resumed after the external turn"
    )
