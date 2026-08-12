"""SQLite connection management and schema bootstrap.

AIDapter has no daemon.  Mutual exclusion between concurrently running CLI
processes comes from SQLite itself: every state-changing operation runs inside
a ``BEGIN IMMEDIATE`` transaction, which takes the database write lock up front
rather than mid-transaction.  That makes "read the queue, decide, write the
queue" atomic across processes, which is exactly the guarantee a broker loop
would otherwise provide.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "2"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: How long a writer waits for a competing writer before giving up.
DEFAULT_BUSY_TIMEOUT_MS = 10_000


def connect(db_path: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a connection with the pragmas AIDapter relies on."""
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existed = db_path.exists()
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,  # explicit transaction control, see transaction()
        check_same_thread=False,
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
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    with transaction(conn):
        _apply_additive_migrations(conn)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )


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
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single ``BEGIN IMMEDIATE`` write transaction.

    Nested use is a no-op: the outermost block owns the commit, so a broker
    operation composed of smaller helpers still commits exactly once.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a deferred (read) transaction for a consistent snapshot."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN DEFERRED")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
