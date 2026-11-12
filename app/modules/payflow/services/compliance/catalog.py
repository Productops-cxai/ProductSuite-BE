"""Compliance catalogue: rule types, categories, allowed values, jurisdictions, library.

Everything here is reference data. The 25 rule types (JUR-01 … JUR-25) are the
story's rule categories; ``category`` is the group the UI shows them under.
A new rule type is one more entry in ``RULE_TYPES``: the rule table itself does
not change (settings and conditions are stored as JSON and validated here).

The jurisdiction library is illustrative and seeded as Draft: nothing in it is
enforced until an administrator has it legally validated and activates it.
"""

from __future__ import annotations

from typing import Any

CATEGORIES = [
    "Consent & Opt-Out",
    "Reply Handling",
    "Frequency",
    "Permitted Hours",
    "Holidays & Restricted Dates",
    "Content",
    "Contact Restrictions",
    "Notice & Waiting Periods",
    "Additional Controls",
]

STATUS_DRAFT = "Draft"
STATUS_PENDING = "Pending Approval"
STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
STATUS_ARCHIVED = "Archived"
STATUSES = [STATUS_DRAFT, STATUS_PENDING, STATUS_ACTIVE, STATUS_INACTIVE, STATUS_ARCHIVED]

ENFORCEMENT_MANDATORY = "Mandatory"
ENFORCEMENT_CONFIGURABLE = "Client Configurable"
ENFORCEMENT_ADVISORY = "Advisory"
ENFORCEMENTS = [ENFORCEMENT_MANDATORY, ENFORCEMENT_CONFIGURABLE, ENFORCEMENT_ADVISORY]

ACTIONS = ["Allow", "Block", "Reschedule", "Suppress", "Require Approval", "Flag for Review"]
CHANNELS = ["All Channels", "Email", "SMS", "Voice", "WhatsApp"]
LOGICS = ["AND", "OR"]
OPERATORS = ["Equals", "Greater Than", "Less Than", "Between", "Includes", "Excludes"]
JURISDICTION_TYPES = ["Federal", "State / Province", "Regional", "Municipal", "Postal"]
COLLECTOR_TYPES = ["First Party", "Third Party", "Debt Buyer"]
DEBT_TYPES = ["Consumer", "Commercial", "Medical", "Student", "Utility", "Other"]
OVERRIDE_POLICIES = ["Not Permitted", "Operations Admin With Reason", "Client Configurable"]
COUNT_EVENTS = ["attempted", "completed", "conversation"]
COUNT_SCOPES = ["account", "customer", "creditor"]

CONDITION_FIELDS = [
    "Consent", "Opt-Out", "Contact Attempts", "Outside Permitted Hours", "Restricted Date",
    "Do Not Contact", "Cease Communication", "Disputed Debt", "Legal Representation",
    "Bankruptcy", "Deceased", "Hardship", "Third-Party Contact", "Sender Identified",
    "Unsubscribe Instructions", "Template Approved", "Reply Handling Enabled", "Language",
    "Communication Type", "Outstanding Balance", "Days Past Due", "Days Since Last Contact",
    "Days Since Conversation", "Channel Permitted", "Workplace Contact", "Employer Email",
    "Written Notice Sent", "Days Since Notice", "Validation Notice Sent",
    "Accounting Request Overdue", "Time-Barred Debt", "Unauthorized Fee", "Collector Licensed",
    "Outside Customer Preference", "Chargeable Method",
]
NUMERIC_FIELDS = {
    "Contact Attempts", "Outstanding Balance", "Days Past Due", "Days Since Last Contact",
    "Days Since Conversation", "Days Since Notice",
}

# Customer / account flags an operator records (FR-09). Each is also a condition field.
ACCOUNT_FLAGS = [
    "Do Not Contact", "Cease Communication", "Disputed Debt", "Legal Representation",
    "Bankruptcy", "Deceased", "Hardship", "Third-Party Contact", "Workplace Contact",
    "Employer Email", "Time-Barred Debt", "Accounting Request Overdue", "Unauthorized Fee",
    "Chargeable Method", "Validation Notice Sent",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "start": "09:00",
    "end": "18:00",
    "weekend_start": "",
    "weekend_end": "",
    "saturday_start": "",
    "saturday_end": "",
    "sunday_start": "",
    "sunday_end": "",
    "days": [1, 2, 3, 4, 5],
    "interval_hours": 0,
    "daily": 0,
    "weekly": 0,
    "monthly": 0,
    "combined": True,
    "count_events": ["attempted"],
    "scope": "account",
    "reply": "Two-way",
    "keywords": "STOP, HELP",
    "consent_method": "Recorded customer preference",
    "consent_expiry_days": 0,
    "resubscribe": True,
    "promotional_only": False,
    "required_text": "",
    "retention_days": 365,
}

# What each rule type needs before it may be activated ("needs"), on top of the
# checks every rule gets. hours = a valid start/end/days window; limit = a
# rolling threshold; period = a positive number of days; text = required text.
RULE_TYPES: list[dict[str, Any]] = [
    {"code": "JUR-01", "name": "Communication Hours", "category": "Permitted Hours", "action": "Reschedule", "field": "Outside Permitted Hours", "value": "Yes", "needs": ["hours"]},
    {"code": "JUR-02", "name": "Allowed Communication Days", "category": "Permitted Hours", "action": "Reschedule", "field": "Outside Permitted Hours", "value": "Yes", "needs": ["days"]},
    {"code": "JUR-03", "name": "Public Holiday Restrictions", "category": "Holidays & Restricted Dates", "action": "Block", "field": "Restricted Date", "value": "Yes"},
    {"code": "JUR-04", "name": "Maximum Contact Attempts", "category": "Frequency", "action": "Block", "field": "Contact Attempts", "value": "3", "operator": "Greater Than", "needs": ["limit"]},
    {"code": "JUR-05", "name": "Communication Frequency", "category": "Frequency", "action": "Reschedule", "field": "Days Since Last Contact", "value": "1", "operator": "Less Than", "needs": ["limit"]},
    {"code": "JUR-06", "name": "Post-Conversation Cooling Period", "category": "Frequency", "action": "Block", "field": "Days Since Conversation", "value": "7", "operator": "Less Than", "needs": ["period"]},
    {"code": "JUR-07", "name": "Communication Channel Restrictions", "category": "Contact Restrictions", "action": "Block", "field": "Channel Permitted", "value": "No"},
    {"code": "JUR-08", "name": "Consent Requirements", "category": "Consent & Opt-Out", "action": "Block", "field": "Consent", "value": "No"},
    {"code": "JUR-09", "name": "Unsubscribe / Opt-Out", "category": "Consent & Opt-Out", "action": "Suppress", "field": "Opt-Out", "value": "Yes"},
    {"code": "JUR-10", "name": "Cease Communication", "category": "Contact Restrictions", "action": "Suppress", "field": "Cease Communication", "value": "Yes"},
    {"code": "JUR-11", "name": "Attorney Representation", "category": "Contact Restrictions", "action": "Require Approval", "field": "Legal Representation", "value": "Yes"},
    {"code": "JUR-12", "name": "Workplace Communication", "category": "Contact Restrictions", "action": "Block", "field": "Workplace Contact", "value": "Yes"},
    {"code": "JUR-13", "name": "Third-Party Contact", "category": "Contact Restrictions", "action": "Require Approval", "field": "Third-Party Contact", "value": "Yes"},
    {"code": "JUR-14", "name": "Initial Collection Notice", "category": "Notice & Waiting Periods", "action": "Block", "field": "Written Notice Sent", "value": "No"},
    {"code": "JUR-15", "name": "Notice Waiting Period", "category": "Notice & Waiting Periods", "action": "Reschedule", "field": "Days Since Notice", "value": "5", "operator": "Less Than", "needs": ["period"]},
    {"code": "JUR-16", "name": "Debt Validation", "category": "Notice & Waiting Periods", "action": "Require Approval", "field": "Validation Notice Sent", "value": "No"},
    {"code": "JUR-17", "name": "Dispute Management", "category": "Contact Restrictions", "action": "Suppress", "field": "Disputed Debt", "value": "Yes"},
    {"code": "JUR-18", "name": "Time-Barred Debt", "category": "Additional Controls", "action": "Flag for Review", "field": "Time-Barred Debt", "value": "Yes"},
    {"code": "JUR-19", "name": "Collection Fees and Charges", "category": "Content", "action": "Block", "field": "Unauthorized Fee", "value": "Yes"},
    {"code": "JUR-20", "name": "Collector Identification", "category": "Content", "action": "Require Approval", "field": "Sender Identified", "value": "No"},
    {"code": "JUR-21", "name": "Licensing Requirements", "category": "Additional Controls", "action": "Block", "field": "Collector Licensed", "value": "No"},
    {"code": "JUR-22", "name": "Customer Communication Preferences", "category": "Contact Restrictions", "action": "Reschedule", "field": "Outside Customer Preference", "value": "Yes"},
    {"code": "JUR-23", "name": "Communication Content Restrictions", "category": "Content", "action": "Require Approval", "field": "Template Approved", "value": "No"},
    {"code": "JUR-24", "name": "Communication Cost Restrictions", "category": "Contact Restrictions", "action": "Block", "field": "Chargeable Method", "value": "Yes"},
    {"code": "JUR-25", "name": "Record Retention and Audit", "category": "Additional Controls", "action": "Allow", "field": "Communication Type", "value": "Collection", "needs": ["retention"]},
]
RULE_TYPE_BY_CODE = {t["code"]: t for t in RULE_TYPES}

# --------------------------------------------------------------------------- jurisdictions

_US_STATES = [
    ("AL", "Alabama", "America/Chicago"), ("AK", "Alaska", "America/Anchorage"),
    ("AZ", "Arizona", "America/Phoenix"), ("AR", "Arkansas", "America/Chicago"),
    ("CA", "California", "America/Los_Angeles"), ("CO", "Colorado", "America/Denver"),
    ("CT", "Connecticut", "America/New_York"), ("DE", "Delaware", "America/New_York"),
    ("DC", "District of Columbia", "America/New_York"), ("FL", "Florida", "America/New_York"),
    ("GA", "Georgia", "America/New_York"), ("HI", "Hawaii", "Pacific/Honolulu"),
    ("ID", "Idaho", "America/Boise"), ("IL", "Illinois", "America/Chicago"),
    ("IN", "Indiana", "America/Indiana/Indianapolis"), ("IA", "Iowa", "America/Chicago"),
    ("KS", "Kansas", "America/Chicago"), ("KY", "Kentucky", "America/New_York"),
    ("LA", "Louisiana", "America/Chicago"), ("ME", "Maine", "America/New_York"),
    ("MD", "Maryland", "America/New_York"), ("MA", "Massachusetts", "America/New_York"),
    ("MI", "Michigan", "America/Detroit"), ("MN", "Minnesota", "America/Chicago"),
    ("MS", "Mississippi", "America/Chicago"), ("MO", "Missouri", "America/Chicago"),
    ("MT", "Montana", "America/Denver"), ("NE", "Nebraska", "America/Chicago"),
    ("NV", "Nevada", "America/Los_Angeles"), ("NH", "New Hampshire", "America/New_York"),
    ("NJ", "New Jersey", "America/New_York"), ("NM", "New Mexico", "America/Denver"),
    ("NY", "New York", "America/New_York"), ("NC", "North Carolina", "America/New_York"),
    ("ND", "North Dakota", "America/Chicago"), ("OH", "Ohio", "America/New_York"),
    ("OK", "Oklahoma", "America/Chicago"), ("OR", "Oregon", "America/Los_Angeles"),
    ("PA", "Pennsylvania", "America/New_York"), ("RI", "Rhode Island", "America/New_York"),
    ("SC", "South Carolina", "America/New_York"), ("SD", "South Dakota", "America/Chicago"),
    ("TN", "Tennessee", "America/Chicago"), ("TX", "Texas", "America/Chicago"),
    ("UT", "Utah", "America/Denver"), ("VT", "Vermont", "America/New_York"),
    ("VA", "Virginia", "America/New_York"), ("WA", "Washington", "America/Los_Angeles"),
    ("WV", "West Virginia", "America/New_York"), ("WI", "Wisconsin", "America/Chicago"),
    ("WY", "Wyoming", "America/Denver"),
]
_CA_PROVINCES = [
    ("AB", "Alberta", "America/Edmonton"), ("BC", "British Columbia", "America/Vancouver"),
    ("MB", "Manitoba", "America/Winnipeg"), ("NB", "New Brunswick", "America/Moncton"),
    ("NL", "Newfoundland and Labrador", "America/St_Johns"),
    ("NT", "Northwest Territories", "America/Yellowknife"), ("NS", "Nova Scotia", "America/Halifax"),
    ("NU", "Nunavut", "America/Iqaluit"), ("ON", "Ontario", "America/Toronto"),
    ("PE", "Prince Edward Island", "America/Halifax"), ("QC", "Quebec", "America/Toronto"),
    ("SK", "Saskatchewan", "America/Regina"), ("YT", "Yukon", "America/Whitehorse"),
]

# Regions / cities that carry their own rules today. More are added as rules need them.
_REGIONS: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("US", "NY"): [{"name": "New York City", "cities": ["New York"], "postal_prefixes": ["100", "101", "102", "103", "104", "111", "112", "113", "114", "116"]}],
    ("CA", ""): [{"name": "Federally regulated financial institutions", "cities": [], "postal_prefixes": []}],
}

COUNTRIES: list[dict[str, Any]] = [
    {
        "code": "US",
        "name": "United States",
        "aliases": ["US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"],
        "default_timezone": "America/New_York",
        "subdivision_label": "State",
        "postal_label": "ZIP code",
        "states": [{"code": c, "name": n, "timezone": tz} for c, n, tz in _US_STATES],
    },
    {
        "code": "CA",
        "name": "Canada",
        "aliases": ["CA", "CAN", "CANADA"],
        "default_timezone": "America/Toronto",
        "subdivision_label": "Province / Territory",
        "postal_label": "Postal code",
        "states": [{"code": c, "name": n, "timezone": tz} for c, n, tz in _CA_PROVINCES],
    },
]


def _key(value: str | None) -> str:
    return " ".join((value or "").strip().upper().split())


def find_country(value: str | None) -> dict[str, Any] | None:
    key = _key(value)
    if not key:
        return None
    for country in COUNTRIES:
        if key == _key(country["name"]) or key in country["aliases"]:
            return country
    return None


def find_state(country: dict[str, Any] | None, value: str | None) -> dict[str, Any] | None:
    key = _key(value)
    if not country or not key:
        return None
    if key == "QUÉBEC":
        key = "QUEBEC"
    for state in country["states"]:
        if key in (_key(state["name"]), state["code"]):
            return state
    return None


def regions_for(country_code: str, state_code: str = "") -> list[dict[str, Any]]:
    return list(_REGIONS.get((country_code, state_code), []))


def timezone_for(country: str | None, state: str | None = None) -> str:
    """Time zone of a state / province (its main one), else the country default, else ""."""
    found = find_country(country)
    if not found:
        return ""
    region = find_state(found, state)
    if region:
        return region["timezone"]
    # Several zones in the country and no state: the local time cannot be known.
    return ""


def normalize_jurisdiction(country: str | None, state: str | None) -> tuple[str, str]:
    """Canonical (country name, state name) for known values; unknown text is kept as typed."""
    found = find_country(country)
    if not found:
        return (country or "").strip(), (state or "").strip()
    region = find_state(found, state)
    return found["name"], region["name"] if region else (state or "").strip()


# --------------------------------------------------------------------------- library

_MON_SAT = [1, 2, 3, 4, 5, 6]
_ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
_US = "United States"
_CA = "Canada"
_FDCPA = "FDCPA / Regulation F (12 CFR 1006)"


def _hours(start: str, end: str, days: list[int], sun: tuple[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"start": start, "end": end, "days": days}
    if sun:
        out["sunday_start"], out["sunday_end"] = sun
    return out


_SUN_1_5 = ("13:00", "17:00")

# code, rule type, name, country, state, region, channel, description, reference, source, extras
JURISDICTION_LIBRARY: list[dict[str, Any]] = [
    {"code": "JL-US-01", "type": "JUR-01", "name": "US federal · permitted contact hours 8am–9pm", "country": _US, "description": "Generally 8:00 a.m.–9:00 p.m. in the debtor's local time.", "reference": _FDCPA, "source": "Time-window restriction + time-zone mapping", "hours": _hours("08:00", "21:00", _ALL_DAYS)},
    {"code": "JL-US-02", "type": "JUR-04", "name": "US federal · 7 calls in 7 days", "country": _US, "channel": "Voice", "description": "Presumed violation if more than 7 calls within 7 consecutive days about a particular debt.", "reference": _FDCPA, "source": "Rolling call counter", "limit": (7, 7)},
    {"code": "JL-US-03", "type": "JUR-06", "name": "US federal · 7-day post-conversation cooling-off", "country": _US, "channel": "Voice", "description": "Generally presumed violation if calling again within 7 days after a telephone conversation about the debt.", "reference": _FDCPA, "source": "Cooling-off period"},
    {"code": "JL-US-04", "type": "JUR-03", "name": "US federal · Sunday & holiday contact (state check)", "country": _US, "description": "Not universally prohibited federally; subject to inconvenient-time rules, state rules and individual preferences.", "reference": _FDCPA, "source": "Check state rules and individual preferences"},
    {"code": "JL-US-05", "type": "JUR-12", "name": "US federal · workplace contact", "country": _US, "description": "Restricted if the collector knows the employer prohibits it.", "reference": _FDCPA, "source": "Workplace contact restriction"},
    {"code": "JL-US-06", "type": "JUR-11", "name": "US federal · attorney representation", "country": _US, "description": "Direct communication generally prohibited when representation is known, subject to exceptions.", "reference": _FDCPA, "source": "Attorney-only routing"},
    {"code": "JL-US-07", "type": "JUR-10", "name": "US federal · cease-communication request", "country": _US, "description": "Further communication generally prohibited after a qualifying written request, subject to statutory exceptions.", "reference": _FDCPA, "source": "Collection communication suppression"},
    {"code": "JL-US-08", "type": "JUR-09", "name": "US federal · email/SMS opt-out", "country": _US, "description": "Must honor applicable electronic communication opt-out requests.", "reference": _FDCPA, "source": "Channel-specific unsubscribe"},
    {"code": "JL-US-09", "type": "JUR-13", "name": "US federal · third-party disclosure", "country": _US, "description": "Generally prohibited except in permitted circumstances.", "reference": _FDCPA, "source": "Recipient and disclosure validation"},
    {"code": "JL-US-10", "type": "JUR-16", "name": "US federal · initial debt validation", "country": _US, "description": "Required information generally at initial communication or within five days.", "reference": _FDCPA, "source": "Validation notice workflow"},
    {"code": "JL-CA-AB", "type": "JUR-01", "name": "Alberta · home contact 7am–10pm", "country": _CA, "state": "Alberta", "description": "Home contact generally 7 a.m.–10 p.m. Alberta time; Sunday verified separately.", "reference": "Alberta Collection and Debt Repayment Practices Regulation", "source": "Provincial matrix", "hours": _hours("07:00", "22:00", _ALL_DAYS)},
    {"code": "JL-CA-AB2", "type": "JUR-04", "name": "Alberta · 3 unsolicited contacts in 7 days", "country": _CA, "state": "Alberta", "description": "Max 3 unsolicited contacts per 7 consecutive days, with specified exclusions. Track by creditor and debtor.", "reference": "Alberta Collection and Debt Repayment Practices Regulation", "source": "Track unsolicited contacts", "limit": (3, 7)},
    {"code": "JL-CA-AB3", "type": "JUR-17", "name": "Alberta · dispute suppression", "country": _CA, "state": "Alberta", "description": "Stop collection activity in specified dispute circumstances.", "reference": "Alberta Collection and Debt Repayment Practices Regulation", "source": "Debt dispute suppression"},
    {"code": "JL-CA-AB4", "type": "JUR-16", "name": "Alberta · 30-day accounting request", "country": _CA, "state": "Alberta", "description": "If requested accounting is not provided within 30 days, collection must stop until it is provided.", "reference": "Alberta Collection and Debt Repayment Practices Regulation", "source": "Accounting request deadline", "field": "Accounting Request Overdue", "value": "Yes"},
    {"code": "JL-CA-BC", "type": "JUR-01", "name": "British Columbia · Mon–Sat 7am–9pm, Sun 1–5pm", "country": _CA, "state": "British Columbia", "description": "No calls on statutory holidays.", "reference": "BC Business Practices and Consumer Protection Act", "source": "Provincial matrix", "hours": _hours("07:00", "21:00", _ALL_DAYS, _SUN_1_5)},
    {"code": "JL-CA-ON", "type": "JUR-01", "name": "Ontario · Mon–Sat 7am–9pm, Sun 1–5pm", "country": _CA, "state": "Ontario", "description": "Sunday contact limited to 1–5 p.m.", "reference": "Ontario Collection and Debt Settlement Services Act", "source": "Provincial matrix", "hours": _hours("07:00", "21:00", _ALL_DAYS, _SUN_1_5)},
    {"code": "JL-CA-ON2", "type": "JUR-04", "name": "Ontario · 3 contacts in 7 days", "country": _CA, "state": "Ontario", "description": "Generally no more than 3 contacts in 7 days on behalf of the same creditor, subject to conditions.", "reference": "Ontario Collection and Debt Settlement Services Act", "source": "Provincial matrix", "limit": (3, 7)},
    {"code": "JL-CA-ON3", "type": "JUR-14", "name": "Ontario · initial notice & waiting period", "country": _CA, "state": "Ontario", "description": "Initial notice and waiting-period requirements before telephone collection.", "reference": "Ontario Collection and Debt Settlement Services Act", "source": "Prevent premature telephone collection"},
    {"code": "JL-CA-QC", "type": "JUR-01", "name": "Québec · Mon–Sat 8am–8pm, no Sunday", "country": _CA, "state": "Quebec", "description": "Sunday prohibited; no collection-agent contact on listed statutory holidays.", "reference": "Québec Act respecting the collection of certain debts", "source": "Provincial matrix", "hours": _hours("08:00", "20:00", _MON_SAT)},
    {"code": "JL-CA-QC2", "type": "JUR-15", "name": "Québec · written notice, wait 5 days", "country": _CA, "state": "Quebec", "description": "Written notice before calling; wait 5 days after sending notice.", "reference": "Québec Act respecting the collection of certain debts", "source": "First-contact workflow", "period": 5},
    {"code": "JL-CA-MB", "type": "JUR-01", "name": "Manitoba · Mon–Sat 7am–9pm, no Sunday", "country": _CA, "state": "Manitoba", "description": "No phone calls or visits on statutory holidays.", "reference": "Manitoba Consumer Protection Act", "source": "Provincial matrix", "hours": _hours("07:00", "21:00", _MON_SAT)},
    {"code": "JL-CA-SK", "type": "JUR-01", "name": "Saskatchewan · Mon–Sat 8am–9pm, no Sunday", "country": _CA, "state": "Saskatchewan", "description": "No calling on Sundays or defined holidays (province-specific calendar).", "reference": "Saskatchewan Collection Agents Regulations", "source": "Province-specific calendar", "hours": _hours("08:00", "21:00", _MON_SAT)},
    {"code": "JL-CA-NS", "type": "JUR-01", "name": "Nova Scotia · Mon–Sat 8am–9pm, no Sunday", "country": _CA, "state": "Nova Scotia", "description": "Statutory holiday restrictions apply.", "reference": "Nova Scotia Collection Agencies Regulations", "source": "Provincial matrix", "hours": _hours("08:00", "21:00", _MON_SAT)},
    {"code": "JL-CA-NS2", "type": "JUR-04", "name": "Nova Scotia · 3 contacts in 7 days", "country": _CA, "state": "Nova Scotia", "description": "No more than 3 contacts in 7 days.", "reference": "Nova Scotia Collection Agencies Regulations", "source": "Provincial matrix", "limit": (3, 7)},
    {"code": "JL-CA-NS3", "type": "JUR-14", "name": "Nova Scotia · written notice before contact", "country": _CA, "state": "Nova Scotia", "description": "Written notice required before telephone or electronic contact by collection agencies.", "reference": "Nova Scotia Collection Agencies Regulations", "source": "Communication prerequisite"},
    {"code": "JL-CA-NB", "type": "JUR-01", "name": "New Brunswick · Mon–Sat 7am–9pm, Sun 1–5pm", "country": _CA, "state": "New Brunswick", "description": "No calls on holidays.", "reference": "New Brunswick Collection and Debt Settlement Act", "source": "Provincial matrix", "hours": _hours("07:00", "21:00", _ALL_DAYS, _SUN_1_5)},
    {"code": "JL-CA-NB2", "type": "JUR-15", "name": "New Brunswick · first call 5 days after notice", "country": _CA, "state": "New Brunswick", "description": "Telephone or personal contact generally no earlier than 5 days after required notice.", "reference": "New Brunswick Collection and Debt Settlement Act", "source": "First-call eligibility date", "period": 5},
    {"code": "JL-CA-PE", "type": "JUR-01", "name": "Prince Edward Island · Mon–Sat 8am–9pm, no Sunday", "country": _CA, "state": "Prince Edward Island", "description": "Restrictions on workplace contact.", "reference": "PEI Collection Agencies Act", "source": "Provincial matrix", "hours": _hours("08:00", "21:00", _MON_SAT)},
    {"code": "JL-CA-PE2", "type": "JUR-12", "name": "Prince Edward Island · workplace contact", "country": _CA, "state": "Prince Edward Island", "description": "Workplace contact restrictions.", "reference": "PEI Collection Agencies Act", "source": "Provincial matrix"},
    {"code": "JL-CA-NL", "type": "JUR-01", "name": "Newfoundland & Labrador · 8am–10pm", "country": _CA, "state": "Newfoundland and Labrador", "description": "Generally 8 a.m.–10 p.m.; Sunday not independently verified.", "reference": "NL Collections Act regulations", "source": "Provincial matrix", "hours": _hours("08:00", "22:00", _ALL_DAYS)},
    {"code": "JL-CA-NL2", "type": "JUR-12", "name": "Newfoundland & Labrador · no workplace contact", "country": _CA, "state": "Newfoundland and Labrador", "description": "No workplace telephone, mail or in-person contact; prior written notice required.", "reference": "NL Collections Act regulations", "source": "Workplace communication restriction"},
    {"code": "JL-CA-FED", "type": "JUR-01", "name": "Canada federal FI · Mon–Sat 7am–9pm, Sun 1–5pm", "country": _CA, "region": "Federally regulated financial institutions", "description": "No contact on holidays; applies to federally regulated financial institutions and parties collecting on their behalf.", "reference": "Federal Financial Consumer Protection Framework", "source": "Federal rules", "hours": _hours("07:00", "21:00", _ALL_DAYS, _SUN_1_5)},
    {"code": "JL-US-MA", "type": "JUR-04", "name": "Massachusetts · 2 initiated calls per 7 days", "country": _US, "state": "Massachusetts", "channel": "Voice", "description": "Generally two initiated communications per 7-day period to personal numbers for covered creditors.", "reference": "940 CMR 7.04", "source": "State-specific frequency policy", "limit": (2, 7)},
    {"code": "JL-US-WA", "type": "JUR-04", "name": "Washington · 3 communications per week", "country": _US, "state": "Washington", "description": "More than 3 communications in a week can trigger a harassment presumption (cross-channel counter).", "reference": "RCW 19.16.250", "source": "Weekly cross-channel counter", "limit": (3, 7)},
    {"code": "JL-US-WA2", "type": "JUR-01", "name": "Washington · residential quiet hours 9pm–7:30am", "country": _US, "state": "Washington", "description": "Residential contact between 9 p.m. and 7:30 a.m. can trigger a harassment presumption.", "reference": "RCW 19.16.250", "source": "State residential quiet hours", "hours": _hours("07:30", "21:00", _ALL_DAYS)},
    {"code": "JL-US-NY", "type": "JUR-01", "name": "New York State · calling hours 8am–9pm", "country": _US, "state": "New York", "description": "General debt collection calling hours are 8 a.m.–9 p.m.", "reference": "23 NYCRR 1", "source": "State time-window rule", "hours": _hours("08:00", "21:00", _ALL_DAYS)},
    {"code": "JL-US-NYC", "type": "JUR-05", "name": "New York City · local layered requirements", "country": _US, "state": "New York", "region": "New York City", "description": "Additional local contact-frequency and electronic communication protections layered over state and federal rules.", "reference": "NYC Rules Title 6 §5-77", "source": "City-level layering"},
    {"code": "JL-US-E1", "type": "JUR-01", "name": "US · email sending hours (send time)", "country": _US, "channel": "Email", "description": "Inconvenient-time rules extend to electronic communications; the relevant time is when the collector sends it.", "reference": _FDCPA, "source": "Scheduled-send restrictions", "hours": _hours("08:00", "21:00", _ALL_DAYS)},
    {"code": "JL-US-E2", "type": "JUR-01", "name": "US · SMS sending hours (send time)", "country": _US, "channel": "SMS", "description": "Time-zone-based suppression for collection texts.", "reference": _FDCPA, "source": "Time-zone-based suppression", "hours": _hours("08:00", "21:00", _ALL_DAYS)},
    {"code": "JL-US-E3", "type": "JUR-09", "name": "US · SMS STOP handling", "country": _US, "channel": "SMS", "description": "Required opt-out mechanism for covered electronic collection communications.", "reference": _FDCPA, "source": "STOP handling and suppression"},
    {"code": "JL-US-E4", "type": "JUR-08", "name": "US · email/number eligibility (consent & provenance)", "country": _US, "description": "Specific procedures govern lawful use of an email address or text number.", "reference": _FDCPA, "source": "Consent/provenance verification"},
    {"code": "JL-US-E5", "type": "JUR-22", "name": "US · consumer-designated inconvenient hours", "country": _US, "description": "Customer-level contact preferences must generally be respected.", "reference": _FDCPA, "source": "Customer preference override"},
    {"code": "JL-US-E6", "type": "JUR-12", "name": "US · employer-provided email", "country": _US, "channel": "Email", "description": "Special restrictions and third-party disclosure concerns.", "reference": _FDCPA, "source": "Workplace email validation", "field": "Employer Email", "value": "Yes"},
]

HOLIDAY_SEED = [
    {"name": "New Year's Day", "date": "2026-01-01", "recurring": True, "country": _CA, "state": ""},
    {"name": "Christmas Day", "date": "2026-12-25", "recurring": True, "country": _CA, "state": ""},
    {"name": "Independence Day", "date": "2026-07-04", "recurring": True, "country": _US, "state": ""},
]
ILLUSTRATIVE_NOTE = "illustrative, legal validation required"

# Sample rules shown on the Compliance screens (the prototype's CMP-001 ... CMP-010): one per
# category and status, so the lists, filters, detail, history and audit pages have data.
# Unlike the library they are not all Drafts: the Active ones are enforced like any Active rule.
SAMPLE_REFERENCE = "Illustrative configuration — legal validation required"
SAMPLE_ACTOR = "PayFlow sample catalogue"
# The prototype's default settings for a new rule.
_SAMPLE_SETTINGS = {"weekend_start": "10:00", "weekend_end": "16:00"}
# code, rule type, name, category, country, state, channel, condition (field, operator, value),
# action, enforcement, status
SAMPLE_RULES: list[dict[str, Any]] = [
    {"code": "CMP-001", "type": "JUR-09", "name": "Canada · channel opt-out suppression", "category": "Consent & Opt-Out", "country": _CA, "condition": ("Opt-Out", "Equals", "Yes"), "action": "Suppress"},
    {"code": "CMP-002", "type": "JUR-08", "name": "Canada · explicit consent", "category": "Consent & Opt-Out", "country": _CA, "channel": "SMS", "condition": ("Consent", "Equals", "No"), "action": "Block"},
    {"code": "CMP-003", "type": "JUR-01", "name": "Ontario · recipient-local contact window", "category": "Permitted Hours", "country": _CA, "state": "Ontario", "condition": ("Outside Permitted Hours", "Equals", "Yes"), "action": "Reschedule"},
    {"code": "CMP-004", "type": "JUR-04", "name": "United States · rolling voice attempts", "category": "Frequency", "country": _US, "channel": "Voice", "condition": ("Contact Attempts", "Greater Than", "6"), "action": "Block"},
    {"code": "CMP-005", "type": "JUR-10", "name": "United States · cease communication", "category": "Contact Restrictions", "country": _US, "condition": ("Cease Communication", "Equals", "Yes"), "action": "Suppress"},
    {"code": "CMP-006", "type": "JUR-03", "name": "Canada · restricted calendar dates", "category": "Holidays & Restricted Dates", "country": _CA, "condition": ("Restricted Date", "Equals", "Yes"), "action": "Reschedule"},
    {"code": "CMP-007", "type": "JUR-20", "name": "United States · sender identification", "category": "Content", "country": _US, "condition": ("Sender Identified", "Equals", "No"), "action": "Require Approval", "enforcement": ENFORCEMENT_CONFIGURABLE},
    # No rule type of its own in the JUR list: the nearest one, with the prototype's category.
    {"code": "CMP-008", "type": "JUR-07", "name": "Canada · two-way SMS replies", "category": "Reply Handling", "country": _CA, "channel": "SMS", "condition": ("Reply Handling Enabled", "Equals", "No"), "action": "Block"},
    {"code": "CMP-009", "type": "JUR-23", "name": "Quebec · language review", "category": "Additional Controls", "country": _CA, "state": "Quebec", "condition": ("Language", "Equals", "French"), "action": "Flag for Review", "enforcement": ENFORCEMENT_ADVISORY, "status": STATUS_PENDING},
    {"code": "CMP-010", "type": "JUR-23", "name": "California · approved content", "category": "Content", "country": _US, "state": "California", "condition": ("Template Approved", "Equals", "No"), "action": "Require Approval", "status": STATUS_DRAFT},
]


def sample_rule_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """A sample rule as a rule payload (same shape the create API takes)."""
    field, operator, value = entry["condition"]
    return {
        "code": entry["code"],
        "name": entry["name"],
        "rule_type": entry["type"],
        "category": entry["category"],
        "description": "",
        "legal_reference": SAMPLE_REFERENCE,
        "source": "",
        "country": entry["country"],
        "state": entry.get("state", ""),
        "channel": entry.get("channel", "All Channels"),
        "enforcement": entry.get("enforcement", ENFORCEMENT_MANDATORY),
        "action": entry["action"],
        "logic": "AND",
        "conditions": [{"field": field, "operator": operator, "value": value, "period": 7}],
        "settings": {**DEFAULT_SETTINGS, **_SAMPLE_SETTINGS},
        "priority": 100,
        "effective_from": "2026-01-01",
    }


def library_rule_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """A library entry as a Draft rule payload (same shape the create API takes)."""
    kind = RULE_TYPE_BY_CODE[entry["type"]]
    settings = dict(DEFAULT_SETTINGS)
    # A window only where the entry defines one: the defaults are for hours rules.
    for key in ("start", "end"):
        settings[key] = ""
    settings["days"] = []
    limit = entry.get("limit")
    if entry.get("hours"):
        settings.update(entry["hours"])
    if limit:
        settings["weekly" if limit[1] == 7 else "monthly"] = limit[0]
    if limit:
        condition = {"field": "Contact Attempts", "operator": "Greater Than", "value": str(limit[0] - 1), "period": limit[1]}
    else:
        period = entry.get("period")
        condition = {
            "field": entry.get("field") or kind["field"],
            "operator": entry.get("operator") or kind.get("operator") or "Equals",
            "value": entry.get("value") or (str(period) if period else kind["value"]),
            "period": period or 7,
        }
    return {
        "code": entry["code"],
        "name": entry["name"],
        "rule_type": kind["code"],
        "category": kind["category"],
        "description": f"{kind['code']} {kind['name']}: {entry['description']}",
        "legal_reference": f"{entry['reference']} — {ILLUSTRATIVE_NOTE}",
        "regulatory_authority": entry["reference"],
        "source": entry["source"],
        "jurisdiction_type": "Regional" if entry.get("region") else ("State / Province" if entry.get("state") else "Federal"),
        "country": entry["country"],
        "state": entry.get("state", ""),
        "region": entry.get("region", ""),
        "channel": entry.get("channel", "All Channels"),
        "enforcement": ENFORCEMENT_MANDATORY,
        "action": kind["action"],
        "logic": "AND",
        "conditions": [condition],
        "settings": settings,
        "priority": 100,
        "effective_from": "2026-01-01",
    }


def catalog_payload() -> dict[str, Any]:
    return {
        "categories": CATEGORIES,
        "rule_types": RULE_TYPES,
        "statuses": STATUSES,
        "enforcements": ENFORCEMENTS,
        "actions": ACTIONS,
        "channels": CHANNELS,
        "logics": LOGICS,
        "operators": OPERATORS,
        "condition_fields": CONDITION_FIELDS,
        "jurisdiction_types": JURISDICTION_TYPES,
        "collector_types": COLLECTOR_TYPES,
        "debt_types": DEBT_TYPES,
        "override_policies": OVERRIDE_POLICIES,
        "count_events": COUNT_EVENTS,
        "count_scopes": COUNT_SCOPES,
        "account_flags": ACCOUNT_FLAGS,
        "default_settings": DEFAULT_SETTINGS,
        "countries": [{"code": c["code"], "name": c["name"]} for c in COUNTRIES],
    }
