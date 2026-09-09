"""
Best-effort press feed for the private/thin-disclosure entities (neoclouds,
OpenAI, Anthropic) -- spec 3.4/4/7.5, "reported/unverified" tier.

No structured filings exist for these entities, so this is explicitly a
human-in-the-loop-friendly qualitative feed, not a clean financial-statement
table. Source: Google News RSS search (free, no key) per entity, filtered to
funding/debt/credit-facility keywords. Every row lands with confidence
'unverified' and a source link -- nothing here should be treated as
equivalent-quality data to the structured SEC-filer tables.

A manual entry point (add_manual_event) is also provided for curating
events the automated feed misses or garbles.
"""
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import ENTITIES, REPORTED_TIER_ENTITY_IDS
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
KEYWORDS = ["credit facility", "funding round", "debt", "raises", "billion", "bond", "term loan", "revenue run-rate"]


def _entity_name(entity_id: str) -> str:
    for e in ENTITIES:
        if e["entity_id"] == entity_id:
            return e["name"]
    return entity_id


def collect_entity_feed(entity_id: str) -> int:
    name = _entity_name(entity_id)
    query = quote(f'"{name}" ({" OR ".join(KEYWORDS)})')
    resp = get(GOOGLE_NEWS_RSS.format(query=query), headers={"User-Agent": "Mozilla/5.0"})
    root = ET.fromstring(resp.text)

    rows_written = 0
    with db_cursor() as cur:
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date_raw = item.findtext("pubDate") or ""
            source_el = item.find("source")
            source_name = source_el.text if source_el is not None else None

            if not title or not link:
                continue
            try:
                pub_date = datetime.strptime(pub_date_raw, "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
            except ValueError:
                pub_date = date.today().isoformat()

            cur.execute("SELECT id FROM reported_events WHERE source_url=?", (link,))
            if cur.fetchone():
                continue

            cur.execute(
                """INSERT INTO reported_events
                     (entity_id, event_date, event_type, headline, source_name, source_url, confidence)
                   VALUES (?, 'other', ?, ?, ?, ?, 'unverified')""",
                (entity_id, pub_date, title, source_name, link),
            )
            rows_written += 1
    return rows_written


def add_manual_event(entity_id: str, event_date: str, event_type: str, headline: str,
                       amount_usd: float | None = None, detail: str | None = None,
                       source_name: str | None = None, source_url: str | None = None):
    """Human-curated addition -- confidence is elevated to 'press_confirmed'
    since a person is vouching for the source, unlike the raw automated feed."""
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO reported_events
                 (entity_id, event_date, event_type, headline, amount_usd, detail, source_name, source_url, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'press_confirmed')""",
            (entity_id, event_date, event_type, headline, amount_usd, detail, source_name, source_url),
        )


def collect_all():
    run_id = log_collector_start("reported_events_feed")
    total = 0
    errors = []
    for entity_id in REPORTED_TIER_ENTITY_IDS:
        try:
            n = collect_entity_feed(entity_id)
            total += n
            print(f"  {entity_id}: {n} new items")
        except BlockedDomainError as e:
            errors.append(f"{entity_id}: {e}")
            print(f"  {entity_id}: BLOCKED -- {e}")
        except Exception as e:
            errors.append(f"{entity_id}: {type(e).__name__}: {e}")
            print(f"  {entity_id}: FAILED -- {e}")

    status = "success" if not errors else ("partial" if total else "failed")
    log_collector_end(run_id, status, total, "; ".join(errors) if errors else None)
    return total


if __name__ == "__main__":
    n = collect_all()
    print(f"Reported-events feed: wrote {n} new items total.")
