"""
Shared DB connection helper -- local SQLite by default, or a remote Turso
(libSQL) database when TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are set.

Why: collectors run locally (mini PC, Task Scheduler) and always have; the
dashboard now also needs to run on Vercel, which has no access to a local
file and no persistent local disk between requests. Rather than maintain
two code paths, every caller in this codebase (collectors, analysis,
alerts, dashboard) uses the same sqlite3-shaped interface --
get_connection()/db_cursor() -- and this module decides underneath which
backend actually answers it. When Turso env vars are present, collectors
running on the mini PC write directly to the same remote database the
Vercel dashboard reads from; local db/monitor.db is unused in that mode.
Unset the env vars (or don't set them) to keep everything fully local, as
before.

The Turso path is a thin compatibility shim (_TursoConnection/_TursoCursor)
over libsql_client's HTTP transport -- NOT the websocket transport, which
failed the handshake in testing (see git history) -- that mimics just the
sqlite3 surface this codebase actually uses: execute/executescript,
fetchone/fetchall/iteration, .description, .lastrowid, .rowcount, and
dict-convertible rows (sqlite3.Row also supports dict(row); Turso rows are
converted to plain dicts directly, which is a strict superset of what's
needed here -- confirmed no code in this project relies on positional
integer row indexing or cursor.description's non-name fields).
"""
import os
import sys
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # fine on Vercel: env vars are injected directly, no .env file involved

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")


class _TursoCursor:
    def __init__(self, client):
        self._client = client
        self._rows = []
        self._idx = 0
        self.lastrowid = None
        self.rowcount = -1
        self.description = None

    def execute(self, sql, params=()):
        args = list(params) if params else None
        rs = self._client.execute(sql, args)
        self._rows = [dict(zip(rs.columns, row)) for row in rs]
        self._idx = 0
        self.lastrowid = rs.last_insert_rowid
        self.rowcount = rs.rows_affected
        self.description = [(c, None, None, None, None, None, None) for c in rs.columns]
        return self

    def executescript(self, sql: str):
        # Strip '--' line comments before splitting on ';' -- schema.sql has
        # a comment containing a literal semicolon ("-- 1 = ...; 0 = ..."),
        # which a naive split-on-';' truncates mid-statement (confirmed live:
        # cut a CREATE TABLE in half). Still not a general SQL parser --
        # doesn't handle semicolons inside string literals or triggers --
        # fine for this project's schema.sql, don't reuse for arbitrary SQL.
        no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
        for stmt in (s.strip() for s in no_comments.split(";")):
            if stmt:
                self._client.execute(stmt)
        return self

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows[self._idx:])


class _TursoConnection:
    def __init__(self, client):
        self._client = client

    def cursor(self):
        return _TursoCursor(self._client)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def commit(self):
        pass  # each statement commits immediately over Hrana-over-HTTP; no explicit transactions used here

    def close(self):
        self._client.close()


def _turso_client():
    import libsql_client
    # libsql:// selects the websocket transport, which failed the TLS/WS
    # handshake in testing on this network -- https:// forces the plain
    # HTTP (Hrana-over-HTTP) transport instead, confirmed working.
    url = TURSO_DATABASE_URL.replace("libsql://", "https://", 1)
    return libsql_client.create_client_sync(url=url, auth_token=TURSO_AUTH_TOKEN)


def get_connection():
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        return _TursoConnection(_turso_client())

    import sqlite3
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


def query_df(sql: str, params=()):
    """pandas.read_sql_query needs raw DBAPI2 cursor internals that the
    Turso shim doesn't (and shouldn't try to) fully replicate -- this builds
    the DataFrame manually from db_cursor() instead, so it works identically
    against either backend. Use this instead of pd.read_sql_query(sql, conn)
    anywhere a query result needs to become a DataFrame."""
    import pandas as pd
    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    return pd.DataFrame([dict(r) for r in rows], columns=columns)


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
