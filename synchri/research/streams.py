"""Minimal parsing of the pinned CLIs' stdout streams (a secondary record).

The relay's capture is the primary record of what the model received and
returned (LOG-11). This parser only extracts what the CLI itself *reported*:
its final text, the model it says it used, its usage block, its session or
thread id, how many tool events it streamed, and Claude's ``is_error``.

It exists instead of reusing ``synchri.runner.stream_events`` because
importing anything under ``synchri.runner`` executes that package's
``__init__``, which loads the conductor, broker, session manager, Planning
Mode, and dropbox — code Research Mode must not load (PRO-01).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class StreamFacts:
    saw_stream: bool = False
    final_text: str | None = None
    model_reported: str | None = None
    usage: dict | None = None
    provider_session_id: str | None = None
    tool_events: int = 0
    is_error: bool | None = None
    event_types: list[str] = field(default_factory=list)


def _events(stdout: bytes):
    for line in stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_claude(stdout: bytes) -> StreamFacts:
    facts = StreamFacts()
    texts: list[str] = []
    for event in _events(stdout):
        facts.saw_stream = True
        kind = event.get("type")
        facts.event_types.append(str(kind))
        if event.get("session_id"):
            facts.provider_session_id = event["session_id"]
        if kind == "system" and event.get("subtype") == "init":
            facts.model_reported = event.get("model")
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    facts.tool_events += 1
        elif kind == "result":
            if isinstance(event.get("result"), str):
                facts.final_text = event["result"]
            facts.usage = event.get("usage")
            facts.is_error = bool(event.get("is_error"))
    if facts.final_text is None and texts:
        facts.final_text = "".join(texts)
    return facts


def parse_codex(stdout: bytes) -> StreamFacts:
    facts = StreamFacts()
    for event in _events(stdout):
        facts.saw_stream = True
        kind = str(event.get("type"))
        facts.event_types.append(kind)
        if kind == "thread.started":
            facts.provider_session_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                facts.final_text = item.get("text")
            elif item.get("type") not in ("error", "reasoning"):
                facts.tool_events += 1
        elif kind == "turn.completed":
            facts.usage = event.get("usage")
        elif kind == "turn.failed":
            facts.is_error = True
    return facts


PARSERS = {"claude_code": parse_claude, "codex": parse_codex}
