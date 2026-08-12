"""Startup and session initialization.

The invariants under test, in code terms:

* a session cannot activate without a validated worktree that is not the primary tree
* a session cannot activate until every participant acknowledged the CURRENT revision
* changing material terms forces a new revision and clears acknowledgments
* permissions are read from state, never inferred; ASK is not a grant
* a deadline never produces a false completion claim
* completion requires evidence and both sign-offs
* restart restores state without resuming anything
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from synchri.broker import Broker
from synchri.errors import NotFoundError, StateError, ValidationError
from synchri.session import presets as presets_module
from synchri.session import worktree as worktree_module
from synchri.session.contract import ACK_TOKEN, parse_acknowledgment
from synchri.session.deadline import Deadline, parse_duration
from synchri.session.draft import SessionDraft
from synchri.session.escalation import EscalationPolicy
from synchri.session.gates import Gate, summarize
from synchri.session.manager import SessionManager, SessionStatus
from synchri.session.modes import ParticipantPlan, policy_for
from synchri.session.permissions import Decision, PermissionDenied, PermissionSet
from synchri.session.spec import ProductSpec

SPEC_TEXT = "Build a task API.\n\n## Acceptance\n- AUTH-01 login\n- API-01 CRUD"


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


def _git(root, *args):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
        "PATH": __import__("os").environ.get("PATH", ""), "HOME": str(root),
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "marnie"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def manager(broker):
    return SessionManager(broker)


@pytest.fixture
def agents():
    return [
        ParticipantPlan("claude", "claude_code", "primary_builder"),
        ParticipantPlan("codex", "codex", "adversarial_reviewer"),
    ]


def make_session(manager, repo, agents, **kwargs):
    defaults = dict(
        name="PR 89",
        mode="long_horizon",
        repo_root=str(repo),
        participants=agents,
        spec=ProductSpec(text=SPEC_TEXT),
        deadline=Deadline.from_duration("10 hours"),
    )
    defaults.update(kwargs)
    return manager.create(**defaults)


def accept_all(manager, record):
    for plan in record.participants:
        manager.acknowledge(record.session_id, plan.name, ACK_TOKEN)


# ----------------------------------------------------------------------
# repository validation
# ----------------------------------------------------------------------


def test_repository_validation_reports_a_usable_repo(repo):
    status = worktree_module.inspect_repository(repo)
    assert status.is_valid and status.problems == []
    assert status.name == "marnie" and status.branch == "main"
    assert status.head and not status.is_dirty


@pytest.mark.parametrize("case", ["missing", "not_a_repo", "a_file"])
def test_repository_validation_rejects_unusable_paths(tmp_path, case):
    target = {
        "missing": tmp_path / "nope",
        "not_a_repo": tmp_path / "plain",
        "a_file": tmp_path / "file.txt",
    }[case]
    if case == "not_a_repo":
        target.mkdir()
    if case == "a_file":
        target.write_text("x", encoding="utf-8")
    status = worktree_module.inspect_repository(target)
    assert not status.is_valid and status.problems


def test_a_dirty_primary_tree_is_reported_but_never_touched(repo):
    (repo / "scratch.txt").write_text("work in progress\n", encoding="utf-8")
    status = worktree_module.inspect_repository(repo)
    assert status.is_valid, "dirty is a warning, not a blocker"
    assert status.is_dirty and "scratch.txt" in status.dirty_files
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == "work in progress\n"


def test_an_unfinished_merge_blocks_the_repository(repo):
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    status = worktree_module.inspect_repository(repo)
    assert not status.is_valid
    assert any("merge" in p for p in status.problems)


# ----------------------------------------------------------------------
# worktree
# ----------------------------------------------------------------------


def test_worktree_names_are_readable_and_unique():
    names = {worktree_module.generate_name("long_horizon") for _ in range(300)}
    assert len(names) > 290, "names must not collide in practice"
    sample = next(iter(names))
    assert sample.startswith("synchri-lh-")
    assert worktree_module.NAME_PATTERN.match(sample)
    assert len(sample.split("-")) == 5, "prefix, mode, adjective, noun, digits"


def test_worktree_creation_isolates_from_the_primary_tree(repo, tmp_path):
    tree = worktree_module.create(repo, "main", mode="long_horizon")
    assert Path(tree.path).exists()
    assert Path(tree.path).resolve() != Path(repo).resolve()
    assert Path(tree.path).resolve() not in Path(repo).resolve().parents
    assert tree.branch == tree.name and tree.base_branch == "main"
    # It is a real checkout of the same repository.
    assert (Path(tree.path) / "README.md").exists()


def test_worktree_changes_do_not_reach_the_primary_tree(repo):
    tree = worktree_module.create(repo, "main")
    (Path(tree.path) / "agent_work.py").write_text("print('hi')\n", encoding="utf-8")
    worktree_module.git(tree.path, "add", "-A")
    worktree_module.git(
        tree.path, "-c", "user.email=a@e.com", "-c", "user.name=a", "commit", "-qm", "agent work"
    )

    assert not (repo / "agent_work.py").exists(), "the primary tree must be untouched"
    assert worktree_module.git(repo, "status", "--porcelain") == ""
    assert worktree_module.git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_worktree_lives_outside_the_repository(repo):
    tree = worktree_module.create(repo, "main")
    assert Path(repo).resolve() not in Path(tree.path).resolve().parents, (
        "a worktree inside the repo would show up as untracked files in the primary tree"
    )


def test_duplicate_worktree_name_is_refused(repo):
    tree = worktree_module.create(repo, "main", name="synchri-fixed-name-1111")
    assert tree.name == "synchri-fixed-name-1111"
    with pytest.raises(ValidationError):
        worktree_module.create(repo, "main", name="synchri-fixed-name-1111")


def test_collision_is_retried_when_the_name_is_generated(repo, monkeypatch):
    """A name collision must not surface to the user."""
    taken = worktree_module.generate_name("long_horizon")
    worktree_module.create(repo, "main", name=taken)
    sequence = iter([taken, taken, "synchri-lh-fresh-name-2222"])
    monkeypatch.setattr(worktree_module, "generate_name", lambda mode=None: next(sequence))

    tree = worktree_module.create(repo, "main", mode="long_horizon")
    assert tree.name == "synchri-lh-fresh-name-2222"


def test_invalid_worktree_names_are_rejected(repo):
    for bad in ["../escape", "not-prefixed", "synchri-UPPER", "synchri-a b"]:
        with pytest.raises(ValidationError):
            worktree_module.create(repo, "main", name=bad)


def test_unknown_base_branch_is_rejected(repo):
    with pytest.raises(ValidationError):
        worktree_module.create(repo, "no-such-branch")


def test_validate_refuses_the_primary_tree_as_a_worktree(repo):
    with pytest.raises(StateError) as exc:
        worktree_module.validate(repo, repo, "synchri-bad-name-0001", "main")
    assert exc.value.code == "worktree_is_primary"


def test_concurrent_worktree_creation_yields_distinct_trees(repo):
    """Two sessions starting at once must not land in the same tree."""
    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def create():
        try:
            barrier.wait(timeout=30)
            results.append(worktree_module.create(repo, "main", mode="long_horizon"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    paths = {r.path for r in results}
    branches = {r.branch for r in results}
    assert len(paths) == 4 and len(branches) == 4
    assert all(Path(p).exists() for p in paths)


# ----------------------------------------------------------------------
# permissions
# ----------------------------------------------------------------------


def test_default_permissions_are_conservative():
    permissions = PermissionSet.defaults()
    for allowed in ("repo.read", "repo.edit", "repo.test", "repo.build", "git.commit", "gh.pr_review"):
        assert permissions.allows(allowed), allowed
    for denied in ("git.push", "git.force_push", "gh.pr_create", "gh.pr_merge",
                   "sys.deploy", "sys.destructive", "git.reset"):
        assert permissions.decision(denied) is Decision.DENY, denied
    assert permissions.decision("repo.install_deps") is Decision.ASK


def test_ask_is_not_a_grant():
    permissions = PermissionSet.defaults()
    assert permissions.requires_escalation("repo.install_deps")
    assert not permissions.allows("repo.install_deps")


def test_permission_check_denies_and_records(manager, repo, agents):
    record = make_session(manager, repo, agents)
    assert manager.check_permission(record.session_id, "repo.edit") is True

    with pytest.raises(PermissionDenied) as exc:
        manager.check_permission(record.session_id, "git.push", actor="claude")
    assert exc.value.decision is Decision.DENY
    assert "not authorized" in exc.value.message

    with pytest.raises(PermissionDenied) as ask:
        manager.check_permission(record.session_id, "repo.install_deps")
    assert "approval" in ask.value.message


def test_permission_denial_is_audited(manager, repo, agents, broker):
    from synchri.broker import Credential

    record = make_session(manager, repo, agents)
    with pytest.raises(PermissionDenied):
        manager.check_permission(record.session_id, "sys.deploy", actor="codex")

    human = record.metadata["human"]
    events = broker.events(
        record.room_id, credential=Credential(human["name"], human["secret"])
    )["events"]
    denials = [e for e in events if e["event_type"] == "session.permission_denied"]
    assert denials and denials[-1]["payload"]["capability"] == "sys.deploy"


def test_permissions_persist_across_a_restart(workspace, repo, agents):
    with Broker(workspace) as first:
        manager = SessionManager(first)
        permissions = PermissionSet.defaults().set("git.push", "allow")
        record = make_session(manager, repo, agents, permissions=permissions)
        session_id = record.session_id

    with Broker(workspace) as second:
        reloaded = SessionManager(second).get(session_id)
        assert reloaded.permissions.allows("git.push")
        assert not reloaded.permissions.allows("sys.deploy")


def test_a_mode_can_narrow_permissions_but_the_user_cannot_widen_past_it(manager, repo):
    """Review mode forbids shipping, whatever the user ticked."""
    permissions = PermissionSet.defaults().set("git.push", "allow").set("sys.deploy", "allow")
    record = manager.create(
        name="Audit",
        mode="review_audit",
        repo_root=str(repo),
        participants=[ParticipantPlan("codex", "codex", "auditor")],
        permissions=permissions,
        spec=ProductSpec(text="Audit the auth module."),
    )
    assert not record.permissions.allows("git.push")
    assert not record.permissions.allows("sys.deploy")


def test_unknown_capabilities_are_rejected_and_stale_ones_dropped():
    with pytest.raises(ValidationError):
        PermissionSet.defaults().set("does.not.exist", "allow")
    loaded = PermissionSet.from_dict({"git.push": "allow", "legacy.capability": "allow"})
    assert loaded.allows("git.push")
    assert "legacy.capability" not in loaded.to_dict()


# ----------------------------------------------------------------------
# contract
# ----------------------------------------------------------------------


def test_contract_contains_every_material_term(manager, repo, agents):
    record = make_session(manager, repo, agents)
    document = manager.issue_contract(record.session_id)
    text = document.core_text

    assert "SYNCHRI SESSION CONTRACT" in text
    assert record.session_id in text
    assert "Long Horizon Development" in text
    assert record.repo_name in text and record.base_branch in text
    assert record.worktree_name in text and record.worktree_path in text
    assert "Do not modify, stage, commit in, or switch to the primary working tree" in text
    assert "claude — Primary Builder" in text and "codex — Adversarial Reviewer" in text
    assert "Run tests" in text and "Push branches" in text
    assert "do not override restrictions imposed by your runtime" in text
    assert "AUTH-01" in text, "the canonical spec is embedded verbatim"
    assert "Do NOT escalate merely because you finished a turn" in text


def test_contract_generation_is_deterministic(manager, repo, agents):
    record = make_session(manager, repo, agents)
    first = manager.issue_contract(record.session_id)
    from synchri.session import contract as contract_module

    again = contract_module.generate(
        session_id=record.session_id,
        revision=first.revision,
        policy=record.policy,
        repo_name=record.repo_name,
        repo_root=record.repo_root,
        repo_remote=record.repo_remote,
        base_branch=record.base_branch,
        worktree=record.worktree,
        participants=record.participants,
        permissions=record.permissions,
        spec=record.spec,
        deadline=record.deadline,
        escalation=record.escalation,
        created_at=first.created_at,
    )
    assert again.core_text == first.core_text
    assert again.digest == first.digest


def test_every_agent_receives_the_same_core_contract(manager, repo, agents):
    record = make_session(manager, repo, agents)
    document = manager.issue_contract(record.session_id)

    for_claude = document.for_participant("claude")
    for_codex = document.for_participant("codex")
    assert document.core_text in for_claude and document.core_text in for_codex
    assert "YOUR ROLE: PRIMARY BUILDER" in for_claude
    assert "YOUR ROLE: ADVERSARIAL REVIEWER" in for_codex
    assert "falsify" in for_codex.lower()
    assert for_claude != for_codex


def test_contract_asks_for_acknowledgment_and_forbids_starting(manager, repo, agents):
    record = make_session(manager, repo, agents)
    text = manager.issue_contract(record.session_id).for_participant("claude")
    assert "Do not begin work yet." in text
    assert "UNDERSTOOD" in text and "CONFLICT:" in text


@pytest.mark.parametrize(
    "reply,accepted",
    [
        ("UNDERSTOOD", True),
        ("  understood  ", True),
        ("```\nUNDERSTOOD\n```", True),
        ("CONFLICT: push is required", False),
        ("conflict: no reason", False),
        ("I understood the contract and will begin now", False),
        ("", False),
        ("Sure, sounds good", False),
    ],
)
def test_acknowledgment_parsing_is_strict(reply, accepted):
    ok, conflict = parse_acknowledgment(reply)
    assert ok is accepted
    if not accepted:
        assert conflict


# ----------------------------------------------------------------------
# the activation gate
# ----------------------------------------------------------------------


def test_a_new_session_is_not_active(manager, repo, agents):
    record = make_session(manager, repo, agents)
    assert record.status == SessionStatus.CONFIGURING.value
    assert record.activated_at is None


def test_activation_requires_a_contract(manager, repo, agents):
    record = make_session(manager, repo, agents)
    with pytest.raises(StateError) as exc:
        manager.activate(record.session_id)
    assert exc.value.code == "no_contract"


def test_activation_waits_for_every_acknowledgment(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    manager.acknowledge(record.session_id, "claude", ACK_TOKEN)

    with pytest.raises(StateError) as exc:
        manager.activate(record.session_id)
    assert exc.value.code == "awaiting_acknowledgment"
    assert "codex" in exc.value.message

    manager.acknowledge(record.session_id, "codex", ACK_TOKEN)
    assert manager.activate(record.session_id).status == SessionStatus.ACTIVE.value


def test_a_conflict_blocks_activation_and_is_visible(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    manager.acknowledge(record.session_id, "claude", ACK_TOKEN)
    manager.acknowledge(
        record.session_id, "codex", "CONFLICT: you asked for a PR but did not grant push"
    )

    with pytest.raises(StateError) as exc:
        manager.activate(record.session_id)
    assert exc.value.code == "contract_conflict"

    state = manager.acknowledgment_state(record.session_id)
    assert state["conflicts"][0]["participant"] == "codex"
    assert "did not grant push" in state["conflicts"][0]["reason"]


def test_resolving_a_conflict_needs_a_new_contract_and_fresh_acknowledgment(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    manager.acknowledge(record.session_id, "claude", ACK_TOKEN)
    manager.acknowledge(record.session_id, "codex", "CONFLICT: push not granted")

    manager.update_configuration(
        record.session_id,
        permissions=PermissionSet.defaults().set("git.push", "allow"),
        reason="granted push after codex objected",
    )
    state = manager.acknowledgment_state(record.session_id)
    assert state["revision"] == 2
    assert state["accepted"] == [], "revision 1 agreement does not carry over"
    assert sorted(state["waiting"]) == ["claude", "codex"]

    accept_all(manager, manager.get(record.session_id))
    assert manager.activate(record.session_id).status == SessionStatus.ACTIVE.value


def test_activation_fails_if_the_worktree_disappeared(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    worktree_module.remove(record.worktree, force=True)

    with pytest.raises(StateError) as exc:
        manager.activate(record.session_id)
    assert exc.value.code in {"worktree_missing", "worktree_invalid"}


def test_activation_is_idempotent(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    first = manager.activate(record.session_id)
    assert manager.activate(record.session_id).activated_at == first.activated_at


# ----------------------------------------------------------------------
# configuration changes
# ----------------------------------------------------------------------


def test_changing_permissions_creates_a_revision_and_deactivates(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    manager.activate(record.session_id)

    result = manager.update_configuration(
        record.session_id, permissions=PermissionSet.defaults().set("git.push", "allow")
    )
    assert result["revision"] == 2 and result["requires_reacknowledgment"]
    assert manager.get(record.session_id).status == SessionStatus.AWAITING_ACK.value
    assert "Push branches" in manager.current_contract(record.session_id).core_text


def test_the_spec_is_versioned_not_overwritten(manager, repo, agents):
    record = make_session(manager, repo, agents)
    original = record.spec
    revised = original.revise(text=SPEC_TEXT + "\n- AUTH-02 logout")

    manager.update_configuration(record.session_id, spec=revised, reason="spec updated")
    stored = manager.get(record.session_id).spec
    assert stored.version == 2 and stored.digest != original.digest
    assert original.text == SPEC_TEXT, "the earlier version object is untouched"
    assert "AUTH-02" in manager.current_contract(record.session_id).core_text


def test_the_spec_digest_changes_with_content():
    first = ProductSpec(text="build a thing")
    assert first.digest == ProductSpec(text="build a thing").digest
    assert first.digest != ProductSpec(text="build another thing").digest


def test_every_contract_revision_is_retained(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    manager.update_configuration(
        record.session_id, permissions=PermissionSet.defaults().set("git.push", "allow")
    )
    first = manager.contract_revision(record.session_id, 1)
    second = manager.contract_revision(record.session_id, 2)
    assert first.digest != second.digest
    assert re.search(r"Push branches\s+NO$", first.core_text, re.M)
    assert re.search(r"Push branches\s+YES$", second.core_text, re.M)


# ----------------------------------------------------------------------
# required fields
# ----------------------------------------------------------------------


def test_long_horizon_requires_a_spec_and_a_deadline(manager, repo, agents):
    with pytest.raises(ValidationError, match="specification"):
        manager.create(
            name="x", mode="long_horizon", repo_root=str(repo), participants=agents,
            deadline=Deadline.from_duration("2 hours"),
        )
    with pytest.raises(ValidationError, match="deadline"):
        manager.create(
            name="x", mode="long_horizon", repo_root=str(repo), participants=agents,
            spec=ProductSpec(text=SPEC_TEXT),
        )


def test_long_horizon_requires_two_agents(manager, repo):
    with pytest.raises(ValidationError, match="at least 2"):
        manager.create(
            name="x", mode="long_horizon", repo_root=str(repo),
            participants=[ParticipantPlan("claude", "claude_code", "primary_builder")],
            spec=ProductSpec(text=SPEC_TEXT), deadline=Deadline.from_duration("2 hours"),
        )


def test_an_empty_spec_is_rejected():
    with pytest.raises(ValidationError):
        ProductSpec(text="   ")


def test_a_failed_creation_leaves_no_orphan_worktree(manager, repo, agents, monkeypatch):
    before = worktree_module.list_session_worktrees(repo)
    monkeypatch.setattr(
        manager.broker, "create_room", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        make_session(manager, repo, agents)
    assert worktree_module.list_session_worktrees(repo) == before


# ----------------------------------------------------------------------
# deadlines
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [("10 hours", 36000), ("1h", 3600), ("90 minutes", 5400), ("2h30m", 9000), ("1 day", 86400)],
)
def test_duration_parsing(text, seconds):
    assert parse_duration(text) == seconds


def test_unparseable_durations_are_rejected():
    for bad in ["", "soon", "later today", "0 hours"]:
        with pytest.raises(ValidationError):
            parse_duration(bad)


def test_a_fixed_deadline_in_the_past_is_rejected():
    with pytest.raises(ValidationError, match="past"):
        Deadline.from_timestamp("2000-01-01T00:00:00Z")


def test_deadline_phases_advance_with_elapsed_time():
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc)
    deadline = Deadline.from_duration("10 hours", now=start)
    phases = [
        deadline.phase(now=start + timedelta(hours=h))[0] for h in (0, 4, 8, 9.5)
    ]
    assert phases == ["exploration", "implementation", "stabilisation", "freeze"]
    assert deadline.phase(now=start + timedelta(hours=11))[0] == "expired"


def test_an_expired_deadline_never_produces_a_completion_claim(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.expire(record.session_id)

    reloaded = manager.get(record.session_id)
    assert reloaded.status == SessionStatus.TIMED_OUT.value
    report = manager.handoff_report(record.session_id)
    assert report["complete"] is False
    assert report["reason_stopped"] == "deadline reached"
    assert report["gates"]["unmet"] == ["AUTH-01"]
    assert report["branch"] and report["worktree"]
    assert report["recommended_next_action"]


def test_a_session_cannot_activate_after_its_deadline(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    manager._update(
        record.session_id,
        deadline=json.dumps(
            {"ends_at": "2000-01-01T00:00:00.000Z", "started_at": "1999-01-01T00:00:00.000Z",
             "source": "fixed"}
        ),
    )
    # Reading the session sweeps it: a live session past its deadline ends as
    # timed_out before anything can activate it.
    with pytest.raises(StateError) as exc:
        manager.activate(record.session_id)
    assert exc.value.code == "session_finished"
    assert manager.get(record.session_id).status == SessionStatus.TIMED_OUT.value


# ----------------------------------------------------------------------
# gates and completion
# ----------------------------------------------------------------------


def test_agreement_alone_is_not_completion(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(
        record.session_id,
        [Gate("AUTH-01", "login works"), Gate("API-01", "CRUD works")],
    )
    # Both agents say it is fine, but nothing is evidenced.
    manager.update_gate(record.session_id, "AUTH-01", status="pass",
                        builder_assessment="done", reviewer_assessment="lgtm")
    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert "no evidence" in exc.value.message


def test_completion_requires_evidence_and_both_sign_offs(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])

    manager.update_gate(record.session_id, "AUTH-01", status="pass",
                        evidence=["tests/test_auth.py::test_login"], commits=["abc123"])
    with pytest.raises(StateError, match="sign-off"):
        manager.complete(record.session_id)

    manager.update_gate(record.session_id, "AUTH-01", builder_assessment="implemented")
    with pytest.raises(StateError, match="reviewer"):
        manager.complete(record.session_id)

    manager.update_gate(record.session_id, "AUTH-01", reviewer_assessment="verified independently")
    assert manager.complete(record.session_id).status == SessionStatus.COMPLETE.value


def test_unverified_is_not_a_pass(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.update_gate(record.session_id, "AUTH-01", status="unverified")
    with pytest.raises(StateError):
        manager.complete(record.session_id)
    assert manager.handoff_report(record.session_id)["gates"]["unverified"] == ["AUTH-01"]


def test_a_session_with_no_gates_cannot_complete(manager, repo, agents):
    # A spec with no detectable criteria yields no gates to extract.
    record = make_session(manager, repo, agents, spec=ProductSpec(text="Make it nicer."))
    assert manager.gates(record.session_id) == []
    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert exc.value.code == "no_gates"


def test_gate_summary_lists_concrete_blockers():
    gates = [
        Gate("A", "a", status="pass", evidence=["t"], builder_assessment="b",
             reviewer_assessment="r"),
        Gate("B", "b", status="fail"),
        Gate("C", "c", required=False),
    ]
    report = summarize(gates)
    assert report["complete"] is False
    assert "B is fail" in report["blockers"]
    assert report["satisfied"] == 1 and report["required"] == 2


# ----------------------------------------------------------------------
# escalation
# ----------------------------------------------------------------------


def test_mandatory_escalation_rules_cannot_be_removed():
    policy = EscalationPolicy(enabled=["spec_ambiguity"])
    assert "deadline_reached" in policy.enabled
    assert "destructive_action" in policy.enabled


def test_the_contract_forbids_escalating_just_to_continue():
    block = EscalationPolicy().render_contract_block()
    assert "Do NOT escalate merely because you finished a turn" in block


def test_escalations_are_recorded_and_surface_on_the_dashboard(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.escalate(record.session_id, "spec_ambiguity", "AUTH-01 and API-01 conflict", "codex")
    dashboard = manager.dashboard(record.session_id)
    assert len(dashboard["open_escalations"]) == 1
    assert dashboard["user_intervention_required"] is True


# ----------------------------------------------------------------------
# presets
# ----------------------------------------------------------------------


def test_presets_round_trip(workspace, repo, agents):
    draft = SessionDraft()
    draft.set_mode("long_horizon").set_repository(str(repo))
    draft.set_agents(agents)
    draft.set_permission("git.push", "allow")

    presets_module.save(workspace, "Claude + Codex Safe Build", draft.to_preset())
    loaded = SessionDraft.from_preset(presets_module.load(workspace, "Claude + Codex Safe Build"))

    assert loaded.mode == "long_horizon"
    assert [p.name for p in loaded.participants] == ["claude", "codex"]
    assert loaded.participants[0].role == "primary_builder"
    assert loaded.permissions.allows("git.push")


def test_presets_never_store_session_specific_state(workspace, repo, agents):
    draft = SessionDraft()
    draft.set_mode("long_horizon").set_repository(str(repo))
    draft.set_agents(agents)
    draft.set_spec(SPEC_TEXT)
    draft.set_deadline_duration("10 hours")

    path = presets_module.save(workspace, "No leakage", draft.to_preset())
    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    for forbidden in ("spec", "deadline", "worktree", "session_id", "repo_path"):
        assert forbidden not in stored


def test_loading_a_missing_preset_is_not_found(workspace):
    with pytest.raises(NotFoundError):
        presets_module.load(workspace, "nope")


# ----------------------------------------------------------------------
# restart
# ----------------------------------------------------------------------


def test_a_session_survives_a_restart_intact(workspace, repo, agents):
    with Broker(workspace) as first:
        manager = SessionManager(first)
        record = make_session(manager, repo, agents)
        manager.issue_contract(record.session_id)
        accept_all(manager, record)
        manager.activate(record.session_id)
        manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
        session_id = record.session_id
        digest = manager.current_contract(session_id).digest

    with Broker(workspace) as second:
        manager = SessionManager(second)
        reloaded = manager.get(session_id)
        assert reloaded.status == SessionStatus.ACTIVE.value
        assert reloaded.worktree_path == record.worktree_path
        assert reloaded.spec.text == SPEC_TEXT
        assert reloaded.deadline is not None
        assert [p.name for p in reloaded.participants] == ["claude", "codex"]
        assert manager.current_contract(session_id).digest == digest
        assert manager.acknowledgment_state(session_id)["all_accepted"]
        assert [g.gate_id for g in manager.gates(session_id)] == ["AUTH-01"]


def test_restore_reports_state_without_resuming(workspace, repo, agents):
    with Broker(workspace) as first:
        manager = SessionManager(first)
        record = make_session(manager, repo, agents)
        manager.issue_contract(record.session_id)
        accept_all(manager, record)
        manager.activate(record.session_id)
        session_id = record.session_id

    with Broker(workspace) as second:
        report = SessionManager(second).restore(session_id)
        assert report["was_active"] is True
        assert report["worktree_present"] is True
        assert report["resumable"] is True
        assert sorted(report["participants_must_reconnect"]) == ["claude", "codex"]


def test_restore_flags_a_missing_worktree(workspace, repo, agents):
    with Broker(workspace) as first:
        manager = SessionManager(first)
        record = make_session(manager, repo, agents)
        worktree_module.remove(record.worktree, force=True)
        session_id = record.session_id

    with Broker(workspace) as second:
        report = SessionManager(second).restore(session_id)
        assert report["worktree_present"] is False
        assert report["resumable"] is False
        assert any("worktree is gone" in p for p in report["problems"])


def test_restore_flags_a_deadline_that_passed_while_offline(workspace, repo, agents):
    with Broker(workspace) as first:
        manager = SessionManager(first)
        record = make_session(manager, repo, agents)
        manager._update(
            record.session_id,
            deadline=json.dumps(
                {"ends_at": "2000-01-01T00:00:00.000Z",
                 "started_at": "1999-01-01T00:00:00.000Z", "source": "fixed"}
            ),
        )
        session_id = record.session_id

    with Broker(workspace) as second:
        report = SessionManager(second).restore(session_id)
        assert report["deadline_expired"] is True
        assert report["resumable"] is False


# ----------------------------------------------------------------------
# stopped participants
# ----------------------------------------------------------------------


def test_a_removed_participant_cannot_keep_acting(manager, repo, agents, broker):
    """The room-level guarantee still holds under a session."""
    from synchri.broker import Credential
    from synchri.errors import AuthError
    from synchri.models.envelope import MessageDraft

    record = make_session(manager, repo, agents)
    human = record.metadata["human"]
    human_credential = Credential(human["name"], human["secret"])
    invite = next(i for i in record.metadata["invites"] if i["participant_name"] == "codex")
    joined = broker.join(invite["token"], "codex")
    codex = Credential("codex", joined["secret"])

    broker.remove_participant(record.room_id, "codex", credential=human_credential)
    with pytest.raises(AuthError):
        broker.send(record.room_id, credential=codex, draft=MessageDraft(content="still here"))


def test_acknowledging_as_a_non_participant_is_rejected(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    with pytest.raises(NotFoundError):
        manager.acknowledge(record.session_id, "gemini", ACK_TOKEN)


# ----------------------------------------------------------------------
# the wizard
# ----------------------------------------------------------------------


def test_the_wizard_blocks_until_every_requirement_is_met(repo, agents):
    draft = SessionDraft()
    assert not draft.is_ready

    draft.set_mode("long_horizon")
    assert "choose a repository" in " ".join(draft.blocking_problems())

    draft.set_repository(str(repo))
    assert any("at least 2 agents" in p for p in draft.blocking_problems())

    draft.set_agents(agents)
    problems = " ".join(draft.blocking_problems())
    assert "specification" in problems and "deadline" in problems

    draft.set_spec(SPEC_TEXT)
    draft.set_deadline_duration("10 hours")
    assert draft.is_ready and draft.blocking_problems() == []


def test_the_wizard_hides_steps_a_mode_does_not_need(repo):
    review = SessionDraft()
    review.set_mode("review_audit")
    keys = [key for key, _ in review.visible_steps()]
    assert "deadline" in keys, "review still allows an optional deadline"

    long_horizon = SessionDraft()
    long_horizon.set_mode("long_horizon")
    labels = dict(long_horizon.visible_steps())
    assert labels["spec"] == "Describe what to build"


def test_the_wizard_can_go_back_and_change_earlier_choices(repo, agents):
    draft = SessionDraft()
    draft.set_mode("long_horizon").set_repository(str(repo)).set_agents(agents)
    draft.set_spec(SPEC_TEXT).set_deadline_duration("10 hours")
    assert draft.is_ready

    draft.set_mode("review_audit")
    assert draft.mode == "review_audit"
    assert draft.is_ready, "review needs fewer things, so it stays ready"
    assert not draft.permissions.allows("git.push"), "the new mode's denials applied"


def test_the_review_screen_warns_about_a_dirty_tree_and_risky_grants(repo, agents):
    (repo / "wip.txt").write_text("x", encoding="utf-8")
    draft = SessionDraft()
    draft.set_mode("long_horizon").set_repository(str(repo)).set_agents(agents)
    draft.set_spec(SPEC_TEXT).set_deadline_duration("10 hours")
    draft.set_permission("git.push", "allow")

    warnings = " ".join(draft.summary()["warnings"])
    assert "uncommitted change" in warnings
    assert "push branches" in warnings.lower()


def test_the_wizard_rejects_a_bad_repository(tmp_path):
    draft = SessionDraft()
    draft.set_mode("long_horizon")
    with pytest.raises(ValidationError):
        draft.set_repository(str(tmp_path / "nope"))


def test_the_wizard_rejects_duplicate_agent_names():
    draft = SessionDraft()
    draft.set_mode("long_horizon")
    with pytest.raises(ValidationError):
        draft.set_agents([ParticipantPlan("claude"), ParticipantPlan("claude")])


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def run_cli(workspace, *args):
    from synchri.cli.main import main

    return main(["--home", str(workspace.home), *args])


def test_cli_start_runs_the_whole_flow(workspace, repo, capsys):
    code = run_cli(
        workspace, "--json", "start", "--yes", "--mode", "long_horizon",
        "--repo", str(repo), "--agent", "claude:claude_code:primary_builder",
        "--agent", "codex:codex:adversarial_reviewer", "--spec", SPEC_TEXT,
        "--deadline", "10 hours", "--name", "PR 89",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    session_id = payload["session"]["session_id"]
    assert payload["session"]["status"] == "awaiting_ack"
    assert payload["session"]["worktree"]["path"] != payload["session"]["repository"]["root"]
    assert payload["contract"]["revision"] == 1

    code = run_cli(workspace, "--json", "session", "activate", "--session", session_id)
    err = capsys.readouterr()
    assert code == 6, "cannot activate before acknowledgment"

    for agent in ("claude", "codex"):
        run_cli(workspace, "--json", "session", "ack", agent, "--reply", "UNDERSTOOD",
                "--session", session_id)
        capsys.readouterr()

    assert run_cli(workspace, "--json", "session", "activate", "--session", session_id) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"


def test_cli_start_refuses_incomplete_configuration(workspace, repo, capsys):
    code = run_cli(
        workspace, "--json", "start", "--yes", "--mode", "long_horizon",
        "--repo", str(repo), "--agent", "claude:claude_code:primary_builder",
        "--agent", "codex:codex:adversarial_reviewer",
    )
    capsys.readouterr()
    assert code == 2, "missing spec and deadline must block"


def test_cli_dry_run_creates_nothing(workspace, repo, capsys):
    before = worktree_module.list_session_worktrees(repo)
    code = run_cli(
        workspace, "--json", "start", "--yes", "--dry-run", "--mode", "review_audit",
        "--repo", str(repo), "--agent", "codex:codex:auditor", "--spec", "Audit auth.",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["dry_run"] is True
    assert worktree_module.list_session_worktrees(repo) == before


def test_cli_shows_the_contract_and_the_dashboard(workspace, repo, capsys):
    run_cli(
        workspace, "--json", "start", "--yes", "--mode", "long_horizon", "--repo", str(repo),
        "--agent", "claude:claude_code:primary_builder",
        "--agent", "codex:codex:adversarial_reviewer",
        "--spec", SPEC_TEXT, "--deadline", "10 hours",
    )
    capsys.readouterr()

    assert run_cli(workspace, "session", "contract", "--as", "codex") == 0
    contract = capsys.readouterr().out
    assert "SYNCHRI SESSION CONTRACT" in contract
    assert "YOUR ROLE: ADVERSARIAL REVIEWER" in contract

    assert run_cli(workspace, "session", "dashboard") == 0
    dashboard = capsys.readouterr().out
    assert "Session status     AWAITING_ACK" in dashboard
    assert "User intervention  REQUIRED" in dashboard


def test_cli_saves_and_reuses_a_preset(workspace, repo, capsys):
    run_cli(
        workspace, "--json", "start", "--yes", "--dry-run", "--mode", "long_horizon",
        "--repo", str(repo), "--agent", "claude:claude_code:primary_builder",
        "--agent", "codex:codex:adversarial_reviewer", "--spec", SPEC_TEXT,
        "--deadline", "10 hours", "--save-preset", "Safe Build",
    )
    capsys.readouterr()

    assert run_cli(workspace, "--json", "preset", "list") == 0
    presets = json.loads(capsys.readouterr().out)["presets"]
    assert presets[0]["name"] == "Safe Build"

    code = run_cli(
        workspace, "--json", "start", "--yes", "--preset", "Safe Build", "--repo", str(repo),
        "--spec", SPEC_TEXT, "--deadline", "4 hours",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [p["name"] for p in payload["session"]["participants"]] == ["claude", "codex"]


# ----------------------------------------------------------------------
# gate extraction from the specification
# ----------------------------------------------------------------------


def test_gates_are_extracted_from_explicit_ids(manager, repo, agents):
    record = make_session(manager, repo, agents)
    gates = manager.gates(record.session_id)
    assert [g.gate_id for g in gates] == ["API-01", "AUTH-01"]
    assert all(g.status == "pending" and not g.has_evidence for g in gates), (
        "extraction proposes gates; it never claims they pass"
    )


def test_gates_are_extracted_from_an_acceptance_section():
    from synchri.session.extract import extract_gates

    gates = extract_gates(
        "# Thing\n\nSome prose.\n\n## Acceptance Criteria\n"
        "- users can sign in\n- [ ] sessions expire\n\n## Non-goals\n- billing\n"
    )
    assert [g.gate_id for g in gates] == ["GATE-01", "GATE-02"]
    assert gates[0].description == "users can sign in"
    assert "billing" not in " ".join(g.description for g in gates), "a later section ends it"


def test_explicit_ids_win_over_bullets():
    from synchri.session.extract import extract_gates

    gates = extract_gates("## Acceptance\n- AUTH-01 log in\n- AUTH-02 log out\n")
    assert [g.gate_id for g in gates] == ["AUTH-01", "AUTH-02"]


def test_extraction_reports_when_it_finds_nothing():
    from synchri.session.extract import describe, extract_gates

    assert extract_gates("just build something good") == []
    assert "No acceptance criteria detected" in describe([])


def test_extracted_gates_can_be_replaced_by_the_user(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("CUSTOM-1", "my own criterion")])
    assert [g.gate_id for g in manager.gates(record.session_id)] == ["CUSTOM-1"]


# ----------------------------------------------------------------------
# measured evidence
# ----------------------------------------------------------------------


def test_tests_are_run_in_the_worktree_and_counted(manager, repo, agents):
    tree = None
    record = make_session(manager, repo, agents)
    tree = Path(record.worktree_path)
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tree / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_also_ok():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    result = manager.run_tests(record.session_id)
    assert result["ran"] and result["green"]
    assert result["passed"] == 2 and result["failed"] == 0
    assert manager.last_test_run(record.session_id)["passed"] == 2


def test_a_failing_suite_is_not_green(manager, repo, agents):
    record = make_session(manager, repo, agents)
    tree = Path(record.worktree_path)
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tree / "test_bad.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8",
    )
    result = manager.run_tests(record.session_id)
    assert result["passed"] == 1 and result["failed"] == 1
    assert result["green"] is False


def test_an_undetectable_test_command_is_admitted_not_faked(tmp_path):
    from synchri.session.verify import run_tests

    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_tests(plain)
    assert result.ran is False and result.green is False
    assert "could not detect" in result.detail


def test_changes_count_real_commits(manager, repo, agents):
    record = make_session(manager, repo, agents)
    tree = Path(record.worktree_path)
    for index in range(3):
        (tree / f"file{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
        worktree_module.git(tree, "add", "-A")
        worktree_module.git(
            tree, "-c", "user.email=a@e.com", "-c", "user.name=a", "commit", "-qm", f"c{index}"
        )
    changes = manager.changes(record.session_id)
    assert changes["commits"] == 3
    assert changes["files_changed"] == 3 and changes["insertions"] == 3
    assert [c["subject"] for c in changes["recent"]] == ["c2", "c1", "c0"]
    assert manager.diff(record.session_id).startswith("diff --git")


# ----------------------------------------------------------------------
# deadline sweeping without a daemon
# ----------------------------------------------------------------------


def _backdate(manager, session_id):
    manager._update(
        session_id,
        deadline=json.dumps(
            {"ends_at": "2000-01-01T00:00:00.000Z",
             "started_at": "1999-01-01T00:00:00.000Z", "source": "fixed"}
        ),
    )


def test_reading_a_session_past_its_deadline_ends_it(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    manager.activate(record.session_id)

    _backdate(manager, record.session_id)
    reloaded = manager.get(record.session_id)
    assert reloaded.status == SessionStatus.TIMED_OUT.value
    assert reloaded.ended_reason == "deadline reached"
    assert manager.handoff_report(record.session_id)["complete"] is False


def test_a_configuring_session_is_not_swept(manager, repo, agents):
    """Still in the wizard: an expired draft deadline is the user's to fix."""
    record = make_session(manager, repo, agents)
    _backdate(manager, record.session_id)
    assert manager.get(record.session_id).status == SessionStatus.CONFIGURING.value


def test_sweep_expired_ends_every_live_session(manager, repo, agents):
    first = make_session(manager, repo, agents)
    second = make_session(manager, repo, [ParticipantPlan(a.name, a.runtime, a.role) for a in agents])
    for record in (first, second):
        manager.issue_contract(record.session_id)
        accept_all(manager, record)
        manager.activate(record.session_id)
        _backdate(manager, record.session_id)

    expired = manager.sweep_expired()
    assert sorted(expired) == sorted([first.session_id, second.session_id])
    assert manager.sweep_expired() == [], "already-ended sessions are not swept twice"


# ----------------------------------------------------------------------
# repository discovery
# ----------------------------------------------------------------------


def test_local_discovery_finds_a_repository(repo, monkeypatch):
    from synchri.session import discovery

    monkeypatch.chdir(repo)
    found = discovery.local_repositories(roots=[str(repo.parent)])
    paths = [r["path"] for r in found]
    assert str(repo.resolve()) in paths
    entry = next(r for r in found if r["path"] == str(repo.resolve()))
    assert entry["name"] == "marnie" and entry["branch"] == "main"


def test_discovery_skips_synchri_worktrees(repo, monkeypatch):
    from synchri.session import discovery

    worktree_module.create(repo, "main", mode="long_horizon")
    monkeypatch.chdir(repo)
    found = discovery.local_repositories(roots=[str(repo.parent)])
    assert not any(Path(r["path"]).name.startswith("synchri-") for r in found)


def test_github_listing_degrades_when_gh_is_absent(monkeypatch):
    from synchri.session import discovery

    monkeypatch.setattr(discovery, "github_available", lambda: False)
    payload = discovery.repositories()
    assert payload["github"] == [] and payload["github_available"] is False
    assert "local" in payload, "local repositories remain fully supported"


def test_quick_clone_puts_a_github_project_in_the_desktop_folder(repo, tmp_path, monkeypatch):
    from synchri.session import discovery

    observed = []

    def fake_clone(source, target):
        observed.append((source, target))
        subprocess.run(["git", "clone", str(repo), str(target)], check=True, capture_output=True)

    monkeypatch.setattr(discovery, "_clone", fake_clone)
    desktop = tmp_path / "Desktop" / "Synchri"
    result = discovery.clone_github_repository(
        "fenixawiles/synchri", destination_root=desktop
    )

    expected = desktop / "fenixawiles-synchri"
    assert observed == [("https://github.com/fenixawiles/synchri.git", expected)]
    assert result["path"] == str(expected.resolve()) and result["cloned"] is True
    with pytest.raises(ValidationError, match="already exists"):
        discovery.clone_github_repository("fenixawiles/synchri", destination_root=desktop)


@pytest.mark.parametrize("reference", [
    "fenixawiles/synchri",
    "github.com/fenixawiles/synchri",
    "https://github.com/fenixawiles/synchri",
    "https://www.github.com/fenixawiles/synchri.git",
    "git@github.com:fenixawiles/synchri.git",
])
def test_github_reference_accepts_the_forms_people_actually_paste(reference):
    from synchri.session import discovery

    assert discovery.github_reference(reference) == (
        "fenixawiles", "synchri", "https://github.com/fenixawiles/synchri.git"
    )
