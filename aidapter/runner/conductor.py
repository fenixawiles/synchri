"""Drive a whole room from a single terminal.

Nothing about AIDapter ever required one terminal per agent — a participant is
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

import time
from dataclasses import dataclass, field
from typing import Callable

from ..broker import Broker, Credential
from ..errors import AidapterError, ValidationError
from ..models.enums import MessageType, ResponseStatus, RoomStatus, TurnState
from ..models.envelope import MessageDraft
from .agent_command import AgentCommand, parse_directives

#: Why the conductor handed control back.
STOP_AWAITING_HUMAN = "awaiting_human"
STOP_ROOM_STOPPED = "room_stopped"
STOP_ROOM_PAUSED = "room_paused"
STOP_TURN_LIMIT = "turn_limit"
STOP_IDLE = "idle"
STOP_UNMANAGED_SPEAKER = "unmanaged_speaker"


@dataclass
class ConductorReport:
    """What happened during one `aidapter run`."""

    room_id: str
    reason: str
    turns: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        if not agents:
            raise ValidationError("at least one --agent is required")
        missing = sorted(set(agents) - set(credentials))
        if missing:
            raise ValidationError(
                f"no credential available for managed agent(s) {', '.join(missing)}; "
                "run 'aidapter join' for each one first"
            )
        self.broker = broker
        self.room_id = room_id
        self.agents = agents
        self.credentials = credentials
        self.observer = observer
        self.context_messages = context_messages
        self.include_memory = include_memory
        self.on_event = on_event or (lambda event, payload: None)

    # ------------------------------------------------------------------

    def run(self, *, max_turns: int | None = None, poll_interval: float = 0.5) -> ConductorReport:
        report = ConductorReport(room_id=self.room_id, reason=STOP_IDLE)
        while True:
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

    # ------------------------------------------------------------------

    def _take_turn(self, name: str) -> dict:
        credential = self.credentials[name]
        status = self.broker.turn_status(self.room_id, credential=credential)
        if status["state"] != TurnState.YOUR_TURN.value:
            # Lost the floor between the status read and now — a human can
            # interrupt at any moment, and that is allowed to beat us.
            return {"participant": name, "skipped": status["state"]}

        prompt = self.build_prompt(name, status)
        self.on_event("agent.invoking", {"participant": name, "prompt_chars": len(prompt)})

        result = self.agents[name].invoke(prompt)
        self.on_event(
            "agent.returned",
            {
                "participant": name,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "output_chars": len(result.stdout),
            },
        )
        return self._post(name, result)

    def _post(self, name: str, result) -> dict:
        credential = self.credentials[name]
        body, directives = parse_directives(result.stdout)
        warnings = list(directives.warnings)

        try:
            if not result.ok:
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

            if directives.passed or not body.strip():
                passed = self.broker.pass_turn(
                    self.room_id,
                    credential=credential,
                    reason=body.strip() or "nothing material to add",
                )
                return {
                    "participant": name,
                    "status": "passed",
                    "message_id": passed["message"]["message_id"],
                    "next_speaker": passed.get("next_speaker"),
                    "warnings": warnings,
                }

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
                    metadata={"conductor": True},
                ),
            )
            return {
                "participant": name,
                "status": "spoke",
                "message_id": sent["message"]["message_id"],
                "target": directives.to,
                "handoff_target": directives.handoff,
                "next_speaker": sent.get("next_speaker"),
                "warnings": warnings,
            }
        except AidapterError as exc:
            # The room refused the post (interrupted, stopped, bad target...).
            # Report it; the loop re-reads room state and decides what is next.
            warnings.append(f"{name}: {exc.code}: {exc.message}")
            return {"participant": name, "status": "rejected", "error": exc.code, "warnings": warnings}

    # ------------------------------------------------------------------

    def build_prompt(self, name: str, status: dict) -> str:
        """Assemble what a managed agent is told when it gets the floor."""
        room = self.broker.room_status(self.room_id, credential=self.observer)
        others = [
            p["name"]
            for p in room["participants"]
            if p["name"] != name and p["status"] == "active"
        ]

        parts: list[str] = [
            f"You are '{name}' in the AIDapter room \"{room['room']['name']}\".",
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

        parts.extend(
            [
                "--- how to reply ---",
                "Do the work, then print your reply on stdout. Everything you print becomes",
                "your message in the room, so do not print progress chatter.",
                "",
                "You may end your output with any of these control lines:",
                "  AIDAPTER-TO: <participant>        address them directly (blocks everyone else)",
                "  AIDAPTER-HANDOFF: <participant>   hand the baton over without a demand",
                "  AIDAPTER-PASS                     you have nothing material to add",
                "  AIDAPTER-STATUS: complete|partial|blocked|failed",
                "  AIDAPTER-CONFIDENCE: 0.0-1.0",
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
