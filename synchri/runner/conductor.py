"""Drive a whole room from a single terminal.

Nothing about Synchri ever required one terminal per agent — a participant is
whatever process can run the CLI.  The conductor makes the single-terminal case
practical: it watches the room, and whenever a *managed* participant is handed
the floor, it invokes that participant's command, feeds it the pending request,
and posts the reply back into the room.

The conductor deliberately holds **no scheduling authority**.  It asks the
broker who is on point and does what it is told; every priority, blocking, and
loop-limit decision stays in the queue where it is tested.  Stopping the
conductor stops nothing about the room's state.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from typing import Callable

from ..broker import Broker, Credential
from ..errors import SynchriError, ValidationError
from ..models.enums import MessageType, ResponseStatus, RoomStatus, TurnState
from ..models.envelope import MessageDraft
from ..session.manager import SessionManager
from . import recovery
from .agent_command import AgentCommand, parse_directives

#: Why the conductor handed control back.
STOP_AWAITING_HUMAN = "awaiting_human"
STOP_ROOM_STOPPED = "room_stopped"
STOP_ROOM_PAUSED = "room_paused"
STOP_TURN_LIMIT = "turn_limit"
STOP_IDLE = "idle"
STOP_UNMANAGED_SPEAKER = "unmanaged_speaker"
STOP_CANCELLED = "cancelled"
STOP_AGENT_FAILED = "agent_failed"


@dataclass
class ConductorReport:
    """What happened during one `synchri run`."""

    room_id: str
    reason: str
    turns: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Set when ``reason == "agent_failed"``: who tripped the breaker.
    failed_participant: str | None = None
    #: How the failed participant failed (see :mod:`recovery`), so supervision
    #: can pick the right rung: ladder kinds get a replacement chance, auth
    #: failures and provider refusals go straight to the human.
    failure_kind: str | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id,
            "reason": self.reason,
            "turns_run": self.turn_count,
            "turns": self.turns,
            "warnings": self.warnings,
        }


class Conductor:
    """Runs managed agents' turns in one process, in one terminal."""

    def __init__(
        self,
        broker: Broker,
        room_id: str,
        agents: dict[str, AgentCommand],
        credentials: dict[str, Credential],
        observer: Credential,
        *,
        context_messages: int = 12,
        include_memory: bool = True,
        role_guidance: dict[str, str] | None = None,
        session_id: str | None = None,
        cancel_event=None,
        cancel_events: dict[str, object] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        max_consecutive_failures: int = 2,
        initial_failures: dict[str, int] | None = None,
    ) -> None:
        if not agents:
            raise ValidationError("at least one --agent is required")
        missing = sorted(set(agents) - set(credentials))
        if missing:
            raise ValidationError(
                f"no credential available for managed agent(s) {', '.join(missing)}; "
                "run 'synchri join' for each one first"
            )
        self.broker = broker
        self.room_id = room_id
        self.agents = agents
        self.credentials = credentials
        self.observer = observer
        self.context_messages = context_messages
        self.include_memory = include_memory
        self.role_guidance = dict(role_guidance or {})
        self.session_id = session_id
        self.cancel_event = cancel_event
        #: Optional per-participant cancel signals (composed with the session
        #: event by the caller) so restarting one agent never touches another
        #: agent's invocation.
        self.cancel_events = dict(cancel_events or {})
        self.on_event = on_event or (lambda event, payload: None)
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        #: Seeded from durable per-agent counts so the breaker composes across
        #: idle/resume boundaries: a crash before an idle stop still counts
        #: against the same agent when supervision picks the room back up.
        self._failures: dict[str, int] = {
            name: int(count) for name, count in (initial_failures or {}).items() if count
        }
        self._low_signal: dict[str, int] = {}
        self._failure_kinds: dict[str, str] = {}

    # ------------------------------------------------------------------

    def run(self, *, max_turns: int | None = None, poll_interval: float = 0.5) -> ConductorReport:
        report = ConductorReport(room_id=self.room_id, reason=STOP_IDLE)
        while True:
            # Session controls own the room transition.  The conductor's job
            # is simply to stop supervising immediately, not to sneak one
            # more provider invocation in after Pause or Stop was pressed.
            if self.cancel_event is not None and self.cancel_event.is_set():
                report.reason = STOP_CANCELLED
                return report
            if max_turns is not None and report.turn_count >= max_turns:
                report.reason = STOP_TURN_LIMIT
                return report

            status = self.broker.room_status(self.room_id, credential=self.observer)
            room = status["room"]

            if room["status"] == RoomStatus.STOPPED.value:
                report.reason = STOP_ROOM_STOPPED
                return report
            if room["status"] == RoomStatus.PAUSED.value:
                report.reason = STOP_ROOM_PAUSED
                return report
            if room["awaiting_human"]:
                report.reason = STOP_AWAITING_HUMAN
                return report

            speaker = status["active_speaker"]
            if speaker is None:
                if not status["queue"]:
                    report.reason = STOP_IDLE
                    return report
                # A queued participant that has not been promoted yet: the
                # broker promotes on the next mutation, so give it a moment.
                time.sleep(max(0.05, poll_interval))
                continue

            if speaker not in self.agents:
                # The floor belongs to the human or to an agent driven from
                # another terminal.  Not ours to run.
                report.reason = STOP_UNMANAGED_SPEAKER
                report.warnings.append(f"{speaker} holds the floor and is not managed here")
                return report

            outcome = self._take_turn(speaker)
            report.turns.append(outcome)
            report.warnings.extend(outcome.get("warnings") or [])

            # Circuit breaker: an agent failing turn after turn must not keep
            # receiving the floor forever — after the limit, hand the problem
            # to the human instead of burning further invocations. Auth
            # failures and provider refusals break immediately: retrying an
            # expired sign-in or a refusal only burns invocations.
            if outcome.get("status") == "failed":
                kind = outcome.get("failure_kind") or recovery.CRASH
                self._failure_kinds[speaker] = kind
                count = self._failures.get(speaker, 0) + 1
                self._failures[speaker] = count
                if kind not in recovery.LADDER_KINDS or count >= self.max_consecutive_failures:
                    report.reason = STOP_AGENT_FAILED
                    report.failed_participant = speaker
                    report.failure_kind = kind
                    report.warnings.append(
                        f"{speaker} failed {count} consecutive turn(s) ({kind}); "
                        "supervision stopped"
                    )
                    return report
            elif outcome.get("status") in {"spoke", "passed"}:
                self._failures[speaker] = 0
                self._failure_kinds.pop(speaker, None)

    # ------------------------------------------------------------------

    def _resolve_agent(self, name: str) -> AgentCommand:
        """Resolve one participant's command for this turn.

        A plain :class:`AgentCommand` behaves as before. A callable is a
        factory the supervising registry provides so per-turn decisions —
        like resuming a provider session after a failure — read durable state
        at the moment the turn starts, not at conductor construction.
        """
        agent = self.agents[name]
        return agent() if callable(agent) else agent

    def _take_turn(self, name: str) -> dict:
        credential = self.credentials[name]
        status = self.broker.turn_status(self.room_id, credential=credential)
        if status["state"] != TurnState.YOUR_TURN.value:
            # Lost the floor between the status read and now — a human can
            # interrupt at any moment, and that is allowed to beat us.
            return {"participant": name, "skipped": status["state"]}

        # Give the local UI an immediate, honest indication that the agent has
        # started. This is intentionally a separate, expiring work note: the
        # invocation's stdout remains the one completed room response.
        try:
            self.broker.publish_activity(
                self.room_id,
                credential=credential,
                summary="Planning the next step. This can take a few minutes.",
            )
        except SynchriError:
            # A human can interrupt between turn inspection and this note. The
            # post path will re-check room state and report the actual outcome.
            pass

        agent = self._resolve_agent(name)
        prompt = self.build_prompt(name, status, agent=agent)
        self.on_event("agent.invoking", {"participant": name, "prompt_chars": len(prompt)})

        result = agent.invoke(
            prompt, cancel_event=self.cancel_events.get(name) or self.cancel_event
        )
        classification = None
        if not result.ok:
            classification = recovery.classify_failure(result)
        self.on_event(
            "agent.returned",
            {
                "participant": name,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "output_chars": len(result.stdout),
                "classification": classification,
                "failure_detail": (
                    recovery.failure_detail(classification, result)
                    if classification and classification != recovery.NORMAL_STOP
                    else None
                ),
                "provider_session_id": result.provider_session_id,
            },
        )
        outcome = self._post(name, result)
        if outcome.get("status") == "failed" and classification:
            outcome["failure_kind"] = classification
        return outcome

    def _note_low_signal(self, name: str, result, body: str, directives) -> bool:
        """Warn-only heuristic for clean exits that did no visible work.

        Only streaming runtimes report ``tool_events``; for them, a turn with
        zero tool activity, a tiny reply, and no directives twice in a row
        usually means the provider refused or stalled. This never drops the
        agent — a clean exit can be legitimate — it only raises a flag.
        """
        if result.tool_events is None:
            return False
        low = (
            result.tool_events == 0
            and len(body.strip()) < 200
            and not directives.gate_updates
            and not directives.to
            and not directives.handoff
            and not directives.complete_requested
        )
        if not low:
            self._low_signal[name] = 0
            return False
        count = self._low_signal.get(name, 0) + 1
        self._low_signal[name] = count
        if count >= 2:
            self.on_event("agent.low_signal", {"participant": name, "consecutive": count})
        return True

    def _post(self, name: str, result) -> dict:
        credential = self.credentials[name]
        body, directives = parse_directives(result.stdout)
        warnings = list(directives.warnings)

        try:
            if not result.ok:
                if result.cancelled:
                    return {
                        "participant": name,
                        "status": "cancelled",
                        "warnings": warnings,
                    }
                detail = (result.stderr or "").strip() or "no output"
                sent = self.broker.send(
                    self.room_id,
                    credential=credential,
                    draft=MessageDraft(
                        content=(
                            f"{name} could not complete this turn "
                            f"({'timed out' if result.timed_out else f'exit {result.returncode}'}): "
                            f"{_tail(detail)}"
                        ),
                        message_type=MessageType.RESPONSE.value,
                        response_status=ResponseStatus.FAILED.value,
                        metadata={"conductor": True, "returncode": result.returncode},
                    ),
                )
                return {
                    "participant": name,
                    "status": "failed",
                    "message_id": sent["message"]["message_id"],
                    "next_speaker": sent.get("next_speaker"),
                    "warnings": warnings,
                }

            # Gate progress is meaningful even if this particular turn ends in
            # a pass.  Recording it first keeps a terse evidence report from
            # disappearing just because the agent had no prose to add.
            gate_updates = self._record_gate_updates(name, directives, warnings)
            low_signal = self._note_low_signal(name, result, body, directives)

            if directives.passed or not body.strip():
                passed = self.broker.pass_turn(
                    self.room_id,
                    credential=credential,
                    reason=body.strip() or "nothing material to add",
                )
                outcome = {
                    "participant": name,
                    "status": "passed",
                    "message_id": passed["message"]["message_id"],
                    "next_speaker": passed.get("next_speaker"),
                    "warnings": warnings,
                }
                if low_signal:
                    outcome["low_signal"] = True
                if gate_updates:
                    outcome["gate_updates"] = gate_updates
                if directives.complete_requested:
                    outcome["completion_requested"] = self._try_complete(name, warnings)
                return outcome

            sent = self.broker.send(
                self.room_id,
                credential=credential,
                draft=MessageDraft(
                    content=body,
                    message_type=(
                        MessageType.TASK.value if directives.to else MessageType.RESPONSE.value
                    ),
                    target=directives.to,
                    handoff_target=directives.handoff,
                    response_status=_response_status(directives.status, bool(directives.to)),
                    confidence=directives.confidence,
                    metadata={
                        "conductor": True,
                        **(
                            {
                                "approval_request": directives.approval_request,
                                "approval_capability": directives.approval_capability,
                            }
                            if directives.approval_request else {}
                        ),
                    },
                ),
            )
            outcome = {
                "participant": name,
                "status": "spoke",
                "message_id": sent["message"]["message_id"],
                "target": directives.to,
                "handoff_target": directives.handoff,
                "next_speaker": sent.get("next_speaker"),
                "warnings": warnings,
            }
            if low_signal:
                outcome["low_signal"] = True
            if gate_updates:
                outcome["gate_updates"] = gate_updates
            if directives.complete_requested:
                outcome["completion_requested"] = self._try_complete(name, warnings)
            return outcome
        except SynchriError as exc:
            # The room refused the post (interrupted, stopped, bad target...).
            # Report it; the loop re-reads room state and decides what is next.
            warnings.append(f"{name}: {exc.code}: {exc.message}")
            return {"participant": name, "status": "rejected", "error": exc.code, "warnings": warnings}

    def _record_gate_updates(self, name: str, directives, warnings: list[str]) -> list[dict]:
        """Persist concise gate reports before the completed message is queued."""
        if not directives.gate_updates or not self.session_id:
            return []
        manager = SessionManager(self.broker)
        saved = []
        for update in directives.gate_updates:
            try:
                gate = manager.report_gate(
                    self.session_id,
                    update.gate_id,
                    actor=name,
                    status=update.status,
                    assessment=update.assessment,
                    evidence=update.evidence,
                    tests=update.tests,
                    commits=update.commits,
                )
                saved.append(gate.to_dict())
            except SynchriError as exc:
                warnings.append(f"{name}: could not update gate {update.gate_id}: {exc.message}")
        return saved

    def _try_complete(self, name: str, warnings: list[str]) -> bool:
        """A Primary Builder may request completion; manager remains the authority."""
        if not self.session_id:
            warnings.append("completion request ignored: this room is not attached to a session")
            return False
        manager = SessionManager(self.broker)
        record = manager.get(self.session_id)
        plan = next((item for item in record.participants if item.name == name), None)
        if plan is None or plan.role != "primary_builder":
            warnings.append("completion request ignored: only the Primary Builder may request it")
            return False
        try:
            manager.complete_by_agent(self.session_id, name)
            return True
        except SynchriError as exc:
            warnings.append(f"completion request not accepted: {exc.message}")
            return False

    # ------------------------------------------------------------------

    def build_prompt(self, name: str, status: dict, agent: AgentCommand | None = None) -> str:
        """Assemble what a managed agent is told when it gets the floor."""
        if agent is None:
            agent = self._resolve_agent(name)
        room = self.broker.room_status(self.room_id, credential=self.observer)
        others = [
            p["name"]
            for p in room["participants"]
            if p["name"] != name and p["status"] == "active"
        ]

        parts: list[str] = [
            f"You are '{name}' in the Synchri room \"{room['room']['name']}\".",
            f"Other participants: {', '.join(others) if others else '(none)'}.",
            "",
        ]

        request = status.get("request")
        if request:
            parts.append(f"{request['sender']} addressed you directly:")
            parts.append("")
            parts.append(_indent(request.get("content") or ""))
            if request.get("goal"):
                parts.append(f"\nGoal: {request['goal']}")
            for constraint in request.get("constraints") or []:
                parts.append(f"Constraint: {constraint}")
            for artifact in request.get("artifact_references") or []:
                parts.append(f"Artifact: {artifact}")
            parts.append("")
        else:
            parts.append("You have the floor. There is no specific request addressed to you.")
            parts.append("")

        if self.include_memory:
            memory = self.broker.memory_show(self.room_id, credential=self.observer)
            markdown = (memory.get("markdown") or "").strip()
            if markdown:
                parts.extend(["--- shared room memory ---", markdown, ""])

        if self.context_messages:
            transcript = self.broker.read(
                self.room_id, credential=self.observer, tail=self.context_messages
            )["messages"]
            if transcript:
                parts.append("--- recent conversation ---")
                for message in transcript:
                    target = f" -> {message['target']}" if message.get("target") else ""
                    parts.append(f"[{message['sender']}{target}] {message.get('content') or ''}")
                parts.append("")

        # The same persistence contract attached agents get on join, so a
        # conducted agent is told the same thing without the human arranging it.
        briefing = self.broker.briefing(self.room_id, credential=self.credentials[name])
        if briefing.repo:
            parts.extend(["--- repository ---", f"  {briefing.repo.get('description')}"])
            if briefing.repo_mismatch:
                parts.append(f"  !! {briefing.repo_mismatch}")
            parts.append("")
        parts.extend(["--- persistence ---", briefing.memory_note, ""])

        guidance = self.role_guidance.get(name)
        if guidance:
            parts.extend(["--- session agreement and your role ---", guidance.rstrip(), ""])

        # ``SYNCHRI_CLI`` is a shell fragment constructed by the managed
        # launcher (and may be ``'/Applications/…/Synchri'``).  It is only
        # rendered into an instructional prompt; it is never executed here.
        activity_command = agent.env.get("SYNCHRI_CLI", "synchri")

        parts.extend(
            [
                "--- how to reply ---",
                "Do the work, then print your reply on stdout. Everything you print becomes",
                "your message in the room, so do not print progress chatter.",
                "Synchri has published “Planning” for this turn. Before any skill or long-running",
                "tool use, replace it with one concise public semantic update, for example:",
                f"  {activity_command} activity --as {shlex.quote(name)} -m \"…\"",
                "Use phrases such as “Inspecting the auth module” or “Running the focused tests.”",
                "Do not publish private reasoning or tool-by-tool chatter. The live note clears",
                "when you respond, hand off, or pass.",
                "",
                "You may end your output with any of these control lines:",
                "  SYNCHRI-TO: <participant>        address them directly (blocks everyone else)",
                "  SYNCHRI-HANDOFF: <participant>   hand the baton over without a demand",
                "  SYNCHRI-PASS                     you have nothing material to add",
                "  SYNCHRI-STATUS: complete|partial|blocked|failed",
                "  SYNCHRI-CONFIDENCE: 0.0-1.0",
                "  SYNCHRI-GATE: <id>|<pending|in_progress|pass|fail|unverified>|<your assessment>",
                "  SYNCHRI-EVIDENCE: <artifact or observation for the preceding gate>",
                "  SYNCHRI-TEST: <test command or test name for the preceding gate>",
                "  SYNCHRI-COMMIT: <commit sha for the preceding gate>",
                "  SYNCHRI-APPROVAL: <ASK capability>|<specific decision needed from the human>",
                "  SYNCHRI-COMPLETE              Primary Builder: request final completion when evidence is complete",
                "",
                "Keep the collaboration moving. When another agent should act next, use the",
                "direct-address control line above. For a human decision, direct it to human",
                "and mark the response blocked. Add SYNCHRI-APPROVAL so the app can show clear",
                "Approve and Deny buttons. Gate reports update the shared progress view immediately.",
            ]
        )
        return "\n".join(parts)


def _response_status(requested: str | None, is_task: bool) -> str | None:
    valid = {s.value for s in ResponseStatus}
    if requested in valid:
        return requested
    return None if is_task else ResponseStatus.COMPLETE.value


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
