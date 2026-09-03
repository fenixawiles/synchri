"""The historian: grounded reports over the deliberative record.

A historian run is a one-shot, read-only invocation of the user's own
installed agent CLI (the ancillary-scout pattern), handed a question, the
derived timeline, and a bounded evidence set of exact excerpts. Its output
is the one place the ``synthesis`` provenance layer is ever produced — and
it is admitted only under a fail-closed grounding contract:

* every claim in the report's evidenced sections must cite ``[En]`` markers
  from the provided set; a reply that cites nothing, or cites evidence that
  was not provided, is refused and the caller falls back to the mechanical
  timeline-plus-evidence answer;
* causal language ("because", "caused", "led to", "resolved by") is allowed
  only where a cited excerpt states the relationship explicitly — sequence
  and cross-excerpt reconstruction belong under SYNTHESIS, and what the
  record cannot establish belongs under UNRESOLVED, never inferred from
  temporal proximity or from the final code;
* an honest inability to answer ("INSUFFICIENT: …") is a valid, accepted
  report — preferable to a plausible story.

The invocation runs under the maintained enforced read-only command
(``planning_command``) in a disposable temp directory: the evidence travels
in the prompt, so there is nothing for the historian to read or write.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from typing import TYPE_CHECKING, Callable

from ..session.modes import (
    ParticipantPlan,
    plan_launch_status,
    planning_command,
    planning_workspace_supported,
    stream_format_for,
)
from . import doctor as doctor_module
from .agent_command import AgentCommand
from .ancillary import _UsageRecorder, _sections
from .stream_events import parser_for

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from ..broker import Broker
    from ..session.manager import SessionManager, SessionRecord

HISTORIAN_TIMEOUT_SECONDS = 300.0

#: The report's required shape (spec §8). SYNTHESIS and UNRESOLVED are the
#: registers the grounding rules push reconstruction and gaps into.
SECTIONS = (
    "SUMMARY", "TRIGGER", "POSITIONS", "EVIDENCE",
    "RESOLUTION", "SYNTHESIS", "UNRESOLVED",
)
#: Sections whose claims must carry citations for the report to stand.
_GROUNDED_SECTIONS = ("SUMMARY", "TRIGGER", "POSITIONS", "EVIDENCE", "RESOLUTION")

_CITATION = re.compile(r"\[E(\d+)\]")


def _pick_runtime(broker: "Broker") -> str | None:
    """The first connected runtime with an enforced read-only launch."""
    try:
        connections = doctor_module.stored_connections(broker.conn)
    except Exception:  # pragma: no cover - defensive
        connections = {}
    for runtime in ("claude_code", "codex"):
        if not planning_workspace_supported(runtime):
            continue
        if connections.get(runtime, {}).get("state") != "connected":
            continue
        plan = ParticipantPlan("historian", runtime, "participant")
        if plan_launch_status(plan).get("ready"):
            return runtime
    return None


def build_prompt(record: "SessionRecord", question: str, retrieval: dict) -> str:
    lines = [
        "You are the historian for a Synchri development session. Answer ONE",
        "question about what actually happened, using ONLY the evidence below —",
        "never your own knowledge of similar code, and never a plausible story.",
        "",
        "THE QUESTION",
        question,
        "",
        "THE SESSION",
        f"{record.name} · {record.mode} · {record.status}",
        "",
    ]
    events = retrieval.get("events") or []
    if events:
        lines.append("THE DELIBERATIVE TIMELINE (a derived index over the same record)")
        for event in events[:40]:
            lines.append(
                f"- [{event['kind']} · {event.get('actor') or 'synchri'} · "
                f"{(event.get('at') or '')[:19]}] {event['summary']}"
            )
        lines.append("")
    lines.append("THE EVIDENCE (exact excerpts from the durable record)")
    for index, item in enumerate(retrieval.get("evidence") or [], start=1):
        origin = " · ".join(
            str(part) for part in (
                item.get("kind"), item.get("layer"), item.get("actor"),
                (item.get("at") or "")[:19],
            ) if part
        )
        lines.append(f'[E{index}] ({origin}) "{item.get("excerpt")}"')
    lines += [
        "",
        "RULES",
        "- Ground every claim: cite [En] markers inline in SUMMARY, TRIGGER,",
        "  POSITIONS, EVIDENCE, and RESOLUTION. An uncited report is refused.",
        '- Causal language — "because", "caused", "led to", "resolved by" — is',
        "  allowed only where a single cited excerpt states that relationship",
        "  explicitly. Where you connect excerpts, or read sequence as influence,",
        "  put the reconstruction under SYNTHESIS and say it is reconstruction.",
        "- Never infer rationale from temporal proximity, and never from what",
        "  the final code looks like.",
        "- What the record does not establish goes under UNRESOLVED, plainly:",
        '  e.g. "the record does not contain enough evidence to determine which',
        '  objection was decisive."',
        "- If the evidence cannot answer the question at all, print exactly one",
        "  line: INSUFFICIENT: <what the record lacks> — and nothing else.",
        "- Print only the report. No preamble, no tool use, no questions.",
        "",
        "FORMAT — print exactly these labeled sections",
        "SUMMARY: 2-5 plain-English sentences, cited",
        "TRIGGER: what started this, cited — or: not established",
        "POSITIONS: who proposed or objected to what, cited",
        "EVIDENCE: the decisive artifacts (tests, objections, dispositions), cited",
        "RESOLUTION: how it settled, cited — or: not established",
        "SYNTHESIS: reconstruction across excerpts, clearly labeled — or: none",
        "UNRESOLVED: what the record does not establish — or: none",
    ]
    return "\n".join(lines)


def _parse(text: str | None, evidence_count: int) -> dict | None:
    """Accept a grounded report or an honest insufficiency; refuse the rest."""
    body = (text or "").strip()
    if not body:
        return None
    if body.upper().startswith("INSUFFICIENT:"):
        return {
            "insufficient": True,
            "sections": {"SUMMARY": body[len("INSUFFICIENT:"):].strip()},
            "citations": [],
        }
    sections = _sections(body, SECTIONS)
    if not sections.get("SUMMARY"):
        return None
    grounded = " ".join(sections.get(key, "") for key in _GROUNDED_SECTIONS)
    grounded_citations = {int(number) for number in _CITATION.findall(grounded)}
    if not grounded_citations:
        return None
    cited = sorted({int(number) for number in _CITATION.findall(body)})
    if any(number < 1 or number > evidence_count for number in cited):
        return None
    return {"insufficient": False, "sections": sections, "citations": cited}


def _invoke(workspace, record: "SessionRecord", runtime: str, prompt: str,
            timeout: float) -> tuple[str | None, str | None]:
    plan = ParticipantPlan("historian", runtime, "participant")
    command = planning_command(plan)
    if not command:
        return None, f"{runtime} has no enforced read-only command"
    scratch = tempfile.mkdtemp(prefix="synchri-historian-")
    try:
        agent = AgentCommand.parse(f"historian={command}", timeout=timeout, cwd=scratch)
        stream_format = stream_format_for(plan)
        if stream_format:
            agent.parser_factory = lambda fmt=stream_format: parser_for(fmt)
            agent.recorder = _UsageRecorder(
                workspace,
                session_id=record.session_id,
                room_id=record.room_id,
                participant="historian",
                runtime=runtime,
                drop_id=None,
                origin_kind="historian",
            )
        result = agent.invoke(prompt)
        if not result.ok:
            if result.timed_out:
                return None, "the invocation timed out"
            detail = (result.stderr or "").strip()[:200]
            return None, detail or f"exit {result.returncode}"
        return result.stdout, None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def report(
    broker: "Broker",
    manager: "SessionManager",
    record: "SessionRecord",
    question: str,
    retrieval: dict,
    *,
    runner: Callable[[str], str] | None = None,
    timeout: float = HISTORIAN_TIMEOUT_SECONDS,
) -> dict:
    """Produce a grounded report — or an honest, identically shaped fallback.

    ``runner`` overrides the provider invocation (tests script it); the
    default path picks a connected read-only runtime and invokes it once.
    The mechanical evidence-and-timeline answer is always the floor: no
    runtime, a failed invocation, and a refused reply all degrade to it
    with the reason named.
    """

    def fallback(reason: str, runtime: str | None = None) -> dict:
        return {"report": None, "fallback": True, "fallback_reason": reason,
                "runtime": runtime}

    evidence = retrieval.get("evidence") or []
    if not evidence:
        return fallback("nothing in the recorded history matched the question")
    prompt = build_prompt(record, question, retrieval)
    if runner is not None:
        runtime: str | None = "scripted"
        text = runner(prompt)
    else:
        runtime = _pick_runtime(broker)
        if runtime is None:
            return fallback(
                "no connected agent offers an enforced read-only launch; "
                "showing the evidence and timeline instead"
            )
        text, error = _invoke(broker.workspace, record, runtime, prompt, timeout)
        if text is None:
            return fallback(f"the historian invocation failed: {error}", runtime)
    parsed = _parse(text, evidence_count=len(evidence))
    if parsed is None:
        return fallback(
            "the reply was not grounded in the provided evidence and was refused",
            runtime,
        )
    return {
        "report": {**parsed, "layer": "synthesis", "runtime": runtime},
        "fallback": False,
        "fallback_reason": None,
        "runtime": runtime,
    }
