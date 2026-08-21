"""Planning Mode: the workspace, the review loop, budgets, and the artifact.

The invariants under test, in code terms:

* Planning Mode is offered only on runtimes declaring the workspace capability
* the workspace is a disposable remote-less clone anchored to inspection_sha
* the planner drafts first; the reviewer only ever reviews
* PLAN-READY requires review closure: open blocking items and forks prevent it
* a human edit or a captured consideration invalidates PLAN-READY
* budgets end the loop mechanically, with exactly one user-authorized resume
* the workspace's Git state is verified and restored after every turn
* acceptance criteria parse deterministically from their explicit section
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from synchri.errors import StateError, ValidationError
from synchri.runner.agent_command import AgentCommand, parse_directives
from synchri.runner.conductor import Conductor
from synchri.session import dropbox, planning
from synchri.session.contract import ACK_TOKEN
from synchri.session.manager import SessionManager
from synchri.session.modes import (
    ParticipantPlan,
    list_modes,
    planning_workspace_supported,
    runtime_status,
)
from synchri.session.spec import ProductSpec
from synchri.broker import Credential

from test_runner import write_agent
from test_session_startup import manager, repo  # noqa: F401 - fixtures by import

IDEA = (
    "I want request-level caching for the API: repeated identical GET requests "
    "within a short window should be served from memory. Keep it simple."
)


def _plans() -> list[ParticipantPlan]:
    return [
        ParticipantPlan("claude", "claude_code", "planner"),
        ParticipantPlan("codex", "codex", "plan_reviewer"),
    ]


def _planning_session(manager, repo, idea=IDEA):
    return manager.create(
        name="Plan the cache",
        mode="planning",
        repo_root=str(repo),
        participants=_plans(),
        metadata={"idea": idea},
    )


def _activated(manager, repo, idea=IDEA):
    record = _planning_session(manager, repo, idea)
    manager.issue_contract(record.session_id)
    credentials = {}
    for invite in record.metadata["invites"]:
        joined = manager.broker.join(invite["token"], invite["participant_name"])
        credentials[joined["name"]] = Credential(joined["name"], secret=joined["secret"])
    for plan in record.participants:
        manager.acknowledge(record.session_id, plan.name, ACK_TOKEN)
    return manager.activate(record.session_id), credentials


PLAN_BODY = """# Implementation plan

1. Add a cache module with a bounded LRU map.
2. Wire it into the GET handler behind a flag.

## Preservation
- Existing uncached behavior stays the default.

## Acceptance criteria
- CACHE-01: repeated identical GETs within the window are served from memory
- CACHE-02: the cache is bounded and evicts LRU entries
"""


def _write_plan(record, body=PLAN_BODY):
    from pathlib import Path

    workspace = Path(record.planning_workspace["path"])
    (workspace / planning.PLAN_FILENAME).write_text(body, encoding="utf-8")


def _turn(manager, record, actor, text):
    """One agent turn's trailing control lines, through the real parser."""
    _body, directives = parse_directives(text)
    warnings: list[str] = []
    actions = planning.handle_turn(manager, record, actor, directives, warnings)
    return actions, warnings


def _wake_messages(manager, record, source):
    payload = manager.broker.read(
        record.room_id, credential=manager._human_credential(record)
    )
    return [
        message for message in payload["messages"]
        if (message.get("metadata") or {}).get("source") == source
    ]


# ----------------------------------------------------------------------
# mode, capability, and the workspace
# ----------------------------------------------------------------------


def test_planning_is_offered_and_capability_gated():
    modes = {entry["mode"]: entry for entry in list_modes()}
    assert "planning" in modes and modes["planning"]["planning"] is True
    assert planning_workspace_supported("claude_code") is True
    assert planning_workspace_supported("codex") is True
    assert planning_workspace_supported("generic") is False
    assert runtime_status("generic")["planning_supported"] is False


def test_a_planning_session_gets_a_disposable_remoteless_clone(manager, repo):
    from pathlib import Path

    record = _planning_session(manager, repo)
    assert record.worktree is None and record.spec is None
    workspace = record.planning_workspace
    path = Path(workspace["path"])
    assert path.exists() and (path / "README.md").exists()

    repo_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert workspace["inspection_sha"] == repo_head

    remotes = subprocess.run(
        ["git", "-C", str(path), "remote"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remotes == "", "the clone must hold no path back to the repository"

    plan = planning.get_plan(manager, record.session_id)
    assert plan["plan_id"] == "PLAN-001"
    assert plan["status"] == "drafting"
    assert plan["idea"] == IDEA
    assert plan["inspection_sha"] == repo_head
    assert manager.gates(record.session_id) == []

    manager.stop(record.session_id)
    manager.delete_session(record.session_id)
    assert not path.exists(), "the workspace is disposable — deletion always removes it"


def test_planning_refuses_the_unsupported_and_the_unarticulated(manager, repo):
    with pytest.raises(ValidationError) as exc:
        manager.create(
            name="x", mode="planning", repo_root=str(repo),
            participants=[
                ParticipantPlan("a", "generic", "planner"),
                ParticipantPlan("b", "generic", "plan_reviewer"),
            ],
            metadata={"idea": IDEA},
        )
    assert "read-only planning workspace" in str(exc.value)
    with pytest.raises(ValidationError):
        manager.create(
            name="x", mode="planning", repo_root=str(repo),
            participants=_plans(), metadata={},
        )
    with pytest.raises(ValidationError) as exc:
        manager.create(
            name="x", mode="planning", repo_root=str(repo),
            participants=_plans(), metadata={"idea": IDEA},
            spec=ProductSpec(text="a spec"),
        )
    assert "no product specification" in str(exc.value)


def test_the_contract_authorizes_the_planning_workspace(manager, repo):
    record = _planning_session(manager, repo)
    document = manager.issue_contract(record.session_id)
    assert record.planning_workspace["path"] in document.core_text
    assert "Planning" in document.core_text
    assert "planner" in document.role_sections["claude"].lower()
    assert "adversarial plan reviewer" in document.role_sections["codex"].lower()


# ----------------------------------------------------------------------
# the review loop
# ----------------------------------------------------------------------


def test_the_loop_planner_first_review_revision_ready(manager, repo):
    record, _credentials = _activated(manager, repo)
    session_id = record.session_id

    # The opening task went to the planner alone — the reviewer waits.
    opening = _wake_messages(manager, record, "session_activation")
    assert opening and opening[-1]["target"] == "claude"
    assert "PLAN-DRAFT revision 1" in opening[-1]["content"]

    # The reviewer cannot review nothing, and cannot submit.
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert any("no submitted revision" in w for w in warnings)
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-SUBMIT: sneaky")
    assert any("only the planner submits" in w for w in warnings)

    # Submit requires the plan file to exist.
    _actions, warnings = _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: draft")
    assert any("PLAN.md" in w for w in warnings)

    _write_plan(record)
    actions, warnings = _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: first full draft")
    assert actions == [{"kind": "submitted", "revision": 1}]
    plan = planning.get_plan(manager, session_id)
    assert plan["status"] == "awaiting_review" and plan["revision"] == 1
    review_wakes = _wake_messages(manager, record, "plan_review")
    assert review_wakes and review_wakes[-1]["target"] == "codex"

    # The reviewer raises findings; a blocking one prevents PLAN-READY.
    actions, warnings = _turn(
        manager, record, "codex",
        "SYNCHRI-OBJECTION: blocking|Step 2 lands before the cache is bounded\n"
        "SYNCHRI-OBJECTION: nonblocking|Assumes single-process deployment\n"
        "SYNCHRI-PLAN-REVIEW: approve|otherwise fine",
    )
    assert any("approval refused" in w for w in warnings)
    plan = planning.get_plan(manager, session_id)
    assert plan["status"] == "awaiting_review", "a refused approval changes nothing"

    _actions, _warnings = _turn(
        manager, record, "codex", "SYNCHRI-PLAN-REVIEW: revise|fix the ordering"
    )
    plan = planning.get_plan(manager, session_id)
    assert plan["status"] == "under_revision"
    revision_wakes = _wake_messages(manager, record, "plan_revision")
    assert revision_wakes and revision_wakes[-1]["target"] == "claude"
    assert "OBJ-001" in revision_wakes[-1]["content"]

    # Only the planner records dispositions; only the reviewer objects.
    _actions, warnings = _turn(
        manager, record, "codex", "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|self-serve"
    )
    assert any("only the planner" in w for w in warnings)
    _actions, warnings = _turn(
        manager, record, "claude", "SYNCHRI-OBJECTION: blocking|planner objecting"
    )
    assert any("only the plan reviewer" in w for w in warnings)

    _write_plan(record, PLAN_BODY.replace("2. Wire", "2. Bound the cache first, then wire"))
    actions, _warnings = _turn(
        manager, record, "claude",
        "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|reordered so bounding lands first\n"
        "SYNCHRI-PLAN-SUBMIT: reordered per review",
    )
    assert {a["kind"] for a in actions} == {"resolved", "submitted"}
    objections = {o["objection_id"]: o for o in planning.objections(manager, session_id)}
    assert objections["OBJ-001"]["status"] == "resolved"
    assert objections["OBJ-001"]["resolved_revision"] == 2
    assert objections["OBJ-002"]["status"] == "open"

    _actions, warnings = _turn(
        manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|ordering fixed; assumption noted"
    )
    assert not any("refused" in w for w in warnings)
    payload = planning.payload(manager, session_id)
    assert payload["ready"] is True and payload["status"] == "ready"
    assert payload["open_blockers"] == []
    assert [o["objection_id"] for o in payload["open_objections"]] == ["OBJ-002"], (
        "nonblocking assumptions may remain, clearly classified"
    )
    assert [c["gate_id"] for c in payload["acceptance_criteria"]] == ["CACHE-01", "CACHE-02"]


def test_an_open_fork_prevents_ready_until_resolved(manager, repo):
    record, _ = _activated(manager, repo)
    _write_plan(record)
    _turn(manager, record, "claude",
          "SYNCHRI-FORK: cache in-process vs sidecar; both defensible\n"
          "SYNCHRI-PLAN-SUBMIT: draft with an open fork")
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert any("OBJ-001 (fork)" in w for w in warnings)
    _turn(manager, record, "claude",
          "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|human chose in-process\n"
          "SYNCHRI-PLAN-SUBMIT: fork resolved")
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|closed")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"


def test_ready_is_invalidated_by_a_human_edit_and_by_a_new_consideration(manager, repo):
    record, _ = _activated(manager, repo)
    _write_plan(record)
    _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: draft")
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"

    planning.reopen(manager, record.session_id, "Also cover HEAD requests.")
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "under_revision" and plan["ready_at"] is None
    wakes = _wake_messages(manager, record, "plan_reopened")
    assert wakes and "HEAD requests" in wakes[-1]["content"]

    _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: covers HEAD")
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"

    dropbox.capture(manager, record.session_id, "Consider ETag support while at it")
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "under_revision", (
        "a consideration promoted into planning invalidates PLAN-READY"
    )
    wakes = _wake_messages(manager, record, "plan_consideration")
    assert wakes and "DROP-001" in wakes[-1]["content"]


def test_a_human_waiver_goes_back_through_review(manager, repo):
    record, _ = _activated(manager, repo)
    _write_plan(record)
    _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: draft")
    _turn(manager, record, "codex",
          "SYNCHRI-OBJECTION: blocking|No rollback story\n"
          "SYNCHRI-PLAN-REVIEW: revise|add rollback")
    planning.waive_objection(
        manager, record.session_id, "OBJ-001", "Accepted risk for this prototype."
    )
    objection = planning.objections(manager, record.session_id)[0]
    assert objection["status"] == "waived" and objection["resolved_by"] == "human"
    assert planning.get_plan(manager, record.session_id)["status"] == "awaiting_review", (
        "a waiver never skips re-review"
    )
    wakes = _wake_messages(manager, record, "plan_waiver_review")
    assert wakes and wakes[-1]["target"] == "codex"
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|waiver on the record")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"


# ----------------------------------------------------------------------
# budgets
# ----------------------------------------------------------------------


def test_turn_budget_exhaustion_goes_to_the_human_with_one_resume(manager, repo):
    record, _ = _activated(manager, repo)
    for _ in range(planning.TURN_BUDGET):
        planning.count_turn(manager, record)
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "needs_human_resolution"
    assert any(
        e["rule"] == "planning_budget_exhausted"
        for e in manager.open_escalations(record.session_id)
    )
    _actions, warnings = _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: late")
    assert any("needs_human_resolution" in w for w in warnings)

    payload = planning.resume_budget(manager, record.session_id)
    assert payload["status"] == "under_revision"
    assert payload["budgets"]["turn_budget"] == planning.TURN_BUDGET * 2
    assert not any(
        e["rule"] == "planning_budget_exhausted"
        for e in manager.open_escalations(record.session_id)
    )
    for _ in range(planning.TURN_BUDGET):
        planning.count_turn(manager, record)
    assert planning.get_plan(manager, record.session_id)["status"] == "needs_human_resolution"
    with pytest.raises(StateError) as exc:
        planning.resume_budget(manager, record.session_id)
    assert exc.value.code == "plan_resume_spent"


def test_the_revision_budget_preserves_the_last_draft(manager, repo):
    record, _ = _activated(manager, repo)
    _write_plan(record)
    for index in range(planning.REVISION_BUDGET):
        _actions, _warnings = _turn(
            manager, record, "claude", f"SYNCHRI-PLAN-SUBMIT: revision {index + 1}"
        )
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "needs_human_resolution"
    assert plan["revision"] == planning.REVISION_BUDGET, "the last draft is preserved"
    assert planning.revision_body(manager, record.session_id, plan["revision"])


# ----------------------------------------------------------------------
# workspace verification
# ----------------------------------------------------------------------


def test_workspace_git_state_is_verified_and_restored(manager, repo):
    from pathlib import Path

    record, _ = _activated(manager, repo)
    workspace = Path(record.planning_workspace["path"])
    _write_plan(record)  # notes and drafts are the point of the analysis area
    assert planning.verify_workspace(manager, record) is True

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
           "PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "sneaky"], check=True, env=env)
    assert planning.verify_workspace(manager, record) is False
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == record.planning_workspace["inspection_sha"], "HEAD restored to the baseline"
    assert any(
        e["rule"] == "planning_workspace_mutated"
        for e in manager.open_escalations(record.session_id)
    )


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------


def test_acceptance_criteria_parse_deterministically():
    assert planning.parse_acceptance_criteria(PLAN_BODY) == [
        ("CACHE-01", "repeated identical GETs within the window are served from memory"),
        ("CACHE-02", "the cache is bounded and evicts LRU entries"),
    ]
    assert planning.parse_acceptance_criteria("no section here") == []
    stops = "## Acceptance criteria\n- A-01: one\n## Risks\n- B-02: not a criterion\n"
    assert planning.parse_acceptance_criteria(stops) == [("A-01", "one")]


def test_planning_directives_parse():
    _body, directives = parse_directives(
        "Reviewed it.\n"
        "SYNCHRI-OBJECTION: blocking|No rollback\n"
        "SYNCHRI-OBJECTION: sideways|bad kind\n"
        "SYNCHRI-OBJECTION-RESOLVED: obj-003|reordered\n"
        "SYNCHRI-FORK: sqlite vs files\n"
        "SYNCHRI-PLAN-SUBMIT: second draft\n"
        "SYNCHRI-PLAN-REVIEW: approve|good enough\n"
    )
    assert directives.objections == [("blocking", "No rollback")]
    assert directives.objection_resolutions == [("OBJ-003", "reordered")]
    assert directives.forks == ["sqlite vs files"]
    assert directives.plan_submitted == "second draft"
    assert directives.plan_review == ("approve", "good enough")
    assert len(directives.warnings) == 1


# ----------------------------------------------------------------------
# the conducted loop, end to end
# ----------------------------------------------------------------------


def test_agents_drive_the_loop_through_the_conductor(manager, repo, tmp_path):
    record, credentials = _activated(manager, repo)
    workspace = record.planning_workspace["path"]

    plan_lines = PLAN_BODY.replace("\n", "\\n").replace('"', '\\"')
    scripted = {
        "claude": (
            f'open("PLAN.md", "w").write("{plan_lines}")\n'
            "print('Drafted the plan from the idea and the repository.')\n"
            "print('SYNCHRI-PLAN-SUBMIT: first full draft')\n"
        ),
        "codex": (
            "print('Reviewed revision 1 against the workspace.')\n"
            "print('SYNCHRI-OBJECTION: nonblocking|Assumes one process')\n"
            "print('SYNCHRI-PLAN-REVIEW: approve|executable as ordered')\n"
        ),
    }
    commands = {}
    for name, body in scripted.items():
        agent = AgentCommand.parse(write_agent(tmp_path, name, body), timeout=30)
        agent.cwd = workspace
        commands[name] = agent
    conductor = Conductor(
        manager.broker,
        record.room_id,
        commands,
        {name: credentials[name] for name in commands},
        manager._human_credential(record),
        session_id=record.session_id,
    )

    prompt = conductor.build_prompt("claude", {}, agent=commands["claude"])
    assert "--- planning state ---" in prompt
    assert "the human's idea, verbatim" in prompt and IDEA in prompt

    # Exactly the two turns of interest: the planner's lone first draft, then
    # the reviewer's closure. (The scripts are static; further turns would
    # just repeat them.)
    conductor.run(max_turns=2)
    payload = planning.payload(manager, record.session_id)
    assert payload["status"] == "ready" and payload["revision"] == 1
    assert [o["classification"] for o in payload["open_objections"]] == ["nonblocking"]

    prompt = conductor.build_prompt("claude", {}, agent=commands["claude"])
    assert "PLAN-READY" in prompt


# ----------------------------------------------------------------------
# the API surface
# ----------------------------------------------------------------------


def test_dashboard_and_plan_controls(manager, repo):
    from synchri.ui.api import Api

    record, _ = _activated(manager, repo)
    _write_plan(record)
    _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: draft")
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")

    dashboard = manager.dashboard(record.session_id)
    assert dashboard["plan"]["status"] == "ready"
    assert dashboard["plan"]["plan_id"] == "PLAN-001"

    api = Api(manager.broker, manager)

    class _Idle:
        def resume(self, record):
            return {"phase": "resuming"}

    api.managed = _Idle()
    payload = api.plan({"session": record.session_id}, {})
    assert payload["latest_body"].startswith("# Implementation plan")
    payload = api.plan_control(
        {}, {"session": record.session_id, "action": "reopen", "note": "tighten scope"}
    )
    assert payload["status"] == "under_revision"
