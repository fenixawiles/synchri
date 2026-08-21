"""Acceptance gates: the evidence model for completion.

"Both agents think it looks good" is not completion. A gate moves to PASS only
with cited evidence and sign-off from both the builder and the reviewer, and a
gate that cannot be checked is UNVERIFIED rather than optimistically passed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from ..errors import ValidationError


class GateStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIED = "unverified"
    WAIVED = "waived"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Statuses that let a session complete.
SATISFIED = {GateStatus.PASS.value, GateStatus.WAIVED.value}


@dataclass
class Gate:
    """One acceptance criterion drawn from the product specification."""

    gate_id: str
    description: str
    status: str = GateStatus.PENDING.value
    required: bool = True
    evidence: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    builder_assessment: str | None = None
    reviewer_assessment: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    #: Provenance: 'main' gates come from the original specification;
    #: 'extension' gates were materialized from an approved dropbox item.
    #: You can always tell what was part of the original job.
    origin_kind: str = "main"
    drop_id: str | None = None
    extension_id: str | None = None

    def __post_init__(self) -> None:
        if not self.gate_id or not str(self.gate_id).strip():
            raise ValidationError("a gate needs an id, e.g. AUTH-03")
        if not self.description or not str(self.description).strip():
            raise ValidationError(f"gate {self.gate_id} needs a description")
        if self.status not in {s.value for s in GateStatus}:
            raise ValidationError(f"unknown gate status {self.status!r}")

    @property
    def is_satisfied(self) -> bool:
        return self.status in SATISFIED

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence or self.tests or self.commits)

    @property
    def is_signed_off(self) -> bool:
        return bool(self.builder_assessment and self.reviewer_assessment)

    def blocks_completion(self) -> str | None:
        """Why this gate prevents completion, or None if it does not."""
        if not self.required:
            return None
        if self.status == GateStatus.WAIVED.value:
            return None
        if self.status != GateStatus.PASS.value:
            return f"{self.gate_id} is {self.status}"
        if not self.has_evidence:
            return f"{self.gate_id} is marked pass with no evidence"
        if not self.is_signed_off:
            missing = "builder" if not self.builder_assessment else "reviewer"
            return f"{self.gate_id} has no {missing} sign-off"
        return None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["satisfied"] = self.is_satisfied
        payload["blocker"] = self.blocks_completion()
        return payload

    def render(self) -> str:
        lines = [f"{self.gate_id}  [{self.status.upper()}]  {self.description}"]
        if self.origin_kind and self.origin_kind != "main":
            source = f" of {self.drop_id}" if self.drop_id else ""
            lines.append(f"    origin: {self.origin_kind}{source} — not part of the original specification")
        for label, values in (("evidence", self.evidence), ("tests", self.tests),
                              ("commits", self.commits)):
            for value in values:
                lines.append(f"    {label}: {value}")
        if self.builder_assessment:
            lines.append(f"    builder: {self.builder_assessment}")
        if self.reviewer_assessment:
            lines.append(f"    reviewer: {self.reviewer_assessment}")
        return "\n".join(lines)


def summarize(gates: list[Gate]) -> dict:
    """Counts plus the concrete reasons completion is still blocked."""
    required = [g for g in gates if g.required]
    blockers = [b for b in (g.blocks_completion() for g in gates) if b]
    return {
        "total": len(gates),
        "required": len(required),
        "passed": sum(1 for g in gates if g.status == GateStatus.PASS.value),
        "failed": sum(1 for g in gates if g.status == GateStatus.FAIL.value),
        "unverified": sum(1 for g in gates if g.status == GateStatus.UNVERIFIED.value),
        "pending": sum(1 for g in gates if g.status == GateStatus.PENDING.value),
        "waived": sum(1 for g in gates if g.status == GateStatus.WAIVED.value),
        "satisfied": sum(1 for g in required if g.is_satisfied),
        "blockers": blockers,
        "complete": bool(gates) and not blockers,
    }
