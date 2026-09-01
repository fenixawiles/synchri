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

import pytest

from synchri.errors import StateError, ValidationError
from synchri.runner.agent_command import AgentCommand, parse_directives
from synchri.runner.conductor import Conductor
from synchri.session import dropbox, planning
from synchri.storage import db
from synchri.session.contract import ACK_TOKEN
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


def _submission(body=PLAN_BODY, summary="draft"):
    """A planner turn's text: the plan travels through the reply protocol."""
    return (
        "Drafted the plan.\n"
        "SYNCHRI-PLAN-BEGIN\n" + body + "\nSYNCHRI-PLAN-END\n"
        f"SYNCHRI-PLAN-SUBMIT: {summary}"
    )


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
    assert runtime_status("claude_code")["connection_test_available"] is True
    assert runtime_status("codex")["connection_test_available"] is True
    assert runtime_status("copilot")["connection_test_available"] is True
    assert runtime_status("copilot")["planning_supported"] is False


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

    # Submit requires the plan document in the same reply.
    _actions, warnings = _turn(manager, record, "claude", "SYNCHRI-PLAN-SUBMIT: draft")
    assert any("SYNCHRI-PLAN-BEGIN" in w for w in warnings)

    actions, warnings = _turn(
        manager, record, "claude", _submission(summary="first full draft")
    )
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

    actions, _warnings = _turn(
        manager, record, "claude",
        "SYNCHRI-PLAN-BEGIN\n"
        + PLAN_BODY.replace("2. Wire", "2. Bound the cache first, then wire")
        + "\nSYNCHRI-PLAN-END\n"
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
    _turn(manager, record, "claude",
          "SYNCHRI-PLAN-BEGIN\n" + PLAN_BODY + "\nSYNCHRI-PLAN-END\n"
          "SYNCHRI-FORK: cache in-process vs sidecar; both defensible\n"
          "SYNCHRI-PLAN-SUBMIT: draft with an open fork")
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert any("OBJ-001 (fork)" in w for w in warnings)
    _turn(manager, record, "claude",
          "SYNCHRI-PLAN-BEGIN\n" + PLAN_BODY + "\nSYNCHRI-PLAN-END\n"
          "SYNCHRI-OBJECTION-RESOLVED: OBJ-001|human chose in-process\n"
          "SYNCHRI-PLAN-SUBMIT: fork resolved")
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|closed")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"


def test_ready_is_invalidated_by_a_human_edit_and_by_a_new_consideration(manager, repo):
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"

    planning.reopen(manager, record.session_id, "Also cover HEAD requests.")
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "under_revision" and plan["ready_at"] is None
    wakes = _wake_messages(manager, record, "plan_reopened")
    assert wakes and "HEAD requests" in wakes[-1]["content"]

    _turn(manager, record, "claude", _submission(summary="covers HEAD"))
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
    _turn(manager, record, "claude", _submission())
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


def test_review_closure_enforces_the_promotion_criteria_rule(manager, repo):
    """PLAN-READY implies approvable: invalid criteria are caught at closure,
    while the loop can still fix them, never first at human approval."""
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission(body="# Plan\n\n1. do it\n"))
    actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert any("cannot reach PLAN-READY" in w for w in warnings)
    assert actions == [{"kind": "review", "verdict": "revise", "revision": 1,
                        "criteria_correction": True}]
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "under_revision" and plan["ready_at"] is None
    wakes = _wake_messages(manager, record, "plan_criteria_correction")
    assert wakes and wakes[-1]["target"] == "claude"
    assert "Acceptance" in wakes[-1]["content"]

    colliding = PLAN_BODY.replace("CACHE-02", "CACHE-01")
    _turn(manager, record, "claude", _submission(body=colliding, summary="colliding ids"))
    _actions, warnings = _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert any("collide on gate id CACHE-01" in w for w in warnings)
    assert planning.get_plan(manager, record.session_id)["status"] == "under_revision"

    _turn(manager, record, "claude", _submission(summary="criteria fixed"))
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    assert planning.get_plan(manager, record.session_id)["status"] == "ready"
    assert planning.approve(manager, record.session_id)["promoted"] is True, (
        "what review closure accepts, approval accepts"
    )


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
    for index in range(planning.REVISION_BUDGET):
        _actions, _warnings = _turn(
            manager, record, "claude", _submission(summary=f"revision {index + 1}")
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
    (workspace / "notes.md").write_text("scratch notes\n", encoding="utf-8")
    assert planning.verify_workspace(manager, record) is True, (
        "untracked files never trip verification — only a moved HEAD does"
    )

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

    # The reviewer approves ONLY if the durable plan text reached its prompt.
    # The needle exists nowhere but the submitted body (the plan block is
    # stripped from the room transcript), so a blind approval — the regression
    # where the reviewer never receives the plan — fails this test.
    scripted = {
        "claude": (
            "print('Drafted the plan from the idea and the repository.')\n"
            "print('SYNCHRI-PLAN-BEGIN')\n"
            f"print({PLAN_BODY!r})\n"
            "print('SYNCHRI-PLAN-END')\n"
            "print('SYNCHRI-PLAN-SUBMIT: first full draft')\n"
        ),
        "codex": (
            "if 'bounded LRU map' in prompt:\n"
            "    print('Reviewed revision 1; the durable plan text is in my prompt.')\n"
            "    print('SYNCHRI-OBJECTION: nonblocking|Assumes one process')\n"
            "    print('SYNCHRI-PLAN-REVIEW: approve|executable as ordered')\n"
            "else:\n"
            "    print('SYNCHRI-OBJECTION: blocking|I never received the plan text to review')\n"
            "    print('SYNCHRI-PLAN-REVIEW: revise|cannot review an invisible plan')\n"
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
    assert "the plan: revision 1, full text, verbatim" in prompt
    assert "bounded LRU map" in prompt


def test_render_status_carries_the_full_revision_body(manager, repo):
    """Both roles read the durable revision from the planning panel — the
    only channel, since transcripts strip the block and turns relaunch cold."""
    record, _ = _activated(manager, repo)
    status = planning.render_status(manager, manager.get(record.session_id))
    assert "full text, verbatim" not in status, "no body section before a submission"
    _turn(manager, record, "claude", _submission())
    status = planning.render_status(manager, manager.get(record.session_id))
    assert "the plan: revision 1, full text, verbatim" in status
    assert PLAN_BODY.strip() in status


# ----------------------------------------------------------------------
# the API surface
# ----------------------------------------------------------------------


def test_dashboard_and_plan_controls(manager, repo):
    from synchri.ui.api import Api

    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission())
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


# ----------------------------------------------------------------------
# approval and promotion
# ----------------------------------------------------------------------


def _ready(manager, repo, body=PLAN_BODY):
    record, credentials = _activated(manager, repo)
    _turn(manager, record, "claude", _submission(body))
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|fine")
    return manager.get(record.session_id), credentials


def _repo_commit(repo, filename="more.txt"):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
           "PATH": __import__("os").environ.get("PATH", ""), "HOME": str(repo)}
    (repo / filename).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "move the tip"],
                   check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_approval_promotes_into_exactly_one_linked_coordination_session(manager, repo):
    record, _ = _ready(manager, repo)
    inspection = planning.get_plan(manager, record.session_id)["inspection_sha"]

    result = planning.approve(manager, record.session_id)
    assert result["promoted"] is True and result["already_promoted"] is False
    coordination = manager.get(result["coordination_session_id"])

    # The plan became the executable contract: mode, spec, gates, worktree.
    assert coordination.mode == "long_horizon"
    assert coordination.spec.text.startswith("# Approved implementation plan PLAN-001")
    assert "## Plan provenance" in coordination.spec.text
    roles = {p.name: p.role for p in coordination.participants}
    assert roles == {"claude": "primary_builder", "codex": "adversarial_reviewer"}
    runtimes = {p.name: p.runtime for p in coordination.participants}
    assert runtimes == {"claude": "claude_code", "codex": "codex"}
    gates = manager.gates(coordination.session_id)
    assert [g.gate_id for g in gates] == ["CACHE-01", "CACHE-02"], "no SPEC-01 collapse"
    assert gates[0].description == (
        "repeated identical GETs within the window are served from memory"
    )
    assert coordination.contract_revision == 1
    assert coordination.metadata["promoted_from"] == record.session_id
    assert coordination.metadata["plan_id"] == "PLAN-001"
    assert coordination.metadata["inspection_sha"] == inspection
    worktree_head = subprocess.run(
        ["git", "-C", coordination.worktree_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == inspection

    # The planning session and its plan are immutable now.
    planning_record = manager.get(record.session_id)
    assert planning_record.status == "complete"
    assert "PLAN-001" in planning_record.ended_reason
    assert planning_record.metadata["promoted_to"] == coordination.session_id
    assert planning.get_plan(manager, record.session_id)["status"] == "approved"

    # Duplicate approval is idempotent — never a second session.
    again = planning.approve(manager, record.session_id)
    assert again["already_promoted"] is True
    assert again["coordination_session_id"] == coordination.session_id
    linked = [
        s for s in manager.list_sessions()
        if (s.metadata or {}).get("promoted_from") == record.session_id
    ]
    assert len(linked) == 1


def test_post_approval_captures_route_to_the_coordination_session(manager, repo):
    from synchri.ui.api import Api

    record, _ = _ready(manager, repo)
    result = planning.approve(manager, record.session_id)
    coordination_id = result["coordination_session_id"]

    api = Api(manager.broker, manager)

    class _Recorder:
        def investigate(self, session_id, drop_id):
            raise AssertionError("no scout should start here")

    api.ancillary = _Recorder()
    payload = api.capture_drop({}, {"session": record.session_id, "prompt": "late idea"})
    assert payload["item"]["drop_id"] == "DROP-001"
    assert [e["drop_id"] for e in dropbox.items(manager, coordination_id)] == ["DROP-001"]
    assert dropbox.items(manager, record.session_id) == []


def _forced_ready(manager, repo, body):
    """A plan pushed to READY outside the loop — review closure would refuse
    this body, so force the status directly to exercise approval's re-check."""
    record, _ = _activated(manager, repo)
    _turn(manager, record, "claude", _submission(body=body))
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET status = 'ready' WHERE session_id = ?",
            (record.session_id,),
        )
    return manager.get(record.session_id)


def test_approval_requires_explicit_collision_free_criteria(manager, repo):
    """Defense in depth: review closure enforces the criteria rule, and
    approval re-checks the same rule against plans forced READY outside it."""
    record = _forced_ready(manager, repo, body="# Plan\n\n1. do it\n")
    with pytest.raises(StateError) as exc:
        planning.approve(manager, record.session_id)
    assert exc.value.code == "plan_criteria_missing"
    assert planning.get_plan(manager, record.session_id)["status"] == "ready", (
        "a refused approval reserves nothing"
    )
    assert planning.promotion(manager, record.session_id) is None

    colliding = PLAN_BODY.replace("CACHE-02", "CACHE-01")
    record2 = _forced_ready(manager, repo, body=colliding)
    with pytest.raises(StateError) as exc:
        planning.approve(manager, record2.session_id)
    assert exc.value.code == "plan_gate_collision"


def test_blocking_decisions_cannot_cross_approval(manager, repo):
    record, _ = _ready(manager, repo)
    # A late blocking objection lands while the plan shows ready.
    _turn(manager, record, "codex", "SYNCHRI-OBJECTION: blocking|second thoughts")
    with pytest.raises(StateError) as exc:
        planning.approve(manager, record.session_id)
    assert exc.value.code == "plan_blocking_open"
    assert "OBJ-001" in exc.value.message


def test_exhausted_plans_can_approve_what_stands(manager, repo):
    """The exhaustion copy promises 'approve what stands'; approval honors it:
    a preserved revision with no open blockers promotes without re-review."""
    record, _ = _ready(manager, repo)
    _turn(manager, record, "codex", "SYNCHRI-OBJECTION: blocking|second thoughts")
    for _ in range(planning.TURN_BUDGET):
        planning.count_turn(manager, record)
    assert planning.get_plan(manager, record.session_id)["status"] == "needs_human_resolution"

    # Open blockers still cannot cross approval, exhausted or not.
    with pytest.raises(StateError) as exc:
        planning.approve(manager, record.session_id)
    assert exc.value.code == "plan_blocking_open"

    # With a spent budget the waiver stands as recorded — no forced re-review.
    planning.waive_objection(manager, record.session_id, "OBJ-001", "Accepted for v1.")
    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "needs_human_resolution", "the waiver stands"
    assert _wake_messages(manager, record, "plan_waiver_review") == []

    result = planning.approve(manager, record.session_id)
    assert result["promoted"] is True
    coordination = manager.get(result["coordination_session_id"])
    assert [g.gate_id for g in manager.gates(coordination.session_id)] == [
        "CACHE-01", "CACHE-02",
    ]
    assert planning.get_plan(manager, record.session_id)["status"] == "approved"
    assert not any(
        e["rule"] == "planning_budget_exhausted"
        for e in manager.open_escalations(record.session_id)
    ), "approving what stands settles the exhaustion escalation"


def test_an_exhausted_plan_without_a_revision_cannot_be_approved(manager, repo):
    record, _ = _activated(manager, repo)
    for _ in range(planning.TURN_BUDGET):
        planning.count_turn(manager, record)
    assert planning.get_plan(manager, record.session_id)["status"] == "needs_human_resolution"
    with pytest.raises(StateError) as exc:
        planning.approve(manager, record.session_id)
    assert exc.value.code == "plan_not_ready"
    assert "no submitted revision" in exc.value.message


def test_the_review_record_is_immutable_after_approval(manager, repo):
    record, _ = _ready(manager, repo)
    _turn(manager, record, "codex", "SYNCHRI-OBJECTION: nonblocking|left open on purpose")
    planning.approve(manager, record.session_id)
    with pytest.raises(StateError) as exc:
        planning.waive_objection(manager, record.session_id, "OBJ-001", "too late")
    assert exc.value.code == "plan_approved_immutable"
    with pytest.raises(StateError) as exc:
        planning.reopen(manager, record.session_id, "change it")
    assert exc.value.code == "plan_not_active"


def test_baseline_drift_returns_the_plan_to_review_and_reanchors(manager, repo):
    record, _ = _ready(manager, repo)
    old_inspection = planning.get_plan(manager, record.session_id)["inspection_sha"]
    new_tip = _repo_commit(repo)
    assert new_tip != old_inspection

    with pytest.raises(StateError) as exc:
        planning.approve(manager, record.session_id)
    assert exc.value.code == "baseline_drift"

    plan = planning.get_plan(manager, record.session_id)
    assert plan["status"] == "under_revision"
    assert plan["inspection_sha"] == new_tip, "the workspace was re-anchored"
    workspace_head = subprocess.run(
        ["git", "-C", plan["workspace_path"], "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert workspace_head == new_tip
    drift = [o for o in planning.objections(manager, record.session_id)
             if o["raised_by"] == "synchri"]
    assert drift and drift[0]["classification"] == "blocking"
    assert _wake_messages(manager, record, "plan_drift")

    # Re-verify against the new baseline, re-review, approve — now it lands.
    record = manager.get(record.session_id)
    _turn(manager, record, "claude",
          "SYNCHRI-PLAN-BEGIN\n" + PLAN_BODY + "\nSYNCHRI-PLAN-END\n"
          f"SYNCHRI-OBJECTION-RESOLVED: {drift[0]['objection_id']}|re-verified on the new tip\n"
          "SYNCHRI-PLAN-SUBMIT: re-verified")
    _turn(manager, record, "codex", "SYNCHRI-PLAN-REVIEW: approve|holds on the new baseline")
    result = planning.approve(manager, record.session_id)
    coordination = manager.get(result["coordination_session_id"])
    worktree_head = subprocess.run(
        ["git", "-C", coordination.worktree_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == new_tip


def test_a_retry_resumes_the_reserved_promotion_never_a_second_session(manager, repo):
    record, _ = _ready(manager, repo)
    # Provisioning fails mid-way (an invalid staffing choice) AFTER the
    # reservation transaction committed.
    with pytest.raises(ValidationError):
        planning.approve(
            manager, record.session_id,
            staffing=[{"name": "solo", "runtime": "claude_code", "role": "primary_builder"}],
        )
    promo = planning.promotion(manager, record.session_id)
    assert promo["status"] == "reserved" and promo["coordination_session_id"] is None
    assert planning.get_plan(manager, record.session_id)["status"] == "approved"

    # The source branch moves while the promotion sits reserved. The retry
    # must reuse the recorded SHA, not the moved tip.
    moved_tip = _repo_commit(repo, "after-reserve.txt")
    result = planning.approve(manager, record.session_id)
    coordination = manager.get(result["coordination_session_id"])
    worktree_head = subprocess.run(
        ["git", "-C", coordination.worktree_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == promo["inspection_sha"]
    assert worktree_head != moved_tip
    linked = [
        s for s in manager.list_sessions()
        if (s.metadata or {}).get("promoted_from") == record.session_id
    ]
    assert len(linked) == 1


def test_a_crash_between_provision_and_finalize_is_adopted_and_completed(manager, repo):
    record, _ = _ready(manager, repo)
    with pytest.raises(ValidationError):
        planning.approve(
            manager, record.session_id,
            staffing=[{"name": "solo", "runtime": "claude_code", "role": "primary_builder"}],
        )
    promo = planning.promotion(manager, record.session_id)
    # Simulate the worst crash window: the coordination session row exists
    # with its provenance, but the crash landed before gates and contract.
    body = planning.revision_body(manager, record.session_id, promo["plan_revision"])
    orphan = manager.create(
        name=record.name, mode="long_horizon", repo_root=record.repo_root,
        base_branch="main",
        participants=planning._staff(manager.get(record.session_id), None),
        spec=planning._spec_from_plan(manager, manager.get(record.session_id), promo, body),
        metadata={"promoted_from": record.session_id, "plan_id": promo["plan_id"]},
        worktree_start_point=promo["inspection_sha"],
    )
    manager.set_gates(orphan.session_id, [])  # the crash landed before materialization
    assert orphan.contract_revision == 0
    assert planning.promotion(manager, record.session_id)["coordination_session_id"] is None

    result = planning.approve(manager, record.session_id)
    assert result["coordination_session_id"] == orphan.session_id
    assert planning.promotion(manager, record.session_id)["status"] == "provisioned"
    # Adoption is never "as found": the half-provisioned session was finished.
    adopted = manager.get(orphan.session_id)
    assert [g.gate_id for g in manager.gates(orphan.session_id)] == ["CACHE-01", "CACHE-02"]
    assert adopted.contract_revision == 1
    linked = [
        s for s in manager.list_sessions()
        if (s.metadata or {}).get("promoted_from") == record.session_id
    ]
    assert len(linked) == 1


def test_the_database_refuses_a_second_linked_session_and_the_loser_leaks_nothing(manager, repo):
    """Concurrent promotion retries converge by constraint, not by hoping."""
    import sqlite3
    from pathlib import Path

    record, _ = _ready(manager, repo)
    result = planning.approve(manager, record.session_id)
    before = {s.session_id for s in manager.list_sessions()}
    rooms_before = {p.name for p in manager.broker.workspace.rooms_dir.iterdir()}
    trees_before = {
        t["path"] for t in __import__("synchri.session.worktree", fromlist=["list_worktrees"])
        .list_worktrees(str(repo))
    }
    with pytest.raises(sqlite3.IntegrityError):
        manager.create(
            name="duplicate", mode="long_horizon", repo_root=str(repo),
            participants=planning._staff(manager.get(record.session_id), None),
            spec=ProductSpec(text="dup"),
            metadata={"promoted_from": record.session_id},
        )
    assert {s.session_id for s in manager.list_sessions()} == before
    trees_after = {
        t["path"] for t in __import__("synchri.session.worktree", fromlist=["list_worktrees"])
        .list_worktrees(str(repo))
    }
    assert trees_after == trees_before, "the refused insert must clean its worktree"
    rooms_after = {p.name for p in manager.broker.workspace.rooms_dir.iterdir()}
    assert rooms_after == rooms_before, (
        "the refused insert must also clean the room directory and ledger"
    )
    assert result["coordination_session_id"] in before


def test_a_capture_in_the_provisioning_window_queues_and_drains(manager, repo, monkeypatch):
    import json

    from synchri.runner import doctor as doctor_module
    from synchri.ui import api as api_module
    from synchri.ui.api import Api

    record, _ = _ready(manager, repo)
    with pytest.raises(ValidationError):
        planning.approve(
            manager, record.session_id,
            staffing=[{"name": "solo", "runtime": "claude_code", "role": "primary_builder"}],
        )
    api = Api(manager.broker, manager)

    # Reserved, no coordination session anywhere yet: the capture is neither
    # lost nor bounced back — it queues durably on the promotion record.
    payload = api.capture_drop({}, {"session": record.session_id, "prompt": "mid-window idea"})
    assert payload["queued"] is True and payload["item"] is None

    # The queued entry persists on the promotion row — indefinitely, after a
    # crash — so its title takes the same fail-closed screen at queue time as
    # every other stored free-form field.
    token = "ghp_" + "q7W3" * 8
    payload = api.capture_drop(
        {}, {"session": record.session_id, "prompt": "rotate the key",
             "title": f"found {token} in config"},
    )
    assert payload["queued"] is True
    promo_row = manager.conn.execute(
        "SELECT * FROM session_promotions WHERE planning_session_id = ?",
        (record.session_id,),
    ).fetchone()
    assert token not in json.dumps(
        {key: promo_row[key] for key in promo_row.keys()}, default=str
    ), "a queued capture's title must never persist a raw secret"

    # The retry finalizes the promotion and drains the queue atomically: the
    # window captures enter the coordination session's dropbox — and each
    # drained item gets the same scout start a direct capture would, instead
    # of idling as captured until the completion cutoff times it out.
    class _Scouts:
        def __init__(self):
            self.calls = []

        def investigate(self, session_id, drop_id):
            self.calls.append((session_id, drop_id))

    class _Managed:
        cli_command = "synchri"

        def cancel(self, session_id, reason=""):
            pass

        def readiness(self, record):
            return {"available": False, "agents": []}

        def status(self, session_id):
            return {"phase": "not_started"}

    api.ancillary = _Scouts()
    api.managed = _Managed()
    monkeypatch.setattr(
        api_module, "plan_launch_status",
        lambda plan: {"mode": "managed", "ready": True, "command": "agent {prompt}",
                      "detail": "stubbed for the drained-scout check"},
    )
    monkeypatch.setattr(
        doctor_module, "stored_connections",
        lambda conn: {"claude_code": {"state": "connected"}},
    )
    result = api.approve_plan({}, {"session": record.session_id})
    coordination_id = result["coordination_session_id"]
    drained = dropbox.items(manager, coordination_id)
    assert [(e["drop_id"], e["prompt"]) for e in drained] == [
        ("DROP-001", "mid-window idea"), ("DROP-002", "rotate the key"),
    ]
    assert token not in json.dumps(drained, default=str)
    assert dropbox.items(manager, record.session_id) == []
    promo = planning.promotion(manager, record.session_id)
    assert promo["pending_captures"] == "[]", "drained exactly once"
    assert api.ancillary.calls == [
        (coordination_id, "DROP-001"), (coordination_id, "DROP-002"),
    ], "a drained capture starts its scout exactly like a direct capture"

    # After finalize, the same endpoint captures straight into the
    # coordination session.
    payload = api.capture_drop({}, {"session": record.session_id, "prompt": "late idea"})
    assert payload["item"]["drop_id"] == "DROP-003"
    assert [e["drop_id"] for e in dropbox.items(manager, coordination_id)] == [
        "DROP-001", "DROP-002", "DROP-003",
    ]


def test_the_approval_capture_race_is_atomic_across_threads(manager, repo, monkeypatch):
    """Two real request threads at the approval boundary.

    The capture's routing decision and its insert are one transaction,
    serialized with approval's reserve and finalize by the shared
    connection's write lock — so a capture racing an approval queues or
    routes, and is never bounced with ``session_finished`` or
    ``dropbox_frozen`` because approval completed in a gap between a read
    and the insert.
    """
    import threading

    from synchri.ui.api import Api

    record, _ = _ready(manager, repo)
    api = Api(manager.broker, manager)

    in_window = threading.Event()
    finish = threading.Event()
    real_provision = planning._provision

    def paused_provision(*args, **kwargs):
        in_window.set()
        assert finish.wait(30), "the capture thread never released provisioning"
        return real_provision(*args, **kwargs)

    monkeypatch.setattr(planning, "_provision", paused_provision)

    results: dict = {}

    def approver():
        results["approve"] = planning.approve(manager, record.session_id)

    thread = threading.Thread(target=approver)
    thread.start()
    try:
        assert in_window.wait(30), "approve never reached provisioning"
        # The promotion is reserved on another thread, mid-provisioning: the
        # capture endpoint queues durably — no error, and no deadlock against
        # the approval's transactions.
        payload = api.capture_drop(
            {}, {"session": record.session_id, "prompt": "raced idea"}
        )
        assert payload["queued"] is True and payload["item"] is None
    finally:
        finish.set()
        thread.join(60)
    assert not thread.is_alive(), "approve never finished"
    coordination_id = results["approve"]["coordination_session_id"]
    drained = dropbox.items(manager, coordination_id)
    assert [(e["drop_id"], e["prompt"]) for e in drained] == [("DROP-001", "raced idea")]
    assert dropbox.items(manager, record.session_id) == []


def test_worktrees_can_branch_from_an_exact_start_point(repo):
    from synchri.session import worktree as worktree_module

    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _repo_commit(repo, "second.txt")
    tree = worktree_module.create(str(repo), "main", start_point=first)
    head = subprocess.run(
        ["git", "-C", tree.path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == first


def test_the_app_ships_the_planning_flow():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert '"plan","gates"' in source, "the plan panel is a first-class session tab"
    assert 'api("plan" + q)' in source
    assert "plan/approve" in source and "plan/control" in source
    assert "Approve &amp; Continue to Coordination" in source
    assert "showLaunch(result.coordination)" in source, (
        "approval lands on the coordination preflight, never an inactive dashboard"
    )
    assert "approve what stands" in source, (
        "the exhausted state explains its three ways forward"
    )
    assert "Articulate the idea" in source
    assert "no planning support" in source, "unsupported runtimes are shown unavailable"
    assert "Resume the budget (once)" in source
    assert "renderManagedOnlyBlocked" in source and "l.managed_only" in source, (
        "planning preflight hides the dead paste path"
    )


def test_the_approve_endpoint_returns_the_coordination_launch(manager, repo):
    from synchri.ui.api import Api

    record, _ = _ready(manager, repo)
    api = Api(manager.broker, manager)

    class _Recorder:
        cli_command = "synchri"

        def __init__(self):
            self.cancelled = []

        def cancel(self, session_id, reason=""):
            self.cancelled.append(session_id)

        def readiness(self, record):
            return {"available": False, "agents": []}

        def status(self, session_id):
            return {"phase": "not_started"}

    api.managed = _Recorder()
    planning_launch = api._launch_payload(manager.get(record.session_id))
    assert planning_launch["launch"]["managed_only"] is True, (
        "a pasted agent cannot submit plan revisions, so planning is managed-only"
    )
    result = api.approve_plan({}, {"session": record.session_id})
    assert result["promoted"] is True
    assert api.managed.cancelled == [record.session_id]
    launch = result["coordination"]["launch"]
    assert launch["worktree_path"]
    assert launch["managed_only"] is False, "coordination keeps the manual fallback"
    assert {agent["role"] for agent in launch["agents"]} == {
        "primary_builder", "adversarial_reviewer",
    }
    assert result["coordination"]["session"]["mode"] == "long_horizon"


# ----------------------------------------------------------------------
# enforced isolation
# ----------------------------------------------------------------------


def test_planning_commands_carry_the_clis_own_enforcement():
    """Isolation is the runtime's, not a request in a prompt."""
    from synchri.session.modes import KNOWN_RUNTIMES

    claude = KNOWN_RUNTIMES["claude_code"]
    for key in ("planning_command", "plain_planning_command"):
        assert "--permission-mode plan" in claude[key]
        # Hardened beyond the permission mode: configuration surfaces off,
        # no MCP servers, and an explicit read-only tool allowlist.
        assert "--safe-mode" in claude[key]
        assert "--strict-mcp-config" in claude[key]
        assert '--tools "Read,Glob,Grep"' in claude[key]
        # --tools is variadic in current Claude CLIs. Prompts travel over
        # stdin so they cannot be swallowed as another tool-list value.
        assert "{prompt}" not in claude[key]
        assert AgentCommand.parse(f"claude={claude[key]}").takes_prompt_in_argv is False
    codex = KNOWN_RUNTIMES["codex"]
    assert "--sandbox read-only" in codex["planning_command"]
    assert "--sandbox read-only" in codex["plain_planning_command"]
    # No verified enforcement flag in the Copilot adapter yet: unavailable,
    # never silently downgraded to contract-only isolation.
    assert planning_workspace_supported("copilot") is False


def test_custom_commands_are_refused_for_planning(manager, repo):
    with pytest.raises(ValidationError) as exc:
        manager.create(
            name="x", mode="planning", repo_root=str(repo),
            participants=[
                ParticipantPlan("claude", "claude_code", "planner",
                                command="my-wrapper {prompt}"),
                ParticipantPlan("codex", "codex", "plan_reviewer"),
            ],
            metadata={"idea": IDEA},
        )
    assert "custom command" in str(exc.value)


def test_the_plan_block_is_extracted_and_kept_out_of_the_transcript():
    body, directives = parse_directives(
        "Here is my thinking.\n"
        "SYNCHRI-PLAN-BEGIN\n"
        "# The plan\n\n1. do the thing\n"
        "SYNCHRI-PLAN-END\n"
        "And a closing remark.\n"
        "SYNCHRI-PLAN-SUBMIT: first draft\n"
    )
    assert directives.plan_body == "# The plan\n\n1. do the thing"
    assert directives.plan_submitted == "first draft"
    assert "do the thing" not in body, "the stored revision is the durable copy"
    assert "Here is my thinking." in body and "closing remark" in body

    # Quoting one marker mid-prose never opens a block.
    body, directives = parse_directives("The convention uses SYNCHRI-PLAN-BEGIN markers.")
    assert directives.plan_body is None
    assert "SYNCHRI-PLAN-BEGIN" in body


def test_the_registry_launches_planning_agents_under_enforcement(
    manager, repo, workspace, monkeypatch
):
    import synchri.session.modes as modes
    from synchri.runner.managed import ManagedRunnerRegistry

    monkeypatch.setattr(
        modes, "runtime_status",
        lambda runtime: {"installed": True, "managed": True, "executable": "x",
                         "path": "/x", "detail": "",
                         "planning_supported": modes.planning_workspace_supported(runtime)},
    )
    record = _planning_session(manager, repo)
    registry = ManagedRunnerRegistry(workspace)
    planner_agent = registry._agent(record, record.participants[0])
    assert "--permission-mode" in planner_agent.argv and "plan" in planner_agent.argv
    assert planner_agent.cwd == record.planning_workspace["path"]
    reviewer_agent = registry._agent(record, record.participants[1])
    assert "--sandbox" in reviewer_agent.argv and "read-only" in reviewer_agent.argv
    # The resume rung never applies to planning: a resume invocation would
    # not carry the enforcement flag.
    turn_agent = registry._turn_agent(manager, record, record.participants[0])
    assert "--permission-mode" in turn_agent.argv
