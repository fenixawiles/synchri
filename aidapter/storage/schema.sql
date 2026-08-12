-- AIDapter orchestration state (layer 1 of 3).
--
-- This file is authoritative machine state only: rooms, identities, the queue,
-- turns, tasks, the transcript, and the audit log.  Shared semantic memory
-- lives in a per-room Markdown ledger, not here.
--
-- Ordering rule: rooms.seq is a per-room monotonic counter shared by messages
-- and events, so the two can be interleaved into one totally ordered history.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id                     TEXT PRIMARY KEY,
    name                        TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'paused', 'stopped')),
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    stopped_at                  TEXT,
    -- Join token is stored salted+hashed; the plaintext is shown exactly once.
    token_salt                  TEXT NOT NULL,
    token_hash                  TEXT NOT NULL,
    seq                         INTEGER NOT NULL DEFAULT 0,
    active_participant_id       TEXT,
    active_turn_id              TEXT,
    consecutive_agent_turns     INTEGER NOT NULL DEFAULT 0,
    max_consecutive_agent_turns INTEGER NOT NULL DEFAULT 8,
    awaiting_human              INTEGER NOT NULL DEFAULT 0,
    owner_participant_id        TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    room_id        TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('agent', 'human')),
    status         TEXT NOT NULL CHECK (status IN ('active', 'removed')),
    secret_salt    TEXT NOT NULL,
    secret_hash    TEXT NOT NULL,
    joined_at      TEXT NOT NULL,
    last_seen_at   TEXT,
    removed_at     TEXT,
    metadata       TEXT NOT NULL DEFAULT '{}'
);

-- One live identity per name per room.  Removed rows keep their name reserved,
-- which is deliberate: it stops a removed agent's name being silently reused.
CREATE UNIQUE INDEX IF NOT EXISTS participants_room_name
    ON participants(room_id, name);

CREATE TABLE IF NOT EXISTS messages (
    message_id            TEXT PRIMARY KEY,
    room_id               TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    seq                   INTEGER NOT NULL,
    timestamp             TEXT NOT NULL,
    sender                TEXT NOT NULL,
    sender_participant_id TEXT NOT NULL,
    target                TEXT,
    target_participant_id TEXT,
    message_type          TEXT NOT NULL,
    content               TEXT NOT NULL DEFAULT '',
    goal                  TEXT,
    task_id               TEXT,
    artifact_references   TEXT NOT NULL DEFAULT '[]',
    constraints           TEXT NOT NULL DEFAULT '[]',
    claim                 TEXT,
    evidence              TEXT,
    request_type          TEXT,
    response_status       TEXT,
    confidence            REAL,
    handoff_target        TEXT,
    in_reply_to           TEXT,
    turn_id               TEXT,
    was_targeted          INTEGER NOT NULL DEFAULT 0,
    human_override        INTEGER NOT NULL DEFAULT 0,
    resulted_in_handoff   INTEGER NOT NULL DEFAULT 0,
    metadata              TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS messages_room_seq ON messages(room_id, seq);
CREATE INDEX IF NOT EXISTS messages_room_target ON messages(room_id, target_participant_id);
CREATE INDEX IF NOT EXISTS messages_task ON messages(room_id, task_id);

CREATE TABLE IF NOT EXISTS queue_entries (
    entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id           TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    participant_id    TEXT NOT NULL REFERENCES participants(participant_id) ON DELETE CASCADE,
    priority          INTEGER NOT NULL,
    enqueue_seq       INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('waiting', 'active', 'done', 'cancelled')),
    blocking          INTEGER NOT NULL DEFAULT 0,
    reason            TEXT,
    task_id           TEXT,
    source_message_id TEXT,
    enqueued_at       TEXT NOT NULL,
    resolved_at       TEXT
);

-- A participant may hold at most one live (waiting or active) queue entry.
CREATE UNIQUE INDEX IF NOT EXISTS queue_live_participant
    ON queue_entries(room_id, participant_id)
    WHERE status IN ('waiting', 'active');

CREATE INDEX IF NOT EXISTS queue_room_order
    ON queue_entries(room_id, status, priority, enqueue_seq);

CREATE TABLE IF NOT EXISTS turns (
    turn_id             TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    participant_id      TEXT NOT NULL,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL,
    blocking            INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    task_id             TEXT,
    request_message_id  TEXT,
    response_message_id TEXT,
    handoff_target      TEXT
);

CREATE INDEX IF NOT EXISTS turns_room ON turns(room_id, started_at);

CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    status              TEXT NOT NULL,
    goal                TEXT,
    created_by          TEXT NOT NULL,
    assigned_to         TEXT,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    request_message_id  TEXT,
    response_message_id TEXT
);

CREATE INDEX IF NOT EXISTS tasks_room ON tasks(room_id, status);

-- Audit log for state transitions that are not themselves messages
-- (joins, removals, promotions, interrupts, pause/stop, memory writes).
CREATE TABLE IF NOT EXISTS events (
    event_id             TEXT PRIMARY KEY,
    room_id              TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    seq                  INTEGER NOT NULL,
    event_type           TEXT NOT NULL,
    actor_participant_id TEXT,
    actor_name           TEXT,
    payload              TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS events_room_seq ON events(room_id, seq);
CREATE INDEX IF NOT EXISTS events_room_type ON events(room_id, event_type);
