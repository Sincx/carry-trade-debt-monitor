"""
Public-company debt instrument collector via SEC EDGAR (3.3, 4, 7.3, 7.6).

Two layers, per the honest limitation documented in 7.3:
  1. Aggregate debt (reliable): LongTermDebtNoncurrent/Current via XBRL
     company-facts. Good for sanity-checking totals, not itemized terms.
  2. Itemized instruments (best-effort): text-pattern parse of the "Debt"
     note in the latest 10-K. Confirmed live against MSFT's and Amazon's
     FY2026/2025 10-Ks (2026-09-09): large filers commonly disclose debt as
     tranches grouped by issuance year, each row spanning a coupon range and
     maturity range (e.g. "2015 issuance of $23.8 billion, 2035-2055,
     3.50%-4.75%") rather than one row per individual bond/CUSIP -- and the
     exact wording/column layout differs by filer (MSFT: "issuance of $X
     billion"; Amazon: "Notes issuance of $X billion", always a range even
     for a single tranche). XBRL company-facts doesn't expose this at
     instrument granularity either, so this parses the plain text of the
     note (not the raw HTML table, which breaks across filers -- e.g.
     Amazon's heading is split by an inline XBRL tag) rather than a
     structured-data pull.

     For a range row this collector records the LOWEST stated coupon and
     the LATEST maturity year in the range (conservative for
     refinancing-wall visibility) and preserves the matched raw segment in
     `reference_rate` so nothing is silently lost -- every row also carries
     source_filing_url. Spot-check a new filer's output against its actual
     footnote before trusting exact figures (7.3).

Status transitions (7.6): an instrument already in the DB with a
maturity_date in the past is marked 'matured' on each run; instruments are
never deleted, only excluded from the "open" view via the status column.
"""
import re
import sys
import warnings
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import ENTITIES
from config.xbrl_tags import DEBT_AGGREGATE_CONCEPTS
from collectors.sec_edgar_common import fetch_company_facts, resolve_tag, latest_10k_filing, filing_document_url
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

DEBT_NOTE_HEADING = re.compile(
    r"NOTE\s+\d{1,2}[.:–—\s]{0,4}(?:[A-Za-z-]+\s+){0,3}DEBT\b"
    # Oracle-style: numbered note heading with no literal word "NOTE" at
    # all ("6. NOTES PAYABLE AND OTHER BORROWINGS") -- confirmed live,
    # 2026-09-10.
    r"|\d{1,2}\.\s+NOTES\s+PAYABLE(?:\s+AND\s+OTHER\s+BORROWINGS)?\b",
    re.IGNORECASE,
)
COUPON_PATTERN = re.compile(r"(\d{1,2}\.\d{2,3})\s*%")

# Three observed row-anchor styles (confirmed live, 2026-09-10):
#   MSFT/Amazon restate the tranche size inline ("2015 issuance of $23.8
#     billion ...").
#   Google/Meta label the row by year + note type only, with the size
#     appearing solely in the value columns ("2016 US dollar notes 2026
#     2.00% ... $2,000").
#   Oracle lists one row per individual bond, face value inline, with
#     separate issuance-date and current/prior-year amount columns ("$750,
#     3.125%, due July 2025 July 2013 $ -- N.A" -- see
#     parse_orcl_style_bonds).
# The first two share _parse_segments_with_anchor(); Oracle's is different
# enough (per-instrument, with its own matured/active signal) to need its
# own function. All three are tried in order -- whichever anchor actually
# finds rows in this filer's table wins.
ISSUANCE_PATTERN = re.compile(
    r"(\d{4})\s+(?:[A-Za-z]+\s+){0,2}?issuance of\s*[\$€£]?\s*([\d.,]+)\s*(billion|million)",
    re.IGNORECASE,
)
NOTES_LABEL_PATTERN = re.compile(
    r"(\d{4})\s+(?:[A-Za-z]{2,15}\s+){0,3}?[Nn]otes?\b(?!\s+due)",
)
# Oracle-style per-bond row: face value, coupon, and maturity all inline,
# followed by an issuance date and the current-period amount (either a real
# figure if still outstanding, or "N.A"/a dash if already matured/not yet
# issued as of the reporting date the column represents).
ORCL_BOND_PATTERN = re.compile(
    r"\$\s?([\d,]+)\s*,\s*(\d{1,2}\.\d{2,3})\s*%\s*,\s*due\s+(?:[A-Za-z]+\s+)?(\d{4})"
    r"(?:\s*\(\d+\))?"
    r"\s+[A-Za-z]+\s+\d{4}"
    r"\s+\$?\s*([\d,]+|\D{1,4}N\.?A\.?)",
    re.IGNORECASE,
)

# Phrases that typically introduce the actual tranche TABLE, as opposed to
# the narrative paragraph above it (which can itself contain "YYYY ... notes"
# mentions that would otherwise false-match NOTES_LABEL_PATTERN). Scoping to
# after this marker keeps the notes-label anchor precise.
TABLE_INTRO_MARKERS = ["summarized below", "summarizes our", "is as follows", "are as follows", "were as follows",
                        "consisted of the following", "consists of the following"]

DEBT_SECTION_END_MARKERS = ["Total face value", "Total long-term debt", "Total debt", "Other long-term debt"]
MAX_SECTION_CHARS = 20_000
MAX_SEGMENT_CHARS = 800


def fetch_filing_text(cik: str) -> tuple[str, str]:
    """Return (doc_url, plain_text) for the filer's latest 10-K. Plain text
    (not raw HTML) is the basis for extraction throughout this module --
    HTML table structure varies too much across filers and can be broken up
    by inline XBRL tags (confirmed on Amazon's filing), while a single
    get_text() pass gives a consistent, filer-agnostic string to pattern-match."""
    filing = latest_10k_filing(cik)
    if not filing:
        raise ValueError("No 10-K found in SEC submissions feed.")
    doc_url = filing_document_url(cik, filing["accessionNumber"], filing["primaryDocument"])
    resp = get(doc_url, headers={"User-Agent": "Carry Trade Debt Monitor sinclair.ma77@gmail.com"})
    soup = BeautifulSoup(resp.text, "lxml")
    return doc_url, soup.get_text(" ", strip=True)


def find_debt_section(text: str) -> str | None:
    """Locate the actual 'Debt' note body (as opposed to earlier
    cross-reference mentions of it in the MD&A) and return a bounded slice
    of text covering its tranche table. Returns None if this filer's note
    doesn't match the expected shape at all (page structure differs --
    caller logs this as a partial result, not a hard failure).

    Prefers starting right after a table-intro phrase ("...summarized below:")
    when one is found -- this skips the narrative paragraph that commonly
    precedes the table (e.g. Google's "During 2025, we issued $22.5 billion
    of ... notes...") which can itself contain false anchor-pattern matches."""
    for m in DEBT_NOTE_HEADING.finditer(text):
        window = text[m.end(): m.end() + 5000]
        if not (ISSUANCE_PATTERN.search(window) or NOTES_LABEL_PATTERN.search(window) or ORCL_BOND_PATTERN.search(window)):
            continue

        table_start = m.end()
        marker_positions = [text.find(marker, m.end(), m.end() + 5000) + len(marker)
                            for marker in TABLE_INTRO_MARKERS if text.find(marker, m.end(), m.end() + 5000) != -1]
        if marker_positions:
            table_start = min(marker_positions)  # earliest intro phrase, not first-in-list-order

        end = table_start + MAX_SECTION_CHARS
        for marker in DEBT_SECTION_END_MARKERS:
            idx = text.find(marker, table_start)
            if idx != -1:
                end = min(end, idx)  # stop BEFORE the subtotal row -- it must not be mistaken for a tranche
        return text[table_start:end]
    return None


def parse_tranche_segments(section_text: str) -> list[dict]:
    """Split the debt-note table text into tranche rows and extract a
    best-effort record from each. Tries the MSFT/Amazon-style anchor
    ("YYYY issuance of $X billion") first since it also yields a fallback
    principal (the issuance size) when the value columns can't be parsed;
    falls back to the label-only anchor ("YYYY US dollar notes", Google-
    style) if that finds nothing, where the value columns are the only
    source of principal."""
    # Require at least 3 matches before trusting an anchor style as "the
    # real table" -- a real tranche/bond table has many rows, so 1-2
    # matches are more likely a stray false positive elsewhere in the
    # section (confirmed live: NOTES_LABEL_PATTERN spuriously matched "2025
    # Notes" twice in Oracle's footnotes, which would otherwise have
    # short-circuited before ever trying the pattern that actually fits
    # Oracle's per-bond table).
    MIN_REAL_MATCHES = 3

    matches = list(ISSUANCE_PATTERN.finditer(section_text))
    if len(matches) >= MIN_REAL_MATCHES:
        return _parse_segments_with_anchor(section_text, matches, has_inline_size=True)

    matches = list(NOTES_LABEL_PATTERN.finditer(section_text))
    if len(matches) >= MIN_REAL_MATCHES:
        return _parse_segments_with_anchor(section_text, matches, has_inline_size=False)

    return parse_orcl_style_bonds(section_text)


def parse_orcl_style_bonds(section_text: str) -> list[dict]:
    """Parse Oracle-style per-instrument rows: '$750, 3.125%, due July 2025
    July 2013 $ -- N.A' (face, coupon, maturity, then issuance date, then
    the current-period amount). A non-numeric current-period amount ("N.A"
    or a bare dash) means the bond is no longer outstanding as of the
    reporting date this column represents -- skipped rather than inserted
    as an open instrument, consistent with 7.6's status-based scoping."""
    records = []
    for m in ORCL_BOND_PATTERN.finditer(section_text):
        principal_str, coupon_str, maturity_year, current_amount = m.groups()
        current_amount_clean = current_amount.replace(",", "").strip()
        if not re.match(r"^\d+$", current_amount_clean):
            continue  # "N.A" / dash -- not currently outstanding

        records.append({
            "coupon_rate": float(coupon_str),
            "maturity_year": int(maturity_year),
            "amount_candidates": [float(current_amount_clean) * 1_000_000],
            "raw_segment": m.group(0)[:300],
        })
    return records


def _parse_segments_with_anchor(section_text: str, matches: list, has_inline_size: bool) -> list[dict]:
    records = []
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(section_text), m.start() + MAX_SEGMENT_CHARS)
        segment = section_text[m.start():seg_end]
        issuance_year = m.group(1)

        issuance_size = None
        anchor_end_offset = m.end() - m.start()
        if has_inline_size:
            size_val = float(m.group(2).replace(",", ""))
            unit = m.group(3)
            issuance_size = size_val * (1_000_000_000 if unit.lower() == "billion" else 1_000_000)

        coupons = [float(c) for c in COUPON_PATTERN.findall(segment)]
        if not coupons:
            continue  # can't find a coupon in this segment -- skip rather than guess
        # 4+ figures => a [stated_low, stated_high, effective_low, effective_high]
        # range table (Amazon/Google-style, ranged when multi-tranche); 1-2
        # figures => single stated value optionally followed by a single
        # effective value (MSFT-style). Either way the stated rate comes first.
        coupon_rate = min(coupons[0], coupons[1]) if len(coupons) >= 4 else coupons[0]

        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", segment) if y != issuance_year]
        if not years:
            continue
        maturity_year = max(years)

        # Carrying-value figures in these tables are plain integer tokens
        # (comma-formatted when >=1,000), distinct from coupon percentages
        # (always X.XX with a trailing %) and maturity years (bare 4-digit).
        # Search only AFTER the anchor match itself ("$ 23.8 billion" in an
        # inline-size anchor would otherwise false-match its "23") to
        # isolate the carrying-value column(s). There are normally two --
        # one per fiscal year-end shown -- but which one is "most recent"
        # varies by filer (confirmed: MSFT lists the newer year first,
        # Amazon/Google list it last), so both are kept here and the caller
        # picks a column consistently per entity, calibrated against the
        # known XBRL total.
        remainder = segment[anchor_end_offset:]
        candidates = re.findall(r"(?<![\d.])[\d,]{2,}(?![\d.%])", remainder)
        amount_tokens = [float(c.replace(",", "")) * 1_000_000
                          for c in candidates if not re.match(r"^(19|20)\d{2}$", c)]

        if not amount_tokens and issuance_size is None:
            continue  # no principal figure of any kind found -- skip rather than guess

        records.append({
            "coupon_rate": coupon_rate,
            "maturity_year": maturity_year,
            "amount_candidates": amount_tokens or [issuance_size],
            "raw_segment": segment[:300],
        })
    return records


def pick_principal_column(tranches: list[dict], target_total: float | None) -> list[float]:
    """Each tranche may carry 2+ candidate dollar figures (one per reported
    fiscal year-end, order varies by filer). Pick a single column index
    (first vs. last) for ALL tranches at once -- whichever produces a total
    closer to the known XBRL aggregate -- rather than guessing per row."""
    first_total = sum(t["amount_candidates"][0] for t in tranches)
    last_total = sum(t["amount_candidates"][-1] for t in tranches)
    if target_total is None:
        use_index = 0  # no calibration reference available -- default to "first"
    else:
        use_index = 0 if abs(first_total - target_total) <= abs(last_total - target_total) else -1
    return [t["amount_candidates"][use_index] for t in tranches]


def update_aggregate_debt_note(entity_id: str, facts: dict) -> dict:
    """Resolve the XBRL aggregate long-term debt figures AND persist them to
    entity_debt_aggregate. This is the only place that snapshot is written,
    so downstream consumers (entity_financial_summary, the dashboard) can
    show a real debt figure for an entity whose itemized instrument
    extraction is empty (best-effort, per 7.3) without confusing that with
    an entity that genuinely carries no debt (e.g. Palantir)."""
    tag_nc, values_nc = resolve_tag(facts, DEBT_AGGREGATE_CONCEPTS["long_term_debt_noncurrent"])
    tag_c, values_c = resolve_tag(facts, DEBT_AGGREGATE_CONCEPTS["long_term_debt_current"])
    latest_nc = max(values_nc, key=lambda v: v.get("end", ""), default=None) if values_nc else None
    latest_c = max(values_c, key=lambda v: v.get("end", ""), default=None) if values_c else None
    result = {
        "resolved_tag_noncurrent": tag_nc,
        "latest_noncurrent": latest_nc.get("val") if latest_nc else None,
        "resolved_tag_current": tag_c,
        "latest_current": latest_c.get("val") if latest_c else None,
        "as_of": (latest_nc or {}).get("end"),
    }

    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO entity_debt_aggregate
                 (entity_id, noncurrent, current, resolved_tag_noncurrent, resolved_tag_current, as_of)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_id) DO UPDATE SET
                 noncurrent=excluded.noncurrent, current=excluded.current,
                 resolved_tag_noncurrent=excluded.resolved_tag_noncurrent,
                 resolved_tag_current=excluded.resolved_tag_current,
                 as_of=excluded.as_of, collected_at=datetime('now')""",
            (entity_id, result["latest_noncurrent"], result["latest_current"],
             tag_nc, tag_c, result["as_of"]),
        )

    return result


def latest_10k_aggregate_total(facts: dict) -> float | None:
    """Aggregate long-term debt (noncurrent + current) as of the most recent
    10-K specifically -- not the globally latest XBRL fact, which may come
    from a newer 10-Q and therefore not match the period the itemized
    tranche table (also pulled from the latest 10-K) actually covers. Used
    only to calibrate pick_principal_column(), not for display."""
    _, values_nc = resolve_tag(facts, DEBT_AGGREGATE_CONCEPTS["long_term_debt_noncurrent"])
    _, values_c = resolve_tag(facts, DEBT_AGGREGATE_CONCEPTS["long_term_debt_current"])
    nc_10k = [v for v in values_nc if v.get("form") == "10-K"]
    c_10k = [v for v in values_c if v.get("form") == "10-K"]
    latest_nc = max(nc_10k, key=lambda v: v.get("end", ""), default=None)
    latest_c = max(c_10k, key=lambda v: v.get("end", ""), default=None)
    if latest_nc is None:
        return None
    return latest_nc.get("val", 0) + (latest_c.get("val", 0) if latest_c else 0)


def extract_itemized_bonds(entity_id: str, cik: str, facts: dict | None = None) -> int:
    doc_url, text = fetch_filing_text(cik)
    section = find_debt_section(text)
    if section is None:
        raise ValueError("Could not locate a parseable 'Debt' note in the latest 10-K -- page/note structure differs for this filer (7.3).")
    tranches = parse_tranche_segments(section)

    target_total = latest_10k_aggregate_total(facts) if facts else None
    principals = pick_principal_column(tranches, target_total)

    rows_written = 0
    with db_cursor() as cur:
        cur.execute(
            "SELECT yield FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=1 ORDER BY timestamp DESC LIMIT 1"
        )
        latest_30y_row = cur.fetchone()
        latest_30y_yield = latest_30y_row["yield"] if latest_30y_row else None

        for t, principal in zip(tranches, principals):
            maturity_date = f"{t['maturity_year']}-12-31"  # exact month/day not disclosed at this granularity

            cur.execute(
                """SELECT instrument_id FROM debt_instruments
                   WHERE entity_id=? AND coupon_rate=? AND maturity_date=? AND ABS(principal - ?) < 1""",
                (entity_id, t["coupon_rate"], maturity_date, principal),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE debt_instruments SET last_verified_date=?, source_filing_url=? WHERE instrument_id=?",
                    (date.today().isoformat(), doc_url, existing["instrument_id"]),
                )
                continue

            cur.execute(
                """INSERT INTO debt_instruments
                     (entity_id, instrument_type, principal, currency, coupon_rate, coupon_type,
                      reference_rate, maturity_date, status, yield_at_issuance_30y, source_filing_url, last_verified_date)
                   VALUES (?, 'note', ?, 'USD', ?, 'fixed', ?, ?, 'open', ?, ?, ?)""",
                (entity_id, principal, t["coupon_rate"], t["raw_segment"], maturity_date,
                 latest_30y_yield, doc_url, date.today().isoformat()),
            )
            rows_written += 1

    return rows_written


def mark_matured_instruments() -> int:
    """Per 7.6: flip status to 'matured' for anything past its maturity date; never delete."""
    today = date.today().isoformat()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE debt_instruments SET status='matured' WHERE status='open' AND maturity_date IS NOT NULL AND maturity_date < ?",
            (today,),
        )
        return cur.rowcount


def collect_all():
    run_id = log_collector_start("sec_edgar_debt")
    total = 0
    errors = []
    for entity in ENTITIES:
        if entity["data_tier"] != "structured" or not entity.get("cik"):
            continue
        entity_id = entity["entity_id"]
        try:
            facts = fetch_company_facts(entity["cik"])
            agg = update_aggregate_debt_note(entity_id, facts)
            print(f"  {entity_id} aggregate debt: tag={agg['resolved_tag_noncurrent']} "
                  f"noncurrent={agg['latest_noncurrent']} as_of={agg['as_of']}")

            # If aggregate long-term debt is negligible, there's genuinely
            # nothing to itemize -- confirmed for Palantir (aggregate $0;
            # its 10-K explicitly states no borrowings are outstanding under
            # its credit facility). Skip rather than log a parse "failure"
            # for a filer that just doesn't carry debt.
            aggregate_total = (agg["latest_noncurrent"] or 0) + (agg["latest_current"] or 0)
            if aggregate_total < 10_000_000:
                print(f"  {entity_id}: aggregate debt negligible (${aggregate_total:,.0f}) -- skipping itemized extraction")
                continue

            n = extract_itemized_bonds(entity_id, entity["cik"], facts=facts)
            total += n
            print(f"  {entity_id}: {n} new/updated itemized instruments")
        except BlockedDomainError as e:
            errors.append(f"{entity_id}: {e}")
            print(f"  {entity_id}: BLOCKED -- {e}")
        except Exception as e:
            errors.append(f"{entity_id}: {type(e).__name__}: {e}")
            print(f"  {entity_id}: FAILED -- {e}")

    matured = mark_matured_instruments()
    print(f"Marked {matured} instruments matured.")

    status = "success" if not errors else ("partial" if total else "failed")
    log_collector_end(run_id, status, total, "; ".join(errors) if errors else None)
    return total


if __name__ == "__main__":
    n = collect_all()
    print(f"Debt collector: wrote/updated {n} itemized instruments total.")
