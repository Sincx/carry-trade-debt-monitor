"""
BOJ policy rate / meeting calendar / statement-tone collector (spec 3.1, 7.4).

Confirmed live against the real BOJ site (2026-09-10):
  - The decisions "index" page (/en/mopo/mpmdeci/index.htm) is just site
    navigation -- the actual policy rate is stated in the per-meeting
    "Statement on Monetary Policy" PDF, linked from the year index page
    (/en/mopo/mpmdeci/mpr_YYYY/index.htm) as e.g. "k260731a.pdf". These are
    PDF-only (no HTML mirror for recent meetings), so this collector
    downloads and parses that PDF's text (pypdf) rather than scraping HTML.
  - The meeting-schedule page (/en/mopo/mpmsche_minu/index.htm) has a table
    listing each MPM's two meeting days as e.g. "Mar. 18 (Wed.), 19
    (Thurs.)" -- the SECOND day is the decision date, which this extracts
    via a day-pair regex (single-day entries in other table columns don't
    match this shape, which is what distinguishes the MPM-date column from
    the rest of the flattened table text).

Statement tone classification remains an explicit best-effort heuristic
(7.4) -- keyword counting on the PDF's extracted text, not NLP -- and the
source PDF URL is always stored alongside the tone label.
"""
import re
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

BOJ_YEAR_INDEX_URL = "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{year}/index.htm"
BOJ_SCHEDULE_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) research contact sinclair.ma77@gmail.com"}

# Best-effort keyword lists (7.4) -- deliberately simple and auditable.
HAWKISH_KEYWORDS = [
    "raise the policy rate", "raised the policy rate", "increase the target",
    "will continue to raise", "upside risk to prices", "reduce monetary accommodation",
    "gradually adjust", "normalize", "voting against the action", "considered that",
]
DOVISH_KEYWORDS = [
    "maintain the current", "maintain accommodative", "continue with",
    "downside risk", "patiently continue", "additional easing",
    "will not hesitate to take additional easing", "accommodative financial conditions",
]

RATE_PATTERN = re.compile(r"around\s+(\d+\.\d+)\s*(?:to|[-–])?\s*(\d+\.\d+)?\s*percent", re.IGNORECASE)
STATEMENT_LINK_PATTERN = re.compile(r"k\d{6}a\.pdf$", re.IGNORECASE)
MEETING_DATE_HEADER_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s*(\d{4})",
    re.IGNORECASE,
)
DAY_PAIR_PATTERN = re.compile(
    r"([A-Z][a-z]+\.?)\s+\d{1,2}\s*\([A-Za-z]+\.?\)\s*,\s*(\d{1,2})\s*\([A-Za-z]+\.?\)"
)
MONTH_ABBREV = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def classify_tone(text: str):
    text_l = text.lower()
    hawk_hits = [k for k in HAWKISH_KEYWORDS if k in text_l]
    dove_hits = [k for k in DOVISH_KEYWORDS if k in text_l]
    if len(hawk_hits) > len(dove_hits):
        return "hawkish", hawk_hits
    if len(dove_hits) > len(hawk_hits):
        return "dovish", dove_hits
    return "neutral", (hawk_hits + dove_hits)


def collect_upcoming_meetings():
    run_id = log_collector_start("boj_schedule")
    rows_written = 0
    try:
        resp = get(BOJ_SCHEDULE_URL, headers=UA)
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)
        today = date.today()

        # The schedule page lists a full table per year (confirmed live: both
        # a 2026 and a 2027 table appear on one page) -- each day-pair match
        # must use the year of the table it actually falls under, not a
        # "roll forward if it looks past" guess (that heuristic collided
        # with the real next-year table and produced wrong dates). Table
        # year headings look like "2026 Table :" / "2027 Table :".
        year_headings = [(m.start(), int(m.group(1))) for m in re.finditer(r"\b(20\d{2})\s+Table\b", text)]

        def _year_for_position(pos: int) -> int:
            applicable = [y for start, y in year_headings if start <= pos]
            return applicable[-1] if applicable else today.year

        with db_cursor() as cur:
            for m in DAY_PAIR_PATTERN.finditer(text):
                month_key = re.sub(r"\.$", "", m.group(1)).lower()
                month = MONTH_ABBREV.get(month_key)
                if not month:
                    continue
                day = int(m.group(2))
                year = _year_for_position(m.start())
                try:
                    meeting_date = date(year, month, day)
                except ValueError:
                    continue
                if meeting_date <= today:
                    continue
                cur.execute(
                    """INSERT INTO boj_events (date, event_type, is_upcoming, source_url)
                       VALUES (?, 'scheduled_meeting', 1, ?)
                       ON CONFLICT(date, event_type) DO UPDATE SET source_url=excluded.source_url""",
                    (meeting_date.isoformat(), BOJ_SCHEDULE_URL),
                )
                rows_written += 1
        log_collector_end(run_id, "success" if rows_written else "partial", rows_written)
        return rows_written
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", rows_written, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", rows_written, f"{type(e).__name__}: {e}")
        raise


def _find_latest_statement_pdf_url(year: int) -> str | None:
    resp = get(BOJ_YEAR_INDEX_URL.format(year=year), headers=UA)
    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.find_all("a", href=True):
        if "statement on monetary policy" in a.get_text(strip=True).lower() and STATEMENT_LINK_PATTERN.search(a["href"]):
            href = a["href"]
            return href if href.startswith("http") else f"https://www.boj.or.jp{href}"
    return None


def collect_latest_decision():
    """Download and parse the latest 'Statement on Monetary Policy' PDF for
    the policy rate level and a best-effort tone classification."""
    run_id = log_collector_start("boj_rate_decision")
    try:
        from pypdf import PdfReader
        import io

        pdf_url = _find_latest_statement_pdf_url(date.today().year)
        if pdf_url is None:
            pdf_url = _find_latest_statement_pdf_url(date.today().year - 1)  # early-January edge case
        if pdf_url is None:
            raise ValueError("Could not find a 'Statement on Monetary Policy' PDF link on the BOJ year-index page.")

        pdf_resp = get(pdf_url, headers=UA)
        reader = PdfReader(io.BytesIO(pdf_resp.content))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)

        rate_match = RATE_PATTERN.search(text)
        if not rate_match:
            raise ValueError(f"Could not locate a policy rate figure in {pdf_url} -- statement wording may have changed.")
        rate_after = float(rate_match.group(2) or rate_match.group(1))

        tone, keywords = classify_tone(text)

        # PDF text extraction can insert stray whitespace inside numbers
        # (e.g. "202 6" for "2026", confirmed live) -- strip internal spaces
        # around the year before date-matching.
        date_match = MEETING_DATE_HEADER_PATTERN.search(re.sub(r"(\d{3})\s+(\d)\b", r"\1\2", text[:200]))
        event_date = date.today().isoformat()
        if date_match:
            month = MONTH_ABBREV.get(date_match.group(1)[:3].lower())
            if month:
                event_date = date(int(date_match.group(3)), month, int(date_match.group(2))).isoformat()

        with db_cursor() as cur:
            cur.execute("SELECT rate_after FROM boj_events WHERE rate_after IS NOT NULL ORDER BY date DESC LIMIT 1")
            prev = cur.fetchone()
            rate_before = prev["rate_after"] if prev else None

            cur.execute(
                """INSERT INTO boj_events (date, event_type, rate_before, rate_after, statement_tone, tone_keywords, is_upcoming, source_url)
                   VALUES (?, 'rate_decision', ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(date, event_type) DO UPDATE SET
                     rate_before=excluded.rate_before, rate_after=excluded.rate_after,
                     statement_tone=excluded.statement_tone, tone_keywords=excluded.tone_keywords,
                     source_url=excluded.source_url""",
                (event_date, rate_before, rate_after, tone, ",".join(keywords), pdf_url),
            )
        log_collector_end(run_id, "success", 1)
        return {"date": event_date, "rate_after": rate_after, "tone": tone, "source": pdf_url}
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", 0, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", 0, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    for label, fn in (("Schedule", collect_upcoming_meetings), ("Latest decision", collect_latest_decision)):
        try:
            result = fn()
            print(f"{label}: {result}")
        except Exception as e:
            print(f"{label} FAILED: {e}")
