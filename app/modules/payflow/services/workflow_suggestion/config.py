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
# Raised from 12, then 15: stopping when the customer pays adds a check before every follow-up and a 3-step stop
# branch, so 5 messages need ~19 steps. With 15 the AI often failed Regenerate remarks such as "add an SMS route".
# Raised to 20 when the stop branch grew from 3 to 5 steps (paid in full / on a plan / part paid kept apart).
MAX_STEPS = 20
MAX_COMMUNICATIONS = 5

# Stop when the customer acts: on the payment page they can pay in full, pay part or set up a plan.
# Before every follow-up message, PAID_OR_PLAN_CHECK — yes = they acted → the stop branch:
# PAID_IN_FULL_CHECK yes → Outcome "Paid In Full"; no → ON_A_PLAN_CHECK yes → Outcome "Payment Plan Active";
# no (paid part only) → Human Review. The worker's Outcome "Paid In Full" closes the case and sets the balance
# to 0, so it must never be reached by a customer who is on a plan or paid only part.
# Payment Plan customers are already on a plan: for them PAID_OR_PLAN_CHECK and ON_A_PLAN_CHECK are always
# yes, so their workflows check MISSED_INSTALMENT_CHECK instead.
# Stated in the prompt (prompt.py), checked by validator.py.
PAID_OR_PLAN_CHECK = {"attribute": "Payment Status", "operator": "Not Equals", "value": "Unpaid"}
PAID_IN_FULL_CHECK = {"attribute": "Payment Status", "operator": "Equals", "value": "Paid In Full"}
ON_A_PLAN_CHECK = {"attribute": "Payment Status", "operator": "Equals", "value": "Payment Plan Active"}
MISSED_INSTALMENT_CHECK = {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}
PAID_OUTCOME = "Paid In Full"
ON_A_PLAN_OUTCOME = "Payment Plan Active"
STOP_WHEN_PAID_SITUATIONS = ("Active", "Promise to Pay")
ON_A_PLAN_SITUATION = "Payment Plan"

# AI workflows contact the customer with Communication steps that have NO channel: the worker picks email or
# SMS for each customer, switches when one fails and flags customers it cannot reach. So the AI never sets a
# channel or template (removed from its reply) and never branches on delivery or contact details.
CONTACT_CHANNEL = "Email + SMS"  # a channel-free step's messages: an email (subject + body) and an SMS (sms_body)
CHANNEL_FIELDS = ("channel", "template_id")
CONTACT_ATTRIBUTES = ("Email Delivery", "SMS Delivery", "Email Address", "Mobile Number")
CHANNEL_EVENTS = ("Previous Email", "Previous SMS")  # timing after a contact step: "Previous Action"

# The worker waits until reference date + amount. These dates are in the past for most accounts, so a wait
# after the first message timed from them ends at once and the next message follows right away.
FIXED_DATE_EVENTS = ("Case Received", "Due Date")
# The worker counts PayFlow's own messages as contact, so Customer Response "Received" / "None" say nothing
# about the customer: only this value can be checked.
CUSTOMER_RESPONSE_VALUE = "Dispute Raised"

# Why the AI chose a workflow — one short text per field, saved as ai_context lines.
# changes_made is only filled on regenerate.
REASONING_LABELS: dict[str, str] = {
    "segment_fit": "Why it fits",
    "channels": "Channels",
    "timing": "Timing",
    "escalation": "Escalation",
    "changes_made": "Changes made",
}
REASONING_FIELDS = tuple(REASONING_LABELS)

# Segmenting — small groups are merged until each has MIN_SEGMENT_SIZE accounts.
# MAX_SEGMENTS = Promise to Pay + Payment Plan + up to 6 Active segments.
MIN_SEGMENT_SIZE = 20
MAX_SEGMENTS = 8
