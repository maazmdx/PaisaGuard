import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

# Default database path in PaisaGuard workspace
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "reconciliation.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

def get_db_connection(db_path: str | Path = DEFAULT_DB_PATH, use_wal: bool = True) -> sqlite3.Connection:
    """
    Returns an SQLite connection configured for high-concurrency WAL mode.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    if use_wal:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    else:
        conn.execute("PRAGMA journal_mode=DELETE;")
        
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn

@contextmanager
def get_db_cursor(db_path: str | Path = DEFAULT_DB_PATH, use_wal: bool = True):
    conn = get_db_connection(db_path, use_wal)
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

def init_db(db_path: str | Path = DEFAULT_DB_PATH, schema_path: str | Path = SCHEMA_PATH) -> None:
    """
    Initializes the SQLite database with the PaisaGuard schema.
    """
    conn = get_db_connection(db_path, use_wal=True)
    with open(schema_path, "r") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized successfully at: {DEFAULT_DB_PATH}")
