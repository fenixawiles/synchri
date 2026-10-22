"""Audit probes for the Synchri source audit (not part of the repository).

Each probe is an executable demonstration of one audited property at the
audited commit. Probes import the repository's own fixtures and helpers and
never modify repository files. A passing probe DEMONSTRATES the behavior its
docstring names; several demonstrate gaps (behavior a preregistered study
would need to exclude or instrument).

Run from the repository root (outside the default testpaths, so the main
suite is unaffected):

    python -m pytest -q -p no:cacheprovider docs/audits/probes/test_audit_probes.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SYNCHRI_REPO") or Path(__file__).resolve().parents[3])
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from conftest import broker, workspace  # noqa: E402,F401 - fixtures
from test_session_startup import (  # noqa: E402,F401 - fixtures + helpers
    accept_all,
    agents,
    make_session,
    manager,
    repo,
)
from test_planning import IDEA, PLAN_BODY, _activated, _submission, _turn  # noqa: E402
from test_runner import write_agent  # noqa: E402

from synchri.broker import Credential  # noqa: E402
from synchri.models.envelope import MessageDraft  # noqa: E402
from synchri.runner.agent_command import AgentCommand, parse_directives  # noqa: E402
from synchri.runner.conductor import Conductor  # noqa: E402
from synchri.runner.stream_events import (  # noqa: E402
    KEEP_EVENTS_PER_ROOM,
    ClaudeStreamParser,
    CodexStreamParser,
)
from synchri.session import planning  # noqa: E402
from synchri.session.gates import Gate  # noqa: E402
from synchri.session.manager import SessionStatus  # noqa: E402
from synchri.storage import dao  # noqa: E402


def _all_db_text(db_path: Path) -> str:
    """Every TEXT value in every table, concatenated (for absence checks)."""
    conn = sqlite3.connect(str(db_path))
    try:
        chunks: list[str] = []
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_index%'"
        )]
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(v) for v in row if isinstance(v, str))
        return "\n".join(chunks)
    finally:
        conn.close()


def _events(manager, record) -> list[dict]:
    return manager.broker.events(
        record.room_id, credential=manager._human_credential(record)
    )["events"]


# ----------------------------------------------------------------------
# P1-P4: disagreement mechanics in Planning Mode
# ----------------------------------------------------------------------


def test_P1_reviewer_can_reach_plan_ready_with_zero_objections(manager, repo):
    """No minimum-challenge rule: approve on revision 1 with no objection."""
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert actions == [{"kind": "review", "verdict": "approve", "revision": 1}]
    assert warnings == []
    assert planning.objections(manager, record.session_id) == []
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"


def test_P2_reviewer_objection_is_accepted_before_any_draft_exists(manager, repo):
    """'The planner drafts first, alone' is enforced only for review closure;
    an objection at revision 0 is recorded, so reviewer ordering is a prompt
    rule, not a programmatic one."""
    record, _ = _activated(manager, repo)
    actions, warnings = _turn(
        manager, record, "codex", "SYNCHRI-OBJECTION: blocking|pre-emptive concern"
    )
    assert actions and actions[0]["kind"] == "objection"
    assert warnings == []
    objection = planning.objections(manager, record.session_id)[0]
    assert objection["raised_revision"] == 0


def test_P3_planner_resolves_a_blocking_objection_unilaterally(manager, repo):
    """Resolution needs no reviewer concurrence and no new revision; the
    reviewer's later approve is the only remaining check."""
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    _turn(manager, record, "codex",
          "SYNCHRI-OBJECTION: blocking|the plan is wrong\n"
          "SYNCHRI-PLAN-REVIEW: revise|fix it")
    actions, warnings = _turn(
        manager, record, "claude", "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|I disagree; no change"
    )
    assert actions == [{"kind": "resolved", "objection_id": "OBJ-001"}]
    objection = planning.objections(manager, record.session_id)[0]
    assert objection["status"] == "resolved" and objection["resolved_by"] == "claude"
    assert objection["resolved_revision"] == 1, "no new revision was required"
    assert planning.open_blockers(manager, record.session_id) == []


def test_P4_either_role_may_record_a_fork(manager, repo):
    """SYNCHRI-FORK has no role check (planning.py handle_turn)."""
    record, _ = _activated(manager, repo)
    actions, warnings = _turn(manager, record, "codex", "SYNCHRI-FORK: A vs B")
    assert actions and actions[0]["kind"] == "fork" and warnings == []


# ----------------------------------------------------------------------
# P5-P6: gate sign-off does not encode a verdict
# ----------------------------------------------------------------------


def test_P5_gate_completes_although_the_reviewer_reported_fail(manager, repo, agents):
    """Gate status is last-writer-wins and 'sign-off' is any non-empty
    assessment, so a reviewer FAIL is overridden by a later builder PASS."""
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.report_gate(record.session_id, "AUTH-01", actor="codex",
                        status="fail", assessment="BROKEN: login accepts any password")
    gate = manager.report_gate(record.session_id, "AUTH-01", actor="claude",
                               status="pass", assessment="works for me",
                               evidence=["src/auth.py"])
    assert gate.status == "pass"
    assert gate.reviewer_assessment.startswith("BROKEN")
    completed = manager.complete_by_agent(record.session_id, "claude")
    assert completed.status == SessionStatus.COMPLETE.value


def test_P6_gate_assessments_are_overwritten_not_versioned(manager, repo, agents):
    """Only the latest assessment survives on the row; the gate_updated
    event carries status and actor but not the assessment text."""
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.report_gate(record.session_id, "AUTH-01", actor="codex",
                        assessment="FIRST-ASSESSMENT-NEEDLE")
    manager.report_gate(record.session_id, "AUTH-01", actor="codex",
                        assessment="second assessment")
    gate = manager.gates(record.session_id)[0]
    assert gate.reviewer_assessment == "second assessment"
    assert "FIRST-ASSESSMENT-NEEDLE" not in _all_db_text(manager.broker.workspace.db_path)


# ----------------------------------------------------------------------
# P7-P8: turn routing, passing, and early termination
# ----------------------------------------------------------------------


def test_P7_long_horizon_reviewer_is_not_scheduled_after_the_builder_opening(
    manager, repo, agents
):
    """The activation task carries no human_direction marker, so the
    reviewer's first challenge depends on the builder handing off."""
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    manager.activate(record.session_id)
    opening = [m for m in manager.broker.read(
        record.room_id, credential=manager._human_credential(record))["messages"]
        if (m.get("metadata") or {}).get("source") == "session_activation"][-1]
    assert "human_direction" not in (opening.get("metadata") or {})

    status = manager.broker.room_status(
        record.room_id, credential=manager._human_credential(record))
    assert status["active_speaker"] == "claude"


def test_P7b_builder_reply_without_handoff_leaves_the_room_idle(broker, tmp_path):
    """With no handoff line, nobody is scheduled next; the conductor stops
    'idle' and that stop reason is not written to the durable event log."""
    from helpers import make_room

    room = make_room(broker, "claude", "codex", name="probe")
    room.broker.send(room.room_id, credential=room.credential("human"),
                     draft=MessageDraft(content="answer the question", target="claude",
                                        message_type="task"))
    agent = AgentCommand.parse(write_agent(tmp_path, "claude", "print('The answer is 42.')"),
                               timeout=30)
    conductor = Conductor(broker, room.room_id, {"claude": agent},
                          {"claude": room.credential("claude")}, room.credential("human"))
    report = conductor.run(max_turns=5)
    assert report.reason == "idle" and report.turn_count == 1
    types = {e["event_type"] for e in broker.events(
        room.room_id, credential=room.credential("human"))["events"]}
    assert not any("idle" in t or "conductor" in t for t in types)


def test_P8_a_reviewer_may_pass_with_no_content(broker, tmp_path):
    """SYNCHRI-PASS (or an empty reply) releases the floor with a recorded
    'pass' message; no challenge content is required."""
    from helpers import make_room

    room = make_room(broker, "claude", "codex", name="probe")
    room.broker.send(room.room_id, credential=room.credential("human"),
                     draft=MessageDraft(content="review this", target="codex",
                                        message_type="task"))
    agent = AgentCommand.parse(write_agent(tmp_path, "codex", "print('SYNCHRI-PASS')"),
                               timeout=30)
    conductor = Conductor(broker, room.room_id, {"codex": agent},
                          {"codex": room.credential("codex")}, room.credential("human"))
    report = conductor.run(max_turns=1)
    assert report.turns[0]["status"] == "passed"
    last = broker.read(room.room_id, credential=room.credential("human"))["messages"][-1]
    assert last["message_type"] == "pass"


# ----------------------------------------------------------------------
# P9-P10: what is (not) persisted for each turn
# ----------------------------------------------------------------------


def test_P9_prompts_and_raw_replies_are_not_persisted(broker, tmp_path):
    """The assembled prompt is never stored, and control lines are stripped
    from the stored message body (their parsed values land in typed columns)."""
    from helpers import make_room

    room = make_room(broker, "claude", "codex", name="probe")
    room.broker.send(room.room_id, credential=room.credential("human"),
                     draft=MessageDraft(content="go", target="claude", message_type="task"))
    script = (
        "print('Answer: 42')\n"
        "print('SYNCHRI-CONFIDENCE: 0.9')\n"
        "print('SYNCHRI-HANDOFF: codex')\n"
    )
    agent = AgentCommand.parse(write_agent(tmp_path, "claude", script), timeout=30)
    conductor = Conductor(broker, room.room_id, {"claude": agent},
                          {"claude": room.credential("claude")}, room.credential("human"),
                          role_guidance={"claude": "PROMPT-ONLY-NEEDLE-7731"})
    conductor.run(max_turns=1)
    message = [m for m in broker.read(room.room_id, credential=room.credential("human"))
               ["messages"] if m["sender"] == "claude"][-1]
    assert message["content"] == "Answer: 42"
    assert message["confidence"] == 0.9 and message["handoff_target"] == "codex"
    text = _all_db_text(broker.workspace.db_path)
    assert "PROMPT-ONLY-NEEDLE-7731" not in text, "the prompt is not persisted"
    assert "SYNCHRI-CONFIDENCE" not in text, "the raw reply is not persisted"


def test_P10_plan_block_is_kept_only_as_the_revision_body(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    assert planning.revision_body(manager, record.session_id, 1).strip() == PLAN_BODY.strip()


# ----------------------------------------------------------------------
# P11-P13: model identifiers and telemetry retention
# ----------------------------------------------------------------------


def test_P11_codex_stream_never_yields_a_model_identifier():
    parser = CodexStreamParser()
    for line in (
        {"type": "thread.started", "thread_id": "t1", "model": "gpt-x"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ):
        parser.feed(json.dumps(line) + "\n")
    assert parser.usage is not None and parser.usage["model"] is None


def test_P12_claude_model_is_taken_from_the_stream_init_event_only():
    parser = ClaudeStreamParser()
    for line in (
        {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "s"},
        {"type": "result", "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ):
        parser.feed(json.dumps(line) + "\n")
    assert parser.usage["model"] == "claude-x"
    plain = ClaudeStreamParser()
    plain.feed(json.dumps({"type": "result", "result": "ok", "usage": {}}) + "\n")
    assert plain.usage["model"] is None


def test_P13_stream_telemetry_is_pruned_per_room(broker):
    from helpers import make_room

    room = make_room(broker, "claude", name="probe")
    for index in range(KEEP_EVENTS_PER_ROOM + 50):
        dao.insert_stream_event(broker.conn, room.room_id, session_id=None,
                                participant="claude", invoke_key="inv", kind="status",
                                title=f"e{index}", detail="", file_path=None, payload={})
    removed = dao.prune_stream_events(broker.conn, room.room_id, keep=KEEP_EVENTS_PER_ROOM)
    assert removed == 50


def test_P14_session_audit_writes_fail_silently(manager, repo, agents, monkeypatch):
    record = make_session(manager, repo, agents)
    before = len(_events(manager, record))

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dao, "insert_event", boom)
    manager._log(record, "session.probe", {"x": 1})  # must not raise
    monkeypatch.undo()
    assert len(_events(manager, record)) == before


# ----------------------------------------------------------------------
# P15-P17: exposure of conditions and reference material to models
# ----------------------------------------------------------------------


def test_P15_contract_roster_discloses_each_participants_runtime(manager, repo, agents):
    record = make_session(manager, repo, agents)
    document = manager.issue_contract(record.session_id)
    assert "[claude_code]" in document.core_text and "[codex]" in document.core_text
    assert document.for_participant("codex").count("[claude_code]") == 1


def test_P16_the_idea_reaches_both_planning_agents_verbatim(manager, repo, tmp_path):
    secret = "REFERENCE-ANSWER: the capital is Canberra"
    record, credentials = _activated(manager, repo, idea=IDEA + "\n" + secret)
    agents_ = {name: AgentCommand.parse(write_agent(tmp_path, name, "print('x')"))
               for name in ("claude", "codex")}
    conductor = Conductor(manager.broker, record.room_id, agents_, credentials,
                          manager._human_credential(record), session_id=record.session_id)
    for name in ("claude", "codex"):
        assert secret in conductor.build_prompt(name, {}, agent=agents_[name])


def test_P17_managed_agents_are_told_where_synchri_state_lives(
    manager, repo, agents, workspace, tmp_path
):
    """The managed launcher exports SYNCHRI_HOME; an agent process running as
    the same OS user can open the database holding every session's records."""
    from synchri.runner.managed import ManagedRunnerRegistry

    record = make_session(manager, repo, agents)
    registry = ManagedRunnerRegistry(workspace)
    agent = registry._build_agent(record, record.participants[1], "true {prompt}")
    assert agent.env["SYNCHRI_HOME"] == str(workspace.home)
    reader = AgentCommand.parse(write_agent(tmp_path, "codex", (
        "import os, sqlite3\n"
        "db = os.path.join(os.environ['SYNCHRI_HOME'], 'synchri.db')\n"
        "rows = sqlite3.connect(db).execute('SELECT COUNT(*) FROM sessions').fetchone()\n"
        "print('sessions visible:', rows[0])\n"
    )), timeout=30)
    reader.env.update(agent.env)
    result = reader.invoke("")
    assert result.ok and "sessions visible: 1" in result.stdout


def test_P18_a_process_with_state_access_can_post_as_the_human(manager, repo, agents, workspace):
    """The human's room secret is stored in sessions.metadata; any process
    that can open the database can speak (and interrupt) as the human."""
    from synchri.broker import Broker

    record = make_session(manager, repo, agents)
    conn = sqlite3.connect(str(workspace.db_path))
    metadata = json.loads(conn.execute(
        "SELECT metadata FROM sessions WHERE session_id = ?", (record.session_id,)
    ).fetchone()[0])
    conn.close()
    human = metadata["human"]
    outsider = Broker(workspace)
    try:
        sent = outsider.send(
            record.room_id,
            credential=Credential(participant=human["name"], secret=human["secret"]),
            draft=MessageDraft(content="FORGED human direction", message_type="chat"),
        )
    finally:
        outsider.close()
    assert sent["message"]["sender"] == human["name"]


def test_P19_all_participants_share_one_worktree(manager, repo, agents, workspace):
    """One worktree per session, used as cwd for every participant — not an
    isolated worktree per agent."""
    from synchri.runner.managed import ManagedRunnerRegistry

    record = make_session(manager, repo, agents)
    registry = ManagedRunnerRegistry(workspace)
    cwds = {registry._build_agent(record, plan, "true {prompt}").cwd
            for plan in record.participants}
    assert cwds == {record.worktree_path}
