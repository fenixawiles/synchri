"""The deliberative provenance index.

Invariants under test:

* the timeline is derived, ordered organization of the durable record —
  every entry points back into real rows (message, event, objection,
  revision, gate, drop, escalation) and resolves;
* entries carry their provenance layer, and the derived index never invents
  a stratum it did not read from;
* deterministic evidence (test runs) and repository observations are
  recorded as durable events without ever entering the timeline as prose;
* residual concerns surface exactly at the thresholds the spec names.
"""

from __future__ import annotations

from synchri.broker import Credential
from synchri.models.envelope import MessageDraft
from synchri.protocol import events as ev
from synchri.session import deliberation, dropbox, planning
from synchri.storage import dao

from test_planning import (  # noqa: F401 - fixtures by import
    PLAN_BODY,
    _activated,
    _ready,
    _submission,
    _turn,
    manager,
    repo,
)
from test_session_startup import accept_all, agents, make_session  # noqa: F401


def _resolve(manager, record, entry) -> None:
    """Every ref must open a real underlying row."""
    refs = entry["refs"]
    assert refs, f"entry carries no refs: {entry}"
    if "objection_id" in refs:
        assert any(
            o["objection_id"] == refs["objection_id"]
            for o in planning.objections(manager, record.session_id)
        )
    if "revision" in refs and entry["kind"] in {"proposal", "revision"}:
        assert planning.revision_body(manager, record.session_id, refs["revision"])
    if "message_id" in refs:
        assert dao.get_message(manager.conn, record.room_id, refs["message_id"]) is not None
    if "event_seq" in refs:
        assert any(
            event.seq == refs["event_seq"]
            for event in dao.list_events(manager.conn, record.room_id)
        )
    if "gate_id" in refs:
        assert any(g.gate_id == refs["gate_id"] for g in manager.gates(record.session_id))
    if "drop_id" in refs:
        assert dropbox.item(manager, record.session_id, refs["drop_id"])
    if "escalation_id" in refs:
        row = manager.conn.execute(
            "SELECT 1 FROM session_escalations WHERE escalation_id = ?",
            (refs["escalation_id"],),
        ).fetchone()
        assert row is not None


def test_the_timeline_indexes_the_planning_loop(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission(summary="first full draft"))
    _turn(manager, record, "codex",
          "SYNCHRI-OBJECTION: blocking|Step 2 lands before the cache is bounded\n"
          "SYNCHRI-PLAN-REVIEW: revise|fix the ordering")
    _turn(manager, record, "claude",
          "SYNCHRI-PLAN-BEGIN\n" + PLAN_BODY + "\nSYNCHRI-PLAN-END\n"
          "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|reordered so bounding lands first\n"
          "SYNCHRI-PLAN-SUBMIT: reordered per review")
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|executable as ordered")

    record = manager.get(record.session_id)
    events = deliberation.timeline(manager, record)
    kinds = [entry["kind"] for entry in events]
    # The loop's shape, in order: proposal, objection against it, the
    # revision, its resolution, and review closure.
    for expected in ("proposal", "objection", "revision", "resolution", "acceptance"):
        assert expected in kinds
    assert kinds.index("proposal") < kinds.index("objection") < kinds.index("revision")
    assert kinds.index("resolution") < kinds.index("acceptance")
    for entry in events:
        assert entry["layer"] in {
            deliberation.LAYER_TRANSCRIPT, deliberation.LAYER_STRUCTURED,
            deliberation.LAYER_DETERMINISTIC, deliberation.LAYER_REPO,
            deliberation.LAYER_TELEMETRY,
        }, "the derived index never claims the synthesis layer"
        assert entry["kind"] in deliberation.KINDS
        _resolve(manager, record, entry)

    # kinds filtering narrows without reordering.
    only = deliberation.timeline(manager, record, kinds=["objection", "resolution"])
    assert {entry["kind"] for entry in only} == {"objection", "resolution"}


def test_exhaustion_approval_and_residuals_surface(manager, repo):
    record, _ = _ready(manager, repo)
    _turn(manager, record, "codex", "SYNCHRI-OBJECTION: nonblocking|Assumes one process")
    for _ in range(planning.TURN_BUDGET):
        planning.count_turn(manager, record)
    planning.approve(manager, record.session_id)

    record = manager.get(record.session_id)
    events = deliberation.timeline(manager, record)
    kinds = [entry["kind"] for entry in events]
    assert "phase_block" in kinds, "budget exhaustion is a block on the record"
    assert "phase_completion" in kinds, "promotion closes the phase"
    residuals = [entry for entry in events if entry["kind"] == "residual_concern"]
    assert any(
        entry["refs"].get("objection_id") == "OBJ-001" for entry in residuals
    ), "an objection open at approval is a residual concern"
    for entry in events:
        _resolve(manager, record, entry)


def test_blocked_turns_and_approval_requests_index_from_the_transcript(manager, repo):
    record, credentials = _activated(manager, repo)
    manager.broker.send(
        record.room_id,
        credential=credentials["claude"],
        draft=MessageDraft(
            content="I need a human decision before installing anything.",
            response_status="blocked",
            metadata={"approval_request": "Approve installing the profiler dependency",
                      "approval_capability": "repo.install_deps"},
        ),
    )
    events = deliberation.timeline(manager, manager.get(record.session_id))
    questions = [entry for entry in events if entry["kind"] == "question"]
    blocks = [entry for entry in events if entry["kind"] == "phase_block"]
    assert any("Approve installing" in entry["summary"] for entry in questions)
    assert any(entry["refs"].get("message_id") for entry in blocks)
    for entry in questions + blocks:
        assert entry["layer"] == deliberation.LAYER_TRANSCRIPT
        _resolve(manager, record, entry)


def test_test_runs_become_deterministic_timeline_evidence(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.run_tests(record.session_id)
    logged = [
        event for event in dao.list_events(manager.conn, record.room_id)
        if event.event_type == ev.SESSION_TESTS_RUN
    ]
    assert logged and "green" in logged[0].payload
    events = deliberation.timeline(manager, manager.get(record.session_id))
    runs = [
        entry for entry in events
        if entry["layer"] == deliberation.LAYER_DETERMINISTIC
    ]
    assert runs, "a test run is timeline-addressable evidence"
    assert runs[0]["kind"] in {"test_result", "failure"}
    _resolve(manager, record, runs[0])


def test_repo_observations_are_recorded_but_never_narrated(manager, repo, agents, workspace):
    from synchri.runner.managed import ManagedRunnerRegistry

    record = make_session(manager, repo, agents)
    registry = ManagedRunnerRegistry(workspace)
    registry._observe_turn(
        manager, record, "agent.returned",
        {"participant": "claude", "returncode": 0, "timed_out": False, "cancelled": False},
    )
    observed = [
        event for event in dao.list_events(manager.conn, record.room_id)
        if event.event_type == ev.SESSION_REPO_OBSERVED
    ]
    assert observed and observed[0].payload.get("head")
    assert "changed_files" in observed[0].payload
    # Correlation data, not deliberation: the timeline stays free of it.
    events = deliberation.timeline(manager, manager.get(record.session_id))
    assert all(
        entry["refs"].get("event_seq") != observed[0].seq for entry in events
    )


def test_the_timeline_api_serves_events_and_kinds(manager, repo):
    from synchri.ui.api import Api

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    api = Api(manager.broker, manager)
    payload = api.history_timeline({"session": record.session_id}, {})
    assert payload["events"], "an active planning session has a deliberative record"
    assert "proposal" in payload["kinds"]
    filtered = api.history_timeline(
        {"session": record.session_id, "kinds": "proposal"}, {}
    )
    assert {entry["kind"] for entry in filtered["events"]} == {"proposal"}
