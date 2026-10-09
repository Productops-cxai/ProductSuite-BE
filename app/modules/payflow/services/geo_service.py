"""Server-side geo lookups.

Countries are served from a bundled catalog (no outbound call) so the Add Client
form always loads. Provinces / cities prefer countriesnow.space via httpx
(avoids browser CORS), with bundled CA/US fallbacks and soft failures so the
form stays editable when the provider is down or CRM data is messy.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.exceptions import ValidationAppError

_STATES_URL = "https://countriesnow.space/api/v0.1/countries/states"
_CITIES_URL = "https://countriesnow.space/api/v0.1/countries/state/cities"
_COUNTRY_CITIES_URL = "https://countriesnow.space/api/v0.1/countries/cities"

# name, iso2, currencies, languages — enough for address / locale defaults.
_COUNTRIES: list[dict[str, Any]] = [
    {"name": "Afghanistan", "iso2": "AF", "currencies": ["AFN"], "languages": ["Dari", "Pashto"]},
    {"name": "Albania", "iso2": "AL", "currencies": ["ALL"], "languages": ["Albanian"]},
    {"name": "Algeria", "iso2": "DZ", "currencies": ["DZD"], "languages": ["Arabic"]},
    {"name": "Andorra", "iso2": "AD", "currencies": ["EUR"], "languages": ["Catalan"]},
    {"name": "Angola", "iso2": "AO", "currencies": ["AOA"], "languages": ["Portuguese"]},
    {"name": "Argentina", "iso2": "AR", "currencies": ["ARS"], "languages": ["Spanish"]},
    {"name": "Armenia", "iso2": "AM", "currencies": ["AMD"], "languages": ["Armenian"]},
    {"name": "Australia", "iso2": "AU", "currencies": ["AUD"], "languages": ["English"]},
    {"name": "Austria", "iso2": "AT", "currencies": ["EUR"], "languages": ["German"]},
    {"name": "Azerbaijan", "iso2": "AZ", "currencies": ["AZN"], "languages": ["Azerbaijani"]},
    {"name": "Bahamas", "iso2": "BS", "currencies": ["BSD"], "languages": ["English"]},
    {"name": "Bahrain", "iso2": "BH", "currencies": ["BHD"], "languages": ["Arabic", "English"]},
    {"name": "Bangladesh", "iso2": "BD", "currencies": ["BDT"], "languages": ["Bengali", "English"]},
    {"name": "Barbados", "iso2": "BB", "currencies": ["BBD"], "languages": ["English"]},
    {"name": "Belarus", "iso2": "BY", "currencies": ["BYN"], "languages": ["Belarusian", "Russian"]},
    {"name": "Belgium", "iso2": "BE", "currencies": ["EUR"], "languages": ["Dutch", "French", "German"]},
    {"name": "Belize", "iso2": "BZ", "currencies": ["BZD"], "languages": ["English"]},
    {"name": "Benin", "iso2": "BJ", "currencies": ["XOF"], "languages": ["French"]},
    {"name": "Bhutan", "iso2": "BT", "currencies": ["BTN", "INR"], "languages": ["Dzongkha"]},
    {"name": "Bolivia", "iso2": "BO", "currencies": ["BOB"], "languages": ["Spanish"]},
    {"name": "Bosnia and Herzegovina", "iso2": "BA", "currencies": ["BAM"], "languages": ["Bosnian", "Croatian", "Serbian"]},
    {"name": "Botswana", "iso2": "BW", "currencies": ["BWP"], "languages": ["English"]},
    {"name": "Brazil", "iso2": "BR", "currencies": ["BRL"], "languages": ["Portuguese"]},
    {"name": "Brunei", "iso2": "BN", "currencies": ["BND"], "languages": ["Malay", "English"]},
    {"name": "Bulgaria", "iso2": "BG", "currencies": ["BGN"], "languages": ["Bulgarian"]},
    {"name": "Cambodia", "iso2": "KH", "currencies": ["KHR"], "languages": ["Khmer"]},
    {"name": "Cameroon", "iso2": "CM", "currencies": ["XAF"], "languages": ["French", "English"]},
    {"name": "Canada", "iso2": "CA", "currencies": ["CAD"], "languages": ["English", "French"]},
    {"name": "Chile", "iso2": "CL", "currencies": ["CLP"], "languages": ["Spanish"]},
    {"name": "China", "iso2": "CN", "currencies": ["CNY"], "languages": ["Chinese"]},
    {"name": "Colombia", "iso2": "CO", "currencies": ["COP"], "languages": ["Spanish"]},
    {"name": "Costa Rica", "iso2": "CR", "currencies": ["CRC"], "languages": ["Spanish"]},
    {"name": "Croatia", "iso2": "HR", "currencies": ["EUR"], "languages": ["Croatian"]},
    {"name": "Cuba", "iso2": "CU", "currencies": ["CUP"], "languages": ["Spanish"]},
    {"name": "Cyprus", "iso2": "CY", "currencies": ["EUR"], "languages": ["Greek", "Turkish"]},
    {"name": "Czech Republic", "iso2": "CZ", "currencies": ["CZK"], "languages": ["Czech"]},
    {"name": "Denmark", "iso2": "DK", "currencies": ["DKK"], "languages": ["Danish"]},
    {"name": "Dominican Republic", "iso2": "DO", "currencies": ["DOP"], "languages": ["Spanish"]},
    {"name": "Ecuador", "iso2": "EC", "currencies": ["USD"], "languages": ["Spanish"]},
    {"name": "Egypt", "iso2": "EG", "currencies": ["EGP"], "languages": ["Arabic", "English"]},
    {"name": "El Salvador", "iso2": "SV", "currencies": ["USD"], "languages": ["Spanish"]},
    {"name": "Estonia", "iso2": "EE", "currencies": ["EUR"], "languages": ["Estonian"]},
    {"name": "Ethiopia", "iso2": "ET", "currencies": ["ETB"], "languages": ["Amharic"]},
    {"name": "Finland", "iso2": "FI", "currencies": ["EUR"], "languages": ["Finnish", "Swedish"]},
    {"name": "France", "iso2": "FR", "currencies": ["EUR"], "languages": ["French"]},
    {"name": "Georgia", "iso2": "GE", "currencies": ["GEL"], "languages": ["Georgian"]},
    {"name": "Germany", "iso2": "DE", "currencies": ["EUR"], "languages": ["German"]},
    {"name": "Ghana", "iso2": "GH", "currencies": ["GHS"], "languages": ["English"]},
    {"name": "Greece", "iso2": "GR", "currencies": ["EUR"], "languages": ["Greek"]},
    {"name": "Guatemala", "iso2": "GT", "currencies": ["GTQ"], "languages": ["Spanish"]},
    {"name": "Honduras", "iso2": "HN", "currencies": ["HNL"], "languages": ["Spanish"]},
    {"name": "Hong Kong", "iso2": "HK", "currencies": ["HKD"], "languages": ["Chinese", "English"]},
    {"name": "Hungary", "iso2": "HU", "currencies": ["HUF"], "languages": ["Hungarian"]},
    {"name": "Iceland", "iso2": "IS", "currencies": ["ISK"], "languages": ["Icelandic"]},
    {"name": "India", "iso2": "IN", "currencies": ["INR"], "languages": ["Hindi", "English"]},
    {"name": "Indonesia", "iso2": "ID", "currencies": ["IDR"], "languages": ["Indonesian"]},
    {"name": "Iran", "iso2": "IR", "currencies": ["IRR"], "languages": ["Persian"]},
    {"name": "Iraq", "iso2": "IQ", "currencies": ["IQD"], "languages": ["Arabic", "Kurdish"]},
    {"name": "Ireland", "iso2": "IE", "currencies": ["EUR"], "languages": ["English", "Irish"]},
    {"name": "Israel", "iso2": "IL", "currencies": ["ILS"], "languages": ["Hebrew", "Arabic"]},
    {"name": "Italy", "iso2": "IT", "currencies": ["EUR"], "languages": ["Italian"]},
    {"name": "Jamaica", "iso2": "JM", "currencies": ["JMD"], "languages": ["English"]},
    {"name": "Japan", "iso2": "JP", "currencies": ["JPY"], "languages": ["Japanese"]},
    {"name": "Jordan", "iso2": "JO", "currencies": ["JOD"], "languages": ["Arabic", "English"]},
    {"name": "Kazakhstan", "iso2": "KZ", "currencies": ["KZT"], "languages": ["Kazakh", "Russian"]},
    {"name": "Kenya", "iso2": "KE", "currencies": ["KES"], "languages": ["English", "Swahili"]},
    {"name": "Kuwait", "iso2": "KW", "currencies": ["KWD"], "languages": ["Arabic", "English"]},
    {"name": "Latvia", "iso2": "LV", "currencies": ["EUR"], "languages": ["Latvian"]},
    {"name": "Lebanon", "iso2": "LB", "currencies": ["LBP"], "languages": ["Arabic", "French"]},
    {"name": "Libya", "iso2": "LY", "currencies": ["LYD"], "languages": ["Arabic"]},
    {"name": "Lithuania", "iso2": "LT", "currencies": ["EUR"], "languages": ["Lithuanian"]},
    {"name": "Luxembourg", "iso2": "LU", "currencies": ["EUR"], "languages": ["French", "German", "Luxembourgish"]},
    {"name": "Macau", "iso2": "MO", "currencies": ["MOP"], "languages": ["Chinese", "Portuguese"]},
    {"name": "Malaysia", "iso2": "MY", "currencies": ["MYR"], "languages": ["Malay", "English"]},
    {"name": "Maldives", "iso2": "MV", "currencies": ["MVR"], "languages": ["Dhivehi"]},
    {"name": "Malta", "iso2": "MT", "currencies": ["EUR"], "languages": ["Maltese", "English"]},
    {"name": "Mexico", "iso2": "MX", "currencies": ["MXN"], "languages": ["Spanish"]},
    {"name": "Morocco", "iso2": "MA", "currencies": ["MAD"], "languages": ["Arabic", "French"]},
    {"name": "Myanmar", "iso2": "MM", "currencies": ["MMK"], "languages": ["Burmese"]},
    {"name": "Nepal", "iso2": "NP", "currencies": ["NPR"], "languages": ["Nepali"]},
    {"name": "Netherlands", "iso2": "NL", "currencies": ["EUR"], "languages": ["Dutch", "English"]},
    {"name": "New Zealand", "iso2": "NZ", "currencies": ["NZD"], "languages": ["English", "Māori"]},
    {"name": "Nigeria", "iso2": "NG", "currencies": ["NGN"], "languages": ["English"]},
    {"name": "Norway", "iso2": "NO", "currencies": ["NOK"], "languages": ["Norwegian"]},
    {"name": "Oman", "iso2": "OM", "currencies": ["OMR"], "languages": ["Arabic", "English"]},
    {"name": "Pakistan", "iso2": "PK", "currencies": ["PKR"], "languages": ["Urdu", "English"]},
    {"name": "Panama", "iso2": "PA", "currencies": ["PAB", "USD"], "languages": ["Spanish"]},
    {"name": "Paraguay", "iso2": "PY", "currencies": ["PYG"], "languages": ["Spanish", "Guarani"]},
    {"name": "Peru", "iso2": "PE", "currencies": ["PEN"], "languages": ["Spanish"]},
    {"name": "Philippines", "iso2": "PH", "currencies": ["PHP"], "languages": ["Filipino", "English"]},
    {"name": "Poland", "iso2": "PL", "currencies": ["PLN"], "languages": ["Polish"]},
    {"name": "Portugal", "iso2": "PT", "currencies": ["EUR"], "languages": ["Portuguese"]},
    {"name": "Puerto Rico", "iso2": "PR", "currencies": ["USD"], "languages": ["Spanish", "English"]},
    {"name": "Qatar", "iso2": "QA", "currencies": ["QAR"], "languages": ["Arabic", "English"]},
    {"name": "Romania", "iso2": "RO", "currencies": ["RON"], "languages": ["Romanian"]},
    {"name": "Russia", "iso2": "RU", "currencies": ["RUB"], "languages": ["Russian"]},
    {"name": "Saudi Arabia", "iso2": "SA", "currencies": ["SAR"], "languages": ["Arabic", "English"]},
    {"name": "Senegal", "iso2": "SN", "currencies": ["XOF"], "languages": ["French"]},
    {"name": "Serbia", "iso2": "RS", "currencies": ["RSD"], "languages": ["Serbian"]},
    {"name": "Singapore", "iso2": "SG", "currencies": ["SGD"], "languages": ["English", "Mandarin", "Malay", "Tamil"]},
    {"name": "Slovakia", "iso2": "SK", "currencies": ["EUR"], "languages": ["Slovak"]},
    {"name": "Slovenia", "iso2": "SI", "currencies": ["EUR"], "languages": ["Slovenian"]},
    {"name": "South Africa", "iso2": "ZA", "currencies": ["ZAR"], "languages": ["English", "Afrikaans", "Zulu", "Xhosa"]},
    {"name": "South Korea", "iso2": "KR", "currencies": ["KRW"], "languages": ["Korean"]},
    {"name": "Spain", "iso2": "ES", "currencies": ["EUR"], "languages": ["Spanish"]},
    {"name": "Sri Lanka", "iso2": "LK", "currencies": ["LKR"], "languages": ["Sinhala", "Tamil", "English"]},
    {"name": "Sweden", "iso2": "SE", "currencies": ["SEK"], "languages": ["Swedish"]},
    {"name": "Switzerland", "iso2": "CH", "currencies": ["CHF"], "languages": ["German", "French", "Italian"]},
    {"name": "Taiwan", "iso2": "TW", "currencies": ["TWD"], "languages": ["Chinese"]},
    {"name": "Thailand", "iso2": "TH", "currencies": ["THB"], "languages": ["Thai"]},
    {"name": "Tunisia", "iso2": "TN", "currencies": ["TND"], "languages": ["Arabic", "French"]},
    {"name": "Turkey", "iso2": "TR", "currencies": ["TRY"], "languages": ["Turkish"]},
    {"name": "Ukraine", "iso2": "UA", "currencies": ["UAH"], "languages": ["Ukrainian"]},
    {"name": "United Arab Emirates", "iso2": "AE", "currencies": ["AED"], "languages": ["Arabic", "English"]},
    {"name": "United Kingdom", "iso2": "GB", "currencies": ["GBP"], "languages": ["English"]},
    {"name": "United States", "iso2": "US", "currencies": ["USD"], "languages": ["English", "Spanish"]},
    {"name": "Uruguay", "iso2": "UY", "currencies": ["UYU"], "languages": ["Spanish"]},
    {"name": "Uzbekistan", "iso2": "UZ", "currencies": ["UZS"], "languages": ["Uzbek"]},
    {"name": "Venezuela", "iso2": "VE", "currencies": ["VES"], "languages": ["Spanish"]},
    {"name": "Vietnam", "iso2": "VN", "currencies": ["VND"], "languages": ["Vietnamese"]},
    {"name": "Yemen", "iso2": "YE", "currencies": ["YER"], "languages": ["Arabic"]},
    {"name": "Zambia", "iso2": "ZM", "currencies": ["ZMW"], "languages": ["English"]},
    {"name": "Zimbabwe", "iso2": "ZW", "currencies": ["ZWL", "USD"], "languages": ["English"]},
]

# Offline fallbacks when the remote provider is unreachable / incomplete.
_FALLBACK_STATES: dict[str, list[str]] = {
    "canada": [
        "Alberta",
        "British Columbia",
        "Manitoba",
        "New Brunswick",
        "Newfoundland and Labrador",
        "Northwest Territories",
        "Nova Scotia",
        "Nunavut",
        "Ontario",
        "Prince Edward Island",
        "Quebec",
        "Saskatchewan",
        "Yukon",
    ],
    "united states": [
        "Alabama",
        "Alaska",
        "Arizona",
        "Arkansas",
        "California",
        "Colorado",
        "Connecticut",
        "Delaware",
        "District of Columbia",
        "Florida",
        "Georgia",
        "Hawaii",
        "Idaho",
        "Illinois",
        "Indiana",
        "Iowa",
        "Kansas",
        "Kentucky",
        "Louisiana",
        "Maine",
        "Maryland",
        "Massachusetts",
        "Michigan",
        "Minnesota",
        "Mississippi",
        "Missouri",
        "Montana",
        "Nebraska",
        "Nevada",
        "New Hampshire",
        "New Jersey",
        "New Mexico",
        "New York",
        "North Carolina",
        "North Dakota",
        "Ohio",
        "Oklahoma",
        "Oregon",
        "Pennsylvania",
        "Rhode Island",
        "South Carolina",
        "South Dakota",
        "Tennessee",
        "Texas",
        "Utah",
        "Vermont",
        "Virginia",
        "Washington",
        "West Virginia",
        "Wisconsin",
        "Wyoming",
    ],
}

# Common CRM / ISO aliases → catalog country name.
_COUNTRY_ALIASES: dict[str, str] = {
    "ca": "Canada",
    "can": "Canada",
    "canada": "Canada",
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
}

# Province / state aliases (CRM often stores codes or city names in the wrong field).
_STATE_ALIASES: dict[str, dict[str, str]] = {
    "canada": {
        "ab": "Alberta",
        "bc": "British Columbia",
        "mb": "Manitoba",
        "nb": "New Brunswick",
        "nl": "Newfoundland and Labrador",
        "nf": "Newfoundland and Labrador",
        "nt": "Northwest Territories",
        "ns": "Nova Scotia",
        "nu": "Nunavut",
        "on": "Ontario",
        "ont": "Ontario",
        "ontario": "Ontario",
        "pe": "Prince Edward Island",
        "pei": "Prince Edward Island",
        "qc": "Quebec",
        "que": "Quebec",
        "quebec": "Quebec",
        "sk": "Saskatchewan",
        "yt": "Yukon",
        "yk": "Yukon",
    },
    "united states": {
        "al": "Alabama",
        "ak": "Alaska",
        "az": "Arizona",
        "ar": "Arkansas",
        "ca": "California",
        "co": "Colorado",
        "ct": "Connecticut",
        "de": "Delaware",
        "dc": "District of Columbia",
        "fl": "Florida",
        "ga": "Georgia",
        "hi": "Hawaii",
        "id": "Idaho",
        "il": "Illinois",
        "in": "Indiana",
        "ia": "Iowa",
        "ks": "Kansas",
        "ky": "Kentucky",
        "la": "Louisiana",
        "me": "Maine",
        "md": "Maryland",
        "ma": "Massachusetts",
        "mi": "Michigan",
        "mn": "Minnesota",
        "ms": "Mississippi",
        "mo": "Missouri",
        "mt": "Montana",
        "ne": "Nebraska",
        "nv": "Nevada",
        "nh": "New Hampshire",
        "nj": "New Jersey",
        "nm": "New Mexico",
        "ny": "New York",
        "nc": "North Carolina",
        "nd": "North Dakota",
        "oh": "Ohio",
        "ok": "Oklahoma",
        "or": "Oregon",
        "pa": "Pennsylvania",
        "ri": "Rhode Island",
        "sc": "South Carolina",
        "sd": "South Dakota",
        "tn": "Tennessee",
        "tx": "Texas",
        "ut": "Utah",
        "vt": "Vermont",
        "va": "Virginia",
        "wa": "Washington",
        "wv": "West Virginia",
        "wi": "Wisconsin",
        "wy": "Wyoming",
    },
}

_states_cache: dict[str, list[str]] = {}
_cities_cache: dict[str, list[str]] = {}


def _key(*parts: str) -> str:
    return "||".join(p.strip().lower() for p in parts)


def _normalize_country(country: str) -> str:
    raw = (country or "").strip()
    if not raw:
        return ""
    alias = _COUNTRY_ALIASES.get(raw.lower())
    if alias:
        return alias
    for row in _COUNTRIES:
        if row["name"].lower() == raw.lower() or row["iso2"].lower() == raw.lower():
            return row["name"]
    return raw


def _normalize_state(country_name: str, state: str) -> str:
    raw = (state or "").strip()
    if not raw:
        return ""
    aliases = _STATE_ALIASES.get(country_name.lower()) or {}
    mapped = aliases.get(raw.lower())
    if mapped:
        return mapped
    return raw


def _http_post(url: str, payload: dict[str, Any]) -> Any | None:
    """Return JSON body or None on transport / HTTP failure (soft)."""
    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            res = client.post(url, json=payload)
            res.raise_for_status()
            return res.json()
    except Exception:  # noqa: BLE001
        return None


class PayflowGeoService:
    def list_countries(self) -> dict[str, Any]:
        return {"countries": list(_COUNTRIES)}

    def list_states(self, country: str) -> dict[str, Any]:
        country_name = _normalize_country(country)
        if not country_name:
            raise ValidationAppError("country is required")
        cache_key = _key("state", country_name)
        if cache_key in _states_cache:
            return {"country": country_name, "states": _states_cache[cache_key]}

        states: list[str] = []
        body = _http_post(_STATES_URL, {"country": country_name})
        if body and not body.get("error"):
            states = sorted(
                {
                    (s.get("name") or "").strip()
                    for s in ((body.get("data") or {}).get("states") or [])
                    if (s.get("name") or "").strip()
                },
                key=str.lower,
            )

        if not states:
            states = list(_FALLBACK_STATES.get(country_name.lower()) or [])

        _states_cache[cache_key] = states
        return {"country": country_name, "states": states}

    def list_cities(self, country: str, state: str) -> dict[str, Any]:
        country_name = _normalize_country(country)
        state_name = _normalize_state(country_name, state)
        if not country_name:
            raise ValidationAppError("country is required")
        if not state_name:
            raise ValidationAppError("state is required")
        cache_key = _key("city", country_name, state_name)
        if cache_key in _cities_cache:
            return {
                "country": country_name,
                "state": state_name,
                "cities": _cities_cache[cache_key],
            }

        cities: list[str] = []
        body = _http_post(
            _CITIES_URL, {"country": country_name, "state": state_name}
        )
        if body and not body.get("error"):
            cities = sorted(
                {str(c).strip() for c in (body.get("data") or []) if str(c).strip()},
                key=str.lower,
            )

        # CRM sometimes stores a city in the province field — fall back to all
        # cities for the country so the dropdown still has usable options.
        if not cities:
            body = _http_post(_COUNTRY_CITIES_URL, {"country": country_name})
            if body and not body.get("error"):
                cities = sorted(
                    {
                        str(c).strip()
                        for c in (body.get("data") or [])
                        if str(c).strip()
                    },
                    key=str.lower,
                )

        # Soft success even when empty — FE allows free-text edit.
        _cities_cache[cache_key] = cities
        return {"country": country_name, "state": state_name, "cities": cities}
