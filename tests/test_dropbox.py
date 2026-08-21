"""The side-task dropbox: capture, the exactly-once appendix, evaluation,
and the completion phase machine.

The invariants under test, in code terms:

* capture never interrupts the room, is bounded, and freezes at the cutoff
* reconciliation appends each item exactly once, in capture order
* the spec's bytes and contract are never touched — the appendix renders after them
* a valid completion with items advances the phase instead of closing the room
* every snapshot item needs both main-role evaluations over one immutable digest
* both approve → an extension gate materialized atomically; any decline → declined
* a failed scout is declined on its report and never blocks closing
* only ``closing`` invokes the terminal room-closing behavior
* artifact bounds fail closed: oversized patches are not stored, secrets never persisted
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from synchri.broker import Credential
from synchri.errors import ConflictError, StateError, ValidationError
from synchri.runner.agent_command import AgentCommand, parse_directives
from synchri.runner.conductor import Conductor
from synchri.runner.managed import ManagedRunnerRegistry
from synchri.session import dropbox
from synchri.session.contract import ACK_TOKEN
from synchri.session.manager import SessionManager

from test_runner import write_agent
from test_session_startup import accept_all, agents, make_session, manager  # noqa: F401
from test_ui import _active, call, repo, ui  # noqa: F401 - fixtures by import


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _activated(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    return manager.activate(record.session_id)


def _activated_with_credentials(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    credentials = {}
    for invite in record.metadata["invites"]:
        joined = manager.broker.join(invite["token"], invite["participant_name"])
        credentials[joined["name"]] = Credential(joined["name"], secret=joined["secret"])
    for plan in record.participants:
        manager.acknowledge(record.session_id, plan.name, ACK_TOKEN)
    return manager.activate(record.session_id), credentials


def _pass_main_gates(manager, session_id):
    for gate in manager.gates(session_id):
        if gate.origin_kind != "main" or gate.is_satisfied:
            continue
        _pass_gate(manager, session_id, gate.gate_id)


def _pass_gate(manager, session_id, gate_id):
    manager.update_gate(
        session_id, gate_id, actor="human", status="pass",
        evidence=["verified in the worktree"],
        builder_assessment="done", reviewer_assessment="verified",
    )


def _proposed_item(
    manager, session_id, *, prompt="Investigate the retry backoff jitter",
    patch=None, skip_review=False, evidence="retry.py:42 sleeps a constant 5s",
    rationale="every client retries in lockstep after a recovery",
    recommendation="add +/-20% jitter to the retry sleep",
):
    item = dropbox.capture(manager, session_id, prompt, skip_review=skip_review)
    drop_id = item["drop_id"]
    dropbox.begin_investigation(
        manager, session_id, drop_id,
        base_sha="a" * 40, scratch_branch=f"synchri/drop/x/{drop_id}",
    )
    item = dropbox.deposit_proposal(
        manager, session_id, drop_id,
        evidence=evidence, rationale=rationale, recommendation=recommendation,
        patch=patch,
    )
    if not skip_review:
        item = dropbox.record_review(
            manager, session_id, drop_id,
            verdict="hold", critique="verify the p95 latency first",
        )
    return item


def _wake_messages(manager, record, source):
    payload = manager.broker.read(
        record.room_id, credential=manager._human_credential(record)
    )
    return [
        message for message in payload["messages"]
        if (message.get("metadata") or {}).get("source") == source
    ]


# ----------------------------------------------------------------------
# capture and the exactly-once appendix
# ----------------------------------------------------------------------


def test_capture_assigns_ids_and_never_touches_the_room_or_the_appendix(manager, repo, agents):
    record = _activated(manager, repo, agents)
    before = manager.broker.room_status(
        record.room_id, credential=manager._human_credential(record)
    )

    first = dropbox.capture(manager, record.session_id, "Cache the token refresh")
    second = dropbox.capture(
        manager, record.session_id, "The retry logger double-prints", title="Retry log noise"
    )
    assert (first["drop_id"], second["drop_id"]) == ("DROP-001", "DROP-002")
    assert second["title"] == "Retry log noise"
    assert first["status"] == "captured" and first["disposition"] is None
    # A raw capture is not yet ready: the appendix holds investigated items.
    assert first["appendix_position"] is None
    assert second["appendix_position"] is None

    after = manager.broker.room_status(
        record.room_id, credential=manager._human_credential(record)
    )
    assert after["queue"] == before["queue"]
    assert after["active_turn"] == before["active_turn"]

    # The canonical contract and spec are untouched: no new revision, no digest change.
    refreshed = manager.get(record.session_id)
    assert refreshed.contract_revision == record.contract_revision
    assert refreshed.spec.digest == record.spec.digest


def test_capture_is_bounded_and_requires_text(manager, repo, agents):
    record = _activated(manager, repo, agents)
    with pytest.raises(ValidationError):
        dropbox.capture(manager, record.session_id, "   ")
    with pytest.raises(ValidationError) as exc:
        dropbox.capture(manager, record.session_id, "x" * (dropbox.MAX_PROMPT_CHARS + 1))
    assert "capped" in str(exc.value)


def test_reconciliation_appends_only_ready_items_exactly_once(manager, repo, agents):
    record = _activated(manager, repo, agents)
    # A ready row whose readiness transition raced a crash (proposal stored,
    # append never committed) still lands in the appendix exactly once; an
    # item still under capture never does.
    manager.conn.execute(
        "INSERT INTO session_drops (session_id, drop_id, title, prompt, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (record.session_id, "DROP-001", "Ready", "Investigated already", "proposed", "t", "t"),
    )
    dropbox.capture(manager, record.session_id, "A raw second idea")
    assert dropbox.item(manager, record.session_id, "DROP-001")["appendix_position"] == 1
    assert dropbox.reconcile(manager, record.session_id) == []
    assert dropbox.item(manager, record.session_id, "DROP-002")["appendix_position"] is None

    # The second item becomes ready via its failure report and joins next.
    dropbox.begin_investigation(manager, record.session_id, "DROP-002", base_sha="e" * 40)
    dropbox.mark_failed(manager, record.session_id, "DROP-002", report="scout died")
    rows = manager.conn.execute(
        "SELECT drop_id, position FROM session_appendix WHERE session_id = ? ORDER BY position",
        (record.session_id,),
    ).fetchall()
    assert [(row["drop_id"], row["position"]) for row in rows] == [
        ("DROP-001", 1), ("DROP-002", 2),
    ]


def test_observe_turn_reconciles_after_each_completed_turn(manager, repo, agents, workspace):
    record = _activated(manager, repo, agents)
    registry = ManagedRunnerRegistry(workspace)
    manager.conn.execute(
        "INSERT INTO session_drops (session_id, drop_id, title, prompt, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (record.session_id, "DROP-001", "Orphan", "Ready but unreconciled", "proposed", "t", "t"),
    )
    # A failed turn reconciles nothing; a completed one appends the item.
    registry._observe_turn(
        manager, record, "agent.returned",
        {"participant": "claude", "returncode": 1, "timed_out": False, "cancelled": False},
    )
    assert dropbox.item(manager, record.session_id, "DROP-001")["appendix_position"] is None
    registry._observe_turn(
        manager, record, "agent.returned",
        {"participant": "claude", "returncode": 0, "timed_out": False, "cancelled": False},
    )
    assert dropbox.item(manager, record.session_id, "DROP-001")["appendix_position"] == 1


# ----------------------------------------------------------------------
# the completion phase machine
# ----------------------------------------------------------------------


def test_completion_without_side_tasks_closes_exactly_as_before(manager, repo, agents):
    record = _activated(manager, repo, agents)
    _pass_main_gates(manager, record.session_id)
    completed = manager.complete(record.session_id)
    assert completed.status == "complete"
    assert completed.phase == "closing"
    assert completed.ended_reason == "all acceptance gates satisfied"
    with pytest.raises(StateError):
        dropbox.capture(manager, record.session_id, "too late")


def test_a_valid_completion_with_items_freezes_capture_and_begins_evaluation(manager, repo, agents):
    record = _activated(manager, repo, agents)
    _proposed_item(manager, record.session_id)
    dropbox.capture(manager, record.session_id, "Second idea, never investigated")
    _pass_main_gates(manager, record.session_id)

    advanced = manager.complete(record.session_id)
    assert advanced.status == "active", "a phase transition must not close the session"
    assert advanced.phase == "appendix_evaluation"
    snapshot = advanced.metadata["dropbox_snapshot"]
    assert snapshot["items"] == ["DROP-001", "DROP-002"]

    # Capture and reconciliation are frozen from this exact transition on.
    with pytest.raises(StateError) as exc:
        dropbox.capture(manager, record.session_id, "a third idea")
    assert exc.value.code == "dropbox_frozen"
    assert dropbox.reconcile(manager, record.session_id) == []

    # The cutoff ended the uninvestigated item with a declinable report.
    second = dropbox.item(manager, record.session_id, "DROP-002")
    assert second["status"] == "timed_out"
    assert "completion cutoff" in second["failure_report"]
    first = dropbox.item(manager, record.session_id, "DROP-001")
    assert first["status"] == "proposed"

    # The transition woke the pair: the conductor exits on idle, so the wake
    # task is what restarts the loop, with the lead-then-review discipline.
    wakes = _wake_messages(manager, record, "appendix_evaluation")
    assert len(wakes) == 1
    assert wakes[0]["target"] == "claude"
    assert wakes[0]["metadata"]["human_direction"] == {"lead": "claude", "reviewer": "codex"}
    assert "DROP-001, DROP-002" in wakes[0]["content"]

    # Completing again lists exactly what is still owed.
    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert exc.value.code == "appendix_unevaluated"
    assert "DROP-001" in exc.value.message and "DROP-002" in exc.value.message


def test_evaluation_is_phase_scoped_role_scoped_and_needs_a_rationale(manager, repo, agents):
    record = _activated(manager, repo, agents)
    item = _proposed_item(manager, record.session_id)
    with pytest.raises(StateError) as exc:
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="claude", verdict="approve", rationale="early",
        )
    assert exc.value.code == "not_in_evaluation"

    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)
    with pytest.raises(ValidationError):
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="stranger", verdict="approve", rationale="who am I",
        )
    with pytest.raises(ValidationError):
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="claude", verdict="approve", rationale="   ",
        )
    with pytest.raises(ValidationError):
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="claude", verdict="maybe", rationale="uncertain",
        )


def test_both_approvals_materialize_an_extension_gate_and_extension_work_runs(manager, repo, agents):
    record = _activated(manager, repo, agents)
    item = _proposed_item(manager, record.session_id)
    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)

    first = dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="claude", verdict="approve", rationale="Small, well evidenced, worth doing.",
    )
    assert first["disposition"] is None, "one evaluation must not decide"
    settled = dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="codex", verdict="approve", rationale="The review's concern was addressed.",
    )
    assert settled["disposition"] == "approved"
    assert settled["extension_id"] == "EXT-001"
    assert settled["status"] == "evaluated"
    # Both judged the same immutable proposal.
    digests = {
        settled["evaluations"][slot]["proposal_digest"] for slot in ("builder", "reviewer")
    }
    assert digests == {settled["proposal_digest"]}
    assert settled["evaluations"]["builder"]["participant"] == "claude"
    assert settled["evaluations"]["reviewer"]["participant"] == "codex"

    gate = next(g for g in manager.gates(record.session_id) if g.gate_id == "EXT-001")
    assert gate.origin_kind == "extension"
    assert gate.drop_id == "DROP-001" and gate.extension_id == "EXT-001"
    assert gate.required and gate.status == "pending"

    advanced = manager.complete(record.session_id)
    assert advanced.status == "active" and advanced.phase == "extension_work"
    wakes = _wake_messages(manager, record, "extension_work")
    assert len(wakes) == 1 and "EXT-001" in wakes[0]["content"]

    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert exc.value.code == "gates_unsatisfied"
    assert "EXT-001" in exc.value.message

    _pass_gate(manager, record.session_id, "EXT-001")
    completed = manager.complete(record.session_id)
    assert completed.status == "complete" and completed.phase == "closing"


def test_any_non_approval_declines_with_both_rationales_retained(manager, repo, agents):
    record = _activated(manager, repo, agents)
    item = _proposed_item(manager, record.session_id)
    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)

    dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="claude", verdict="approve", rationale="I would take it.",
    )
    settled = dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="codex", verdict="decline", rationale="The patch lacks tests.",
    )
    assert settled["disposition"] == "declined"
    assert settled["evaluations"]["builder"]["rationale"] == "I would take it."
    assert settled["evaluations"]["reviewer"]["rationale"] == "The patch lacks tests."
    assert settled["extension_id"] is None
    assert not any(g.origin_kind == "extension" for g in manager.gates(record.session_id))

    with pytest.raises(ConflictError):
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="claude", verdict="approve", rationale="changed my mind",
        )

    completed = manager.complete(record.session_id)
    assert completed.status == "complete" and completed.phase == "closing"


def test_a_failed_scout_is_declined_on_its_report_and_never_blocks_closing(manager, repo, agents):
    record = _activated(manager, repo, agents)
    item = dropbox.capture(manager, record.session_id, "Chase the flaky socket test")
    dropbox.begin_investigation(manager, record.session_id, item["drop_id"], base_sha="b" * 40)
    dropbox.mark_failed(
        manager, record.session_id, item["drop_id"],
        kind="timed_out", report="the scout exhausted its 15 minute budget",
    )
    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)

    with pytest.raises(ValidationError) as exc:
        dropbox.evaluate(
            manager, record.session_id, item["drop_id"],
            actor="claude", verdict="approve", rationale="approve anyway",
        )
    assert "failure report" in str(exc.value)

    dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="claude", verdict="decline", rationale="Not worth a retry now.",
    )
    dropbox.evaluate(
        manager, record.session_id, item["drop_id"],
        actor="codex", verdict="decline", rationale="Agreed; the report shows a dead end.",
    )
    completed = manager.complete(record.session_id)
    assert completed.status == "complete"
    final = dropbox.item(manager, record.session_id, item["drop_id"])
    assert final["status"] == "timed_out" and final["disposition"] == "declined"


def test_force_completion_waives_open_items_explicitly(manager, repo, agents):
    record = _activated(manager, repo, agents)
    _proposed_item(manager, record.session_id)
    dropbox.capture(manager, record.session_id, "Never investigated")
    # Gates deliberately unmet: force is the human override for both.
    completed = manager.complete(record.session_id, force=True)
    assert completed.status == "complete" and completed.phase == "closing"
    assert "waived" in completed.ended_reason
    for entry in dropbox.items(manager, record.session_id):
        assert entry["disposition"] == "waived"
        assert entry["evaluations"]["human"]["verdict"] == "waived"


# ----------------------------------------------------------------------
# artifact bounds: fail closed
# ----------------------------------------------------------------------


def test_an_oversized_patch_is_not_stored_only_its_preview_and_note(manager, repo, agents):
    record = _activated(manager, repo, agents)
    big = "+ line\n" * ((dropbox.MAX_PATCH_CHARS // 7) + 10)
    item = _proposed_item(manager, record.session_id, patch=big, skip_review=True)
    proposal = item["proposal"]
    assert proposal["patch"] is None
    assert proposal["patch_preview"] and len(proposal["patch_preview"]) <= dropbox.PATCH_PREVIEW_CHARS
    assert "scratch branch" in proposal["patch_note"]
    assert item["proposal_digest"]


def test_a_detected_secret_quarantines_the_patch_and_is_never_persisted(manager, repo, agents):
    record = _activated(manager, repo, agents)
    token = "ghp_" + "a1B2" * 8
    item = _proposed_item(
        manager, record.session_id,
        patch=f"+ AUTH_TOKEN = '{token}'\n", skip_review=True,
    )
    proposal = item["proposal"]
    assert proposal["patch"] is None and proposal["patch_preview"] is None
    assert "quarantined" in proposal["patch_note"]
    assert "patch" in proposal["withheld"]
    row = manager.conn.execute(
        "SELECT * FROM session_drops WHERE session_id = ? AND drop_id = ?",
        (record.session_id, item["drop_id"]),
    ).fetchone()
    assert token not in json.dumps({key: row[key] for key in row.keys()}, default=str)


def test_failure_reports_and_critiques_are_screened_fail_closed(manager, repo, agents):
    """Raw CLI output tails and reviewer critiques land in the completion
    package — a secret in either is withheld at the storage boundary."""
    record = _activated(manager, repo, agents)
    token = "ghp_" + "z9Y8" * 8

    leaky = dropbox.capture(manager, record.session_id, "leaky failure")
    dropbox.begin_investigation(manager, record.session_id, leaky["drop_id"], base_sha="f" * 40)
    failed = dropbox.mark_failed(
        manager, record.session_id, leaky["drop_id"],
        report=f"the scout printed: export TOKEN={token}",
    )
    assert token not in (failed["failure_report"] or "")
    assert "withheld" in failed["failure_report"]

    chatty = dropbox.capture(manager, record.session_id, "leaky review")
    dropbox.begin_investigation(manager, record.session_id, chatty["drop_id"], base_sha="f" * 40)
    dropbox.deposit_proposal(
        manager, record.session_id, chatty["drop_id"],
        evidence="e", rationale="r", recommendation="adopt",
    )
    reviewed = dropbox.record_review(
        manager, record.session_id, chatty["drop_id"],
        verdict="unsound", critique=f"the diff hardcodes {token} in config.py",
    )
    assert token not in json.dumps(reviewed["review"])
    row = manager.conn.execute(
        "SELECT * FROM session_drops WHERE session_id = ?", (record.session_id,)
    ).fetchall()
    assert token not in json.dumps([dict(r) for r in row], default=str)


def test_oversized_text_artifacts_are_truncated_with_a_visible_mark(manager, repo, agents):
    record = _activated(manager, repo, agents)
    item = _proposed_item(
        manager, record.session_id,
        evidence="e" * (dropbox.MAX_TEXT_CHARS + 500), skip_review=True,
    )
    evidence = item["proposal"]["evidence"]
    assert len(evidence) <= dropbox.MAX_TEXT_CHARS
    assert evidence.endswith("[synchri: truncated at the artifact bound]")


# ----------------------------------------------------------------------
# the SYNCHRI-DROP directive and the conducted evaluation round
# ----------------------------------------------------------------------


def test_drop_directive_parsing_normalizes_and_warns():
    body, directives = parse_directives(
        "Judged both items.\n"
        "SYNCHRI-DROP: drop-001|Approved|Low risk, well evidenced\n"
        "SYNCHRI-DROP: DROP-002 | REJECTED | No tests\n"
        "SYNCHRI-DROP: DROP-003|maybe|unsure\n"
        "SYNCHRI-DROP: |approve|missing id\n"
    )
    assert body == "Judged both items."
    assert [(e.drop_id, e.verdict, e.rationale) for e in directives.drop_evaluations] == [
        ("DROP-001", "approve", "Low risk, well evidenced"),
        ("DROP-002", "decline", "No tests"),
    ]
    assert len(directives.warnings) == 2


def test_agents_record_their_evaluations_through_the_conductor(manager, repo, agents, tmp_path):
    record, credentials = _activated_with_credentials(manager, repo, agents)
    item = _proposed_item(manager, record.session_id)
    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)
    assert manager.get(record.session_id).phase == "appendix_evaluation"

    scripted = {
        "claude": (
            "print('Evaluated the appendix item; approving.')\n"
            "print('SYNCHRI-DROP: DROP-001|approve|Small and well evidenced')\n"
        ),
        "codex": (
            "print('Reviewed the proposal; declining.')\n"
            "print('SYNCHRI-DROP: DROP-001|decline|The patch still lacks tests')\n"
        ),
    }
    commands = {
        name: AgentCommand.parse(write_agent(tmp_path, name, body), timeout=30)
        for name, body in scripted.items()
    }
    conductor = Conductor(
        manager.broker,
        record.room_id,
        commands,
        {name: credentials[name] for name in commands},
        manager._human_credential(record),
        session_id=record.session_id,
    )
    conductor.run(max_turns=8)

    settled = dropbox.item(manager, record.session_id, item["drop_id"])
    assert settled["disposition"] == "declined"
    assert settled["evaluations"]["builder"]["participant"] == "claude"
    assert settled["evaluations"]["reviewer"]["participant"] == "codex"


def test_build_prompt_renders_the_appendix_by_phase(manager, repo, agents, tmp_path):
    record, credentials = _activated_with_credentials(manager, repo, agents)
    _proposed_item(manager, record.session_id)
    command = AgentCommand.parse(write_agent(tmp_path, "claude", "print('hi')\n"), timeout=30)
    conductor = Conductor(
        manager.broker,
        record.room_id,
        {"claude": command},
        {"claude": credentials["claude"]},
        manager._human_credential(record),
        session_id=record.session_id,
    )
    prompt = conductor.build_prompt("claude", {}, agent=command)
    assert "appendix: side-task dropbox" in prompt
    assert "do not act on them now" in prompt
    assert "SYNCHRI-DROP" not in prompt, "the directive is offered only during evaluation"

    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)
    prompt = conductor.build_prompt("claude", {}, agent=command)
    assert "SYNCHRI-DROP: <id>|approve|<your rationale>" in prompt
    assert "proposal digest:" in prompt

    for actor, verdict in (("claude", "approve"), ("codex", "approve")):
        dropbox.evaluate(
            manager, record.session_id, "DROP-001",
            actor=actor, verdict=verdict, rationale="worth doing",
        )
    manager.complete(record.session_id)
    prompt = conductor.build_prompt("claude", {}, agent=command)
    assert "approved extensions" in prompt and "EXT-001" in prompt


# ----------------------------------------------------------------------
# completion-package provenance
# ----------------------------------------------------------------------


def test_the_completion_package_carries_the_dropbox_record_and_split_usage(manager, repo, agents):
    import io
    import zipfile

    from synchri.session import package as package_module
    from synchri.storage import dao

    record = _activated(manager, repo, agents)
    first = _proposed_item(manager, record.session_id)
    second = dropbox.capture(manager, record.session_id, "Chase the flaky socket test")
    dropbox.begin_investigation(manager, record.session_id, second["drop_id"], base_sha="d" * 40)
    dropbox.mark_failed(
        manager, record.session_id, second["drop_id"],
        kind="failed", report="the scout crashed",
    )
    dao.insert_turn_usage(
        manager.conn, session_id=record.session_id, room_id=record.room_id,
        participant="claude", runtime="claude_code", input_tokens=100, output_tokens=50,
    )
    dao.insert_turn_usage(
        manager.conn, session_id=record.session_id, room_id=record.room_id,
        participant="claude", runtime="claude_code", input_tokens=7, output_tokens=3,
        origin_kind="ancillary", drop_id=first["drop_id"],
    )
    _pass_main_gates(manager, record.session_id)
    manager.complete(record.session_id)
    for actor in ("claude", "codex"):
        dropbox.evaluate(manager, record.session_id, first["drop_id"],
                         actor=actor, verdict="approve", rationale="worth doing")
        dropbox.evaluate(manager, record.session_id, second["drop_id"],
                         actor=actor, verdict="decline", rationale="dead end")
    manager.complete(record.session_id)
    _pass_gate(manager, record.session_id, "EXT-001")
    assert manager.complete(record.session_id).status == "complete"

    _filename, data = package_module.build(manager.broker, manager, record.session_id)
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = set(archive.namelist())
    assert {"dropbox.md", "dropbox.json"} <= names

    dropbox_md = archive.read("dropbox.md").decode()
    assert "DROP-001" in dropbox_md and "Extension: EXT-001" in dropbox_md
    assert "declined" in dropbox_md and "the scout crashed" in dropbox_md
    assert "Evaluation (builder: claude" in dropbox_md

    assert "origin: extension of DROP-001" in archive.read("gates.md").decode()
    assert "2 captured — 1 approved as extensions, 1 declined" in archive.read("session.md").decode()

    usage = json.loads(archive.read("usage.json").decode())
    assert set(usage) == {"main", "ancillary"}
    assert usage["main"][0]["input_tokens"] == 100
    assert usage["ancillary"][0]["drop_id"] == "DROP-001"
    summary = archive.read("usage-summary.md").decode()
    assert "Ancillary usage" in summary and "Total (main room)" in summary


# ----------------------------------------------------------------------
# the UI API
# ----------------------------------------------------------------------


class _PhaseRegistry:
    """Observes whether a completion cancelled the team or resumed it."""

    def __init__(self):
        self.resumed = []
        self.cancelled = []

    def resume(self, record):
        self.resumed.append(record.session_id)
        return {"phase": "resuming"}

    def cancel(self, session_id, reason=""):
        self.cancelled.append(session_id)


def test_api_capture_and_inbox(ui, repo):
    session_id = _active(ui, repo)
    payload = call(ui, "/api/dropbox/capture",
                   {"session": session_id, "prompt": "Try caching the token refresh"})
    assert payload["item"]["drop_id"] == "DROP-001"
    assert payload["item"]["appendix_position"] is None, (
        "an item joins the appendix when its investigation is ready, not at capture"
    )

    inbox = call(ui, f"/api/dropbox?session={session_id}")
    assert inbox["phase"] == "original_work" and inbox["frozen"] is False
    assert [entry["drop_id"] for entry in inbox["items"]] == ["DROP-001"]
    assert inbox["summary"]["total"] == 1 and inbox["summary"]["open"] == 1

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/dropbox/capture", {"session": session_id, "prompt": "x" * 5000})
    assert exc.value.code == 400


def test_the_app_ships_the_dropbox_inbox():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert '"gates","dropbox"' in source, "the inbox is a first-class session tab"
    assert 'api("dropbox" + q)' in source
    assert "dropbox/capture" in source
    assert "Add a side task" in source
    assert "Skip the adversarial review" in source
    assert "Capture is frozen" in source


def test_api_completion_resumes_the_team_on_a_phase_transition(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/dropbox/capture", {"session": session_id, "prompt": "Cache the token"})
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass"})
    registry = _PhaseRegistry()
    ui["server"].api.managed = registry

    dashboard = call(ui, "/api/control", {"session": session_id, "action": "complete"})
    assert dashboard["session"]["phase"] == "appendix_evaluation"
    assert dashboard["session"]["status"] == "active"
    assert registry.resumed == [session_id]
    assert registry.cancelled == [], "a phase transition must not stop the agents"
    assert dashboard["dropbox"]["items"][0]["status"] == "timed_out"

    manager = SessionManager(ui["broker"])
    for actor in ("claude", "codex"):
        dropbox.evaluate(
            manager, session_id, "DROP-001",
            actor=actor, verdict="decline", rationale="No investigation arrived.",
        )
    dashboard = call(ui, "/api/control", {"session": session_id, "action": "complete"})
    assert dashboard["session"]["status"] == "complete"
    assert dashboard["session"]["phase"] == "closing"
    assert registry.cancelled == [session_id]
