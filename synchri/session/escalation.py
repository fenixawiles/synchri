"""When agents are allowed to stop and ask the user.

The default set exists to answer one specific failure: agents pinging the human
every time a turn ends. Finishing a turn is not an escalation condition, and the
contract says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ValidationError


@dataclass(frozen=True)
class EscalationRule:
    key: str
    label: str
    always_on: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "always_on": self.always_on}


#: Rules marked always_on cannot be switched off: they are the conditions where
#: continuing without the user would be unsafe or dishonest.
CATALOG: tuple[EscalationRule, ...] = (
    EscalationRule("permission_missing",
                   "A required permission is not granted for this session.", True),
    EscalationRule("credential_missing",
                   "A required credential is unavailable.", True),
    EscalationRule("destructive_action",
                   "A destructive or irreversible operation is required.", True),
    EscalationRule("provider_approval",
                   "Your runtime or provider explicitly requires human approval.", True),
    EscalationRule("deadline_reached",
                   "The suggested timebox has elapsed.", False),
    EscalationRule("user_interrupt",
                   "The user paused, stopped, or interrupted the session.", True),
    EscalationRule("spec_ambiguity",
                   "The specification contains a materially blocking ambiguity."),
    EscalationRule("requirement_conflict",
                   "Two requirements materially conflict and cannot both be satisfied."),
    EscalationRule("deadlock",
                   "The agents remain deadlocked after the configured number of review cycles."),
    EscalationRule("external_service",
                   "Access to an external service is required."),
)

BY_KEY = {rule.key: rule for rule in CATALOG}
# An elapsed timebox informs pacing; it is never a reason to interrupt the
# human or stop the agents. Keep the legacy key readable for old sessions.
DEFAULT_KEYS = tuple(rule.key for rule in CATALOG if rule.key != "deadline_reached")
DEFAULT_DEADLOCK_CYCLES = 3


@dataclass
class EscalationPolicy:
    """Which conditions justify interrupting the human, plus any custom ones."""

    enabled: list[str] = field(default_factory=lambda: list(DEFAULT_KEYS))
    custom: list[str] = field(default_factory=list)
    deadlock_cycles: int = DEFAULT_DEADLOCK_CYCLES

    def __post_init__(self) -> None:
        unknown = set(self.enabled) - set(BY_KEY)
        if unknown:
            raise ValidationError(f"unknown escalation rules: {', '.join(sorted(unknown))}")
        # Mandatory rules are re-added rather than rejected: silently dropping
        # one would be worse than correcting the caller.
        for rule in CATALOG:
            if rule.always_on and rule.key not in self.enabled:
                self.enabled.append(rule.key)
        if not isinstance(self.deadlock_cycles, int) or self.deadlock_cycles < 1:
            raise ValidationError("deadlock cycles must be a positive integer")

    def rules(self) -> list[str]:
        ordered = [BY_KEY[r.key].label for r in CATALOG if r.key in self.enabled]
        return ordered + list(self.custom)

    def render_contract_block(self) -> str:
        lines = [f"  - {line}" for line in self.rules()]
        lines.append(
            f"  - The agents remain deadlocked after {self.deadlock_cycles} review cycles."
            if "deadlock" in self.enabled
            else "  - (deadlock escalation disabled)"
        )
        lines.append("")
        lines.append("  Do NOT escalate merely because you finished a turn. Finishing a turn is")
        lines.append("  not an escalation condition; hand off and keep working.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "enabled": list(self.enabled),
            "custom": list(self.custom),
            "deadlock_cycles": self.deadlock_cycles,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "EscalationPolicy":
        data = data or {}
        return cls(
            enabled=list(data.get("enabled") or DEFAULT_KEYS),
            custom=list(data.get("custom") or []),
            deadlock_cycles=int(data.get("deadlock_cycles") or DEFAULT_DEADLOCK_CYCLES),
        )
