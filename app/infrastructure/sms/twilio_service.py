"""Collection SMS via Twilio REST API. Always writes sms_logs; log-only when creds empty."""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import SmsLogModel

logger = logging.getLogger("app.sms.twilio")

_PHONE_DIGITS_RE = re.compile(r"\D+")
# GSM-7 single-segment ≈ 160; UCS-2 single-segment ≈ 70. Cap conservatively for safety.
_MAX_SMS_CHARS = 1600


def _configured() -> bool:
    return bool(
        (settings.TWILIO_ACCOUNT_SID or "").strip()
        and (settings.TWILIO_AUTH_TOKEN or "").strip()
        and (settings.TWILIO_FROM_NUMBER or "").strip()
    )


def normalize_e164(raw: str | None) -> str | None:
    """Normalize to +E.164. NANP 10-digit → +1XXXXXXXXXX."""
    if not raw:
        return None
    text = str(raw).strip()
    if text.startswith("+"):
        digits = _PHONE_DIGITS_RE.sub("", text)
        if 10 <= len(digits) <= 15:
            return f"+{digits}"
        return None
    digits = _PHONE_DIGITS_RE.sub("", text)
    if not digits:
        return None
    if len(digits) == 10:
        digits = f"1{digits}"
    if len(digits) < 10 or len(digits) > 15:
        return None
    return f"+{digits}"


def truncate_sms_body(body: str) -> str:
    text = body or ""
    if len(text) <= _MAX_SMS_CHARS:
        return text
    return text[: _MAX_SMS_CHARS - 1].rstrip() + "…"


def _status_callback_url() -> str | None:
    base = (settings.PUBLIC_API_BASE or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/webhooks/twilio/status"


def _try_send(*, to_phone: str, body: str) -> tuple[bool, str | None, str | None, str | None]:
    """Returns (ok, error, provider_sid, twilio_status)."""
    if not _configured():
        return False, "twilio_not_configured", None, None
    sid = settings.TWILIO_ACCOUNT_SID.strip()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data: dict[str, str] = {
        "To": to_phone,
        "From": settings.TWILIO_FROM_NUMBER.strip(),
        "Body": body,
    }
    callback = _status_callback_url()
    if callback:
        data["StatusCallback"] = callback
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                data=data,
                auth=(sid, settings.TWILIO_AUTH_TOKEN.strip()),
            )
        if resp.status_code in (200, 201):
            payload = resp.json()
            return True, None, payload.get("sid"), payload.get("status")
        return False, f"twilio_http_{resp.status_code}: {resp.text[:400]}", None, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Twilio send failed to=%s", to_phone)
        return False, str(exc)[:500], None, None


def send_collection_sms(
    db: Session,
    *,
    to_phone: str,
    body: str,
    sms_type: str = "collection_workflow",
    account_id: Optional[int] = None,
    run_id: Optional[int] = None,
    client_id: Optional[int] = None,
    commit: bool = True,
) -> SmsLogModel:
    normalized = normalize_e164(to_phone) or (to_phone or "").strip()
    safe_body = truncate_sms_body(body)
    status = "logged"
    err: str | None = None
    provider_id: str | None = None
    provider_status: str | None = None
    if _configured():
        if not normalize_e164(to_phone):
            status = "failed"
            err = "invalid_phone_number"
        else:
            ok, err, provider_id, provider_status = _try_send(
                to_phone=normalized, body=safe_body
            )
            # Twilio often returns queued/accepted immediately — treat as sent for sms_logs.
            if ok:
                status = "queued" if (provider_status or "").lower() in {
                    "queued",
                    "accepted",
                } else "sent"
            else:
                status = "failed"

    row = SmsLogModel(
        to_phone=normalized,
        body=safe_body,
        sms_type=sms_type,
        status=status,
        provider_id=provider_id,
        error=err,
        account_id=account_id,
        run_id=run_id,
        client_id=client_id,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    logger.info("COLLECTION_SMS %s id=%s to=%s type=%s", status, row.id, normalized, sms_type)
    return row
