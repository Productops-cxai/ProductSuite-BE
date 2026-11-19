"""Who a workflow's messages are written for — code, no AI, no DB.

A workflow's accounts are described by totals only (shares and averages, never one person), so the AI
can fit each message to them. By default accounts are split by age into audiences: a big enough
audience gets its own version of every message; everyone else gets the main text.

When a client enables country / region in segmentation config, those dimensions can also produce
variants (flat list — never a full cross-product). Region / country / balance / languages still
describe each audience for personalization even when they are not split on.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Callable

from app.modules.payflow.services.segmentation_config_service import (
    DEFAULT_AGE_GROUPS,
    age_bands_from_config,
    default_segmentation_config,
    slug_key,
)
from app.modules.payflow.services.workflow_suggestion import config
from app.modules.payflow.services.workflow_suggestion.languages import language_code
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    _band,
    _in_range,
    _years_since,
    contact_state_of,
    filter_eligible,
    match_account_to_segment,
)

# Audience key (stored with each version) → (age band, [low, high) years from date_of_birth).
AUDIENCES: dict[str, tuple[str, tuple[int, int | None]]] = {
    "age_18_24": ("18 – 24", (18, 25)),
    "age_25_34": ("25 – 34", (25, 35)),
    "age_35_44": ("35 – 44", (35, 45)),
    "age_45_54": ("45 – 54", (45, 55)),
    "age_55_64": ("55 – 64", (55, 65)),
    "age_65_plus": ("65 and over", (65, None)),
}
# The wider groups of versions written before the six above (until 2026-11): never written now, but a saved
# workflow's versions for them are still sent to their customers (audience_keys).
OLDER_AUDIENCES: dict[str, tuple[str, tuple[int, int | None]]] = {
    "age_18_34": ("18 – 34", (18, 35)),
    "age_35_64": ("35 – 64", (35, 65)),
}
MAIN_KEY = "main"  # the step's own text, for everyone without a version of their own
MIN_AUDIENCE_SIZE = 12  # fewer accounts → no own version (they get the main text)
MAX_VERSIONS = 6  # per message, besides the main text
UNKNOWN = "Unknown"

PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
COUNTRIES = {"CA": "Canada"}  # any other code is shown as the code itself


def _cfg(segmentation: dict[str, Any] | None) -> dict[str, Any]:
    return segmentation if isinstance(segmentation, dict) else default_segmentation_config()


def _age_bands(segmentation: dict[str, Any] | None) -> dict[str, tuple[str, tuple[int, int | None]]]:
    cfg = _cfg(segmentation)
    age_dim = (cfg.get("dimensions") or {}).get("age") or {}
    if not age_dim.get("enabled", True):
        return {}
    # Default six bands keep exact AUDIENCES keys/labels when config is the built-in default.
    groups = age_dim.get("groups") or DEFAULT_AGE_GROUPS
    if groups == DEFAULT_AGE_GROUPS or (
        len(groups) == len(DEFAULT_AGE_GROUPS)
        and all(g.get("key") == d["key"] for g, d in zip(groups, DEFAULT_AGE_GROUPS))
    ):
        return dict(AUDIENCES)
    return age_bands_from_config(cfg)


def _limits(segmentation: dict[str, Any] | None) -> tuple[int, int]:
    cfg = _cfg(segmentation)
    return (
        int(cfg.get("min_audience_size") or MIN_AUDIENCE_SIZE),
        int(cfg.get("max_versions") or MAX_VERSIONS),
    )


def _priority(segmentation: dict[str, Any] | None) -> list[str]:
    cfg = _cfg(segmentation)
    raw = cfg.get("priority") or ["region", "country", "age"]
    return [str(x) for x in raw if str(x) in {"age", "country", "region"}]


def audience_label(key: str | None) -> str | None:
    """"age_65_plus" → "Age 65 and over", "main" → "Main text" (the step's own text), None → None."""
    if not key:
        return None
    if key == MAIN_KEY:
        return "Main text"
    band = AUDIENCES.get(key) or OLDER_AUDIENCES.get(key)
    if band:
        return f"Age {band[0]}"
    if key.startswith("country_"):
        code = key[len("country_") :].upper()
        name = COUNTRIES.get(code) or key[len("country_") :].replace("_", " ").title()
        return f"Country: {name}"
    if key.startswith("region_"):
        slug = key[len("region_") :]
        # Reverse common province codes stored as region_on → Ontario
        name = PROVINCES.get(slug.upper()) or slug.replace("_", " ").title()
        return f"Region: {name}"
    return key


def audience_of(account, today: date | None = None, *, segmentation: dict[str, Any] | None = None) -> str | None:
    """The account's age audience key from its date of birth; None without one (or under 18)."""
    age = _years_since(getattr(account, "date_of_birth", None), today or date.today())
    bands = _age_bands(segmentation) or AUDIENCES
    return next((key for key, (_, years) in bands.items() if _in_range(age, years)), None)


def country_key_of(account) -> str | None:
    name = country_of(account)
    if not name or name == UNKNOWN:
        return None
    code = str(getattr(account, "country_code", None) or "").strip().upper()
    if code:
        return slug_key("country", code.lower())
    return slug_key("country", name)


def region_key_of(account) -> str | None:
    name = region_of(account)
    if not name or name == UNKNOWN:
        return None
    return slug_key("region", name)


def audience_keys(
    account,
    today: date | None = None,
    *,
    segmentation: dict[str, Any] | None = None,
) -> list[str]:
    """Every version key the account belongs to.

    Default (age-only) order is unchanged for existing workflows/tests: own six-band key, then
    older wider group. When country/region are enabled, keys follow config priority
    (most specific first).
    """
    today = today or date.today()
    cfg = _cfg(segmentation)
    dims = cfg.get("dimensions") or {}
    age_only = (
        (dims.get("age") or {}).get("enabled", True)
        and not (dims.get("country") or {}).get("enabled", False)
        and not (dims.get("region") or {}).get("enabled", False)
    )
    if age_only and _age_bands(cfg) == AUDIENCES:
        age = _years_since(getattr(account, "date_of_birth", None), today)
        return [
            key
            for groups in (AUDIENCES, OLDER_AUDIENCES)
            for key, (_, years) in groups.items()
            if _in_range(age, years)
        ]

    ordered: list[str] = []

    def add(key: str | None) -> None:
        if key and key not in ordered:
            ordered.append(key)

    age = _years_since(getattr(account, "date_of_birth", None), today)
    bands = _age_bands(cfg) or AUDIENCES
    for dim in _priority(cfg):
        if dim == "region" and (dims.get("region") or {}).get("enabled", False):
            add(region_key_of(account))
        elif dim == "country" and (dims.get("country") or {}).get("enabled", False):
            add(country_key_of(account))
        elif dim == "age" and (dims.get("age") or {}).get("enabled", True):
            for key, (_, years) in bands.items():
                if _in_range(age, years):
                    add(key)
            for key, (_, years) in OLDER_AUDIENCES.items():
                if _in_range(age, years):
                    add(key)
    return ordered


def match_keys_for_account(
    account,
    today: date | None = None,
    *,
    segmentation: dict[str, Any] | None = None,
) -> list[str]:
    """Keys to try at send time, most specific first — includes country/region even if disabled.

    Stored workflow variants must still match after a later config change (AC17).
    """
    today = today or date.today()
    cfg = _cfg(segmentation)
    ordered: list[str] = []

    def add(key: str | None) -> None:
        if key and key not in ordered:
            ordered.append(key)

    age = _years_since(getattr(account, "date_of_birth", None), today)
    bands = _age_bands(cfg) or AUDIENCES
    for dim in _priority(cfg):
        if dim == "region":
            add(region_key_of(account))
        elif dim == "country":
            add(country_key_of(account))
        elif dim == "age":
            for key, (_, years) in bands.items():
                if _in_range(age, years):
                    add(key)
            for key, (_, years) in OLDER_AUDIENCES.items():
                if _in_range(age, years):
                    add(key)
    # Ensure any remaining age keys are considered even if age was not in priority.
    for key, (_, years) in {**bands, **OLDER_AUDIENCES}.items():
        if _in_range(age, years):
            add(key)
    add(region_key_of(account))
    add(country_key_of(account))
    return ordered


def find_audiences(
    members: list,
    today: date | None = None,
    *,
    segmentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Who a workflow's messages are for. `members`: the workflow's eligible accounts.

    {"group": profile of all members,
     "audiences": [{"key", "label", "accounts", "profile", "dimension", "criteria"}] - biggest first,
     "main": {"accounts", "profile"} - who gets the main text, and their totals (it is written for them),
     "segmentation": the config snapshot used}

    An audience with min_audience_size+ accounts gets its own version (at most max_versions); everyone
    else gets the main text, which is also the fallback for anyone without a version. When fewer than
    min_audience_size accounts would be left for the main text, the biggest audience gets no version of
    its own: the main text is written for it (two texts for nearly the same people would only cost calls).

    Dimensions are listed independently (no age×country cross-product).
    """
    today = today or date.today()
    cfg = _cfg(segmentation)
    min_size, max_versions = _limits(cfg)
    dims = cfg.get("dimensions") or {}
    candidates: list[tuple[str, list, dict[str, Any]]] = []

    # --- age ---
    if (dims.get("age") or {}).get("enabled", True):
        bands = _age_bands(cfg) or AUDIENCES
        by_key: dict[str, list] = {}
        for account in members:
            key = audience_of(account, today, segmentation=cfg)
            if key:
                by_key.setdefault(key, []).append(account)
        order = list(bands)
        for key, accounts in sorted(by_key.items(), key=lambda item: (-len(item[1]), order.index(item[0]) if item[0] in order else 99)):
            label = audience_label(key) or key
            band = bands.get(key) or AUDIENCES.get(key)
            years = band[1] if band else (None, None)
            criteria = {
                "min_age": years[0],
                "max_age": None if years[1] is None else years[1] - 1,
            }
            candidates.append(
                (key, accounts, {"dimension": "age", "criteria": criteria, "label": label})
            )

    # --- country / region (optional; flat list, never age×country cross-product) ---
    for dim, key_of, label_of in (
        ("country", country_key_of, country_of),
        ("region", region_key_of, region_of),
    ):
        dim_cfg = dims.get(dim) or {}
        if not dim_cfg.get("enabled", False):
            continue
        allowed = {str(v).strip().casefold() for v in (dim_cfg.get("values") or []) if str(v).strip()}
        by_key: dict[str, list] = {}
        labels: dict[str, str] = {}
        for account in members:
            key = key_of(account)
            name = label_of(account)
            if not key or name == UNKNOWN:
                continue
            if allowed:
                code = str(getattr(account, "country_code", None) or "").strip().casefold()
                if name.casefold() not in allowed and key.casefold() not in allowed and code not in allowed:
                    continue
            by_key.setdefault(key, []).append(account)
            labels[key] = name
        for key, accounts in by_key.items():
            name = labels[key]
            display = f"{'Country' if dim == 'country' else 'Region'}: {name}"
            criteria: dict[str, Any] = {dim: name}
            if dim == "country":
                code = str(getattr(accounts[0], "country_code", None) or "").strip().upper() or None
                if code:
                    criteria["country_code"] = code
            candidates.append((key, accounts, {"dimension": dim, "criteria": criteria, "label": display}))

    # Rank by size; for age-only default keep prior tie-break (AUDIENCES order).
    def sort_key(item: tuple[str, list, dict[str, Any]]) -> tuple:
        key, accounts, meta = item
        size = -len(accounts)
        if meta["dimension"] == "age" and key in AUDIENCES:
            return (size, list(AUDIENCES).index(key), key)
        return (size, 99, key)

    ranked = sorted(candidates, key=sort_key)
    big = [(key, accounts, meta) for key, accounts, meta in ranked if len(accounts) >= min_size]

    # Preserve age-only main-text floor behaviour when only age is enabled.
    age_only = (dims.get("age") or {}).get("enabled", True) and not (dims.get("country") or {}).get(
        "enabled", False
    ) and not (dims.get("region") or {}).get("enabled", False)
    if age_only and big and len(members) - sum(len(accounts) for _, accounts, _ in big) < min_size:
        big = big[1:]

    chosen = big[:max_versions]
    in_a_version = {id(account) for _, accounts, _ in chosen for account in accounts}
    rest = [account for account in members if id(account) not in in_a_version]
    return {
        "group": audience_profile(members, today, segmentation=cfg),
        "audiences": [
            {
                "key": key,
                "label": meta["label"],
                "accounts": len(accounts),
                "profile": audience_profile(accounts, today, segmentation=cfg),
                "dimension": meta["dimension"],
                "criteria": meta["criteria"],
            }
            for key, accounts, meta in chosen
        ],
        "main": {
            "accounts": len(rest),
            "profile": audience_profile(rest, today, segmentation=cfg),
        },
        "segmentation": cfg,
    }


def audience_profile(
    accounts: list,
    today: date | None = None,
    *,
    segmentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Totals that describe `accounts`: shares in % of the accounts and averages - never one person."""
    if not accounts:
        return {"accounts": 0}
    today = today or date.today()
    balances = [float(account.outstanding_balance or 0) for account in accounts]
    days = [account.days_past_due for account in accounts if account.days_past_due is not None]
    bands = _age_bands(segmentation) or AUDIENCES
    return {
        "accounts": len(accounts),
        "age": _shares(
            accounts,
            lambda a: bands[key][0] if (key := audience_of(a, today, segmentation=segmentation)) and key in bands else UNKNOWN,
        ),
        "regions": _shares(accounts, region_of),
        "countries": _shares(accounts, country_of),
        "languages": _shares(accounts, lambda a: language_code(getattr(a, "language", None)) or UNKNOWN),
        "avg_balance": round(sum(balances) / len(balances), 2),
        "balance_bands": _shares(accounts, lambda a: _band(config.BALANCE_RANGES, a.outstanding_balance) or UNKNOWN),
        "partly_paid_pct": round(100 * sum(1 for a in accounts if (a.recovered_balance or 0) > 0) / len(accounts)),
        "avg_days_past_due": round(sum(days) / len(days)) if days else None,
    }


CONTACT_HISTORY = {
    "never_contacted": "Never contacted",
    "contacted_no_payment": "Contacted, no payment yet",
    "partly_paid": "Paid part",
}


def text_audiences(
    members: list,
    steps: list[dict],
    today: date | None = None,
    *,
    segmentation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Who gets each of a workflow's texts today: the main text and every audience version its messages have.

    `members`: the workflow's eligible accounts. A customer gets their age group's version when the messages have
    one (as the worker picks it), else the main text. [{"key", "label", "accounts", "profile"}] - the main text
    first, then the versions; each profile is audience_profile plus reach_profile. Totals only, never one person.
    """
    today = today or date.today()
    keys = {
        key
        for step in steps
        if step.get("kind") == "Communication"
        for key in ((step.get("config") or {}).get("versions") or {})
    }
    known = set(AUDIENCES) | set(OLDER_AUDIENCES) | keys
    groups: dict[str, list] = {MAIN_KEY: [], **{key: [] for key in known if key in keys}}
    for account in members:
        matched = next(
            (key for key in audience_keys(account, today, segmentation=segmentation) if key in groups),
            MAIN_KEY,
        )
        groups[matched].append(account)
    return [
        {
            "key": key,
            "label": audience_label(key),
            "accounts": len(accounts),
            "profile": {**audience_profile(accounts, today, segmentation=segmentation), **reach_profile(accounts)},
        }
        for key, accounts in groups.items()
    ]


def reach_profile(accounts: list) -> dict[str, Any]:
    """How `accounts` can be reached and what happened before, in % of the accounts ({} without accounts)."""
    if not accounts:
        return {}
    total = len(accounts)
    return {
        "with_email_pct": round(100 * sum(1 for a in accounts if str(a.email or "").strip()) / total),
        "with_mobile_pct": round(100 * sum(1 for a in accounts if str(a.phone_mobile or "").strip()) / total),
        "contact_history": _shares(accounts, lambda a: CONTACT_HISTORY[contact_state_of(a)]),
    }


def members_by_segment(accounts: list, segments: list[dict], today: date | None = None) -> dict[str, list]:
    """Suggest: each segment's own accounts - every eligible account in the most specific segment it fits
    (the same accounts the segment's stats count)."""
    eligible, _ = filter_eligible(accounts)
    members: dict[str, list] = {segment["segment_id"]: [] for segment in segments}
    for account in eligible:
        best = match_account_to_segment(account, segments, today)
        if best:
            members[best["segment_id"]].append(account)
    return members


def region_of(account) -> str:
    """Province / state name (a Canadian code is spelled out), else the region of the postal code."""
    for field in ("province_state", "region"):
        value = str(getattr(account, field, None) or "").strip()
        if value:
            return PROVINCES.get(value.upper(), value)
    postal = str(getattr(account, "postal_code", None) or "").strip().upper()
    for label, letters in config.POSTAL_REGION_PREFIXES.items():
        if postal and postal[0] in letters:
            return label.split(" (")[0]
    return UNKNOWN


def country_of(account) -> str:
    code = str(getattr(account, "country_code", None) or "").strip().upper()
    return COUNTRIES.get(code, code) if code else UNKNOWN


def _shares(accounts: list, label_of: Callable[[Any], str]) -> dict[str, int]:
    """{label: % of the accounts}, most accounts first."""
    counts = Counter(label_of(account) for account in accounts)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {label: round(100 * count / len(accounts)) for label, count in ranked}
