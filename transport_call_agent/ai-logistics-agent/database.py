"""SQLite connection. One small file, no server to install.

Locking rules used everywhere (see db_queries.write_tx):
  * a NEW connection per operation, always closed - never kept across an await or a network call
  * every write is ONE short transaction that takes the write lock up front (BEGIN IMMEDIATE)
  * a locked database is retried with a short back-off; a call never waits more than a few seconds per try
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv

from models import SCHEMA

load_dotenv()
DB_PATH = Path(os.getenv("DB_PATH", "logistics.db"))
# 30 s of silence on a phone call is worse than failing fast: wait a few seconds, retry, then let the agent hand over.
BUSY_TIMEOUT_MS = int(os.getenv("DB_BUSY_TIMEOUT_MS", "5000"))
WRITE_RETRIES = int(os.getenv("DB_WRITE_RETRIES", "3"))          # tries per write (each waits BUSY_TIMEOUT_MS)
RETRY_BASE_S = float(os.getenv("DB_RETRY_BASE_S", "0.25"))       # back-off: 0.25 s, 0.5 s, 1 s ...

logger = logging.getLogger("logistics-agent")


def get_conn() -> sqlite3.Connection:
    """A new connection per call: safe to use from worker threads.

    NOTE: journal_mode=WAL is deliberately NOT set here. It needs an exclusive lock and is persistent in the
    database file, so it is set once in init_db(). Running it on every connection caused 'database is locked'.
    """
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")   # writers wait for the lock instead of failing
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")                # safe with WAL, much faster commits
    return conn


def is_lock_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def write_tx(fn):
    """Run fn(conn) as ONE write transaction and return its result.

    * BEGIN IMMEDIATE takes the write lock first (waits via busy_timeout), so a read lock is never upgraded
      half-way - that upgrade is what fails instantly with 'database is locked'.
    * If the lock is still busy after BUSY_TIMEOUT_MS, the whole transaction is rolled back and retried
      (fn must only touch the database through `conn`; nothing has been written when it is retried).
    * Any other error is rolled back and raised unchanged.
    """
    for attempt in range(1, WRITE_RETRIES + 1):
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            _rollback(conn)
            if not is_lock_error(e) or attempt == WRITE_RETRIES:
                raise
            logger.warning(f"database locked (try {attempt}/{WRITE_RETRIES}), retrying: {e}")
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()
        time.sleep(RETRY_BASE_S * 2 ** (attempt - 1))


def _enable_wal(conn: sqlite3.Connection, tries: int = 5) -> str | None:
    """Switch to WAL once (it is stored in the .db file). Retries if another process holds the file.
    Returns the journal mode now in use (None if it could not be read)."""
    for i in range(tries):
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            logger.info(f"SQLite journal_mode={mode} ({DB_PATH.resolve()})")
            if str(mode).lower() != "wal":
                logger.warning(f"SQLite is NOT in WAL mode (got {mode!r}): readers will block writers. "
                               "Is the database on a network / synced / virtual folder?")
            return str(mode)
        except sqlite3.OperationalError as e:
            if not is_lock_error(e) or i == tries - 1:
                logger.warning(f"could not enable WAL ({e}); continuing with the current journal mode")
                return None
            time.sleep(0.5 * (i + 1))
    return None


def init_db() -> None:
    """Create tables. Runs in every worker process at start-up, so a busy database is retried, not fatal."""
    tries = WRITE_RETRIES + 2
    for i in range(tries):
        conn = get_conn()
        try:
            _enable_wal(conn)
            conn.executescript(SCHEMA)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if not is_lock_error(e) or i == tries - 1:
                raise
            logger.warning(f"init_db: database locked (try {i + 1}/{tries}), retrying: {e}")
        finally:
            conn.close()
        time.sleep(0.5 * (i + 1))


def db_health() -> dict:
    """Quick diagnosis: which file, which journal mode, can we take the write lock right now?"""
    conn = get_conn()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        try:
            conn.execute("BEGIN IMMEDIATE")
            writable = True
            conn.rollback()
        except sqlite3.OperationalError as e:
            writable = False
            logger.warning(f"write lock not available: {e}")
        return {"path": str(DB_PATH.resolve()), "journal_mode": mode, "writable_now": writable,
                "busy_timeout_ms": BUSY_TIMEOUT_MS, "write_retries": WRITE_RETRIES}
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready: {DB_PATH.resolve()}")
    print(db_health())