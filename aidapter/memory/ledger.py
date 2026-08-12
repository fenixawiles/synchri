"""Shared semantic memory ledger (layer 2 of 3).

One Markdown file per room, at ``rooms/<room_id>/memory.md``.  This is *not*
queue state and it is *not* the transcript: it is the durable shared
understanding — goal, current task, decisions, constraints, open issues,
handoffs, artifacts, disagreements — that both agents and the human can read
and edit with any text editor.

The file is authoritative for its own contents rather than being rendered from
a table, precisely so that a human editing it by hand is a first-class action
and not something the next agent write silently discards.  Writes are
read-modify-write under the room's database write lock, then atomically
renamed into place.

Unknown ``##`` sections a human adds are preserved verbatim on rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Workspace, write_private
from ..errors import ValidationError
from ..ids import utc_now

#: Canonical sections, in the order they are rendered.
#: ``list`` sections accumulate bullet entries; ``value`` sections hold prose.
SECTIONS: dict[str, str] = {
    "Goal": "value",
    "Current Task": "value",
    "Decisions": "list",
    "Constraints": "list",
    "Open Issues": "list",
    "Recent Handoffs": "list",
    "Artifacts": "list",
    "Unresolved Disagreements": "list",
}

#: CLI-friendly aliases -> canonical heading.
SECTION_ALIASES: dict[str, str] = {
    "goal": "Goal",
    "current_task": "Current Task",
    "current-task": "Current Task",
    "task": "Current Task",
    "decisions": "Decisions",
    "decision": "Decisions",
    "constraints": "Constraints",
    "constraint": "Constraints",
    "open_issues": "Open Issues",
    "open-issues": "Open Issues",
    "issues": "Open Issues",
    "issue": "Open Issues",
    "handoffs": "Recent Handoffs",
    "recent_handoffs": "Recent Handoffs",
    "handoff": "Recent Handoffs",
    "artifacts": "Artifacts",
    "artifact": "Artifacts",
    "disagreements": "Unresolved Disagreements",
    "unresolved_disagreements": "Unresolved Disagreements",
    "disagreement": "Unresolved Disagreements",
}

LIST_SECTIONS = {name for name, kind in SECTIONS.items() if kind == "list"}
VALUE_SECTIONS = {name for name, kind in SECTIONS.items() if kind == "value"}

MAX_ENTRY_CHARS = 4_000
MAX_ENTRIES_PER_SECTION = 500
#: How many handoffs to retain; "Recent Handoffs" is a rolling window by design.
MAX_HANDOFFS = 25

_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_EMPTY_MARKER = "_(none)_"


def resolve_section(name: str) -> str:
    """Map a user-supplied section name onto a canonical heading."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("section name is required")
    key = name.strip()
    if key in SECTIONS:
        return key
    canonical = SECTION_ALIASES.get(key.lower().replace(" ", "_"))
    if canonical is None:
        raise ValidationError(
            f"unknown memory section {name!r}; expected one of {sorted(SECTIONS)}"
        )
    return canonical


@dataclass
class Ledger:
    """Parsed representation of one room's memory file."""

    room_id: str
    room_name: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)
    #: Sections a human added that AIDapter does not know about, kept verbatim.
    extra: dict[str, list[str]] = field(default_factory=dict)

    def get_value(self, section: str) -> str:
        lines = self.sections.get(section, [])
        return "\n".join(lines).strip()

    def entries(self, section: str) -> list[str]:
        return list(self.sections.get(section, []))

    def to_dict(self) -> dict:
        data: dict[str, object] = {"room_id": self.room_id, "room_name": self.room_name}
        for section, kind in SECTIONS.items():
            key = section.lower().replace(" ", "_")
            data[key] = self.get_value(section) if kind == "value" else self.entries(section)
        if self.extra:
            data["additional_sections"] = {k: list(v) for k, v in self.extra.items()}
        return data


def default_ledger(room_id: str, room_name: str) -> Ledger:
    return Ledger(room_id=room_id, room_name=room_name, sections={name: [] for name in SECTIONS})


def render(ledger: Ledger) -> str:
    """Serialize a ledger back to Markdown."""
    out: list[str] = [
        f"# AIDapter room memory — {ledger.room_name or ledger.room_id}",
        "",
        f"<!-- room_id: {ledger.room_id} -->",
        "<!-- Shared semantic memory. Humans and agents may edit this file directly.",
        "     This file is NOT the queue and NOT the transcript; orchestration state",
        "     lives in SQLite and the chronological log lives in transcript.jsonl. -->",
        "",
    ]
    for section, kind in SECTIONS.items():
        out.append(f"## {section}")
        out.append("")
        lines = ledger.sections.get(section) or []
        if not lines:
            out.append(_EMPTY_MARKER)
        elif kind == "value":
            out.extend(lines)
        else:
            out.extend(f"- {line}" for line in lines)
        out.append("")
    for title, lines in ledger.extra.items():
        out.append(f"## {title}")
        out.append("")
        out.extend(lines)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def parse(text: str, room_id: str, room_name: str = "") -> Ledger:
    """Parse a memory file, tolerating hand edits.

    Anything before the first ``##`` heading is treated as the file preamble and
    dropped on rewrite; unknown headings are preserved under ``extra``.
    """
    ledger = default_ledger(room_id, room_name)
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        lines = _strip_blank_edges(buffer)
        if lines == [_EMPTY_MARKER]:
            lines = []
        if current in SECTIONS:
            if SECTIONS[current] == "list":
                ledger.sections[current] = [
                    _strip_bullet(line) for line in lines if _strip_bullet(line)
                ]
            else:
                ledger.sections[current] = lines
        else:
            ledger.extra[current] = lines

    for raw in text.splitlines():
        match = _HEADING.match(raw)
        if match:
            flush()
            current = match.group("title")
            buffer = []
            continue
        if current is not None:
            buffer.append(raw)
    flush()
    return ledger


def _strip_bullet(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith(("- ", "* ")):
        return stripped[2:].strip()
    if stripped in {"-", "*"}:
        return ""
    return stripped


def _strip_blank_edges(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def provenance_suffix(actor: str, message_id: str | None = None, at: str | None = None) -> str:
    """Standard trailer that keeps ledger entries attributable."""
    parts = [f"— {actor}", at or utc_now()]
    if message_id:
        parts.append(message_id)
    return f"  _({' · '.join(parts)})_"


class LedgerStore:
    """Filesystem access to per-room memory ledgers."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def path(self, room_id: str) -> Path:
        return self.workspace.memory_path(room_id)

    def load(self, room_id: str, room_name: str = "") -> Ledger:
        path = self.path(room_id)
        if not path.exists():
            return default_ledger(room_id, room_name)
        return parse(path.read_text(encoding="utf-8"), room_id, room_name)

    def save(self, ledger: Ledger) -> Path:
        self.workspace.ensure_room_dir(ledger.room_id)
        path = self.path(ledger.room_id)
        write_private(path, render(ledger))
        return path

    def initialize(self, room_id: str, room_name: str, goal: str | None = None) -> Path:
        ledger = default_ledger(room_id, room_name)
        if goal:
            ledger.sections["Goal"] = [goal.strip()]
        return self.save(ledger)

    # -- mutations ---------------------------------------------------------

    def set_value(self, room_id: str, section: str, value: str, *, actor: str, room_name: str = "") -> Ledger:
        section = resolve_section(section)
        if section not in VALUE_SECTIONS:
            raise ValidationError(
                f"section {section!r} holds a list of entries; use 'memory add' instead of 'memory set'"
            )
        value = _validate_entry(value)
        ledger = self.load(room_id, room_name)
        ledger.sections[section] = [f"{value}{provenance_suffix(actor)}"]
        self.save(ledger)
        return ledger

    def add_entry(
        self,
        room_id: str,
        section: str,
        entry: str,
        *,
        actor: str,
        message_id: str | None = None,
        room_name: str = "",
    ) -> Ledger:
        section = resolve_section(section)
        if section not in LIST_SECTIONS:
            raise ValidationError(
                f"section {section!r} holds a single value; use 'memory set' instead of 'memory add'"
            )
        entry = _validate_entry(entry)
        ledger = self.load(room_id, room_name)
        entries = ledger.sections.setdefault(section, [])
        entries.append(f"{entry}{provenance_suffix(actor, message_id)}")
        cap = MAX_HANDOFFS if section == "Recent Handoffs" else MAX_ENTRIES_PER_SECTION
        if len(entries) > cap:
            del entries[: len(entries) - cap]
        self.save(ledger)
        return ledger

    def remove_entry(self, room_id: str, section: str, index: int, *, room_name: str = "") -> Ledger:
        section = resolve_section(section)
        if section not in LIST_SECTIONS:
            raise ValidationError(f"section {section!r} has no removable entries")
        ledger = self.load(room_id, room_name)
        entries = ledger.sections.get(section) or []
        if not 1 <= index <= len(entries):
            raise ValidationError(
                f"no entry {index} in {section!r} (it has {len(entries)} entries)"
            )
        entries.pop(index - 1)
        self.save(ledger)
        return ledger


def _validate_entry(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("memory entry must be a non-empty string")
    value = value.strip()
    if len(value) > MAX_ENTRY_CHARS:
        raise ValidationError(f"memory entry exceeds {MAX_ENTRY_CHARS} characters")
    # Keep entries single-logical-line so the Markdown list structure survives.
    return " ".join(value.splitlines()).strip()
