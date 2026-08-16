"""Durable stream-event storage and per-invocation usage telemetry."""

from __future__ import annotations

from synchri.storage import dao


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
