"""Twilio SMS status + inbound (STOP) webhooks."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.communication_service import PayflowCommunicationService

logger = logging.getLogger("app.comms.twilio_webhook")

router = APIRouter(tags=["Webhooks · Twilio"])


def _verify_twilio_signature(url: str, params: dict[str, Any], signature: str | None) -> bool:
    """Validate X-Twilio-Signature when Twilio auth token is configured.

    Empty TWILIO_AUTH_TOKEN → accept (local/dev, matches SendGrid empty-key pattern).
    """
    token = (settings.TWILIO_AUTH_TOKEN or "").strip()
    if not token:
        return True
    if not signature:
        return False
    try:
        from hashlib import sha1
        import hmac
        import base64

        # Twilio: sort params, append to URL, HMAC-SHA1 with auth token, base64.
        pieces = []
        for key in sorted(params.keys()):
            values = params[key]
            if isinstance(values, list):
                for v in values:
                    pieces.append(f"{key}{v}")
            else:
                pieces.append(f"{key}{values}")
        payload = url + "".join(pieces)
        digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), sha1).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)
    except Exception:  # noqa: BLE001
        logger.exception("Twilio signature verification failed")
        return False


def _form_params(raw: bytes) -> dict[str, list[str]]:
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v for k, v in parsed.items()}


def _first(params: dict[str, list[str]], key: str) -> str | None:
    vals = params.get(key) or []
    return vals[0] if vals else None


@router.post("/webhooks/twilio/status")
async def twilio_status_webhook(request: Request):
    raw = await request.body()
    params = _form_params(raw)
    # Prefer configured public base so signature matches what Twilio signed.
    public = (settings.PUBLIC_API_BASE or "").strip().rstrip("/")
    if public:
        url = f"{public}/webhooks/twilio/status"
    else:
        url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature")
    if not _verify_twilio_signature(url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio webhook signature")

    message_sid = _first(params, "MessageSid") or _first(params, "SmsSid")
    message_status = (_first(params, "MessageStatus") or _first(params, "SmsStatus") or "").lower()
    if not message_sid or not message_status:
        return {"ok": True, "matched": False, "reason": "missing_fields"}

    error_code = _first(params, "ErrorCode")
    error_message = _first(params, "ErrorMessage")
    occurred_at = datetime.now(timezone.utc)
    provider_event_id = f"twilio:{message_sid}:{message_status}"

    db: Session = SessionLocal()
    try:
        svc = PayflowCommunicationService(db)
        result = svc.apply_provider_event(
            event_type=message_status,
            occurred_at=occurred_at,
            provider_message_id=message_sid,
            provider_event_id=provider_event_id,
            payload={
                "MessageSid": message_sid,
                "MessageStatus": message_status,
                "ErrorCode": error_code,
                "ErrorMessage": error_message,
                "To": _first(params, "To"),
                "From": _first(params, "From"),
            },
            commit=True,
        )
        return {"ok": True, **result}
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Twilio status webhook processing failed")
        raise HTTPException(status_code=500, detail="Webhook processing failed") from None
    finally:
        db.close()


@router.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(request: Request):
    raw = await request.body()
    params = _form_params(raw)
    public = (settings.PUBLIC_API_BASE or "").strip().rstrip("/")
    if public:
        url = f"{public}/webhooks/twilio/inbound"
    else:
        url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature")
    if not _verify_twilio_signature(url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio webhook signature")

    from_phone = _first(params, "From")
    body = (_first(params, "Body") or "").strip().upper()
    opt_out_tokens = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
    is_opt_out = any(tok == body or body.startswith(tok + " ") for tok in opt_out_tokens)

    if not is_opt_out or not from_phone:
        # Empty TwiML so Twilio does not retry.
        return {"ok": True, "opt_out": False}

    db: Session = SessionLocal()
    try:
        svc = PayflowCommunicationService(db)
        updated = svc.mark_sms_opt_out_by_phone(from_phone, commit=True)
        return {"ok": True, "opt_out": True, "accounts_updated": updated}
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Twilio inbound webhook processing failed")
        raise HTTPException(status_code=500, detail="Webhook processing failed") from None
    finally:
        db.close()
