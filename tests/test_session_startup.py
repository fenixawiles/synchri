"""Startup and session initialization.

The invariants under test, in code terms:

* a session cannot activate without a validated worktree that is not the primary tree
* a session cannot activate until every participant acknowledged the CURRENT revision
* changing material terms forces a new revision and clears acknowledgments
* permissions are read from state, never inferred; ASK is not a grant
* a timebox never produces a false stop or completion claim
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
    join_all(manager, record)
    for plan in record.participants:
        manager.acknowledge(record.session_id, plan.name, ACK_TOKEN)


def join_all(manager, record):
    """Make test participants real room members before a live activation."""
    present = {
        participant["name"]
        for participant in manager.broker.room_status(
            record.room_id, credential=manager._human_credential(record)
        )["participants"]
    }
    for invite in record.metadata["invites"]:
        if invite["participant_name"] not in present:
            manager.broker.join(invite["token"], invite["participant_name"])


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


def test_existing_worktrees_are_listed_but_the_primary_checkout_is_not(repo):
    tree = worktree_module.create(repo, "main", name="synchri-existing-tree-1234")

    choices = worktree_module.list_worktrees(repo)

    assert choices == [{
        "name": tree.name,
        "path": tree.path,
        "branch": tree.branch,
        "head": tree.head,
    }]


def test_a_new_session_can_intentionally_use_an_existing_worktree(manager, repo, agents):
    first = make_session(manager, repo, agents)
    second = make_session(
        manager,
        repo,
        agents,
        name="Follow-up review",
        existing_worktree_path=first.worktree_path,
    )

    assert second.worktree_path == first.worktree_path
    assert second.worktree_branch == first.worktree_branch
    assert second.metadata["worktree_strategy"] == "existing"


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


def test_human_can_approve_an_ask_capability_without_widening_a_denial(manager, repo, agents):
    record = make_session(manager, repo, agents)

    approved = manager.approve_capability(record.session_id, "repo.install_deps")

    assert manager.check_permission(approved.session_id, "repo.install_deps", actor="claude") is True
    assert approved.metadata["approved_capabilities"]["repo.install_deps"]["approved_by"] == "human"
    with pytest.raises(StateError) as exc:
        manager.approve_capability(record.session_id, "git.push")
    assert exc.value.code == "permission_denied_by_workflow"


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


def test_permission_profiles_are_explicit_and_keep_external_authority_outside_synchri():
    from synchri.session.permissions import permission_profile, permission_profiles

    profiles = {profile["key"]: profile for profile in permission_profiles()}
    assert {"important_only", "cautious", "god_mode"} <= set(profiles)
    assert profiles["god_mode"]["warning"]
    assert all(value == "allow" for value in permission_profile("god_mode").decisions().values())
    assert permission_profile("important_only").decisions()["repo.edit"] == "allow"
    assert permission_profile("important_only").decisions()["sys.deploy"] == "ask"
    assert permission_profile("cautious").decisions()["git.push"] == "deny"


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

    join_all(manager, record)
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


def test_activation_starts_the_builder_with_an_opening_task(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)

    manager.activate(record.session_id)

    messages = manager.broker.read(
        record.room_id, credential=manager._human_credential(record)
    )["messages"]
    opening = messages[-1]
    assert opening["sender"] == "human"
    assert opening["target"] == "claude"
    assert opening["message_type"] == "task"
    assert "Begin working on the specification" in opening["content"]
    assert manager.dashboard(record.session_id)["current_actor"] == "claude"


def test_audit_activation_starts_the_auditor_not_a_builder(manager, repo):
    record = manager.create(
        name="Audit",
        mode="review_audit",
        repo_root=str(repo),
        participants=[
            ParticipantPlan("codex", "codex", "auditor"),
            ParticipantPlan("claude", "claude_code", "adversarial_reviewer"),
        ],
        spec=ProductSpec(text="Audit the authentication boundary."),
    )
    manager.issue_contract(record.session_id)
    accept_all(manager, record)

    manager.activate(record.session_id)

    opening = manager.broker.read(
        record.room_id, credential=manager._human_credential(record)
    )["messages"][-1]
    assert opening["target"] == "codex"
    assert "Begin the audit" in opening["content"]
    assert "Do not implement product changes" in opening["content"]


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


def test_long_horizon_requires_a_spec_but_not_a_timebox(manager, repo, agents):
    with pytest.raises(ValidationError, match="specification"):
        manager.create(
            name="x", mode="long_horizon", repo_root=str(repo), participants=agents,
            deadline=Deadline.from_duration("2 hours"),
        )
    record = manager.create(
        name="x", mode="long_horizon", repo_root=str(repo), participants=agents,
        spec=ProductSpec(text=SPEC_TEXT),
    )
    assert record.deadline is None


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
    assert deadline.phase(now=start + timedelta(hours=11))[0] == "elapsed"


def test_extending_a_timebox_preserves_the_original_start_and_adds_time():
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc)
    deadline = Deadline.from_duration("2 hours", now=start)
    extended = deadline.extend("30 minutes", now=start + timedelta(minutes=10))

    assert extended.started_at == deadline.started_at
    assert extended.total_seconds() == deadline.total_seconds() + 1800
    assert extended.seconds_remaining(now=start + timedelta(minutes=10)) > deadline.seconds_remaining(
        now=start + timedelta(minutes=10)
    )


def test_extending_an_elapsed_timebox_starts_a_new_advisory_window():
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc)
    deadline = Deadline.from_duration("10 minutes", now=start)
    after_elapsed = start + timedelta(minutes=20)
    extended = deadline.extend("30 minutes", now=after_elapsed)

    assert extended.seconds_remaining(now=after_elapsed) >= 1799
    assert extended.total_seconds() > deadline.total_seconds()


def test_extending_an_active_timebox_does_not_reissue_the_contract(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    join_all(manager, record)
    active = manager.activate(record.session_id)

    extended = manager.extend_timebox(active.session_id, "30 minutes")

    assert extended.status == SessionStatus.ACTIVE.value
    assert extended.contract_revision == active.contract_revision
    assert extended.deadline and extended.deadline.total_seconds() >= active.deadline.total_seconds() + 1800


def test_an_elapsed_timebox_never_stops_or_claims_completion(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.expire(record.session_id)

    reloaded = manager.get(record.session_id)
    assert reloaded.status == SessionStatus.CONFIGURING.value
    report = manager.handoff_report(record.session_id)
    assert report["complete"] is False
    assert report["reason_stopped"] is None
    assert report["gates"]["unmet"] == ["AUTH-01"]
    assert report["branch"] and report["worktree"]
    assert report["recommended_next_action"]


def test_a_session_can_activate_after_its_timebox(manager, repo, agents):
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
    join_all(manager, record)
    assert manager.activate(record.session_id).status == SessionStatus.ACTIVE.value


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
    completed = manager.complete(record.session_id)
    assert completed.status == SessionStatus.COMPLETE.value
    room = manager.broker.room_status(record.room_id, credential=manager._human_credential(record))
    assert room["room"]["status"] == "stopped"
    changelog = manager.final_changelog(record.session_id)
    assert "# Synchri final changelog" in changelog["markdown"]
    assert "AUTH-01" in changelog["markdown"]
    assert changelog["path"] == str(manager.broker.workspace.final_changelog_path(record.room_id))
    assert manager.broker.workspace.final_changelog_path(record.room_id).stat().st_mode & 0o777 == 0o600
    event_types = [event["event_type"] for event in manager.broker.events(
        record.room_id, credential=manager._human_credential(record)
    )["events"]]
    assert "session.completed" in event_types


def test_agent_gate_reports_merge_evidence_and_primary_can_complete(manager, repo, agents):
    """Agent progress is durable data, not just text emitted into the room."""
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])

    builder = manager.report_gate(
        record.session_id,
        "AUTH-01",
        actor="claude",
        status="in_progress",
        assessment="implemented the login flow",
        evidence=["src/auth.py"],
        tests=["pytest tests/test_auth.py::test_login"],
        commits=["abc123"],
    )
    assert builder.status == "in_progress"
    assert builder.builder_assessment == "implemented the login flow"
    assert builder.tests == ["pytest tests/test_auth.py::test_login"]

    reviewer = manager.report_gate(
        record.session_id,
        "AUTH-01",
        actor="codex",
        status="pass",
        assessment="verified independently",
        evidence=["reviewed the login boundary"],
    )
    assert reviewer.status == "pass"
    assert reviewer.builder_assessment == "implemented the login flow"
    assert reviewer.reviewer_assessment == "verified independently"
    assert set(reviewer.evidence) == {"src/auth.py", "reviewed the login boundary"}

    completed = manager.complete_by_agent(record.session_id, "claude")
    assert completed.status == SessionStatus.COMPLETE.value


def test_only_the_primary_builder_can_request_agent_completion(manager, repo, agents):
    record = make_session(manager, repo, agents)
    with pytest.raises(StateError) as exc:
        manager.complete_by_agent(record.session_id, "codex")
    assert exc.value.code == "agent_not_authorized"


def test_stopping_a_session_also_stops_its_room(manager, repo, agents):
    record = make_session(manager, repo, agents)
    stopped = manager.stop(record.session_id)
    assert stopped.status == SessionStatus.STOPPED.value
    room = manager.broker.room_status(record.room_id, credential=manager._human_credential(record))
    assert room["room"]["status"] == "stopped"


def test_force_complete_waives_blocking_gates_and_records_the_override(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [
        Gate("AUTH-01", "login works"),
        Gate("API-01", "crud works", status="pass", evidence=["tests/test_api.py"],
             builder_assessment="implemented", reviewer_assessment="verified independently"),
    ])

    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert exc.value.code == "gates_unsatisfied"
    assert not manager.get(record.session_id).is_terminal

    completed = manager.complete(record.session_id, force=True)
    assert completed.status == SessionStatus.COMPLETE.value
    assert completed.ended_reason == "completed by the user (1 gate waived)"

    gates = {gate.gate_id: gate for gate in manager.gates(record.session_id)}
    assert gates["AUTH-01"].status == "waived"
    assert "Waived by the user at completion" in gates["AUTH-01"].evidence
    # A gate that was genuinely satisfied keeps its verified status.
    assert gates["API-01"].status == "pass"

    changelog = manager.final_changelog(record.session_id)
    assert "AUTH-01" in changelog["markdown"]
    assert "waived" in changelog["markdown"].lower()


def test_racing_stop_and_complete_finish_the_session_exactly_once(manager, repo, agents, workspace):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [
        Gate("AUTH-01", "login works", status="pass", evidence=["tests/test_auth.py"],
             builder_assessment="implemented", reviewer_assessment="verified"),
        Gate("API-01", "crud works", status="pass", evidence=["tests/test_api.py"],
             builder_assessment="implemented", reviewer_assessment="verified"),
    ])

    other_broker = Broker(workspace)
    other_manager = SessionManager(other_broker)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def run(label, action):
        barrier.wait()
        try:
            outcomes[label] = action()
        except StateError as error:
            outcomes[label] = error

    threads = [
        threading.Thread(target=run, args=("stop", lambda: manager.stop(record.session_id))),
        threading.Thread(
            target=run, args=("complete", lambda: other_manager.complete(record.session_id))
        ),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        other_broker.close()

    final = manager.get(record.session_id)
    assert final.is_terminal

    # The terminal transition happened exactly once, whichever caller won.
    ended = [
        event
        for event in manager.broker.events(
            record.room_id, credential=manager._human_credential(record)
        )["events"]
        if event["event_type"] == "session.ended"
    ]
    assert len(ended) == 1

    changelog_path = manager.broker.workspace.final_changelog_path(record.room_id)
    if final.status == SessionStatus.COMPLETE.value:
        assert changelog_path.exists()
        assert not isinstance(outcomes["stop"], Exception)
    else:
        assert final.status == SessionStatus.STOPPED.value
        assert not changelog_path.exists()
        loser = outcomes["complete"]
        assert isinstance(loser, StateError)
        assert loser.code == "session_finished"


def test_unverified_is_not_a_pass(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.set_gates(record.session_id, [Gate("AUTH-01", "login works")])
    manager.update_gate(record.session_id, "AUTH-01", status="unverified")
    with pytest.raises(StateError):
        manager.complete(record.session_id)
    assert manager.handoff_report(record.session_id)["gates"]["unverified"] == ["AUTH-01"]


def test_plain_text_spec_gets_one_honest_generic_gate(manager, repo, agents):
    # Plain text is a valid brief, even when it does not contain formal IDs.
    # It still needs evidence before a session can complete.
    record = make_session(manager, repo, agents, spec=ProductSpec(text="Make it nicer."))
    gates = manager.gates(record.session_id)
    assert [(gate.gate_id, gate.description) for gate in gates] == [
        ("SPEC-01", "Deliver the supplied specification.")
    ]
    with pytest.raises(StateError) as exc:
        manager.complete(record.session_id)
    assert exc.value.code == "gates_unsatisfied"


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


def test_safety_escalation_rules_cannot_be_removed():
    policy = EscalationPolicy(enabled=["spec_ambiguity"])
    assert "deadline_reached" not in policy.enabled
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
    assert stored["deadline_duration"] == "10 hours"


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


def test_restore_reports_an_elapsed_timebox_without_blocking_resume(workspace, repo, agents):
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
        assert report["resumable"] is True


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
    assert "specification" in problems and "deadline" not in problems

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
    assert labels["spec"] == "Describe the work"


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

    with Broker(workspace) as live:
        join_all(SessionManager(live), SessionManager(live).get(session_id))

    assert run_cli(workspace, "--json", "session", "activate", "--session", session_id) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"


def test_cli_start_refuses_incomplete_configuration(workspace, repo, capsys):
    code = run_cli(
        workspace, "--json", "start", "--yes", "--mode", "long_horizon",
        "--repo", str(repo), "--agent", "claude:claude_code:primary_builder",
        "--agent", "codex:codex:adversarial_reviewer",
    )
    capsys.readouterr()
    assert code == 2, "missing specification must block"


def test_cli_dry_run_creates_nothing(workspace, repo, capsys):
    before = worktree_module.list_session_worktrees(repo)
    code = run_cli(
        workspace, "--json", "start", "--yes", "--dry-run", "--mode", "long_horizon",
        "--repo", str(repo), "--agent", "codex:codex:primary_builder",
        "--agent", "copilot:copilot:adversarial_reviewer", "--spec", "Audit auth.",
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

    gates = extract_gates("just build something good")
    assert [gate.gate_id for gate in gates] == ["SPEC-01"]
    assert "SPEC-01" in describe(gates)


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


def test_reading_a_session_past_its_timebox_keeps_it_live(manager, repo, agents):
    record = make_session(manager, repo, agents)
    manager.issue_contract(record.session_id)
    accept_all(manager, record)
    manager.activate(record.session_id)

    _backdate(manager, record.session_id)
    reloaded = manager.get(record.session_id)
    assert reloaded.status == SessionStatus.ACTIVE.value
    assert reloaded.ended_reason is None
    assert manager.handoff_report(record.session_id)["complete"] is False


def test_a_configuring_session_is_not_swept(manager, repo, agents):
    """Still in the wizard: an expired draft deadline is the user's to fix."""
    record = make_session(manager, repo, agents)
    _backdate(manager, record.session_id)
    assert manager.get(record.session_id).status == SessionStatus.CONFIGURING.value


def test_sweep_expired_is_a_timebox_noop(manager, repo, agents):
    first = make_session(manager, repo, agents)
    second = make_session(manager, repo, [ParticipantPlan(a.name, a.runtime, a.role) for a in agents])
    for record in (first, second):
        manager.issue_contract(record.session_id)
        accept_all(manager, record)
        manager.activate(record.session_id)
        _backdate(manager, record.session_id)

    assert manager.sweep_expired() == []
    assert manager.get(first.session_id).status == SessionStatus.ACTIVE.value
    assert manager.get(second.session_id).status == SessionStatus.ACTIVE.value


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


def test_native_github_status_never_requires_the_github_cli(workspace, monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(github_auth, "credentials", lambda _workspace: None)
    status = discovery.github_status(workspace)

    assert status["installed"] is True
    assert status["authenticated"] is False
    assert "CLI" not in status["message"]
    assert status["resolution"]["kind"] == "connect_github"


def test_device_authorization_returns_a_user_code(monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(
        github_auth,
        "_post_form",
        lambda _url, fields: {
            "device_code": "device-secret", "user_code": "WXYZ-1234",
            "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 5,
        },
    )
    login = discovery.begin_github_login()

    assert login["started"] is True
    assert login["user_code"] == "WXYZ-1234"
    # The standalone discovery helper is intentionally lower-level than the
    # local UI. The UI API wraps it and keeps this secret server-side.
    assert login["device_code"] == "device-secret"
    assert login["verification_uri"] == "https://github.com/login/device"


def test_device_authorization_explains_when_the_github_app_has_not_enabled_device_flow(monkeypatch):
    from synchri.errors import StateError
    from synchri.session import github_auth

    monkeypatch.setattr(
        github_auth,
        "_post_form",
        lambda _url, _fields: {"error": "device_flow_disabled"},
    )

    with pytest.raises(StateError) as exc:
        github_auth.start_device_authorization()

    assert exc.value.code == "github_device_flow_disabled"
    assert exc.value.details["resolution"] == {"kind": "github_app_settings"}


def test_github_tls_context_requires_verified_certificates():
    import ssl

    from synchri.session import github_auth

    context = github_auth.trusted_ssl_context()

    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_device_authorization_persists_credentials_without_exposing_them(workspace, monkeypatch):
    from synchri.session import github_auth

    written = {}
    monkeypatch.setattr(github_auth, "_save_credentials", lambda _workspace, value: written.update(value))
    monkeypatch.setattr(
        github_auth,
        "_post_form",
        lambda _url, _fields: {
            "access_token": "ghu_private", "refresh_token": "ghr_private", "expires_in": 28_800,
        },
    )
    monkeypatch.setattr(
        github_auth, "_account_for_token", lambda _token: {"id": 42, "login": "fenixawiles"}
    )
    result = github_auth.complete_device_authorization(workspace, "device-secret")

    assert result == {"state": "connected", "account": {"id": 42, "login": "fenixawiles"}}
    assert written["access_token"] == "ghu_private"
    assert "access_token" not in result
    assert written["account"] == {"id": 42, "login": "fenixawiles"}


def test_macos_keychain_credentials_round_trip_without_a_token_bearing_subprocess(workspace, monkeypatch):
    import inspect

    from synchri.session import github_auth

    source = inspect.getsource(github_auth._save_macos_keychain_secret)
    keychain = {}
    monkeypatch.setattr(github_auth, "_use_macos_keychain", lambda: True)
    monkeypatch.setattr(
        github_auth,
        "_load_macos_keychain_secret",
        lambda _workspace: keychain.get("serialized"),
    )
    monkeypatch.setattr(
        github_auth,
        "_save_macos_keychain_secret",
        lambda _workspace, serialized: keychain.update(serialized=serialized),
    )

    github_auth.save_credentials(
        workspace,
        {"access_token": "ghu_private", "refresh_token": "ghr_private", "expires_in": 28_800},
        account={"login": "fenixawiles"},
    )

    assert github_auth.credentials(workspace)["access_token"] == "ghu_private"
    assert "SecKeychainAddGenericPassword" in source
    assert "subprocess" not in source


def test_github_status_represents_identity_separately_from_repository_access(workspace, monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(
        github_auth,
        "credentials",
        lambda _workspace: {"access_token": "ghu_private", "account": {"login": "fenixawiles"}},
    )

    status = discovery.github_status(workspace)

    assert status["authenticated"] is True
    assert status["account"] == {"login": "fenixawiles"}
    assert "separately" in status["message"]


def test_github_repository_access_distinguishes_an_empty_grant_from_sign_in(workspace, monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(github_auth, "credentials", lambda _workspace: {"access_token": "ghu_private"})
    monkeypatch.setattr(discovery, "_github_get", lambda _path, _token: {"installations": []})

    access = discovery.github_repository_access(workspace)

    assert access["authorized"] is False
    assert access["installations"] == 0
    assert access["install_url"].endswith("/installations/new")
    assert "Choose the repositories" in access["message"]


def test_github_repository_listing_uses_app_installations(workspace, monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(github_auth, "credentials", lambda _workspace: {"access_token": "ghu_private"})
    calls = []

    def fake_get(path, token):
        calls.append((path, token))
        if path.startswith("/user/installations?"):
            return {"installations": [{"id": 41}]}
        return {"repositories": [{
            "name": "private-repo", "full_name": "fenixawiles/private-repo",
            "html_url": "https://github.com/fenixawiles/private-repo", "clone_url": "https://github.com/fenixawiles/private-repo.git",
            "description": "private", "updated_at": "2026-08-13T00:00:00Z", "private": True,
        }]}

    monkeypatch.setattr(discovery, "_github_get", fake_get)
    repos = discovery.github_repositories(local=[], workspace=workspace)

    assert repos[0]["full_name"] == "fenixawiles/private-repo"
    assert repos[0]["private"] is True
    assert calls[0][0].startswith("/user/installations")
    assert "/user/installations/41/repositories" in calls[1][0]


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


def test_resolve_github_repository_reuses_synchris_existing_checkout(repo, tmp_path, monkeypatch):
    from synchri.session import discovery

    desktop = tmp_path / "Desktop" / "Synchri"
    target = desktop / "fenixawiles-synchri"
    desktop.mkdir(parents=True)
    subprocess.run(["git", "clone", str(repo), str(target)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "remote", "set-url", "origin", "https://github.com/fenixawiles/synchri.git"],
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr(discovery, "_clone", lambda *_args: pytest.fail("must reuse, not clone"))
    resolved = discovery.resolve_github_repository("fenixawiles/synchri", destination_root=desktop)

    assert resolved["path"] == str(target.resolve())
    assert resolved["reused"] is True
    assert resolved["cloned"] is False


def test_resolve_github_repository_explains_an_occupied_clone_location(tmp_path):
    from synchri.session import discovery

    desktop = tmp_path / "Desktop" / "Synchri"
    occupied = desktop / "fenixawiles-synchri"
    occupied.mkdir(parents=True)

    with pytest.raises(ValidationError) as exc:
        discovery.resolve_github_repository("fenixawiles/synchri", destination_root=desktop)

    assert exc.value.code == "clone_location_conflict"
    assert exc.value.details["resolution"]["kind"] == "choose_local_repository"


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
