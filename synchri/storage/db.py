"""SQLite connection management and schema bootstrap.

Synchri has no daemon.  Mutual exclusion between concurrently running CLI
processes comes from SQLite itself: every state-changing operation runs inside
a ``BEGIN IMMEDIATE`` transaction, which takes the database write lock up front
rather than mid-transaction.  That makes "read the queue, decide, write the
queue" atomic across processes, which is exactly the guarantee a broker loop
would otherwise provide.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "6"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: How long a writer waits for a competing writer before giving up.
DEFAULT_BUSY_TIMEOUT_MS = 10_000
INITIALIZE_RETRIES = 8
INITIALIZE_RETRY_SECONDS = 0.05


class Connection(sqlite3.Connection):
    """A shared-connection-aware ``sqlite3.Connection``.

    The local app shares one connection across threads — the threaded UI
    server, conductors, recovery timers all reach the broker's connection
    with ``check_same_thread=False``. ``sqlite3`` serializes individual
    statements, but ``in_transaction`` is connection-global: without
    ownership, one thread's open transaction reads as "nested" to every
    other thread, which would then join it instead of waiting — and a bare
    statement from another thread would land inside it. The lock makes a
    read-decide-write block atomic across threads exactly the way ``BEGIN
    IMMEDIATE`` already makes it atomic across processes; ``_txn_owner``
    records which thread may genuinely nest.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._txn_lock = threading.RLock()
        self._txn_owner: int | None = None

    def execute(self, sql, parameters=(), /):
        with self._txn_lock:
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        with self._txn_lock:
            return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        with self._txn_lock:
            return super().executescript(sql)


def connect(db_path: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a connection with the pragmas Synchri relies on."""
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existed = db_path.exists()
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,  # explicit transaction control, see transaction()
        check_same_thread=False,
        factory=Connection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if not existed:
        try:
            os.chmod(db_path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema if it is not already present.

    ``executescript`` issues its own COMMIT, so it cannot run inside our
    transaction helper.  Every statement in the schema is ``IF NOT EXISTS``,
    which makes this idempotent and safe when two processes race to create the
    workspace — SQLite serializes the writers.
    """
    # Independent agent terminals may reach ``Broker()`` for the first time
    # at exactly the same moment. SQLite serializes that safely, but schema
    # bootstrap includes DDL whose lock upgrade can briefly fail before the
    # normal busy timeout applies. Retry the complete idempotent bootstrap so
    # "join the room" does not become an avoidable connection error.
    for attempt in range(INITIALIZE_RETRIES):
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            with transaction(conn):
                _apply_additive_migrations(conn)
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SCHEMA_VERSION,),
                )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == INITIALIZE_RETRIES - 1:
                raise
            time.sleep(INITIALIZE_RETRY_SECONDS * (attempt + 1))


#: Columns added after the first release.  The schema is only ever extended
#: additively, so a workspace created by an older version keeps working: every
#: new column is nullable and ``initialize`` adds whatever is missing.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "rooms": {
        "workspace_root": "TEXT",
        "repo_branch": "TEXT",
        "repo_head": "TEXT",
        "repo_remote": "TEXT",
        "memory_note": "TEXT",
    },
    # v5: per-agent runtime supervision state (NULL = never launched).
    # v6: durable join-assembly phases so the launch UI can show what each
    # agent is actually doing to join (launching -> injecting_bootstrap ->
    # awaiting_acknowledgment -> ready | failed), and retry only the failed
    # participant.
    "session_participants": {
        "runtime_status": "TEXT",
        "runtime_detail": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "runtime_updated_at": "TEXT",
        "join_phase": "TEXT",
        "join_detail": "TEXT",
        "join_updated_at": "TEXT",
        # v6: participant-scoped recovery — the provider's own resume id from
        # the agent's last completed invocation, and a generation counter that
        # increments on every recovery action (resume, replace, restart).
        "resume_id": "TEXT",
        "recovery_generation": "INTEGER NOT NULL DEFAULT 0",
    },
    # v6: the durable session phase (original_work -> appendix_evaluation ->
    # extension_work -> closing).
    "sessions": {
        "phase": "TEXT NOT NULL DEFAULT 'original_work'",
    },
    # v6: gate provenance — main-spec gates vs. extension gates materialized
    # from approved dropbox items.
    "session_gates": {
        "origin_kind": "TEXT NOT NULL DEFAULT 'main'",
        "drop_id": "TEXT",
        "extension_id": "TEXT",
    },
    # v6: ancillary usage reports separately from ordinary participants.
    "agent_turn_usage": {
        "origin_kind": "TEXT NOT NULL DEFAULT 'main'",
        "drop_id": "TEXT",
    },
}


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in existing:
                # Table and column names come from the constant above, never
                # from user input.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


@contextmanager
def _owned_transaction(conn: sqlite3.Connection, begin: str) -> Iterator[sqlite3.Connection]:
    lock = getattr(conn, "_txn_lock", None)
    if lock is None:
        # A bare ``sqlite3.connect`` connection (tests, one-off scripts):
        # single-threaded use, where the original nesting rule is exact.
        if conn.in_transaction:
            yield conn
            return
        conn.execute(begin)
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return
    if conn._txn_owner == threading.get_ident():
        # Nested use by the owning thread: the outermost block owns the
        # commit. Another thread's open transaction never counts as nested —
        # it means "wait for the lock", below.
        yield conn
        return
    with lock:
        conn._txn_owner = threading.get_ident()
        try:
            conn.execute(begin)
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn._txn_owner = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single ``BEGIN IMMEDIATE`` write transaction.

    Nested use by the same thread is a no-op: the outermost block owns the
    commit, so a broker operation composed of smaller helpers still commits
    exactly once. Across threads sharing one connection, transactions are
    serialized by the connection's lock — a concurrent request waits for the
    write transaction instead of silently joining it.
    """
    with _owned_transaction(conn, "BEGIN IMMEDIATE") as inner:
        yield inner


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a deferred (read) transaction for a consistent snapshot."""
    with _owned_transaction(conn, "BEGIN DEFERRED") as inner:
        yield inner
