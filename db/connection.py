"""Shared SQLite connection helper."""
import sqlite3
import sys
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def log_collector_start(collector_name: str) -> int:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO collector_runs (collector_name, status) VALUES (?, 'running')",
            (collector_name,),
        )
        return cur.lastrowid


def log_collector_end(run_id: int, status: str, rows_written: int = 0, error_message: str | None = None):
    with db_cursor() as cur:
        cur.execute(
            """UPDATE collector_runs
               SET finished_at = datetime('now'), status = ?, rows_written = ?, error_message = ?
               WHERE id = ?""",
            (status, rows_written, error_message, run_id),
        )
