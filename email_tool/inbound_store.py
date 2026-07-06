"""
SQLite-backed idempotency / audit store for inbound messages.

Tiny, dependency-free (stdlib sqlite3). The DB path comes from the
INBOUND_DB_PATH env var (default /app/inbound.db, matching the container
working dir). The table is created on first use.

This holds transport bookkeeping only — no message bodies, no business data.
"""

import datetime
import os
import sqlite3
import threading

import state_store

_DEFAULT_DB_PATH = os.environ.get("INBOUND_DB_PATH", "/app/inbound.db")
_lock = threading.Lock()


def _db_path() -> str:
    # Re-read each call so tests / env changes are honoured.
    return os.environ.get("INBOUND_DB_PATH", _DEFAULT_DB_PATH)


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inbound_messages (
            gateway_message_id TEXT PRIMARY KEY,
            rfc822_message_id  TEXT UNIQUE,
            mailbox            TEXT,
            from_addr          TEXT,
            received_at        TEXT,
            gate_result        TEXT,
            status             TEXT,
            attempts           INTEGER DEFAULT 0,
            destination        TEXT,
            raw_ref            TEXT,
            created_at         TEXT
        )
        """
    )
    conn.commit()


def already_seen(rfc822_message_id: str) -> bool:
    """True if a message with this RFC822 Message-ID has already been recorded."""
    if state_store.enabled():
        return state_store.already_seen(rfc822_message_id)
    if not rfc822_message_id:
        return False
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM inbound_messages WHERE rfc822_message_id = ? LIMIT 1",
            (rfc822_message_id,),
        )
        return cur.fetchone() is not None


def record(
    gateway_message_id: str,
    rfc822_message_id: str,
    mailbox: str,
    from_addr: str,
    received_at: str,
    gate_result: str,
    status: str,
    attempts: int = 0,
    destination: str = None,
    raw_ref: str = None,
) -> None:
    """Insert (or upsert by gateway_message_id) a row for an inbound message."""
    if state_store.enabled():
        return state_store.record(
            gateway_message_id=gateway_message_id,
            rfc822_message_id=rfc822_message_id,
            mailbox=mailbox,
            from_addr=from_addr,
            received_at=received_at,
            gate_result=gate_result,
            status=status,
            attempts=attempts,
            destination=destination,
            raw_ref=raw_ref,
        )
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO inbound_messages (
                gateway_message_id, rfc822_message_id, mailbox, from_addr,
                received_at, gate_result, status, attempts, destination,
                raw_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gateway_message_id) DO UPDATE SET
                gate_result = excluded.gate_result,
                status      = excluded.status,
                attempts    = excluded.attempts,
                destination = excluded.destination
            """,
            (
                gateway_message_id,
                rfc822_message_id,
                mailbox,
                from_addr,
                received_at,
                gate_result,
                status,
                attempts,
                destination,
                raw_ref,
                _now(),
            ),
        )
        conn.commit()


def update_status(gateway_message_id: str, status: str, attempts: int = None) -> None:
    """Update the status (and optionally attempts) of an existing row."""
    if state_store.enabled():
        return state_store.update_status(gateway_message_id, status, attempts)
    with _lock, _connect() as conn:
        if attempts is None:
            conn.execute(
                "UPDATE inbound_messages SET status = ? WHERE gateway_message_id = ?",
                (status, gateway_message_id),
            )
        else:
            conn.execute(
                "UPDATE inbound_messages SET status = ?, attempts = ? "
                "WHERE gateway_message_id = ?",
                (status, attempts, gateway_message_id),
            )
        conn.commit()


def recent(limit: int = 20) -> list:
    """Return the most recently recorded rows (for diagnostics/dashboard)."""
    if state_store.enabled():
        return state_store.recent(limit)
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM inbound_messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
