"""Create/upgrade the SQLite database and seed the entities table."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import ENTITIES, DB_PATH
from db.connection import get_connection


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    conn = get_connection()
    try:
        conn.executescript(schema_sql)
        cur = conn.cursor()
        for e in ENTITIES:
            cur.execute(
                """INSERT INTO entities (entity_id, name, category, is_public, ticker, cik, data_tier, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     name=excluded.name, category=excluded.category, is_public=excluded.is_public,
                     ticker=excluded.ticker, cik=excluded.cik, data_tier=excluded.data_tier, notes=excluded.notes""",
                (e["entity_id"], e["name"], e["category"], int(e["is_public"]), e.get("ticker"),
                 e.get("cik"), e["data_tier"], e.get("notes")),
            )
        conn.commit()
        print(f"Database initialized at {DB_PATH}")
        print(f"Seeded/updated {len(ENTITIES)} entities.")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
