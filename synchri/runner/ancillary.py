"""Ancillary execution: the dropbox scout and its adversarial reviewer.

A captured side task is investigated **off the critical path**. The scout and
the reviewer are not room participants: each stage is one bounded provider
invocation in a disposable scratch worktree branched from the session
worktree's HEAD at scout start (`synchri/drop/<session>/<drop-id>`), with no
push, no merge, and no access to the canonical worktree. The durable proposal
deposited into the dropbox is the artifact of record — nothing an ancillary
agent does lands anywhere by itself.

Budgets are enforced here, fail-closed: the scout gets 15 minutes wall-clock
and the reviewer 10, each stage retries automatically exactly once, and any
exhaustion, crash, or unlaunchable runtime becomes a durable ``failed`` /
``timed_out`` item with a failure report the main pair can decline on —
mandatory evaluation never waits on work that will not arrive. Further
retries are the human's call.

Ancillary token spend is recorded with ``origin_kind='ancillary'`` per item,
never merged into an ordinary participant's totals.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from ..broker import Broker
from ..errors import StateError, SynchriError
from ..session import dropbox
from ..session import worktree as worktree_module
from ..session.manager import SessionManager
from ..session.modes import collaboration_pair, managed_command, stream_format_for
from ..storage import dao, db
from .agent_command import AgentCommand
from .stream_events import parser_for

SCOUT_TIMEOUT_SECONDS = 900.0      # 15 minutes wall-clock
REVIEWER_TIMEOUT_SECONDS = 600.0   # 10 minutes wall-clock
ATTEMPTS_PER_STAGE = 2             # one automatic retry; more is user-initiated

_SECTION_KEYS_SCOUT = ("SCOUT-EVIDENCE", "SCOUT-RATIONALE", "SCOUT-RECOMMENDATION")
_SECTION_KEYS_REVIEW = ("REVIEW-VERDICT", "REVIEW-CRITIQUE")


class AncillaryRunner:
    """Runs one scout and one reviewer per session, one item at a time.

    Owned by the UI process, like the managed registry: everything durable
    lives in the dropbox rows, so a UI restart loses only in-flight
    invocations — never a deposited proposal.
    """

    def __init__(
        self,
        workspace,
        *,
        scout_timeout: float = SCOUT_TIMEOUT_SECONDS,
        reviewer_timeout: float = REVIEWER_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self.scout_timeout = scout_timeout
        self.reviewer_timeout = reviewer_timeout
        self._queues: dict[str, deque[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def investigate(self, session_id: str, drop_id: str) -> None:
        """Queue one item; the session's single scout services the queue."""
        with self._lock:
            self._queues.setdefault(session_id, deque()).append(drop_id)
            self._cancels.setdefault(session_id, threading.Event())
            thread = self._threads.get(session_id)
            if thread is not None and thread.is_alive():
                return
            worker = threading.Thread(
                target=self._worker,
                args=(session_id,),
                name=f"synchri-ancillary-{session_id}",
                daemon=True,
            )
            self._threads[session_id] = worker
            worker.start()

    def cancel(self, session_id: str) -> None:
        """End in-flight ancillary work; the dropbox rows say what survived."""
        with self._lock:
            queue = self._queues.get(session_id)
            if queue is not None:
                queue.clear()
            event = self._cancels.get(session_id)
        if event is not None:
            event.set()

    def wait(self, session_id: str, timeout: float = 30.0) -> None:
        with self._lock:
            thread = self._threads.get(session_id)
        if thread is not None:
            thread.join(timeout=timeout)

    # ------------------------------------------------------------------

    def _worker(self, session_id: str) -> None:
        broker = Broker(self.workspace)
        try:
            manager = SessionManager(broker)
            while True:
                with self._lock:
                    queue = self._queues.get(session_id)
                    if not queue:
                        self._threads.pop(session_id, None)
                        self._cancels.pop(session_id, None)
                        return
                    drop_id = queue.popleft()
                    cancel = self._cancels.setdefault(session_id, threading.Event())
                if cancel.is_set():
                    continue  # cancelled upstream; the cutoff/stop already spoke
                try:
                    record = manager.get(session_id)
                    self._investigate_item(manager, record, drop_id, cancel)
                except SynchriError:
                    continue
                except Exception:  # pragma: no cover - defensive boundary
                    try:
                        dropbox.mark_failed(
                            manager, session_id, drop_id,
                            kind=dropbox.FAILED,
                            report="an internal error interrupted the investigation",
                        )
                    except SynchriError:
                        pass
        finally:
            broker.close()

    def _investigate_item(self, manager, record, drop_id: str, cancel) -> None:
        item = dropbox.item(manager, record.session_id, drop_id)
        if (
            record.is_terminal
            or record.phase != dropbox.PHASE_ORIGINAL
            or item["status"] != dropbox.CAPTURED
            or item["disposition"]
        ):
            return

        lead, reviewer = collaboration_pair(record.participants)
        scout_command = managed_command(lead) if lead else None
        if not scout_command:
            dropbox.mark_failed(
                manager, record.session_id, drop_id,
                kind=dropbox.FAILED,
                report=(
                    "no connected agent can run the scout: the lead participant has "
                    "no launchable command. Connect its CLI, then retry the item."
                ),
            )
            return
        if not record.worktree_path:
            dropbox.mark_failed(
                manager, record.session_id, drop_id,
                kind=dropbox.FAILED, report="the session has no worktree to branch from",
            )
            return

        # The scratch worktree: branched from the session worktree's HEAD at
        # scout start, never from the canonical checkout, never pushed.
        branch = f"synchri/drop/{record.session_id}/{drop_id}"
        scratch = Path(self.workspace.home) / "drops" / record.session_id / drop_id
        try:
            base_sha = worktree_module.git(record.worktree_path, "rev-parse", "HEAD").strip()
            worktree_module.remove_scratch(record.repo_root, str(scratch), branch)
            scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            worktree_module.git(
                record.repo_root, "worktree", "add", "-B", branch, str(scratch), base_sha
            )
        except StateError as exc:
            dropbox.mark_failed(
                manager, record.session_id, drop_id,
                kind=dropbox.FAILED,
                report=f"could not prepare the scratch worktree: {exc.message}",
            )
            return

        item = dropbox.begin_investigation(
            manager, record.session_id, drop_id,
            base_sha=base_sha, scratch_branch=branch, scratch_path=str(scratch),
        )

        result = self._invoke_stage(
            lambda: self._build_agent(
                record, lead, cwd=str(scratch), timeout=self.scout_timeout, drop_id=drop_id
            ),
            _scout_prompt(item),
            cancel,
        )
        if result is None or result.cancelled:
            return  # stopped by the human or the cutoff; not the scout's failure
        if not result.ok:
            self._fail_stage(manager, record, drop_id, "scout", result, self.scout_timeout)
            # Nothing durable was deposited: the scratch tree has no proposal
            # to back, so it goes now. A failure after a deposit keeps it
            # until the item's disposition releases it.
            worktree_module.remove_scratch(record.repo_root, str(scratch), branch)
            return

        sections = _sections(result.stdout, _SECTION_KEYS_SCOUT)
        patch, diffstat, commit = self._collect_change(scratch, base_sha)
        item = dropbox.deposit_proposal(
            manager, record.session_id, drop_id,
            evidence=sections["SCOUT-EVIDENCE"] or result.stdout.strip(),
            rationale=sections["SCOUT-RATIONALE"],
            recommendation=sections["SCOUT-RECOMMENDATION"],
            patch=patch, diffstat=diffstat, commit=commit,
        )
        if item["status"] != dropbox.REVIEWING:
            return  # review skipped by the human's explicit per-item choice

        reviewer_command = managed_command(reviewer) if reviewer else None
        if not reviewer_command:
            dropbox.mark_failed(
                manager, record.session_id, drop_id,
                kind=dropbox.FAILED,
                report=(
                    "the mandatory ancillary review could not run: no connected "
                    "agent holds the reviewer role. The proposal is retained; "
                    "decline on this report or retry once a reviewer is connected."
                ),
            )
            return
        result = self._invoke_stage(
            lambda: self._build_agent(
                record, reviewer, cwd=str(scratch), timeout=self.reviewer_timeout,
                drop_id=drop_id,
            ),
            _reviewer_prompt(item),
            cancel,
        )
        if result is None or result.cancelled:
            return
        if not result.ok:
            self._fail_stage(manager, record, drop_id, "reviewer", result, self.reviewer_timeout)
            return
        review = _sections(result.stdout, _SECTION_KEYS_REVIEW)
        dropbox.record_review(
            manager, record.session_id, drop_id,
            verdict=review["REVIEW-VERDICT"] or "reviewed",
            critique=review["REVIEW-CRITIQUE"] or result.stdout.strip(),
        )

    # ------------------------------------------------------------------

    def _invoke_stage(self, agent_factory, prompt: str, cancel):
        """One bounded invocation per attempt, one automatic retry per stage."""
        last = None
        for _attempt in range(ATTEMPTS_PER_STAGE):
            if cancel.is_set():
                return None
            result = agent_factory().invoke(prompt, cancel_event=cancel)
            if result.cancelled or result.ok:
                return result
            last = result
        return last

    def _fail_stage(self, manager, record, drop_id: str, stage: str, result, budget: float) -> None:
        kind = dropbox.TIMED_OUT if result.timed_out else dropbox.FAILED
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
        outcome = (
            f"timed out (budget {budget:g}s)" if result.timed_out
            else f"failed (exit {result.returncode})"
        )
        try:
            dropbox.mark_failed(
                manager, record.session_id, drop_id,
                kind=kind,
                report=(
                    f"the {stage} {outcome} after {ATTEMPTS_PER_STAGE} attempt(s): "
                    f"{detail[-1500:]}"
                ),
            )
        except SynchriError:  # pragma: no cover - the item may have moved on
            pass

    def _collect_change(self, scratch: Path, base_sha: str):
        """The scout's concrete change, if any: committed work first."""
        head = worktree_module.git(scratch, "rev-parse", "HEAD", check=False).strip()
        commit = head if head and head != base_sha else None
        if commit:
            patch = worktree_module.git(scratch, "diff", f"{base_sha}..{head}", check=False)
            diffstat = worktree_module.git(
                scratch, "diff", "--stat", f"{base_sha}..{head}", check=False
            )
        else:
            patch = worktree_module.git(scratch, "diff", check=False)
            diffstat = worktree_module.git(scratch, "diff", "--stat", check=False)
        return (patch or None), ((diffstat or "").strip() or None), commit

    def _build_agent(self, record, plan, *, cwd: str, timeout: float, drop_id: str) -> AgentCommand:
        command = managed_command(plan)
        agent = AgentCommand.parse(f"{plan.name}={command}", timeout=timeout, cwd=cwd)
        stream_format = stream_format_for(plan)
        if stream_format:
            agent.parser_factory = lambda fmt=stream_format: parser_for(fmt)
            agent.recorder = _UsageRecorder(
                self.workspace,
                session_id=record.session_id,
                room_id=record.room_id,
                participant=plan.name,
                runtime=plan.runtime,
                drop_id=drop_id,
            )
        return agent


def sweep(manager, record) -> list[str]:
    """Remove scratch trees whose retention rule has released them.

    Retention: a declined or waived item releases immediately; an approved
    item releases once its patch is durably stored, or — when the patch was
    oversized and lives only on the scratch branch — once its extension gate
    is satisfied (the extension completed) or the patch was imported. A
    failed item with no proposal releases immediately; one with a proposal
    waits for its disposition.
    """
    satisfied = {
        gate.gate_id for gate in manager.gates(record.session_id) if gate.is_satisfied
    }
    removed = []
    for entry in dropbox.items(manager, record.session_id):
        if not (entry["scratch_path"] or entry["scratch_branch"]):
            continue
        disposition = entry["disposition"]
        proposal = entry["proposal"] or {}
        if disposition in {dropbox.DECLINED, dropbox.WAIVED}:
            release = True
        elif disposition == dropbox.APPROVED:
            oversized = bool(proposal.get("patch_note")) and not proposal.get("patch")
            release = (not oversized) or entry["extension_id"] in satisfied
        elif entry["status"] in {dropbox.FAILED, dropbox.TIMED_OUT} and not entry["proposal"]:
            release = True
        else:
            release = False
        if not release:
            continue
        worktree_module.remove_scratch(
            record.repo_root, entry["scratch_path"], entry["scratch_branch"]
        )
        with db.transaction(manager.conn):
            manager.conn.execute(
                "UPDATE session_drops SET scratch_path = NULL, scratch_branch = NULL "
                "WHERE session_id = ? AND drop_id = ?",
                (record.session_id, entry["drop_id"]),
            )
        removed.append(entry["drop_id"])
    return removed


class _UsageRecorder:
    """Usage-only sink for ancillary invocations.

    Ancillary agents never join the room, so their stream chatter is not room
    telemetry — but their token spend is real. It lands tagged
    ``origin_kind='ancillary'`` with its item id, reported separately and
    never merged into an ordinary participant's totals.
    """

    def __init__(
        self, workspace, *, session_id, room_id, participant, runtime, drop_id,
        origin_kind: str = "ancillary",
    ) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.room_id = room_id
        self.participant = participant
        self.runtime = runtime
        self.drop_id = drop_id
        self.origin_kind = origin_kind
        self._started = 0.0

    def begin(self) -> None:
        self._started = time.monotonic()

    def write(self, event) -> None:
        return None

    def finish(self, parser, *, returncode=None, outcome: str = "ok") -> None:
        usage = getattr(parser, "usage", None) if parser is not None else None
        if not usage or not self.session_id:
            return
        duration = (time.monotonic() - self._started) if self._started else None
        try:
            conn = db.connect(self.workspace.db_path)
            try:
                with db.transaction(conn):
                    dao.insert_turn_usage(
                        conn,
                        session_id=self.session_id,
                        room_id=self.room_id,
                        participant=self.participant,
                        runtime=self.runtime,
                        model=usage.get("model"),
                        input_tokens=usage.get("input_tokens") or 0,
                        output_tokens=usage.get("output_tokens") or 0,
                        cached_input_tokens=usage.get("cached_input_tokens") or 0,
                        cost_usd=usage.get("cost_usd"),
                        duration_seconds=usage.get("duration_seconds") or duration,
                        origin_kind=self.origin_kind,
                        drop_id=self.drop_id,
                    )
            finally:
                conn.close()
        except Exception:  # pragma: no cover - telemetry must never break a run
            pass


# ----------------------------------------------------------------------
# prompts and parsing
# ----------------------------------------------------------------------


def _scout_prompt(item: dict) -> str:
    return "\n".join(
        [
            "You are an ancillary scout for a Synchri coding session. Investigate ONE",
            "side task, off the critical path. You are not a room participant: do not",
            "wait for anyone and do not ask questions — investigate and report.",
            "",
            f"Side task {item['drop_id']}: {item['title']}",
            "",
            item["prompt"],
            "",
            "Rules:",
            "- Work ONLY inside the current directory. It is a disposable scratch",
            f"  worktree taken from the session's code at commit {item['base_sha']}.",
            "- Never push, never merge, never touch any other checkout or branch.",
            "- If a concrete change is worth proposing, make it here and commit it",
            "  to the current branch. The main agents decide later whether to use",
            "  it; nothing you do here lands anywhere by itself.",
            "",
            "End your reply with exactly these sections:",
            "SCOUT-EVIDENCE: what you actually found — file:line facts, measurements",
            "SCOUT-RATIONALE: why this matters, or why it should be dropped",
            "SCOUT-RECOMMENDATION: one line — adopt, adapt, or drop, and what to do",
        ]
    )


def _reviewer_prompt(item: dict) -> str:
    proposal = item["proposal"] or {}
    parts = [
        "You are the ancillary adversarial reviewer for a Synchri side task. A",
        "scout investigated it and deposited the proposal below. Attack it: is the",
        "evidence real, is the change safe and minimal, what breaks, what is",
        "untested, what did the scout miss? You may inspect the scratch worktree",
        "in the current directory. You are not a room participant — report and stop.",
        "",
        f"Side task {item['drop_id']}: {item['title']}",
        "",
        item["prompt"],
        "",
        "--- the scout's proposal ---",
        f"Evidence:\n{proposal.get('evidence') or '(none)'}",
        "",
        f"Rationale:\n{proposal.get('rationale') or '(none)'}",
        "",
        f"Recommendation: {proposal.get('recommendation') or '(none)'}",
    ]
    if proposal.get("diffstat"):
        parts += ["", f"Diffstat:\n{proposal['diffstat']}"]
    if proposal.get("patch"):
        parts += ["", "Patch:", proposal["patch"]]
    elif proposal.get("patch_note"):
        parts += ["", f"Patch: {proposal['patch_note']}"]
    parts += [
        "",
        "End your reply with exactly these sections:",
        "REVIEW-VERDICT: one line — sound, unsound, or needs-work",
        "REVIEW-CRITIQUE: your specific objections and what would change your mind",
    ]
    return "\n".join(parts)


def _sections(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Split labeled report sections out of an agent's free-form reply."""
    buckets: dict[str, list[str]] = {key: [] for key in keys}
    current: str | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        matched = next(
            (key for key in keys if stripped.upper().startswith(key + ":")), None
        )
        if matched:
            current = matched
            buckets[current].append(stripped[len(matched) + 1:].strip())
        elif current:
            buckets[current].append(line)
    return {key: "\n".join(value).strip() for key, value in buckets.items()}
