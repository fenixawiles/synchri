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


def test_the_search_schema_bootstraps_idempotently(manager):
    names = {
        row["name"] for row in manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert "search_index" in names and "search_state" in names
    version = manager.conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    assert version == "8"


def test_search_finds_the_plan_from_a_paraphrase(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission(summary="first full draft"))
    result = deliberation.search(
        manager, "why is the cache bounded with an LRU eviction map?",
        session_id=record.session_id,
    )
    assert result["engine"] in {"fts5", "like"}
    assert result["tokens"], "the question produced searchable tokens"
    refs = [item["ref"] for item in result["evidence"]]
    assert any(ref.startswith("revision:") for ref in refs), (
        "the durable plan body answers a paraphrased question"
    )
    top = result["evidence"][0]
    assert top["layer"] in {"structured", "transcript", "telemetry"}
    assert top["excerpt"]


def test_search_survives_match_metacharacters(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    result = deliberation.search(
        manager, 'Why did we "change" the cache-eviction (LRU)? *really*?',
        session_id=record.session_id,
    )
    assert result["tokens"]
    assert any("revision:" in item["ref"] for item in result["evidence"])


def test_the_like_fallback_answers_too(manager, repo, monkeypatch):
    from synchri.storage import db as db_module

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    monkeypatch.setattr(db_module, "search_engine", lambda conn: "like")
    result = deliberation.search(
        manager, "bounded LRU eviction", session_id=record.session_id
    )
    assert result["engine"] == "like"
    assert any(item["ref"].startswith("revision:") for item in result["evidence"])


def test_mutable_sources_refresh_between_queries(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    _turn(manager, record, "codex", "SYNCHRI-OBJECTION: blocking|No rollback story")
    before = deliberation.search(
        manager, "zigzag rollback disposition", session_id=record.session_id
    )
    assert not any(
        "zigzag" in item["excerpt"] for item in before["evidence"]
    )
    _turn(manager, record, "claude",
          "SYNCHRI-PLAN-BEGIN\n" + PLAN_BODY + "\nSYNCHRI-PLAN-END\n"
          "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|added the zigzag rollback path\n"
          "SYNCHRI-PLAN-SUBMIT: rollback added")
    after = deliberation.search(
        manager, "zigzag rollback disposition", session_id=record.session_id
    )
    matches = [item for item in after["evidence"] if "zigzag" in item["excerpt"]]
    assert any(item["ref"] == "objection:OBJ-001" for item in matches), (
        "a disposition recorded after the first query is found by the next one"
    )


def test_global_search_spans_sessions(manager, repo):
    first, _ = _activated(manager, repo)
    second, _ = _activated(
        manager, repo,
        idea="Ship a telemetry exporter: flush latency histograms every minute.",
    )
    result = deliberation.search(manager, "telemetry exporter flush latency")
    assert result["evidence"], "an unscoped query reaches every session"
    assert {item["session_id"] for item in result["evidence"]} == {second.session_id}
    caches = deliberation.search(manager, "request-level caching idea")
    assert first.session_id in {item["session_id"] for item in caches["evidence"]}


def test_the_search_api_and_cli_round_trip(manager, repo, capsys):
    import argparse

    from synchri.cli.main import cmd_why
    from synchri.ui.api import Api

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    api = Api(manager.broker, manager)
    payload = api.history_search(
        {"session": record.session_id, "q": "bounded LRU map"}, {}
    )
    assert payload["evidence"] and payload["engine"] in {"fts5", "like"}

    args = argparse.Namespace(
        question="bounded LRU map", session=record.session_id, json=False
    )
    assert cmd_why(args, manager.broker) == 0
    printed = capsys.readouterr().out
    assert "revision:" in printed and "engine:" in printed


# ----------------------------------------------------------------------
# the historian: fail-closed grounding over the retrieved evidence
# ----------------------------------------------------------------------


def _retrieval(manager, record, question="why is the cache bounded with LRU eviction?"):
    return deliberation.search(manager, question, session_id=record.session_id)


def _grounded_reply(prompt):
    assert "THE EVIDENCE" in prompt and "[E1]" in prompt
    return "\n".join([
        "SUMMARY: The plan bounds the cache with an LRU map [E1].",
        "TRIGGER: The human asked for request-level caching [E1].",
        "POSITIONS: The planner proposed a bounded LRU module [E1].",
        "EVIDENCE: The acceptance criteria require bounded eviction [E1].",
        "RESOLUTION: not established",
        "SYNTHESIS: none",
        "UNRESOLVED: the record does not name who chose LRU over LFU.",
    ])


def test_a_cited_report_is_accepted_with_the_causal_contract(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    seen = {}

    def runner(prompt):
        seen["prompt"] = prompt
        return _grounded_reply(prompt)

    outcome = historian.report(
        manager.broker, manager, record,
        "why is the cache bounded?", _retrieval(manager, record), runner=runner,
    )
    assert outcome["fallback"] is False
    report = outcome["report"]
    assert report["layer"] == "synthesis"
    assert report["citations"] == [1]
    assert report["insufficient"] is False
    assert "LRU map [E1]" in report["sections"]["SUMMARY"]
    # The prompt itself carries the epistemic contract, verbatim commitments:
    prompt = seen["prompt"]
    assert "temporal proximity" in prompt
    assert "resolved by" in prompt, "the causal-language rule is stated"
    assert "INSUFFICIENT:" in prompt
    assert "why is the cache bounded?" in prompt


def test_uncited_synthesis_is_refused(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    outcome = historian.report(
        manager.broker, manager, record, "why bounded?",
        _retrieval(manager, record),
        runner=lambda prompt: (
            "SUMMARY: The agents clearly preferred LRU because it is standard.\n"
            "TRIGGER: not established\nPOSITIONS: none\nEVIDENCE: none\n"
            "RESOLUTION: none\nSYNTHESIS: none\nUNRESOLVED: none"
        ),
    )
    assert outcome["report"] is None and outcome["fallback"] is True
    assert "not grounded" in outcome["fallback_reason"]


def test_out_of_range_citations_are_refused(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    outcome = historian.report(
        manager.broker, manager, record, "why bounded?",
        _retrieval(manager, record),
        runner=lambda prompt: _grounded_reply(prompt).replace("[E1]", "[E99]"),
    )
    assert outcome["report"] is None and "not grounded" in outcome["fallback_reason"]


def test_honest_insufficiency_is_accepted(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    outcome = historian.report(
        manager.broker, manager, record, "why was the logo blue?",
        _retrieval(manager, record),
        runner=lambda prompt: (
            "INSUFFICIENT: the record contains no discussion of any logo."
        ),
    )
    assert outcome["fallback"] is False
    assert outcome["report"]["insufficient"] is True
    assert "no discussion" in outcome["report"]["sections"]["SUMMARY"]


def test_without_a_runtime_the_fallback_is_the_floor(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    outcome = historian.report(
        manager.broker, manager, record, "why bounded?", _retrieval(manager, record)
    )
    assert outcome["report"] is None and outcome["fallback"] is True
    assert "read-only" in outcome["fallback_reason"]


def test_empty_evidence_never_invokes_anything(manager, repo):
    from synchri.runner import historian

    record, _ = _activated(manager, repo)

    def exploding_runner(prompt):  # pragma: no cover - must not run
        raise AssertionError("no invocation without evidence")

    outcome = historian.report(
        manager.broker, manager, record, "why?",
        {"evidence": [], "events": [], "engine": "fts5", "tokens": []},
        runner=exploding_runner,
    )
    assert outcome["fallback"] is True
    assert "nothing in the recorded history" in outcome["fallback_reason"]


def test_the_ask_endpoint_keeps_the_evidence_floor(manager, repo):
    from synchri.ui.api import Api

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    api = Api(manager.broker, manager)
    payload = api.history_ask(
        {}, {"session": record.session_id, "question": "why is the cache bounded?"}
    )
    assert payload["evidence"], "the mechanical floor is always present"
    assert payload["report"] is None and payload["fallback"] is True
    assert payload["fallback_reason"]
    assert payload["question"] == "why is the cache bounded?"


def test_the_historian_usage_recorder_tags_its_origin(workspace, manager, repo):
    from types import SimpleNamespace

    from synchri.runner.ancillary import _UsageRecorder

    record, _ = _activated(manager, repo)
    recorder = _UsageRecorder(
        workspace, session_id=record.session_id, room_id=record.room_id,
        participant="historian", runtime="claude_code", drop_id=None,
        origin_kind="historian",
    )
    recorder.begin()
    recorder.finish(SimpleNamespace(usage={
        "model": "m", "input_tokens": 10, "output_tokens": 5,
        "cached_input_tokens": 0, "cost_usd": 0.01, "duration_seconds": 2.0,
    }))
    row = manager.conn.execute(
        "SELECT origin_kind, participant FROM agent_turn_usage WHERE session_id = ?",
        (record.session_id,),
    ).fetchone()
    assert row["origin_kind"] == "historian" and row["participant"] == "historian"


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
