"""Channel-free AI "contact" Communication steps.

AI writes both email (subject/body) and SMS (sms_body) and leaves channel unset.
The worker picks Email or SMS per customer, switches when one fails, and flags
accounts it cannot reach ("No contactable channel").
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowAccountModel
from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
from app.modules.payflow.services.workflow_condition_runtime import apply_outcome_to_account
from app.modules.payflow.services.workflow_suggestion.config import CONTACT_CHANNEL

# The message templates in other languages: {"FR": {template_id: {"subject", "body"}}}. Written by
# workflow_suggestion/template_translations.py (AI-translated and checked); read once per process.
TEMPLATE_TRANSLATIONS_PATH = Path(__file__).with_name("message_template_translations.json")

# Soft blocks: temporary / governance — do not mark the customer unreachable.
_SOFT_REASON_HINTS = (
    "send window",
    "outside allowed",
    "deferred",
    "frequency",
    "prevent communication",
    "provincial hold",
    "on hold",
)


def is_contact_step(cfg: dict[str, Any], step: dict[str, Any] | None = None) -> bool:
    """True when the step has no fixed channel (AI contact) or is labeled Email + SMS."""
    step = step or {}
    raw = cfg.get("channel") if cfg.get("channel") is not None else step.get("channel")
    channel = str(raw or "").strip()
    return (not channel) or channel == CONTACT_CHANNEL


def preferred_contact_channels(
    db: Session,
    account: PayflowAccountModel,
) -> list[str]:
    """Eligible channels first (Email preferred); if none eligible, still try both for audit."""
    from app.modules.payflow.services.communication_service import (
        PayflowCommunicationService,
    )

    svc = PayflowCommunicationService(db)
    email_ok = bool(svc.evaluate_send_eligibility(account, channel="Email").get("ok"))
    sms_ok = bool(svc.evaluate_send_eligibility(account, channel="SMS").get("ok"))
    ordered: list[str] = []
    if email_ok:
        ordered.append("Email")
    if sms_ok:
        ordered.append("SMS")
    if ordered:
        return ordered
    return ["Email", "SMS"]


def template_for(purpose: str | None, channel: str) -> dict[str, Any] | None:
    purpose_norm = (purpose or "").strip()
    channel_norm = (channel or "Email").strip()
    match = next(
        (
            t
            for t in MESSAGE_TEMPLATES
            if t.get("channel") == channel_norm and t.get("purpose") == purpose_norm
        ),
        None,
    )
    if match:
        return match
    # Fallbacks when purpose has no template on that channel.
    if channel_norm == "SMS":
        return next((t for t in MESSAGE_TEMPLATES if t.get("id") == "sms-reminder"), None)
    return next((t for t in MESSAGE_TEMPLATES if t.get("id") == "em-reminder"), None)


@lru_cache(maxsize=1)
def template_translations() -> dict[str, dict[str, dict[str, str]]]:
    try:
        return json.loads(TEMPLATE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def localized_template(template: dict[str, Any] | None, language: str | None) -> dict[str, Any] | None:
    """The template in the customer's language when there is a translation for it, else as it is."""
    if not template:
        return template
    version = template_translations().get((language or "").strip().upper(), {}).get(template.get("id"))
    if not isinstance(version, dict):
        return template
    return {**template, **{key: version[key] for key in ("subject", "body") if version.get(key)}}


def localized_message_config(cfg: dict[str, Any], language: str | None) -> dict[str, Any]:
    """The step's config with its text in the customer's language, when the step has a version for it.

    `cfg["translations"]`: {"FR": {"subject", "body", "sms_body"}, ...} keyed by account language code.
    No version for the language → the step's main text. A version missing a part (e.g. no SMS text)
    keeps the main text for that part.
    """
    versions = cfg.get("translations")
    version = versions.get((language or "").strip().upper()) if isinstance(versions, dict) else None
    if not isinstance(version, dict):
        return cfg
    return {**cfg, **{key: version[key] for key in ("subject", "body", "sms_body") if version.get(key)}}


def message_for_channel(
    cfg: dict[str, Any],
    *,
    channel: str,
    purpose: str | None,
    language: str | None = None,
) -> tuple[str, str]:
    """Return (subject, body) for the chosen channel — contact steps use sms_body for SMS.
    `language`: the customer's, for the template used when the step has no text of its own."""
    channel_norm = (channel or "Email").strip()
    template = localized_template(template_for(purpose, channel_norm), language)
    if channel_norm == "SMS":
        body = (cfg.get("sms_body") or (template or {}).get("body") or "").strip()
        return "", body
    subject = (
        cfg.get("subject") or (template or {}).get("subject") or (purpose or "Payment Reminder")
    )
    body = cfg.get("body") or (template or {}).get("body") or ""
    return str(subject), str(body)


def send_succeeded(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    if result.get("skipped") or result.get("provider_error"):
        return False
    return str(result.get("status") or "").lower() in {"sent", "logged", "queued"}


def is_soft_failure(result: dict[str, Any] | None) -> bool:
    """True when failure is temporary (window / hold / governance), not 'unreachable'."""
    if not result or send_succeeded(result):
        return False
    if result.get("provider_error"):
        return False
    reason = str(result.get("restriction_reason") or result.get("skipped") or "").lower()
    return any(hint in reason for hint in _SOFT_REASON_HINTS)


def should_flag_unreachable(attempts: list[dict[str, Any]]) -> bool:
    if not attempts:
        return True
    if any(send_succeeded(a) for a in attempts):
        return False
    # Only soft failures → do not brand the customer unreachable.
    if attempts and all(is_soft_failure(a) for a in attempts):
        return False
    return True


def flag_unreachable(account: PayflowAccountModel) -> dict[str, Any]:
    applied = apply_outcome_to_account(account, "No contactable channel")
    # Mid-flow contact failure is a flag, not an Outcome step.
    account.last_action = "No contactable channel"
    return applied
