"""Ancillary execution: the scout, the adversarial reviewer, and their limits.

The invariants under test, in code terms:

* the scout works in a scratch worktree branched from the session worktree's
  HEAD — the session worktree and canonical branch are never touched
* each stage is one bounded invocation with exactly one automatic retry
* timeout / crash / unlaunchable runtime become failed or timed_out items
  with a failure report — a terminal route, never a hang
* the mandatory review runs unless the human explicitly skipped it per item
* a captured idea only auto-triggers investigation for an agent the user
  actually wired up — never a merely-installed CLI
* ancillary usage is tagged origin_kind='ancillary' and reported separately
* scratch trees are released per the retention rules, and always on deletion
"""

from __future__ import annotations

import sys

from synchri.ids import utc_now
from synchri.runner.ancillary import AncillaryRunner, _UsageRecorder, sweep
from synchri.session import dropbox
from synchri.session import worktree as worktree_module
from synchri.session.modes import ParticipantPlan
from synchri.storage import dao
from synchri.ui.api import Api

from test_session_startup import make_session, manager, repo  # noqa: F401


SCOUT_OK = """
import subprocess
open("jitter.py", "w").write("JITTER = 0.2\\n")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-qm", "propose jitter"], check=True)
print("Explored the retry module.")
print("SCOUT-EVIDENCE: retry.py:42 sleeps a constant 5s")
print("SCOUT-RATIONALE: recovery storms hit the API in lockstep")
print("SCOUT-RECOMMENDATION: adopt - add jitter to the sleep")
"""

REVIEWER_OK = """
print("The evidence checks out but the change lacks a test.")
print("REVIEW-VERDICT: needs-work")
print("REVIEW-CRITIQUE: add a regression test before adopting")
"""


def _plans(tmp_path, scout_body: str, reviewer_body: str) -> list[ParticipantPlan]:
    scout = tmp_path / "scout.py"
    scout.write_text("import sys\nprompt = sys.argv[1]\n" + scout_body, encoding="utf-8")
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text("import sys\nprompt = sys.argv[1]\n" + reviewer_body, encoding="utf-8")
    return [
        ParticipantPlan("claude", "claude_code", "primary_builder",
                        command=f"{sys.executable} {scout} {{prompt}}"),
        ParticipantPlan("codex", "codex", "adversarial_reviewer",
                        command=f"{sys.executable} {reviewer} {{prompt}}"),
    ]


def _investigated(manager, repo, tmp_path, workspace, *, scout=SCOUT_OK,
                  reviewer=REVIEWER_OK, skip_review=False, **runner_kwargs):
    record = make_session(manager, repo, _plans(tmp_path, scout, reviewer))
    item = dropbox.capture(
        manager, record.session_id, "Add jitter to the retry backoff",
        skip_review=skip_review,
    )
    runner = AncillaryRunner(workspace, **runner_kwargs)
    runner.investigate(record.session_id, item["drop_id"])
    runner.wait(record.session_id, timeout=60)
    return record, dropbox.item(manager, record.session_id, item["drop_id"]), runner


def test_the_scout_investigates_in_a_scratch_tree_and_deposits_a_reviewed_proposal(
    manager, repo, tmp_path, workspace
):
    record, item, _runner = _investigated(manager, repo, tmp_path, workspace)

    assert item["status"] == "proposed"
    proposal = item["proposal"]
    session_head = worktree_module.git(record.worktree_path, "rev-parse", "HEAD").strip()
    assert item["base_sha"] == session_head
    assert proposal["evidence"] == "retry.py:42 sleeps a constant 5s"
    assert "lockstep" in proposal["rationale"]
    assert proposal["recommendation"].startswith("adopt")
    assert proposal["commit"] and proposal["commit"] != item["base_sha"]
    assert "jitter.py" in proposal["patch"] and "JITTER" in proposal["patch"]
    assert "jitter.py" in proposal["diffstat"]
    assert item["proposal_digest"]

    review = item["review"]
    assert review["verdict"] == "needs-work"
    assert "regression test" in review["critique"]

    # Critical-path isolation: the session worktree and its branch are untouched.
    tree_files = worktree_module.git(record.worktree_path, "ls-files", check=False)
    assert "jitter.py" not in tree_files
    assert worktree_module.git(record.worktree_path, "rev-parse", "HEAD").strip() == session_head
    assert worktree_module.git(record.worktree_path, "status", "--porcelain", check=False).strip() == ""

    # The scratch branch exists in the repo and is retained while proposed.
    branches = worktree_module.git(record.repo_root, "branch", "--list",
                                   f"synchri/drop/{record.session_id}/*")
    assert item["drop_id"] in branches


def test_a_timed_out_scout_gets_one_retry_then_a_declinable_report(
    manager, repo, tmp_path, workspace
):
    attempts = tmp_path / "attempts.log"
    slow = (
        f"with open({str(attempts)!r}, 'a') as handle:\n    handle.write('go\\n')\n"
        "import time\ntime.sleep(5)\nprint('too late')\n"
    )
    record, item, _runner = _investigated(
        manager, repo, tmp_path, workspace, scout=slow, scout_timeout=0.4,
    )
    assert item["status"] == "timed_out"
    assert "timed out" in item["failure_report"]
    assert "2 attempt(s)" in item["failure_report"]
    assert attempts.read_text().count("go") == 2, "exactly one automatic retry"
    # Nothing durable was deposited, so the scratch tree and branch are gone.
    assert item["proposal"] is None
    assert not (workspace.home / "drops" / record.session_id / item["drop_id"]).exists()
    branches = worktree_module.git(record.repo_root, "branch", "--list",
                                   f"synchri/drop/{record.session_id}/*")
    assert branches.strip() == ""


def test_a_crashing_scout_fails_with_its_stderr_in_the_report(
    manager, repo, tmp_path, workspace
):
    crash = "sys.stderr.write('scout exploded')\nraise SystemExit(3)\n"
    record, item, _runner = _investigated(manager, repo, tmp_path, workspace, scout=crash)
    assert item["status"] == "failed"
    assert "exit 3" in item["failure_report"]
    assert "scout exploded" in item["failure_report"]
    assert not (workspace.home / "drops" / record.session_id / item["drop_id"]).exists()


def test_a_failed_reviewer_keeps_the_proposal_and_the_scratch_tree(
    manager, repo, tmp_path, workspace
):
    crash = "sys.stderr.write('reviewer down')\nraise SystemExit(2)\n"
    record, item, _runner = _investigated(manager, repo, tmp_path, workspace, reviewer=crash)
    assert item["status"] == "failed"
    assert "reviewer" in item["failure_report"] and "reviewer down" in item["failure_report"]
    assert item["proposal"] is not None, "the deposited proposal survives the review failure"
    # Retention: an item with a durable proposal keeps its scratch tree until
    # its disposition releases it.
    assert (workspace.home / "drops" / record.session_id / item["drop_id"]).exists()


def test_skipping_review_is_explicit_and_goes_straight_to_proposed(
    manager, repo, tmp_path, workspace
):
    marker = tmp_path / "reviewed.marker"
    reviewer = f"open({str(marker)!r}, 'w').write('ran')\nprint('REVIEW-VERDICT: sound')\n"
    record, item, _runner = _investigated(
        manager, repo, tmp_path, workspace, reviewer=reviewer, skip_review=True,
    )
    assert item["status"] == "proposed"
    assert item["review"] is None
    assert item["skip_review"] is True
    assert not marker.exists(), "the reviewer must not be invoked when the human skipped it"


def test_an_unlaunchable_runtime_fails_closed_with_a_clear_report(manager, repo, workspace):
    record = make_session(manager, repo, [
        ParticipantPlan("alpha", "generic", "primary_builder"),
        ParticipantPlan("beta", "generic", "adversarial_reviewer"),
    ])
    item = dropbox.capture(manager, record.session_id, "An idea with no agent to scout it")
    runner = AncillaryRunner(workspace)
    runner.investigate(record.session_id, item["drop_id"])
    runner.wait(record.session_id, timeout=30)
    final = dropbox.item(manager, record.session_id, item["drop_id"])
    assert final["status"] == "failed"
    assert "no connected agent" in final["failure_report"]


def test_cancel_abandons_the_stage_without_calling_it_a_crash(
    manager, repo, tmp_path, workspace
):
    started = tmp_path / "started.log"
    slow = (
        f"with open({str(started)!r}, 'a') as handle:\n    handle.write('go\\n')\n"
        "import time\ntime.sleep(10)\n"
    )
    record = make_session(manager, repo, _plans(tmp_path, slow, REVIEWER_OK))
    item = dropbox.capture(manager, record.session_id, "A slow investigation")
    runner = AncillaryRunner(workspace, scout_timeout=60)
    runner.investigate(record.session_id, item["drop_id"])
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.05)
    assert started.exists(), "the scout should have started"
    runner.cancel(record.session_id)
    runner.wait(record.session_id, timeout=30)
    final = dropbox.item(manager, record.session_id, item["drop_id"])
    assert final["status"] == "investigating"
    assert final["failure_report"] is None
    assert final["disposition"] is None


def test_ancillary_usage_is_tagged_and_reported_separately(manager, repo, workspace):
    record = make_session(manager, repo, [
        ParticipantPlan("alpha", "generic", "primary_builder"),
        ParticipantPlan("beta", "generic", "adversarial_reviewer"),
    ])

    class _Parser:
        usage = {"model": "m-1", "input_tokens": 10, "output_tokens": 5,
                 "cached_input_tokens": 2, "cost_usd": 0.01, "duration_seconds": 1.5}

    recorder = _UsageRecorder(
        workspace, session_id=record.session_id, room_id=record.room_id,
        participant="alpha", runtime="claude_code", drop_id="DROP-001",
    )
    recorder.begin()
    recorder.finish(_Parser())

    ancillary = dao.ancillary_usage_for_session(manager.conn, record.session_id)
    assert len(ancillary) == 1
    assert ancillary[0]["drop_id"] == "DROP-001"
    assert ancillary[0]["input_tokens"] == 10
    assert dao.usage_for_session(manager.conn, record.session_id) == [], (
        "ancillary spend must never appear in ordinary participant totals"
    )


def test_capture_endpoint_triggers_the_scout_only_for_wired_up_agents(
    manager, repo, tmp_path
):
    api = Api(manager.broker, manager)

    class _Recorder:
        def __init__(self):
            self.calls = []

        def investigate(self, session_id, drop_id):
            self.calls.append((session_id, drop_id))

    api.ancillary = _Recorder()

    wired = make_session(manager, repo, _plans(tmp_path, SCOUT_OK, REVIEWER_OK))
    payload = api.capture_drop({}, {"session": wired.session_id, "prompt": "idea one"})
    assert payload["investigating"] is True
    assert api.ancillary.calls == [(wired.session_id, "DROP-001")]

    # No explicit command and no connected record: never invoked implicitly,
    # even where the CLI happens to be installed.
    unwired = make_session(manager, repo, [
        ParticipantPlan("claude", "claude_code", "primary_builder"),
        ParticipantPlan("codex", "codex", "adversarial_reviewer"),
    ])
    payload = api.capture_drop({}, {"session": unwired.session_id, "prompt": "idea two"})
    assert payload["investigating"] is False
    assert len(api.ancillary.calls) == 1


def test_sweep_releases_scratch_trees_per_the_retention_rules(manager, repo, tmp_path):
    record = make_session(manager, repo, [
        ParticipantPlan("claude", "claude_code", "primary_builder"),
        ParticipantPlan("codex", "codex", "adversarial_reviewer"),
    ])
    session_id = record.session_id

    def _scratched(drop_id, prompt):
        item = dropbox.capture(manager, session_id, prompt)
        scratch = tmp_path / "scratch" / item["drop_id"]
        scratch.mkdir(parents=True)
        dropbox.begin_investigation(
            manager, session_id, item["drop_id"],
            base_sha="c" * 40,
            scratch_branch=f"synchri/drop/{session_id}/{item['drop_id']}",
            scratch_path=str(scratch),
        )
        return item["drop_id"], scratch

    declined_id, declined_tree = _scratched("DROP-001", "small change")
    dropbox.deposit_proposal(
        manager, session_id, declined_id,
        evidence="e", rationale="r", recommendation="adopt", patch="+ small\n",
    )
    dropbox.record_review(manager, session_id, declined_id, verdict="sound", critique="ok")

    oversized_id, oversized_tree = _scratched("DROP-002", "huge change")
    dropbox.deposit_proposal(
        manager, session_id, oversized_id,
        evidence="e", rationale="r", recommendation="adopt",
        patch="+ x\n" * (dropbox.MAX_PATCH_CHARS // 4 + 10),
    )
    dropbox.record_review(manager, session_id, oversized_id, verdict="sound", critique="ok")

    for gate in manager.gates(session_id):
        manager.update_gate(
            session_id, gate.gate_id, actor="human", status="pass",
            evidence=["done"], builder_assessment="done", reviewer_assessment="ok",
        )
    manager.issue_contract(session_id)
    from test_session_startup import accept_all

    accept_all(manager, manager.get(session_id))
    manager.activate(session_id)
    manager.complete(session_id)

    for actor in ("claude", "codex"):
        dropbox.evaluate(manager, session_id, declined_id,
                         actor=actor, verdict="decline", rationale="not now")
        dropbox.evaluate(manager, session_id, oversized_id,
                         actor=actor, verdict="approve", rationale="worth doing")

    removed = sweep(manager, manager.get(session_id))
    assert removed == [declined_id]
    assert not declined_tree.exists()
    assert oversized_tree.exists(), (
        "an approved oversized patch lives only on its scratch branch — retained "
        "until the extension completes, the item is rejected, or the patch is imported"
    )

    extension = dropbox.item(manager, session_id, oversized_id)["extension_id"]
    manager.update_gate(
        session_id, extension, actor="human", status="pass",
        evidence=["extension landed"], builder_assessment="done", reviewer_assessment="ok",
    )
    removed = sweep(manager, manager.get(session_id))
    assert removed == [oversized_id]
    assert not oversized_tree.exists()


def test_deleting_a_session_always_releases_its_scratch_trees(
    manager, repo, tmp_path, workspace
):
    record, item, _runner = _investigated(manager, repo, tmp_path, workspace)
    scratch = workspace.home / "drops" / record.session_id / item["drop_id"]
    assert scratch.exists()
    manager.stop(record.session_id)
    manager.delete_session(record.session_id)
    assert not scratch.exists()
    branches = worktree_module.git(record.repo_root, "branch", "--list",
                                   f"synchri/drop/{record.session_id}/*")
    assert branches.strip() == ""
