"""The normalized message envelope.

A message is never "just a string".  The envelope carries the semantic payload
(content, claim, evidence, constraints, artifacts) alongside the provenance the
room needs to answer *who said what, to whom, under which task, and what it
caused*.  Renderers are free to show only ``content``, which keeps the visible
transcript feeling like ordinary chat.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from ..errors import ValidationError
from ..ids import is_valid_id
from .enums import MessageType, ResponseStatus

MAX_CONTENT_CHARS = 200_000
MAX_LIST_ITEMS = 64
MAX_FIELD_CHARS = 4_000


@dataclass
class MessageEnvelope:
    """One message in a room, with full provenance."""

    # --- identity / routing -------------------------------------------------
    message_id: str
    room_id: str
    seq: int
    sender: str
    sender_participant_id: str
    timestamp: str
    message_type: str = MessageType.CHAT.value
    target: str | None = None
    target_participant_id: str | None = None

    # --- semantic payload ---------------------------------------------------
    content: str = ""
    goal: str | None = None
    task_id: str | None = None
    artifact_references: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    claim: str | None = None
    evidence: str | None = None
    request_type: str | None = None
    response_status: str | None = None
    confidence: float | None = None
    handoff_target: str | None = None

    # --- provenance of effect -----------------------------------------------
    in_reply_to: str | None = None
    turn_id: str | None = None
    was_targeted: bool = False
    human_override: bool = False
    resulted_in_handoff: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_row(cls, row) -> "MessageEnvelope":
        data = dict(row)
        return cls(
            message_id=data["message_id"],
            room_id=data["room_id"],
            seq=data["seq"],
            sender=data["sender"],
            sender_participant_id=data["sender_participant_id"],
            timestamp=data["timestamp"],
            message_type=data["message_type"],
            target=data["target"],
            target_participant_id=data["target_participant_id"],
            content=data["content"] or "",
            goal=data["goal"],
            task_id=data["task_id"],
            artifact_references=json.loads(data["artifact_references"] or "[]"),
            constraints=json.loads(data["constraints"] or "[]"),
            claim=data["claim"],
            evidence=data["evidence"],
            request_type=data["request_type"],
            response_status=data["response_status"],
            confidence=data["confidence"],
            handoff_target=data["handoff_target"],
            in_reply_to=data["in_reply_to"],
            turn_id=data["turn_id"],
            was_targeted=bool(data["was_targeted"]),
            human_override=bool(data["human_override"]),
            resulted_in_handoff=bool(data["resulted_in_handoff"]),
            metadata=json.loads(data["metadata"] or "{}"),
        )

    def to_row(self) -> dict:
        return {
            "message_id": self.message_id,
            "room_id": self.room_id,
            "seq": self.seq,
            "sender": self.sender,
            "sender_participant_id": self.sender_participant_id,
            "timestamp": self.timestamp,
            "message_type": self.message_type,
            "target": self.target,
            "target_participant_id": self.target_participant_id,
            "content": self.content,
            "goal": self.goal,
            "task_id": self.task_id,
            "artifact_references": json.dumps(self.artifact_references, ensure_ascii=False),
            "constraints": json.dumps(self.constraints, ensure_ascii=False),
            "claim": self.claim,
            "evidence": self.evidence,
            "request_type": self.request_type,
            "response_status": self.response_status,
            "confidence": self.confidence,
            "handoff_target": self.handoff_target,
            "in_reply_to": self.in_reply_to,
            "turn_id": self.turn_id,
            "was_targeted": int(self.was_targeted),
            "human_override": int(self.human_override),
            "resulted_in_handoff": int(self.resulted_in_handoff),
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
        }


@dataclass
class MessageDraft:
    """Caller-supplied fields for a new message, before the broker stamps it.

    Validation happens here so that a malformed message is rejected before any
    room state is touched.
    """

    content: str = ""
    message_type: str = MessageType.CHAT.value
    target: str | None = None
    goal: str | None = None
    task_id: str | None = None
    artifact_references: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    claim: str | None = None
    evidence: str | None = None
    request_type: str | None = None
    response_status: str | None = None
    confidence: float | None = None
    handoff_target: str | None = None
    in_reply_to: str | None = None
    metadata: dict = field(default_factory=dict)

    def validate(self) -> "MessageDraft":
        if not isinstance(self.content, str):
            raise ValidationError("content must be a string")
        if len(self.content) > MAX_CONTENT_CHARS:
            raise ValidationError(f"content exceeds {MAX_CONTENT_CHARS} characters")
        if self.message_type not in {m.value for m in MessageType}:
            raise ValidationError(
                f"unknown message_type {self.message_type!r}; "
                f"expected one of {sorted(m.value for m in MessageType)}"
            )
        if self.message_type != MessageType.PASS.value and not self.content.strip():
            raise ValidationError("content must not be empty")
        if self.response_status is not None and self.response_status not in {
            s.value for s in ResponseStatus
        }:
            raise ValidationError(
                f"unknown response_status {self.response_status!r}; "
                f"expected one of {sorted(s.value for s in ResponseStatus)}"
            )
        if self.confidence is not None:
            if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
                raise ValidationError("confidence must be a number between 0 and 1")
            if not 0.0 <= float(self.confidence) <= 1.0:
                raise ValidationError("confidence must be between 0 and 1")
            self.confidence = float(self.confidence)
        if self.task_id is not None and not is_valid_id("task", self.task_id):
            raise ValidationError(f"malformed task id: {self.task_id!r}")
        if self.in_reply_to is not None and not is_valid_id("msg", self.in_reply_to):
            raise ValidationError(f"malformed message id: {self.in_reply_to!r}")
        self.artifact_references = _clean_list(self.artifact_references, "artifact_references")
        self.constraints = _clean_list(self.constraints, "constraints")
        for name in ("goal", "claim", "evidence", "request_type"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValidationError(f"{name} must be a string")
                if len(value) > MAX_FIELD_CHARS:
                    raise ValidationError(f"{name} exceeds {MAX_FIELD_CHARS} characters")
        if not isinstance(self.metadata, dict):
            raise ValidationError("metadata must be an object")
        try:
            json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"metadata is not JSON-serializable: {exc}") from exc
        if self.target is not None and self.handoff_target is not None:
            raise ValidationError(
                "a message may carry a target or a handoff_target, not both: "
                "target addresses the next speaker directly, handoff_target defers to them"
            )
        return self


def _clean_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field_name} must be a list of strings")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"{field_name} must be a list of strings")
        item = item.strip()
        if not item:
            continue
        if len(item) > MAX_FIELD_CHARS:
            raise ValidationError(f"{field_name} entry exceeds {MAX_FIELD_CHARS} characters")
        items.append(item)
    if len(items) > MAX_LIST_ITEMS:
        raise ValidationError(f"{field_name} exceeds {MAX_LIST_ITEMS} entries")
    return items
