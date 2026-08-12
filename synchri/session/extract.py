"""Pull acceptance gates out of a product specification.

The user should not have to restate their spec as a list of gate objects. If the
spec already names criteria -- and most do, as `AUTH-01` style IDs or as bullets
under an "Acceptance" heading -- we lift them out.

Extraction is a *proposal*, never authority: gates land in PENDING with no
evidence, and the spec itself is never rewritten. If we find nothing, we say so
rather than inventing criteria.
"""

from __future__ import annotations

import re

from .gates import Gate

#: "AUTH-03", "API-01:", "GATE 12 -" -- an explicit identifier at the start.
_EXPLICIT_ID = re.compile(r"^\s*(?:[-*+]\s*)?(?P<id>[A-Z][A-Z0-9]{1,15}[-_ ]?\d{1,3})\b[.:)\-\s]*(?P<text>.*)$")

#: Headings that introduce a list of criteria.
_ACCEPTANCE_HEADING = re.compile(
    r"^#{1,6}\s*(acceptance|acceptance criteria|requirements|gates|success criteria|"
    r"definition of done)\b",
    re.I,
)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$")

MAX_GATES = 100
MAX_DESCRIPTION = 300


def extract_gates(spec_text: str) -> list[Gate]:
    """Propose gates from a specification. Empty means "we could not tell"."""
    if not spec_text or not spec_text.strip():
        return []

    explicit = _explicit_gates(spec_text)
    if explicit:
        return explicit[:MAX_GATES]
    return _acceptance_section_gates(spec_text)[:MAX_GATES]


def _explicit_gates(spec_text: str) -> list[Gate]:
    """Lines that already carry an identifier — the strongest signal."""
    found: list[Gate] = []
    seen: set[str] = set()
    for line in spec_text.splitlines():
        if _ANY_HEADING.match(line):
            continue
        match = _EXPLICIT_ID.match(line.rstrip())
        if not match:
            continue
        gate_id = re.sub(r"[ _]", "-", match.group("id").strip()).upper()
        description = (match.group("text") or "").strip(" -:.\t")
        if not description or gate_id in seen:
            continue
        seen.add(gate_id)
        found.append(Gate(gate_id=gate_id, description=_trim(description)))
    return found


def _acceptance_section_gates(spec_text: str) -> list[Gate]:
    """Bullets under an acceptance-style heading, numbered for us."""
    lines = spec_text.splitlines()
    collecting = False
    items: list[str] = []
    for line in lines:
        if _ACCEPTANCE_HEADING.match(line):
            collecting = True
            continue
        if collecting and _ANY_HEADING.match(line):
            break  # a different section began
        if not collecting:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            text = bullet.group("text").strip()
            if text:
                items.append(text)

    return [
        Gate(gate_id=f"GATE-{index:02d}", description=_trim(text))
        for index, text in enumerate(items, start=1)
    ]


def _trim(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= MAX_DESCRIPTION else collapsed[: MAX_DESCRIPTION - 1] + "…"


def describe(gates: list[Gate]) -> str:
    if not gates:
        return (
            "No acceptance criteria detected. Add IDs like 'AUTH-01 ...' or an "
            "'## Acceptance' section, or define gates yourself."
        )
    return f"Detected {len(gates)} acceptance gate(s): " + ", ".join(g.gate_id for g in gates)
