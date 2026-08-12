"""Deadlines, and the phases they impose.

A deadline is an execution boundary, not decoration. It changes what agents are
told to prioritise as it approaches, and when it passes it produces an honest
incomplete handoff -- never a completion claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..errors import ValidationError
from ..ids import utc_now

# Units are ordered longest-first so "10 minutes" does not match bare "m", and
# no word boundary is required so compound forms like "2h30m" parse.
_DURATION = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>days|day|d|hours|hour|hrs|hr|h|minutes|minute|mins|min|m)",
    re.I,
)
_UNIT_SECONDS = {
    "days": 86400, "day": 86400, "d": 86400,
    "hours": 3600, "hour": 3600, "hrs": 3600, "hr": 3600, "h": 3600,
    "minutes": 60, "minute": 60, "mins": 60, "min": 60, "m": 60,
}

#: Fraction of the window elapsed at which each phase begins.
PHASES = (
    (0.00, "exploration", "Architecture, exploration, and initial implementation."),
    (0.35, "implementation", "Implementation, review, and remediation."),
    (0.70, "stabilisation", "Stabilisation, testing, blocker resolution, acceptance verification."),
    (0.90, "freeze", "Freeze non-essential work. No new architecture. Run the relevant suites, "
                     "reconcile open findings, and prepare the final session state."),
)


def parse_duration(text: str) -> int:
    """Parse '10 hours', '2h30m', '45 min' into seconds."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("give a duration like '10 hours' or '2h30m'")
    total = sum(
        float(m.group("value")) * _UNIT_SECONDS[m.group("unit").lower()]
        for m in _DURATION.finditer(text)
    )
    if total <= 0:
        raise ValidationError(f"could not read a duration from {text!r}; try '10 hours' or '90m'")
    return int(total)


@dataclass
class Deadline:
    """A resolved absolute deadline plus the window it was set over."""

    ends_at: str
    started_at: str
    source: str

    @classmethod
    def from_duration(cls, text: str, *, now: datetime | None = None) -> "Deadline":
        seconds = parse_duration(text)
        start = now or datetime.now(timezone.utc)
        return cls(
            ends_at=_stamp(start + timedelta(seconds=seconds)),
            started_at=_stamp(start),
            source=f"duration: {text.strip()}",
        )

    @classmethod
    def from_timestamp(cls, value: str, *, now: datetime | None = None) -> "Deadline":
        """Accept an ISO-8601 instant; naive values are read as local time."""
        raw = (value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError(
                f"could not read {value!r} as a date and time; try '2026-08-12T08:00'"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        start = now or datetime.now(timezone.utc)
        if parsed <= start:
            raise ValidationError("that deadline is already in the past")
        return cls(ends_at=_stamp(parsed), started_at=_stamp(start), source=f"fixed: {value}")

    # -- state ---------------------------------------------------------

    def seconds_remaining(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        return int((_parse(self.ends_at) - current).total_seconds())

    def total_seconds(self) -> int:
        return max(1, int((_parse(self.ends_at) - _parse(self.started_at)).total_seconds()))

    def elapsed_fraction(self, *, now: datetime | None = None) -> float:
        used = self.total_seconds() - self.seconds_remaining(now=now)
        return max(0.0, min(1.0, used / self.total_seconds()))

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.seconds_remaining(now=now) <= 0

    def phase(self, *, now: datetime | None = None) -> tuple[str, str]:
        if self.is_expired(now=now):
            return "expired", "The deadline has passed. Produce an incomplete-status handoff."
        elapsed = self.elapsed_fraction(now=now)
        name, guidance = PHASES[0][1], PHASES[0][2]
        for threshold, phase_name, phase_guidance in PHASES:
            if elapsed >= threshold:
                name, guidance = phase_name, phase_guidance
        return name, guidance

    def remaining_text(self, *, now: datetime | None = None) -> str:
        seconds = self.seconds_remaining(now=now)
        if seconds <= 0:
            return "expired"
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def describe(self) -> str:
        local = _parse(self.ends_at).astimezone()
        return f"{self.source} — ends {local.strftime('%b %d, %Y at %I:%M %p %Z').strip()}"

    def to_dict(self) -> dict:
        return {
            "ends_at": self.ends_at,
            "started_at": self.started_at,
            "source": self.source,
            "description": self.describe(),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Deadline | None":
        if not data or not data.get("ends_at"):
            return None
        return cls(
            ends_at=data["ends_at"],
            started_at=data.get("started_at") or data["ends_at"],
            source=data.get("source") or "fixed",
        )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now_stamp() -> str:
    return utc_now()
