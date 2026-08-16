"""Streaming ingestion: parsers, durable stream events, usage telemetry."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from synchri.runner import AgentCommand, parse_directives
from synchri.runner.managed import _looks_like_flag_rejection
from synchri.runner.stream_events import StreamRecorder, parser_for
from synchri.session.deadline import Deadline
from synchri.session.manager import SessionManager
from synchri.session.modes import ParticipantPlan, managed_command, stream_format_for
from synchri.session.spec import ProductSpec
from synchri.storage import dao


def _git(root, *args):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
        "PATH": __import__("os").environ.get("PATH", ""), "HOME": str(root),
    }
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


CLAUDE_LINES = [
    {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "abc"},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "Let me look at the failing file."}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "boom"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/app.py"}}]}},
    {"type": "system", "subtype": "compact_boundary",
     "compact_metadata": {"trigger": "auto", "pre_tokens": 12000}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Done. The fix is in.\nSYNCHRI-PASS"}]}},
    {"type": "result", "subtype": "success",
     "result": "Done. The fix is in.\nSYNCHRI-PASS",
     "usage": {"input_tokens": 120, "output_tokens": 45, "cache_read_input_tokens": 80},
     "total_cost_usd": 0.0123, "duration_ms": 5400, "num_turns": 3},
]

CODEX_LINES = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i1", "item_type": "reasoning",
                                        "text": "Plan the change."}},
    {"type": "item.started", "item": {"id": "i2", "item_type": "command_execution",
                                      "command": "cargo test"}},
    {"type": "item.completed", "item": {"id": "i2", "item_type": "command_execution",
                                        "command": "cargo test", "aggregated_output": "ok",
                                        "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i3", "item_type": "file_change",
                                        "changes": [{"path": "src/lib.rs", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "i4", "item_type": "assistant_message",
                                        "text": "All tests pass."}},
    {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 300,
                                         "output_tokens": 120}},
]

CODEX_LEGACY_LINES = [
    {"id": "0", "msg": {"type": "task_started"}},
    {"id": "1", "msg": {"type": "agent_reasoning", "text": "Consider the diff."}},
    {"id": "2", "msg": {"type": "exec_command_begin", "command": ["bash", "-lc", "ls"]}},
    {"id": "3", "msg": {"type": "exec_command_end", "exit_code": 0,
                        "aggregated_output": "README.md"}},
    {"id": "4", "msg": {"type": "patch_apply_begin",
                        "changes": {"docs/guide.md": {"kind": "update"}}}},
    {"id": "5", "msg": {"type": "agent_message", "message": "Guide updated."}},
    {"id": "6", "msg": {"type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 9}}}},
    {"id": "7", "msg": {"type": "task_complete", "last_agent_message": "Guide updated."}},
]


def _feed(parser, lines):
    events = []
    for line in lines:
        events.extend(parser.feed(json.dumps(line) if isinstance(line, dict) else line))
    return events


# ----------------------------------------------------------------------
# parsers
# ----------------------------------------------------------------------


def test_claude_stream_maps_to_the_normalized_vocabulary():
    parser = parser_for("claude")
    events = _feed(parser, CLAUDE_LINES)

    assert [event.kind for event in events] == [
        "status", "thinking", "command", "error", "file_edit", "compaction",
        "status", "usage", "result",
    ]
    assert events[0].title == "Started (claude-x)"
    assert events[2].title == "$ pytest -q"
    assert events[4].file_path == "src/app.py"
    assert "12,000" in events[5].title
    assert parser.tool_events == 2
    assert parser.model == "claude-x"
    assert parser.final_text() == "Done. The fix is in.\nSYNCHRI-PASS"
    assert parser.usage["input_tokens"] == 120
    assert parser.usage["cached_input_tokens"] == 80
    assert parser.usage["cost_usd"] == 0.0123
    assert parser.usage["duration_seconds"] == 5.4


def test_codex_stream_current_shape():
    parser = parser_for("codex")
    events = _feed(parser, CODEX_LINES)

    kinds = [event.kind for event in events]
    assert kinds == ["status", "status", "thinking", "command", "command", "file_edit",
                     "status", "usage", "result"]
    file_edits = [event for event in events if event.kind == "file_edit"]
    assert file_edits[0].file_path == "src/lib.rs"
    finished_command = [e for e in events if e.kind == "command"][-1]
    assert finished_command.payload["exit_code"] == 0
    assert parser.final_text() == "All tests pass."
    assert parser.usage["cached_input_tokens"] == 300


def test_codex_stream_legacy_shape():
    parser = parser_for("codex")
    events = _feed(parser, CODEX_LEGACY_LINES)

    kinds = [event.kind for event in events]
    assert "thinking" in kinds and "file_edit" in kinds and "usage" in kinds
    assert [e.file_path for e in events if e.kind == "file_edit"] == ["docs/guide.md"]
    assert parser.final_text() == "Guide updated."
    assert parser.usage["input_tokens"] == 50


def test_parsers_ignore_garbage_and_unknown_events():
    parser = parser_for("claude")
    assert parser.feed("not json at all") == []
    assert parser.feed("{broken json") == []
    assert parser.feed(json.dumps({"type": "mystery_event"})) == []
    assert parser.feed(json.dumps(["a", "list"])) == []
    assert parser.tool_events == 0
    assert parser.final_text() is None


def test_unknown_stream_format_has_no_parser():
    assert parser_for(None) is None
    assert parser_for("copilot") is None


# ----------------------------------------------------------------------
# invoke integration
# ----------------------------------------------------------------------


def _stream_agent(tmp_path, lines, *, stream_format="claude"):
    data = tmp_path / "stream.jsonl"
    data.write_text(
        "\n".join(json.dumps(line) if isinstance(line, dict) else line for line in lines) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "stream_agent.py"
    script.write_text(
        f"import sys\nsys.stdout.write(open({str(data)!r}).read())\n", encoding="utf-8"
    )
    agent = AgentCommand.parse(f"x={sys.executable} {script} {{prompt}}", timeout=30)
    agent.parser_factory = lambda: parser_for(stream_format)
    return agent


def test_invoke_reconstructs_the_final_reply_and_directives_still_parse(tmp_path):
    agent = _stream_agent(tmp_path, CLAUDE_LINES)
    result = agent.invoke("go")

    assert result.ok
    assert result.stdout == "Done. The fix is in.\nSYNCHRI-PASS"
    assert result.tool_events == 2
    body, directives = parse_directives(result.stdout)
    assert body == "Done. The fix is in."
    assert directives.passed is True


def test_invoke_falls_back_to_raw_stdout_when_nothing_streamed(tmp_path):
    agent = _stream_agent(tmp_path, ["A plain answer with no JSON at all."])
    result = agent.invoke("go")

    assert result.ok
    assert result.stdout.strip() == "A plain answer with no JSON at all."
    assert result.tool_events is None


def test_invoke_records_events_and_usage_durably(room, tmp_path, workspace):
    agent = _stream_agent(tmp_path, CLAUDE_LINES)
    agent.recorder = StreamRecorder(
        workspace, room_id=room.room_id, session_id="sess-1",
        participant="claude", runtime="claude_code",
    )
    result = agent.invoke("go")
    assert result.ok

    events = dao.list_stream_events(room.broker.conn, room.room_id)
    kinds = [event["kind"] for event in events]
    assert "thinking" in kinds and "file_edit" in kinds and "compaction" in kinds
    assert len({event["invoke_key"] for event in events}) == 1
    edit = next(event for event in events if event["kind"] == "file_edit")
    assert edit["file_path"] == "src/app.py"
    assert edit["participant"] == "claude"
    assert edit["session_id"] == "sess-1"

    usage = dao.usage_for_session(room.broker.conn, "sess-1")
    assert usage[0]["input_tokens"] == 120
    assert usage[0]["output_tokens"] == 45
    assert usage[0]["cost_usd"] == 0.0123
    assert usage[0]["runtime"] == "claude_code"


# ----------------------------------------------------------------------
# maintained commands and the plain fallback
# ----------------------------------------------------------------------


def test_maintained_commands_stream_and_have_plain_fallbacks():
    claude = ParticipantPlan("claude", "claude_code", "primary_builder")
    codex = ParticipantPlan("codex", "codex", "adversarial_reviewer")
    assert stream_format_for(claude) == "claude"
    assert stream_format_for(codex) == "codex"
    # A user-supplied command must never be parsed as a stream.
    custom = ParticipantPlan("x", "claude_code", "primary_builder", command="claude -p {prompt}")
    assert stream_format_for(custom) is None
    assert managed_command(custom) == "claude -p {prompt}"


def test_flag_rejection_detection_is_narrow():
    from synchri.runner.agent_command import AgentResult

    rejected = AgentResult("x", "", "error: unexpected argument '--json' found", 2)
    assert _looks_like_flag_rejection(rejected) is True
    ordinary_failure = AgentResult("x", "", "provider refused the request", 1)
    assert _looks_like_flag_rejection(ordinary_failure) is False
    success = AgentResult("x", "", "", 0)
    assert _looks_like_flag_rejection(success) is False
    timeout = AgentResult("x", "", "--json mentioned", 1, timed_out=True)
    assert _looks_like_flag_rejection(timeout) is False


# ----------------------------------------------------------------------
# fingerprint
# ----------------------------------------------------------------------


def test_fingerprint_ticks_on_new_stream_events(broker, repo):
    from synchri.ui.stream import Fingerprint

    manager = SessionManager(broker)
    record = manager.create(
        name="s",
        mode="long_horizon",
        repo_root=str(repo),
        participants=[
            ParticipantPlan("claude", "claude_code", "primary_builder"),
            ParticipantPlan("codex", "codex", "adversarial_reviewer"),
        ],
        spec=ProductSpec(text="Do the thing."),
        deadline=Deadline.from_duration("2 hours"),
    )
    before = Fingerprint.take(manager, record.session_id)
    dao.insert_stream_event(
        broker.conn, record.room_id, session_id=record.session_id,
        participant="claude", invoke_key="inv", kind="status", title="working",
    )
    after = Fingerprint.take(manager, record.session_id)
    assert after.stream > before.stream
    assert after != before


# ----------------------------------------------------------------------
# schema v5 reaches existing workspaces
# ----------------------------------------------------------------------


def test_schema_v5_tables_and_columns_exist(broker):
    conn = broker.conn
    participant_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(session_participants)")
    }
    assert {
        "runtime_status",
        "runtime_detail",
        "consecutive_failures",
        "runtime_updated_at",
    } <= participant_columns
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"agent_stream_events", "agent_turn_usage"} <= tables


# ----------------------------------------------------------------------
# stream events
# ----------------------------------------------------------------------


def test_stream_events_round_trip_in_order(room):
    conn = room.broker.conn
    for index in range(5):
        dao.insert_stream_event(
            conn,
            room.room_id,
            session_id="sess",
            participant="claude",
            invoke_key="inv-1",
            kind="status",
            title=f"step {index}",
            payload={"n": index},
        )
    events = dao.list_stream_events(conn, room.room_id)
    assert [event["title"] for event in events] == [f"step {i}" for i in range(5)]
    assert events[-1]["payload"] == {"n": 4}
    assert events[-1]["kind"] == "status"
    assert dao.max_stream_event_id(conn, room.room_id) == events[-1]["event_id"]


def test_stream_events_prune_keeps_the_newest(room):
    conn = room.broker.conn
    for index in range(6):
        dao.insert_stream_event(
            conn,
            room.room_id,
            session_id=None,
            participant="codex",
            invoke_key="inv-1",
            kind="status",
            title=f"step {index}",
        )
    removed = dao.prune_stream_events(conn, room.room_id, keep=2)
    assert removed == 4
    remaining = dao.list_stream_events(conn, room.room_id)
    assert [event["title"] for event in remaining] == ["step 4", "step 5"]
    # Pruning an already-small room removes nothing.
    assert dao.prune_stream_events(conn, room.room_id, keep=2) == 0


def test_stream_event_listing_caps_at_the_newest_limit(room):
    conn = room.broker.conn
    for index in range(10):
        dao.insert_stream_event(
            conn,
            room.room_id,
            session_id=None,
            participant="claude",
            invoke_key="inv-1",
            kind="status",
            title=f"step {index}",
        )
    events = dao.list_stream_events(conn, room.room_id, limit=3)
    assert [event["title"] for event in events] == ["step 7", "step 8", "step 9"]


# ----------------------------------------------------------------------
# usage telemetry
# ----------------------------------------------------------------------


def test_usage_totals_aggregate_per_participant(broker):
    conn = broker.conn
    dao.insert_turn_usage(
        conn, session_id="s1", room_id=None, participant="claude",
        runtime="claude_code", model="claude-x", input_tokens=100, output_tokens=20,
        cached_input_tokens=50, cost_usd=0.03, duration_seconds=12.5,
    )
    dao.insert_turn_usage(
        conn, session_id="s1", room_id=None, participant="claude",
        runtime="claude_code", model="claude-x", input_tokens=40, output_tokens=10,
        cost_usd=0.01, duration_seconds=2.5,
    )
    dao.insert_turn_usage(
        conn, session_id="s2", room_id=None, participant="codex", runtime="codex",
        input_tokens=1,
    )

    totals = dao.usage_for_session(conn, "s1")
    assert len(totals) == 1
    claude = totals[0]
    assert claude["participant"] == "claude"
    assert claude["invocations"] == 2
    assert claude["input_tokens"] == 140
    assert claude["output_tokens"] == 30
    assert claude["cached_input_tokens"] == 50
    assert round(claude["cost_usd"], 4) == 0.04
    assert claude["duration_seconds"] == 15.0

    assert dao.usage_for_session(conn, "s2")[0]["participant"] == "codex"
    assert dao.usage_for_session(conn, "missing") == []
