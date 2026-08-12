"""Human-readable rendering for the CLI.

The transcript should read like ordinary chat even though every message carries
a structured envelope underneath.  Structured fields are shown as indented
annotations only when they are present.
"""

from __future__ import annotations

import json
from typing import Iterable

from ..models.enums import MessageType

_TYPE_MARKS = {
    MessageType.TASK.value: "▶",
    MessageType.RESPONSE.value: "◀",
    MessageType.PASS.value: "·",
    MessageType.HANDOFF.value: "⇢",
    MessageType.INTERRUPT.value: "!",
    MessageType.SYSTEM.value: "*",
    MessageType.CHAT.value: " ",
}


def as_json(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def short_time(timestamp: str) -> str:
    # "2026-08-12T14:03:11.123Z" -> "14:03:11"
    return timestamp[11:19] if len(timestamp) >= 19 else timestamp


def message_line(message: dict) -> str:
    mark = _TYPE_MARKS.get(message.get("message_type", ""), " ")
    header = f"[#{message['seq']:>4}] {short_time(message['timestamp'])} {mark} {message['sender']}"
    if message.get("target"):
        header += f" → {message['target']}"
    tags = []
    if message.get("message_type") not in (MessageType.CHAT.value, None):
        tags.append(message["message_type"])
    if message.get("human_override"):
        tags.append("human-override")
    if message.get("task_id"):
        tags.append(message["task_id"])
    if tags:
        header += f"  ({', '.join(tags)})"

    lines = [header]
    body = (message.get("content") or "").strip()
    if body:
        lines.extend(f"    {line}" for line in body.splitlines())
    elif message.get("message_type") == MessageType.PASS.value:
        lines.append("    (pass — nothing material to add)")

    for label, key in (
        ("goal", "goal"),
        ("claim", "claim"),
        ("evidence", "evidence"),
        ("request", "request_type"),
        ("status", "response_status"),
        ("handoff", "handoff_target"),
        ("reply-to", "in_reply_to"),
    ):
        value = message.get(key)
        if value:
            lines.append(f"    {label}: {value}")
    if message.get("confidence") is not None:
        lines.append(f"    confidence: {message['confidence']:.2f}")
    for label, key in (("constraints", "constraints"), ("artifacts", "artifact_references")):
        values = message.get(key) or []
        for value in values:
            lines.append(f"    {label[:-1]}: {value}")
    lines.append(f"    id: {message['message_id']}")
    return "\n".join(lines)


def transcript(messages: Iterable[dict]) -> str:
    rendered = [message_line(m) for m in messages]
    return "\n\n".join(rendered) if rendered else "(no messages yet)"


def queue_block(queue: list[dict]) -> str:
    if not queue:
        return "  (queue empty)"
    lines = []
    for item in queue:
        flag = " [blocking]" if item.get("blocking") else ""
        reason = f" — {item['reason']}" if item.get("reason") else ""
        lines.append(
            f"  {item['position']}. {item['participant']} "
            f"(p{item['priority']}/{item['priority_name']}){flag}{reason}"
        )
    return "\n".join(lines)


def room_status(status: dict) -> str:
    room = status["room"]
    autonomy = status["autonomy"]
    lines = [
        f"Room     {room['name']}  ({room['room_id']})",
        f"Status   {room['status']}"
        + ("  [AWAITING HUMAN]" if autonomy["awaiting_human"] else ""),
        f"Speaker  {status['active_speaker'] or '(nobody)'}"
        + (
            "  [blocking targeted turn]"
            if status.get("active_turn") and status["active_turn"].get("blocking")
            else ""
        ),
        f"Autonomy {autonomy['consecutive_agent_turns']}/"
        f"{autonomy['max_consecutive_agent_turns']} consecutive agent turns",
        "",
        "Participants:",
    ]
    for participant in status["participants"]:
        marker = "*" if participant["name"] == status["active_speaker"] else " "
        removed = "  (removed)" if participant["status"] != "active" else ""
        lines.append(f"  {marker} {participant['name']:<16} {participant['kind']}{removed}")
    lines.extend(["", "Queue:", queue_block(status["queue"])])
    if status["open_tasks"]:
        lines.extend(["", "Open tasks:"])
        for task in status["open_tasks"]:
            lines.append(f"  {task['task_id']}  {task['assigned_to']}: {task['goal']}")
    lines.extend(
        [
            "",
            f"Memory     {status['memory_path']}",
            f"Transcript {status['transcript_path']}",
        ]
    )
    return "\n".join(lines)


def turn_status(status: dict) -> str:
    state = status["state"]
    lines = [f"state: {state}"]
    if state == "your_turn":
        if status.get("blocking"):
            lines.append("This is a blocking targeted turn: other agents are waiting on you.")
        if status.get("task_id"):
            lines.append(f"task: {status['task_id']}")
        request = status.get("request")
        if request:
            lines.append("")
            lines.append("Request:")
            lines.append(message_line(request))
    elif state == "waiting":
        lines.append(f"position: {status.get('position')}")
        lines.append(f"active speaker: {status.get('active_speaker') or '(nobody)'}")
    elif state == "blocked":
        lines.append(f"blocked by: {status.get('blocked_by')}")
    elif state == "awaiting_human":
        lines.append(
            f"the room hit its limit of {status['max_consecutive_agent_turns']} consecutive "
            "agent turns and needs human input"
        )
    elif state == "paused":
        lines.append("the room is paused by the human")
    elif state == "stopped":
        lines.append("the room has been stopped")
    elif state == "removed":
        lines.append("you have been removed from this room")
    if status.get("queue"):
        lines.extend(["", "Queue:", queue_block(status["queue"])])
    return "\n".join(lines)


def rooms_table(rooms: list[dict]) -> str:
    if not rooms:
        return "(no rooms yet — run 'synchri create-room --name \"...\"')"
    lines = [f"{'ROOM ID':<24} {'STATUS':<8} {'PARTICIPANTS':<28} NAME"]
    for room in rooms:
        people = ", ".join(room.get("participants") or []) or "-"
        if len(people) > 27:
            people = people[:26] + "…"
        lines.append(f"{room['room_id']:<24} {room['status']:<8} {people:<28} {room['name']}")
    return "\n".join(lines)


def memory_block(payload: dict) -> str:
    markdown = payload.get("markdown") or ""
    return markdown.rstrip() or "(memory ledger is empty)"
