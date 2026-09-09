"""
Per-concept XBRL tag mapping for SEC EDGAR 'company facts' pulls (spec 7.3).

Different filers sometimes use different us-gaap tags for the same concept.
Each concept maps to an ordered list of candidate tags -- the collector tries
them in order and uses the first one present in a given company's facts,
recording which tag actually resolved so it can be spot-checked. This list is
a best-effort default; it should be validated against at least one real
filing per company before being trusted (7.3), which the collector does by
logging the resolved tag + a sample value per entity on first run.
"""

CASHFLOW_CONCEPTS = {
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsAtCarryingValue",
    ],
}

# Debt aggregate concepts (structured, reliable) -- NOT itemized per-bond.
# Itemized bond-by-bond terms (coupon/maturity per instrument) require
# parsing the "Debt" footnote text of the 10-K; XBRL company-facts does not
# expose these consistently across filers. See collectors/sec_edgar_debt.py.
DEBT_AGGREGATE_CONCEPTS = {
    # Oracle uses neither LongTermDebtNoncurrent nor LongTermDebt for its
    # long-term notes -- it tags them LongTermNotesPayable/LongTermNotesAndLoans
    # instead (confirmed against ORCL's live company-facts, 2026-09-09).
    # Keep candidates in this order; resolve_tag() takes the first match
    # present for a given filer, so a filer-specific tag never gets
    # shadowed by a generic one that resolves to stale/zero data.
    "long_term_debt_noncurrent": [
        "LongTermDebtNoncurrent",
        "LongTermNotesPayable",
        "LongTermNotesAndLoans",
        "LongTermDebt",
    ],
    "long_term_debt_current": [
        "LongTermDebtCurrent",
        "NotesPayableCurrent",
        "DebtCurrent",
    ],
}

# us-gaap concepts commonly used inside dimensional (DebtInstrumentAxis)
# disclosures, tried by the itemized-debt best-effort extractor.
DEBT_INSTRUMENT_DIMENSIONAL_CONCEPTS = [
    "DebtInstrumentFaceAmount",
    "DebtInstrumentInterestRateStatedPercentage",
    "DebtInstrumentMaturityDate",
]
