"""Which languages a workflow's messages are written in — code, no AI.

The main text is in the client's language. Every other language found among the accounts of the
workflow's segment gets its own version (config.translations[code]), whatever languages the backend
lets accounts have: nothing here limits them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.modules.payflow.services.strategy_service import is_follow_up_step

# Names the AI is told, by account language code (ISO 639-1). Any other code still works: the AI is
# then given the code itself.
LANGUAGE_NAMES = {
    "EN": "English", "FR": "French", "ES": "Spanish", "PT": "Portuguese", "IT": "Italian", "DE": "German",
    "NL": "Dutch", "PL": "Polish", "RU": "Russian", "UK": "Ukrainian", "EL": "Greek", "ZH": "Chinese",
    "JA": "Japanese", "KO": "Korean", "VI": "Vietnamese", "TL": "Tagalog", "HI": "Hindi", "PA": "Punjabi",
    "UR": "Urdu", "BN": "Bengali", "GU": "Gujarati", "TA": "Tamil", "AR": "Arabic", "FA": "Persian",
}
_CODES_BY_NAME = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
NO_LANGUAGE = {"", "UNKNOWN", "NONE"}  # segment stats count accounts without a language as "UNKNOWN"


def language_code(value: Any) -> str | None:
    """"FR", "fr" or "French" → "FR"; empty / unknown → None."""
    text = str(value or "").strip()
    if text.upper() in NO_LANGUAGE:
        return None
    return _CODES_BY_NAME.get(text.lower(), text.upper())


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, f"the language with code {code}")


def extra_languages(client_language: Any, codes: list[Any]) -> list[str]:
    """`codes` without the client's own language (its text is the main text) and without "no language"."""
    main = language_code(client_language) or "EN"
    result: list[str] = []
    for value in codes:
        code = language_code(value)
        if code and code != main and code not in result:
            result.append(code)
    return result


def segment_stats_languages(stats: dict[str, Any]) -> list[str]:
    """Language codes of a Suggest segment (its stats count them), most accounts first."""
    counts = stats.get("languages") or {}
    return [code for code, _ in sorted(counts.items(), key=lambda item: -item[1])]


def accounts_languages(accounts: list, fits) -> list[str]:
    """Language codes of the accounts for which `fits(account)` is true, most accounts first."""
    counts = Counter(code for account in accounts if fits(account) and (code := language_code(account.language)))
    return [code for code, _ in counts.most_common()]


def languages_line(
    client_language: Any, written: list[str], failed: list[str], follow_ups_missing: list[str] | None = None
) -> list[dict[str, str]]:
    """The "Languages" ai_context line for reviewers ([] when the messages have only the main language).
    `follow_ups_missing`: written languages the adaptive follow-ups have no version in."""
    if not written and not failed:
        return []
    main = language_name(language_code(client_language) or "EN")
    value = ", ".join([f"{main} (main)", *(language_name(code) for code in written)])
    if failed:
        names = ", ".join(language_name(code) for code in failed)
        value += f"; not written: {names} - these customers get {main} (use Write messages to try again)"
    if follow_ups_missing:
        names = ", ".join(language_name(code) for code in follow_ups_missing)
        value += f"; follow-ups not written in {names} - these customers get the planned messages instead"
    return [{"label": "Languages", "value": value}]


def languages_line_for_steps(client_language: Any, languages: list[str], steps: list[dict]) -> list[dict[str, str]]:
    """The "Languages" line read from the steps themselves: a language counts as written when every message of
    the flowchart with text has a version in it (after a rewrite or an edit only some may have); the adaptive
    follow-ups are told apart (a language they lack only means no follow-up in it)."""
    messages = [
        step
        for step in steps
        if step.get("kind") == "Communication"
        and any((step.get("config") or {}).get(key) for key in ("subject", "body", "sms_body"))
    ]
    follow_ups = [step for step in messages if is_follow_up_step(step)]
    flowchart = [step for step in messages if not is_follow_up_step(step)] or follow_ups
    if not flowchart:
        return []

    def everywhere(code: str, group: list[dict]) -> bool:
        return all(code in ((step.get("config") or {}).get("translations") or {}) for step in group)

    done = [code for code in languages if everywhere(code, flowchart)]
    missing = [code for code in done if not everywhere(code, follow_ups)]
    return languages_line(client_language, done, [code for code in languages if code not in done], missing)
