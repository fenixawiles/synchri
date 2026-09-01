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
#: The same section openers written as a plain line ("Acceptance criteria:").
#: Briefs pasted from tickets and chats rarely carry markdown '#' marks, and
#: they used to fall through to the generic SPEC-01 gate.
_ACCEPTANCE_LINE = re.compile(
    r"^\s*(acceptance|acceptance criteria|requirements|gates|success criteria|"
    r"definition of done)\s*[:\-]?\s*$",
    re.I,
)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$")

MAX_GATES = 100
MAX_DESCRIPTION = 300

#: How a session's gates came to exist — persisted in session metadata so the
#: dashboard can keep explaining it long after the type-time preview is gone.
DERIVATION_EXPLICIT = "explicit_ids"
DERIVATION_ACCEPTANCE = "acceptance_list"
DERIVATION_GENERIC = "generic_fallback"
#: Set at plan promotion (synchri.session.planning), never by extraction.
DERIVATION_APPROVED_PLAN = "approved_plan"

DERIVATION_NOTES = {
    DERIVATION_EXPLICIT: (
        "These gates come from the explicit identifiers your brief already "
        "carried (AUTH-01-style lines) — nothing was invented."
    ),
    DERIVATION_ACCEPTANCE: (
        "These gates come from the bullets under your brief's "
        "acceptance-criteria heading, numbered in order."
    ),
    DERIVATION_GENERIC: (
        "Your brief carried no parseable acceptance criteria, so one generic "
        "gate holds the whole specification honest at completion. You or the "
        "agents can add sharper gates at any time."
    ),
    DERIVATION_APPROVED_PLAN: (
        "These gates are the approved plan's acceptance criteria, "
        "materialized verbatim when the plan was promoted."
    ),
}


def derivation_note(kind: str | None) -> str | None:
    return DERIVATION_NOTES.get(kind or "")


def extract_gates_with_derivation(spec_text: str) -> tuple[list[Gate], str]:
    """Propose gates from a specification, and say which rule produced them."""
    if not spec_text or not spec_text.strip():
        return [], DERIVATION_GENERIC

    explicit = _explicit_gates(spec_text)
    if explicit:
        return explicit[:MAX_GATES], DERIVATION_EXPLICIT
    accepted = _acceptance_section_gates(spec_text)
    if accepted:
        return accepted[:MAX_GATES], DERIVATION_ACCEPTANCE
    # The product brief is allowed to be a ticket, pasted chat, plain prose,
    # or any other text.  A generic evidence gate keeps that unconstrained
    # input honest at completion without pretending Synchri understood or
    # rewrote the user's requirements.
    return (
        [Gate(gate_id="SPEC-01", description="Deliver the supplied specification.")],
        DERIVATION_GENERIC,
    )


def extract_gates(spec_text: str) -> list[Gate]:
    """Propose gates from a specification. Empty means "we could not tell"."""
    return extract_gates_with_derivation(spec_text)[0]


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
    """Bullets under an acceptance-style heading (markdown or plain), numbered."""
    lines = spec_text.splitlines()
    collecting = False
    plain_section = False
    saw_bullet = False
    items: list[str] = []
    for line in lines:
        if _ACCEPTANCE_HEADING.match(line):
            collecting, plain_section, saw_bullet = True, False, False
            continue
        if _ACCEPTANCE_LINE.match(line):
            collecting, plain_section, saw_bullet = True, True, False
            continue
        if not collecting:
            continue
        if _ANY_HEADING.match(line):
            break  # a different section began
        bullet = _BULLET.match(line)
        if bullet:
            saw_bullet = True
            text = bullet.group("text").strip()
            if text:
                items.append(text)
            continue
        if plain_section and saw_bullet and line.strip():
            # A plain-text section has no closing '#'; the first prose line
            # after the bullets ends it.
            break

    return [
        Gate(gate_id=f"GATE-{index:02d}", description=_trim(text))
        for index, text in enumerate(items, start=1)
    ]


def _trim(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= MAX_DESCRIPTION else collapsed[: MAX_DESCRIPTION - 1] + "…"


def describe(gates: list[Gate]) -> str:
    if not gates:
        return "No acceptance criteria detected. Define gates yourself if you need more than the supplied-specification gate."
    return f"Detected {len(gates)} acceptance gate(s): " + ", ".join(g.gate_id for g in gates)
