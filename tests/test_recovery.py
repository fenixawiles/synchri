"""Failure discrimination and the participant-scoped recovery ladder.

Five failure kinds get five distinct responses; crash/unresponsive walk
resume → relaunch → one automatic replacement → human, while auth failures
and provider refusals go straight to the human. Restarting one agent never
touches another agent's process.
"""

from __future__ import annotations

import os
import sys
import time

from synchri.runner import doctor, recovery
from synchri.runner.agent_command import AgentResult
from synchri.session.manager import SessionManager

from helpers import make_room
from test_runner import conductor_for
from test_ui import call, repo, ui  # noqa: F401 - fixtures by import


def _result(returncode=1, stderr="", timed_out=False, cancelled=False):
    return AgentResult("x", "", stderr, returncode, timed_out=timed_out, cancelled=cancelled)


def test_classification_reads_the_invocation_honestly():
    assert recovery.classify_failure(_result(stderr="Please run /login first")) == recovery.AUTH_FAILURE
    assert recovery.classify_failure(_result(stderr="429: rate limit reached")) == recovery.PROVIDER_REFUSAL
    assert recovery.classify_failure(_result(returncode=3, stderr="segfault")) == recovery.CRASH
    assert recovery.classify_failure(_result(returncode=None, timed_out=True)) == recovery.UNRESPONSIVE
    assert recovery.classify_failure(_result(returncode=0, stderr="")) == recovery.NORMAL_STOP
    assert recovery.classify_failure(_result(cancelled=True)) == recovery.NORMAL_STOP


def test_an_auth_failure_breaks_immediately_not_after_two_strikes(broker, tmp_path):
    """Retrying an expired sign-in only burns invocations."""
    room = make_room(broker, "claude", "codex")
    room.send("human", "Go.", target="claude")
    conductor = conductor_for(room, tmp_path, {
        "claude": "sys.stderr.write('not logged in')\nsys.exit(1)\n",
        "codex": "print('SYNCHRI-PASS')\n",
    })
    report = conductor.run(max_turns=6)
    assert report.reason == "agent_failed"
    assert report.failed_participant == "claude"
    assert report.failure_kind == recovery.AUTH_FAILURE
    assert len([t for t in report.turns if t.get("status") == "failed"]) == 1


def test_a_provider_refusal_breaks_immediately_with_its_own_kind(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("human", "Go.", target="claude")
    conductor = conductor_for(room, tmp_path, {
        "claude": "sys.stderr.write('quota exhausted for today')\nsys.exit(2)\n",
        "codex": "print('SYNCHRI-PASS')\n",
    })
    report = conductor.run(max_turns=6)
    assert report.failure_kind == recovery.PROVIDER_REFUSAL
    assert len([t for t in report.turns if t.get("status") == "failed"]) == 1


def test_crashes_still_take_two_strikes(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send(
        "human", "Go.", target="claude",
        metadata={"human_direction": {"lead": "claude", "reviewer": "codex"}},
    )
    conductor = conductor_for(room, tmp_path, {
        "claude": "sys.stderr.write('boom')\nsys.exit(3)\n",
        "codex": "print('Builder, please retry.')\nprint('SYNCHRI-TO: claude')\n",
    })
    report = conductor.run(max_turns=10)
    assert report.failure_kind == recovery.CRASH
    assert len([t for t in report.turns if t.get("status") == "failed"]) == 2


def test_the_breaker_seeds_from_durable_failure_counts(broker, tmp_path):
    """A strike before an idle stop still counts when supervision resumes."""
    room = make_room(broker, "claude", "codex")
    room.send("human", "Go.", target="claude")
    conductor = conductor_for(room, tmp_path, {
        "claude": "sys.stderr.write('boom')\nsys.exit(3)\n",
        "codex": "print('SYNCHRI-PASS')\n",
    }, initial_failures={"claude": 1})
    report = conductor.run(max_turns=6)
    assert report.reason == "agent_failed"
    assert report.failed_participant == "claude"
    assert len([t for t in report.turns if t.get("status") == "failed"]) == 1


# ----------------------------------------------------------------------
# the ladder, end to end through the managed registry
# ----------------------------------------------------------------------

ACK_OK = "if 'output exactly UNDERSTOOD' in prompt:\n    print('UNDERSTOOD')\n    raise SystemExit(0)\n"

PASSING_REVIEWER = (
    "import sys\nprompt = sys.argv[1]\n" + ACK_OK + "print('Reviewed.')\nprint('SYNCHRI-PASS')\n"
)


def _crashing_builder(count_file, crashes):
    return (
        "import sys\nprompt = sys.argv[1]\n" + ACK_OK +
        f"with open({str(count_file)!r}, 'a') as handle:\n    handle.write('turn\\n')\n"
        f"turns = open({str(count_file)!r}).read().count('turn')\n"
        f"if turns <= {crashes}:\n"
        "    sys.stderr.write('segmentation fault')\n"
        "    raise SystemExit(9)\n"
        "print('Recovered and finished the step.')\nprint('SYNCHRI-PASS')\n"
    )


def _start_pair(ui, repo, tmp_path, builder_body, reviewer_body=PASSING_REVIEWER):
    builder_script = tmp_path / "builder.py"
    reviewer_script = tmp_path / "reviewer.py"
    builder_script.write_text(builder_body, encoding="utf-8")
    reviewer_script.write_text(reviewer_body, encoding="utf-8")
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [{
            "name": "builder", "runtime": "generic", "role": "primary_builder",
            "command": f"{sys.executable} {builder_script} {{prompt}}",
        }, {
            "name": "reviewer", "runtime": "generic", "role": "adversarial_reviewer",
            "command": f"{sys.executable} {reviewer_script} {{prompt}}",
        }],
    })
    session_id = started["session"]["session_id"]
    call(ui, "/api/managed/start", {"session": session_id})
    return session_id


def _wait_phase(ui, session_id, phases, timeout=45):
    deadline = time.monotonic() + timeout
    managed = {}
    while time.monotonic() < deadline:
        managed = call(ui, f"/api/managed?session={session_id}")["managed"]
        if managed["phase"] in phases and not managed["alive"]:
            return managed
        time.sleep(0.05)
    raise AssertionError(f"managed run never reached {phases}: {managed}")


def test_a_crashing_agent_gets_one_replacement_then_recovers(ui, repo, tmp_path):
    count = tmp_path / "turns.log"
    session_id = _start_pair(ui, repo, tmp_path, _crashing_builder(count, crashes=2))
    # The opening turn crashes once (strike 1, durable) and the room idles.
    _wait_phase(ui, session_id, {"waiting"})
    # A human direction resumes supervision; the durable strike still counts,
    # so the second crash trips the breaker and the ladder replaces the agent.
    call(ui, "/api/message", {"session": session_id, "content": "Keep going until it works."})
    managed = _wait_phase(ui, session_id, {"waiting"})
    assert managed["reason"] == "idle"

    manager = SessionManager(ui["broker"])
    states = manager.participant_states(session_id)
    assert states["builder"]["state"] == "active"
    assert states["builder"]["failures"] == 0
    recovery_state = manager.participant_recovery(session_id, "builder")
    assert recovery_state["metadata"].get("auto_replaced")
    assert recovery_state["recovery_generation"] >= 1
    assert manager.open_escalations(session_id) == []
    # Two crashed invocations plus the replacement's successful one.
    assert count.read_text().count("turn") == 3
    conversation = call(ui, f"/api/conversation?session={session_id}")["messages"]
    assert any(
        (m.get("metadata") or {}).get("source") == "agent_replacement" for m in conversation
    ), "the replacement must be handed its turn, not left waiting in an idle room"


BOUNCING_REVIEWER = (
    "import sys\nprompt = sys.argv[1]\n" + ACK_OK +
    "print('Builder, please continue.')\nprint('SYNCHRI-TO: builder')\n"
)


def test_a_persistently_crashing_agent_is_dropped_after_its_replacement(ui, repo, tmp_path):
    count = tmp_path / "turns.log"
    session_id = _start_pair(
        ui, repo, tmp_path, _crashing_builder(count, crashes=99), BOUNCING_REVIEWER
    )
    _wait_phase(ui, session_id, {"waiting"})
    call(ui, "/api/message", {"session": session_id, "content": "Keep going until it works."})
    managed = _wait_phase(ui, session_id, {"needs_attention"})
    assert managed["reason"] == "agent_failed"

    manager = SessionManager(ui["broker"])
    states = manager.participant_states(session_id)
    assert states["builder"]["state"] == "dropped"
    assert "replacement" in states["builder"]["detail"]
    escalations = manager.open_escalations(session_id)
    assert [e["rule"] for e in escalations] == ["agent_failed"]
    assert "even after an automatic replacement" in escalations[0]["detail"]
    # Two strikes, one automatic replacement, two more strikes: four in total.
    assert count.read_text().count("turn") == 4


def test_an_auth_failure_escalates_without_burning_retries(ui, repo, tmp_path):
    count = tmp_path / "turns.log"
    builder = (
        "import sys\nprompt = sys.argv[1]\n" + ACK_OK +
        f"with open({str(count)!r}, 'a') as handle:\n    handle.write('turn\\n')\n"
        "sys.stderr.write('not logged in — please run /login')\n"
        "raise SystemExit(1)\n"
    )
    session_id = _start_pair(ui, repo, tmp_path, builder)
    managed = _wait_phase(ui, session_id, {"needs_attention"})
    assert managed["reason"] == "agent_auth_failed"

    manager = SessionManager(ui["broker"])
    states = manager.participant_states(session_id)
    assert states["builder"]["state"] == "auth_failure"
    escalations = manager.open_escalations(session_id)
    assert [e["rule"] for e in escalations] == ["agent_auth_failed"]
    assert "Sign in" in escalations[0]["detail"]
    assert count.read_text().count("turn") == 1

    # Restart resolves the auth escalation and bumps the recovery generation.
    call(ui, "/api/control", {
        "session": session_id, "action": "restart_agent", "participant": "builder",
    })
    assert manager.open_escalations(session_id) == []
    assert manager.participant_recovery(session_id, "builder")["recovery_generation"] >= 1


def test_restarting_one_agent_leaves_the_other_agents_process_untouched(ui, repo, tmp_path):
    pid_file = tmp_path / "builder.pid"
    builder = (
        "import os, sys, time\nprompt = sys.argv[1]\n" + ACK_OK +
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(4)\n"
        "print('Slept and finished cleanly.')\nprint('SYNCHRI-PASS')\n"
    )
    session_id = _start_pair(ui, repo, tmp_path, builder)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    assert pid_file.exists(), "the builder's turn never started"
    builder_pid = int(pid_file.read_text())

    # Restart the *reviewer* while the builder's invocation is live.
    call(ui, "/api/control", {
        "session": session_id, "action": "restart_agent", "participant": "reviewer",
    })
    os.kill(builder_pid, 0)  # raises if the builder's process was killed

    managed = _wait_phase(ui, session_id, {"waiting"})
    assert managed["reason"] == "idle"
    conversation = call(ui, f"/api/conversation?session={session_id}")["messages"]
    assert any("Slept and finished cleanly." in (m.get("content") or "") for m in conversation), (
        "the builder's in-flight turn must complete, not be cancelled by the reviewer restart"
    )


def test_the_resume_rung_uses_the_stored_provider_session_id(ui, repo, tmp_path, monkeypatch):
    from synchri.session.modes import KNOWN_RUNTIMES

    script = tmp_path / "fakeclaude.py"
    script.write_text("print('unused')\n", encoding="utf-8")
    base = f"{sys.executable} {script}"
    monkeypatch.setitem(KNOWN_RUNTIMES, "fakeclaude", {
        "label": "Fake Claude", "default_name": "Fake",
        "executable": sys.executable,
        "managed_command": base + " --stream {prompt}",
        "plain_managed_command": base + " {prompt}",
        "stream_format": "claude",
        "resume_command": base + " --stream --resume {resume_id} {prompt}",
    })
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [
            {"name": "builder", "runtime": "fakeclaude", "role": "primary_builder"},
            {"name": "reviewer", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })
    session_id = started["session"]["session_id"]
    manager = SessionManager(ui["broker"])
    record = manager.get(session_id)
    plan = next(p for p in record.participants if p.name == "builder")
    registry = ui["server"].api.managed

    doctor._save_connection(ui["broker"].conn, {
        "runtime": "fakeclaude", "state": "connected", "executable_path": sys.executable,
        "version": "1.0.0", "adapter_revision": "x", "auth_indication": None,
        "resume": doctor.RESUME_UNVERIFIED, "checks": [], "detail": "connected",
    })
    manager.record_participant_failure(session_id, "builder", "crash", "the last turn crashed")
    manager.set_participant_resume_id(session_id, "builder", "sess-abc")

    agent = registry._turn_agent(manager, record, plan)
    assert "--resume" in agent.argv
    assert "sess-abc" in agent.argv

    after = manager.participant_recovery(session_id, "builder")
    assert after["resume_id"] is None, "the stored id is consumed so a failed resume cannot loop"
    assert after["recovery_generation"] == 1

    # A completed resumed invocation is the proof the doctor could not make:
    # supported_unverified upgrades to verified.
    registry._observe_turn(manager, record, "agent.returned", {
        "participant": "builder", "returncode": 0, "timed_out": False, "cancelled": False,
    })
    assert doctor.load_connection(ui["broker"].conn, "fakeclaude")["resume"] == doctor.RESUME_VERIFIED

    # With the failure cleared and no stored id, the next turn runs plain.
    plain = registry._turn_agent(manager, record, plan)
    assert "--resume" not in plain.argv


def test_a_runtime_that_recorded_resume_unsupported_never_attempts_it(
    ui, repo, tmp_path, monkeypatch
):
    from synchri.session.modes import KNOWN_RUNTIMES

    script = tmp_path / "fakeclaude.py"
    script.write_text("print('unused')\n", encoding="utf-8")
    base = f"{sys.executable} {script}"
    monkeypatch.setitem(KNOWN_RUNTIMES, "fakeclaude", {
        "label": "Fake Claude", "default_name": "Fake",
        "executable": sys.executable,
        "managed_command": base + " --stream {prompt}",
        "stream_format": "claude",
        "resume_command": base + " --stream --resume {resume_id} {prompt}",
    })
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [
            {"name": "builder", "runtime": "fakeclaude", "role": "primary_builder"},
            {"name": "reviewer", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })
    session_id = started["session"]["session_id"]
    manager = SessionManager(ui["broker"])
    record = manager.get(session_id)
    plan = next(p for p in record.participants if p.name == "builder")
    registry = ui["server"].api.managed

    doctor._save_connection(ui["broker"].conn, {
        "runtime": "fakeclaude", "state": "connected", "executable_path": sys.executable,
        "version": "1.0.0", "adapter_revision": "x", "auth_indication": None,
        "resume": doctor.RESUME_UNSUPPORTED, "checks": [], "detail": "connected",
    })
    manager.record_participant_failure(session_id, "builder", "crash", "crashed")
    manager.set_participant_resume_id(session_id, "builder", "sess-abc")

    agent = registry._turn_agent(manager, record, plan)
    assert "--resume" not in agent.argv
