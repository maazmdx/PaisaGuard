"""
db.py — Database connection management & concurrency configuration for PaisaGuard.

Engine: SQLite in WAL (Write-Ahead-Logging) mode with synchronous=NORMAL.
Provides high-throughput concurrent reads and transactional writes.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(os.environ.get("PAISAGUARD_DB_PATH", str(BASE_DIR / "reconciliation.db")))
SCHEMA_PATH = BASE_DIR / "schema.sql"


def get_db_path() -> Path:
    """Returns the configured database path, resolving from environment if present."""
    return Path(os.environ.get("PAISAGUARD_DB_PATH", str(DEFAULT_DB_PATH)))


def get_db_connection(db_path: Optional[Union[str, Path]] = None, use_wal: bool = True) -> sqlite3.Connection:
    """
    Returns an SQLite connection configured for WAL mode and foreign key enforcement.
    """
    target_path = Path(db_path) if db_path else get_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target_path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    if use_wal:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    else:
        conn.execute("PRAGMA journal_mode=DELETE;")

    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_db_cursor(db_path: Optional[Union[str, Path]] = None, use_wal: bool = True, auto_commit: bool = True):
    """
    Context manager yielding a cursor with transaction boundary control.
    """
    conn = get_db_connection(db_path, use_wal)
    cursor = conn.cursor()
    try:
        yield cursor
        if auto_commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def log_audit_event(
    conn_or_path: Union[sqlite3.Connection, str, Path],
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Dict[str, Any],
) -> int:
    """
    Appends an immutable audit event to audit_events table.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload, sort_keys=True)

    if isinstance(conn_or_path, sqlite3.Connection):
        cursor = conn_or_path.execute(
            """
            INSERT INTO audit_events (event_type, aggregate_type, aggregate_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?);
        """,
            (event_type, aggregate_type, aggregate_id, payload_json, timestamp),
        )
        return cursor.lastrowid
    else:
        with get_db_cursor(conn_or_path) as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (event_type, aggregate_type, aggregate_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?);
            """,
                (event_type, aggregate_type, aggregate_id, payload_json, timestamp),
            )
            return cursor.lastrowid


def init_db(db_path: Optional[Union[str, Path]] = None, schema_path: Union[str, Path] = SCHEMA_PATH) -> None:
    """
    Initializes the SQLite database schema if not already present.
    """
    conn = get_db_connection(db_path, use_wal=True)
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized successfully at: {get_db_path()}")
