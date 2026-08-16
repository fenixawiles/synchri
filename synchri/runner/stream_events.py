"""Streaming ingestion for managed agent CLIs.

Claude Code and Codex can emit machine-readable event streams
(``claude -p --output-format stream-json``, ``codex exec --json``). This
module turns those provider-specific streams into one small normalized event
vocabulary the UI can render live, records the events durably, and
reconstructs the agent's final reply so everything downstream — directive
parsing, gate updates, contract acknowledgment — behaves exactly as it does
for plain-stdout agents.

Both parsers are deliberately defensive: non-JSON lines are skipped, unknown
event types are ignored, and when a stream never yields a final text the
caller falls back to the raw captured stdout. A provider CLI drifting its
event schema degrades to today's behavior, never to a crash.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from ..ids import new_id
from ..storage import dao, db

TITLE_LIMIT = 160
DETAIL_LIMIT = 4000
COMMAND_OUTPUT_LIMIT = 1000
MAX_EVENTS_PER_INVOCATION = 2000
KEEP_EVENTS_PER_ROOM = 1000

#: Claude tool names that mean "the agent is changing this file".
_CLAUDE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_DIFF_PATH = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


@dataclass
class LiveEvent:
    """One normalized moment of live agent work."""

    kind: str  # status | thinking | tool | command | file_edit | compaction | usage | error | result
    title: str = ""
    detail: str = ""
    file_path: str | None = None
    payload: dict = field(default_factory=dict)


def _clip(text: str | None, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _first_line(text: str | None, limit: int = TITLE_LIMIT) -> str:
    value = (text or "").strip()
    return _clip(value.splitlines()[0] if value else "", limit)


def _usage_title(usage: dict) -> str:
    def fmt(count) -> str:
        count = int(count or 0)
        return f"{count / 1000:.1f}k" if count >= 1000 else str(count)

    title = f"Tokens: {fmt(usage.get('input_tokens'))} in · {fmt(usage.get('output_tokens'))} out"
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and cost:
        title += f" · ${cost:.2f}"
    return title


def _input_summary(inputs: dict) -> str:
    parts: list[str] = []
    for key, value in list(inputs.items())[:6]:
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {_first_line(value, 80)}")
        if len(parts) == 3:
            break
    return "; ".join(parts)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(texts)
    return ""


def _diff_paths(diff: str) -> list[str]:
    seen: list[str] = []
    for match in _DIFF_PATH.finditer(diff):
        path = match.group(1).strip()
        if path and path != "/dev/null" and path not in seen:
            seen.append(path)
    return seen


def _jsonable(payload: dict) -> dict:
    clean: dict = {}
    for key, value in (payload or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean


class BaseStreamParser:
    """Shared JSONL handling, counters, and final-text bookkeeping."""

    def __init__(self) -> None:
        #: How many tool/command/file events the stream carried. ``0`` on a
        #: completed turn is the low-signal marker for "did it actually work?".
        self.tool_events = 0
        self.model: str | None = None
        self.usage: dict | None = None
        #: True once at least one JSON object line was seen — the caller only
        #: trusts the parser's reconstruction when the CLI really streamed.
        self.saw_stream = False
        self._final: str | None = None
        self._texts: list[str] = []

    def feed(self, line: str) -> list[LiveEvent]:
        stripped = (line or "").strip()
        if not stripped.startswith("{"):
            return []
        try:
            data = json.loads(stripped)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        self.saw_stream = True
        try:
            events = [event for event in self._map(data) if event is not None]
        except Exception:  # defensive: one odd line must not end ingestion
            return []
        for event in events:
            if event.kind in {"tool", "command", "file_edit"}:
                self.tool_events += 1
        return events

    def _map(self, data: dict):  # pragma: no cover - overridden
        return []

    def final_text(self) -> str | None:
        if self._final and self._final.strip():
            return self._final
        joined = "\n\n".join(text for text in self._texts if text and text.strip()).strip()
        return joined or None


class ClaudeStreamParser(BaseStreamParser):
    """``claude -p --verbose --output-format stream-json`` (message-level JSONL)."""

    def _map(self, data: dict):
        kind = data.get("type")
        if kind == "system":
            subtype = str(data.get("subtype") or "")
            if subtype == "init":
                self.model = data.get("model") or self.model
                yield LiveEvent(
                    "status",
                    title=f"Started ({self.model})" if self.model else "Started",
                    payload={key: data.get(key) for key in ("model",) if data.get(key)},
                )
            elif "compact" in subtype:
                meta = data.get("compact_metadata") if isinstance(data.get("compact_metadata"), dict) else {}
                yield LiveEvent(
                    "compaction",
                    title=_compaction_title(meta),
                    payload=_jsonable(meta),
                )
            return
        if kind == "assistant":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "thinking":
                    text = block.get("thinking") or ""
                    if str(text).strip():
                        yield LiveEvent("thinking", title="Reasoning", detail=_clip(str(text), DETAIL_LIMIT))
                elif block_type == "text":
                    text = str(block.get("text") or "")
                    if text.strip():
                        self._texts.append(text)
                        yield LiveEvent("status", title=_first_line(text))
                elif block_type == "tool_use":
                    yield from self._tool_use(block)
            return
        if kind == "user":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("is_error")
                    ):
                        yield LiveEvent(
                            "error",
                            title="A tool call failed",
                            detail=_clip(_result_text(block.get("content")), DETAIL_LIMIT),
                        )
            return
        if kind == "result":
            text = data.get("result")
            if isinstance(text, str) and text.strip():
                self._final = text
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            duration_ms = data.get("duration_ms")
            self.usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cost_usd": data.get("total_cost_usd")
                if isinstance(data.get("total_cost_usd"), (int, float))
                else None,
                "duration_seconds": (duration_ms / 1000.0)
                if isinstance(duration_ms, (int, float))
                else None,
                "model": self.model,
            }
            yield LiveEvent(
                "usage", title=_usage_title(self.usage), payload=_jsonable(self.usage)
            )
            yield LiveEvent("result", title="Turn finished")
            return

    def _tool_use(self, block: dict):
        name = str(block.get("name") or "tool")
        inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
        if name in _CLAUDE_EDIT_TOOLS:
            path = inputs.get("file_path") or inputs.get("notebook_path")
            if isinstance(path, str) and path.strip():
                verb = "Write" if name == "Write" else "Edit"
                yield LiveEvent(
                    "file_edit",
                    title=f"{verb} {path}",
                    file_path=path,
                    payload={"tool": name},
                )
                return
        if name == "Bash":
            command = inputs.get("command")
            if isinstance(command, str) and command.strip():
                yield LiveEvent("command", title="$ " + _first_line(command), payload={"tool": name})
                return
        yield LiveEvent("tool", title=name, detail=_input_summary(inputs), payload={"tool": name})


def _compaction_title(meta: dict) -> str:
    pre = meta.get("pre_tokens")
    if isinstance(pre, (int, float)) and pre:
        return f"Context compacted ({int(pre):,} tokens folded into a summary)"
    return "Context compacted"


class CodexStreamParser(BaseStreamParser):
    """``codex exec --json`` — both the current ``item.*`` shape and the
    legacy ``{"id": …, "msg": {…}}`` shape."""

    def _map(self, data: dict):
        if isinstance(data.get("msg"), dict):
            yield from self._map_legacy(data["msg"])
            return
        kind = str(data.get("type") or "")
        if kind in {"thread.started", "turn.started"}:
            yield LiveEvent("status", title="Started")
            return
        if kind == "turn.completed":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self.usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                "cost_usd": None,
                "duration_seconds": None,
                "model": self.model,
            }
            yield LiveEvent(
                "usage", title=_usage_title(self.usage), payload=_jsonable(self.usage)
            )
            yield LiveEvent("result", title="Turn finished")
            return
        if kind == "turn.failed":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            yield LiveEvent(
                "error", title="Turn failed", detail=_clip(str(error.get("message") or ""), DETAIL_LIMIT)
            )
            return
        if kind == "error":
            yield LiveEvent(
                "error", title="Error", detail=_clip(str(data.get("message") or ""), DETAIL_LIMIT)
            )
            return
        if kind.startswith("item."):
            item = data.get("item")
            if isinstance(item, dict):
                yield from self._map_item(kind, item)
            return

    def _map_item(self, kind: str, item: dict):
        item_type = str(item.get("item_type") or item.get("type") or "")
        completed = kind == "item.completed"
        if item_type in {"assistant_message", "agent_message"}:
            if completed:
                text = str(item.get("text") or item.get("message") or "")
                if text.strip():
                    self._texts.append(text)
                    self._final = text
                    yield LiveEvent("status", title=_first_line(text))
            return
        if item_type == "reasoning":
            if completed:
                text = str(item.get("text") or "")
                if text.strip():
                    yield LiveEvent("thinking", title="Reasoning", detail=_clip(text, DETAIL_LIMIT))
            return
        if item_type == "command_execution":
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            command = str(command or "")
            if kind == "item.started" and command.strip():
                yield LiveEvent(
                    "command", title="$ " + _first_line(command), payload={"status": "running"}
                )
            elif completed:
                payload: dict = {"status": str(item.get("status") or "completed")}
                if item.get("exit_code") is not None:
                    payload["exit_code"] = item.get("exit_code")
                yield LiveEvent(
                    "command",
                    title="$ " + _first_line(command) if command.strip() else "Command finished",
                    detail=_clip(str(item.get("aggregated_output") or ""), COMMAND_OUTPUT_LIMIT),
                    payload=payload,
                )
            return
        if item_type == "file_change":
            if completed:
                for change in item.get("changes") or []:
                    if isinstance(change, dict) and change.get("path"):
                        yield LiveEvent(
                            "file_edit",
                            title=f"Edit {change['path']}",
                            file_path=str(change["path"]),
                            payload={"kind": str(change.get("kind") or "update")},
                        )
            return
        if item_type in {"mcp_tool_call", "web_search", "todo_list", "tool_call"}:
            if completed:
                title = str(item.get("tool") or item.get("query") or item_type)
                yield LiveEvent("tool", title=title, payload={"type": item_type})
            return

    def _map_legacy(self, msg: dict):
        kind = str(msg.get("type") or "")
        if kind == "task_started":
            yield LiveEvent("status", title="Started")
        elif kind == "agent_reasoning":
            text = str(msg.get("text") or "")
            if text.strip():
                yield LiveEvent("thinking", title="Reasoning", detail=_clip(text, DETAIL_LIMIT))
        elif kind == "agent_message":
            text = str(msg.get("message") or "")
            if text.strip():
                self._texts.append(text)
                self._final = text
                yield LiveEvent("status", title=_first_line(text))
        elif kind == "exec_command_begin":
            command = msg.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            if str(command or "").strip():
                yield LiveEvent(
                    "command", title="$ " + _first_line(str(command)), payload={"status": "running"}
                )
        elif kind == "exec_command_end":
            payload = {}
            if msg.get("exit_code") is not None:
                payload["exit_code"] = msg.get("exit_code")
            yield LiveEvent(
                "command",
                title="Command finished",
                detail=_clip(str(msg.get("aggregated_output") or msg.get("stdout") or ""), COMMAND_OUTPUT_LIMIT),
                payload=payload,
            )
        elif kind == "patch_apply_begin":
            changes = msg.get("changes")
            if isinstance(changes, dict):
                for path in changes:
                    yield LiveEvent("file_edit", title=f"Edit {path}", file_path=str(path))
        elif kind == "turn_diff":
            for path in _diff_paths(str(msg.get("unified_diff") or "")):
                yield LiveEvent("file_edit", title=f"Edit {path}", file_path=path)
        elif kind == "token_count":
            usage = _legacy_token_usage(msg)
            if usage:
                self.usage = usage
                yield LiveEvent("usage", title=_usage_title(usage), payload=_jsonable(usage))
        elif kind == "task_complete":
            text = str(msg.get("last_agent_message") or "")
            if text.strip():
                self._final = text
            yield LiveEvent("result", title="Turn finished")
        elif "compact" in kind:
            yield LiveEvent("compaction", title="Context compacted")
        elif kind == "error":
            yield LiveEvent(
                "error", title="Error", detail=_clip(str(msg.get("message") or ""), DETAIL_LIMIT)
            )


def _legacy_token_usage(msg: dict) -> dict | None:
    info = msg.get("info") if isinstance(msg.get("info"), dict) else msg
    totals = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else info
    if not isinstance(totals, dict):
        return None
    input_tokens = totals.get("input_tokens")
    output_tokens = totals.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cached_input_tokens": int(totals.get("cached_input_tokens") or 0),
        "cost_usd": None,
        "duration_seconds": None,
        "model": None,
    }


PARSERS: dict[str, type[BaseStreamParser]] = {
    "claude": ClaudeStreamParser,
    "codex": CodexStreamParser,
}


def parser_for(stream_format: str | None) -> BaseStreamParser | None:
    parser_class = PARSERS.get(stream_format or "")
    return parser_class() if parser_class else None


class StreamRecorder:
    """Durable sink for one participant's stream events.

    Owns its own SQLite connection — never the UI process's shared one — and
    writes each event as a single autocommit INSERT, atomic under WAL. Any
    storage failure silently disables the recorder for the invocation: live
    telemetry must never break the agent run it is describing.
    """

    def __init__(self, workspace, *, room_id: str, session_id: str | None, participant: str, runtime: str) -> None:
        self.workspace = workspace
        self.room_id = room_id
        self.session_id = session_id
        self.participant = participant
        self.runtime = runtime
        self._conn = None
        self._lock = threading.Lock()
        self._invoke_key = ""
        self._written = 0
        self._started = 0.0
        self._broken = False

    def _connection(self):
        if self._conn is None:
            self._conn = db.connect(self.workspace.db_path)
        return self._conn

    def begin(self) -> None:
        with self._lock:
            self._invoke_key = new_id("inv")
            self._written = 0
            self._started = time.monotonic()
            self._broken = False
            try:
                dao.prune_stream_events(self._connection(), self.room_id, keep=KEEP_EVENTS_PER_ROOM)
            except Exception:
                self._broken = True

    def write(self, event: LiveEvent) -> None:
        with self._lock:
            if self._broken or self._written > MAX_EVENTS_PER_INVOCATION:
                return
            self._written += 1
            over = self._written > MAX_EVENTS_PER_INVOCATION
            try:
                dao.insert_stream_event(
                    self._connection(),
                    self.room_id,
                    session_id=self.session_id,
                    participant=self.participant,
                    invoke_key=self._invoke_key,
                    kind="status" if over else event.kind,
                    title="Further live output was truncated" if over else _clip(event.title, TITLE_LIMIT),
                    detail="" if over else _clip(event.detail, DETAIL_LIMIT),
                    file_path=None if over else event.file_path,
                    payload={} if over else _jsonable(event.payload),
                )
            except Exception:
                self._broken = True

    def finish(self, parser, *, returncode=None, outcome: str = "ok") -> None:
        duration = (time.monotonic() - self._started) if self._started else None
        usage = getattr(parser, "usage", None) if parser is not None else None
        with self._lock:
            if usage and self.session_id:
                try:
                    dao.insert_turn_usage(
                        self._connection(),
                        session_id=self.session_id,
                        room_id=self.room_id,
                        participant=self.participant,
                        runtime=self.runtime,
                        model=usage.get("model"),
                        input_tokens=usage.get("input_tokens") or 0,
                        output_tokens=usage.get("output_tokens") or 0,
                        cached_input_tokens=usage.get("cached_input_tokens") or 0,
                        cost_usd=usage.get("cost_usd"),
                        duration_seconds=usage.get("duration_seconds") or duration,
                    )
                except Exception:
                    pass
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
