"""Collection SMS via Twilio REST API. Always writes sms_logs; log-only when creds empty."""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import SmsLogModel

logger = logging.getLogger("app.sms.twilio")


def _configured() -> bool:
    return bool(
        (settings.TWILIO_ACCOUNT_SID or "").strip()
        and (settings.TWILIO_AUTH_TOKEN or "").strip()
        and (settings.TWILIO_FROM_NUMBER or "").strip()
    )


def _try_send(*, to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    if not _configured():
        return False, "twilio_not_configured", None
    sid = settings.TWILIO_ACCOUNT_SID.strip()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                data={
                    "To": to_phone,
                    "From": settings.TWILIO_FROM_NUMBER.strip(),
                    "Body": body,
                },
                auth=(sid, settings.TWILIO_AUTH_TOKEN.strip()),
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            return True, None, data.get("sid")
        return False, f"twilio_http_{resp.status_code}: {resp.text[:400]}", None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Twilio send failed to=%s", to_phone)
        return False, str(exc)[:500], None


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
    status = "logged"
    err: str | None = None
    provider_id: str | None = None
    if _configured():
        ok, err, provider_id = _try_send(to_phone=to_phone, body=body)
        status = "sent" if ok else "failed"

    row = SmsLogModel(
        to_phone=to_phone,
        body=body,
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
    logger.info("COLLECTION_SMS %s id=%s to=%s type=%s", status, row.id, to_phone, sms_type)
    return row
