"""Workflow suggestion settings: band meanings, workflow limits, segment sizes.

Band *labels* come from the workflow catalog in strategy_service.py (the same
lists the UI "Who it applies to" dropdowns use). This file only adds what the
catalog lacks — what each label means — so accounts can be matched to segments.
tests/test_workflow_config.py checks these keys match the catalog exactly.
"""

from app.modules.payflow.services.strategy_service import (
    AGE_BANDS,
    BALANCE_BANDS,
    DELINQUENCY_BANDS,
    LANGUAGE_PREFERENCES,
    POSTAL_REGIONS,
    TENURE_BANDS,
)

# Segment field (as stored on a workflow's `segment`) → catalog labels.
# Index 0 of every list is the "All …" label.
SEGMENT_FIELDS: dict[str, list[str]] = {
    "age_band": AGE_BANDS,
    "postal_region": POSTAL_REGIONS,
    "balance_band": BALANCE_BANDS,
    "delinquency": DELINQUENCY_BANDS,
    "language": LANGUAGE_PREFERENCES,
    "tenure": TENURE_BANDS,
}
ALL_LABELS: dict[str, str] = {field: labels[0] for field, labels in SEGMENT_FIELDS.items()}

# Numeric ranges are [low, high) — high=None means no upper limit.
DELINQUENCY_RANGES: dict[str, tuple[int, int | None]] = {  # days_past_due
    "1 – 29 days past due": (1, 30),
    "30 – 59 days past due": (30, 60),
    "60 – 89 days past due": (60, 90),
    "90+ days past due": (90, None),
}
BALANCE_RANGES: dict[str, tuple[int, int | None]] = {  # outstanding_balance
    "Under $500": (0, 500),
    "$500 – $1,500": (500, 1500),
    "$1,500 – $5,000": (1500, 5000),
    "$5,000 and over": (5000, None),
}
AGE_RANGES: dict[str, tuple[int, int | None]] = {  # years, from date_of_birth
    "18 – 24": (18, 25),
    "25 – 34": (25, 35),
    "35 – 49": (35, 50),
    "50 – 64": (50, 65),
    "65 and over": (65, None),
}
TENURE_RANGES: dict[str, tuple[int, int | None]] = {  # months since date_listed
    "New customer (under 6 months)": (0, 6),
    "Established (6 – 24 months)": (6, 24),
    "Long-standing (2 years+)": (24, None),
}

# First letter of the postal code (Canadian forward sortation area).
POSTAL_REGION_PREFIXES: dict[str, str] = {
    "Ontario (M, L, K, N, P)": "MLKNP",
    "Quebec (H, J, G)": "HJG",
    "British Columbia (V)": "V",
    "Alberta (T)": "T",
    "Atlantic (A, B, C, E)": "ABCE",
    "Prairies (R, S)": "RS",
}

# Account.language codes covered by each language preference.
LANGUAGE_CODES: dict[str, set[str]] = {
    "English": {"EN"},
    "French": {"FR"},
    "English + French": {"EN", "FR"},
}

# Segment field → meaning of each band label (every label except "All …").
BAND_DEFINITIONS: dict[str, dict] = {
    "age_band": AGE_RANGES,
    "postal_region": POSTAL_REGION_PREFIXES,
    "balance_band": BALANCE_RANGES,
    "delinquency": DELINQUENCY_RANGES,
    "language": LANGUAGE_CODES,
    "tenure": TENURE_RANGES,
}

# Workflow limits — stated in the prompt and enforced by the validator.
MIN_STEPS = 3
MAX_STEPS = 12
MAX_COMMUNICATIONS = 5

# Segmenting — small groups are merged until each has MIN_SEGMENT_SIZE accounts.
# MAX_SEGMENTS = Promise to Pay + Payment Plan + up to 6 Active segments.
MIN_SEGMENT_SIZE = 20
MAX_SEGMENTS = 8
