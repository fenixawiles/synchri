"""The join-time briefing.

Two things have to happen the moment an agent enters a room, without the human
arranging either of them:

1. **Orient it.** Which repo is this about, what is the room trying to do, what
   has already been decided, and what happened while it was away.
2. **Tell it where durable state belongs.** AIDapter persists the *room* —
   transcript, ledger, queue, provenance — across sessions and restarts. It does
   not persist an agent's own working context, and should not try to. The
   briefing says so explicitly and tells the agent to keep shared conclusions in
   the ledger and its own resumption notes in whatever memory its platform
   provides.

The briefing is assembled here as a pure function so the broker, the CLI, and
the conductor all deliver exactly the same text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: What every agent is told about persistence on joining. Overridable per room
#: with ``--memory-note``; ``{name}`` is substituted with the participant's name.
DEFAULT_MEMORY_NOTE = """\
AIDapter durably stores the ROOM: the transcript, the shared memory ledger, the
queue and all provenance survive this session and any restart. It does NOT store
your own reasoning, plans, or working context — that is yours to keep.

So, as you work:
  * Record durable, shared conclusions in the room ledger, where the other
    agents and the human can all see them:
        aidapter memory add decisions   "..." --as {name}
        aidapter memory add constraints "..." --as {name}
        aidapter memory add open_issues "..." --as {name}
        aidapter memory set current_task "..." --as {name}
  * Record whatever YOU need in order to resume later in whatever persistent
    memory your own platform gives you (your project memory file, session notes,
    scratch files in the repo). AIDapter deliberately does not manage that.
  * Re-read this briefing at any time with:  aidapter briefing --as {name}"""


@dataclass
class Briefing:
    """Everything a participant needs on arrival, structured and rendered."""

    room_id: str
    room_name: str
    participant: str
    kind: str
    room_status: str
    participants: list[str] = field(default_factory=list)
    repo: dict | None = None
    repo_mismatch: str | None = None
    memory: dict = field(default_factory=dict)
    missed_messages: list[dict] = field(default_factory=list)
    your_turn: bool = False
    pending_request: dict | None = None
    autonomy: dict = field(default_factory=dict)
    memory_note: str = ""
    returning: bool = False

    def to_dict(self) -> dict:
        payload = {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "participant": self.participant,
            "kind": self.kind,
            "room_status": self.room_status,
            "participants": self.participants,
            "repo": self.repo,
            "repo_mismatch": self.repo_mismatch,
            "memory": self.memory,
            "missed_messages": self.missed_messages,
            "your_turn": self.your_turn,
            "pending_request": self.pending_request,
            "autonomy": self.autonomy,
            "memory_note": self.memory_note,
            "returning": self.returning,
        }
        payload["text"] = self.render()
        return payload

    # ------------------------------------------------------------------

    def render(self) -> str:
        others = [p for p in self.participants if p != self.participant] or ["(nobody yet)"]
        lines = [
            f"AIDAPTER BRIEFING — {self.room_name}  ({self.room_id})",
            f"You are: {self.participant} ({self.kind})   Also here: {', '.join(others)}",
            f"Room status: {self.room_status}"
            + (
                f"   Autonomy: {self.autonomy.get('consecutive_agent_turns', 0)}"
                f"/{self.autonomy.get('max_consecutive_agent_turns', '?')} agent turns"
                if self.autonomy
                else ""
            ),
            "",
        ]

        lines.append("REPOSITORY")
        if self.repo:
            lines.append(f"  {self.repo.get('description') or self.repo.get('root')}")
        else:
            lines.append("  (this room is not bound to a repository)")
        if self.repo_mismatch:
            lines.append(f"  !! {self.repo_mismatch}")
        lines.append("")

        lines.append("SHARED MEMORY  (durable — this is what survives the session)")
        lines.extend(_render_memory(self.memory))
        lines.append("")

        if self.missed_messages:
            label = "WHILE YOU WERE AWAY" if self.returning else "CONVERSATION SO FAR"
            lines.append(f"{label}  ({len(self.missed_messages)} message(s))")
            for message in self.missed_messages:
                target = f" → {message['target']}" if message.get("target") else ""
                body = " ".join((message.get("content") or "").split())
                if len(body) > 300:
                    body = body[:299] + "…"
                lines.append(f"  [{message['sender']}{target}] {body or '(no content)'}")
            lines.append("")

        if self.your_turn:
            lines.append("IT IS YOUR TURN RIGHT NOW.")
            if self.pending_request:
                request = self.pending_request
                lines.append(f"  {request['sender']} asked you:")
                lines.append(f"    {' '.join((request.get('content') or '').split())}")
                for constraint in request.get("constraints") or []:
                    lines.append(f"    constraint: {constraint}")
                for artifact in request.get("artifact_references") or []:
                    lines.append(f"    artifact: {artifact}")
        else:
            lines.append(
                f"It is not your turn yet. Park with:  aidapter wait --as {self.participant}"
            )
        lines.extend(["", "PERSISTENCE", *(f"  {line}" for line in self.memory_note.splitlines())])
        return "\n".join(lines)


def _render_memory(memory: dict) -> list[str]:
    if not memory:
        return ["  (empty)"]
    lines: list[str] = []
    for key, label in (
        ("goal", "Goal"),
        ("current_task", "Current task"),
    ):
        value = memory.get(key)
        if value:
            lines.append(f"  {label}: {value}")
    for key, label in (
        ("decisions", "Decisions"),
        ("constraints", "Constraints"),
        ("open_issues", "Open issues"),
        ("unresolved_disagreements", "Unresolved disagreements"),
        ("recent_handoffs", "Recent handoffs"),
        ("artifacts", "Artifacts"),
    ):
        entries = memory.get(key) or []
        if entries:
            lines.append(f"  {label}:")
            lines.extend(f"    - {entry}" for entry in entries)
    return lines or ["  (empty)"]


def memory_note_for(template: str | None, name: str) -> str:
    """Fill the room's persistence note in for one participant."""
    text = template or DEFAULT_MEMORY_NOTE
    try:
        return text.format(name=name)
    except (KeyError, IndexError, ValueError):
        # A custom note with stray braces should still be delivered verbatim
        # rather than blowing up the join it is attached to.
        return text
