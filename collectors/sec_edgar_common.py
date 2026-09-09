"""Shared SEC EDGAR helpers (company facts fetch, tag resolution)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import SEC_USER_AGENT
from collectors.http_utils import get

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def fetch_company_facts(cik: str) -> dict:
    resp = get(COMPANY_FACTS_URL.format(cik=cik), headers={"User-Agent": SEC_USER_AGENT})
    return resp.json()


def fetch_submissions(cik: str) -> dict:
    resp = get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": SEC_USER_AGENT})
    return resp.json()


def resolve_tag(facts: dict, candidate_tags: list[str]) -> tuple[str | None, list]:
    """Return (resolved_tag_name, list_of_USD_unit_facts) for whichever
    candidate tag has the most recent data in this filer's us-gaap facts.

    Picking the first tag merely *present* is not enough -- confirmed live
    on Amazon (2026-09-10): it used PaymentsToAcquirePropertyPlantAndEquipment
    for capex through 2017, then switched to PaymentsToAcquireProductiveAssets
    with no further updates to the old tag. The old tag was still "present"
    (152 historical values) but stale, so picking it first silently returned
    capex=None for every recent period. Comparing candidates by their own
    latest `end` date and taking the freshest one is filer-agnostic and
    avoids needing to guess the right tag order per company."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    best_tag, best_values, best_end = None, [], ""
    for tag in candidate_tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        values = units.get("USD") or next(iter(units.values()), [])
        if not values:
            continue
        latest_end = max((v.get("end", "") for v in values), default="")
        if latest_end > best_end:
            best_tag, best_values, best_end = tag, values, latest_end
    return best_tag, best_values


def latest_10k_filing(cik: str) -> dict | None:
    """Return {'accessionNumber', 'primaryDocument', 'filingDate'} for the
    most recent 10-K, via the submissions API."""
    subs = fetch_submissions(cik)
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form == "10-K":
            return {
                "accessionNumber": recent["accessionNumber"][i],
                "primaryDocument": recent["primaryDocument"][i],
                "filingDate": recent["filingDate"][i],
            }
    return None


def filing_document_url(cik: str, accession_number: str, primary_document: str) -> str:
    accn_nodash = accession_number.replace("-", "")
    cik_nozero = str(int(cik))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{accn_nodash}/{primary_document}"
