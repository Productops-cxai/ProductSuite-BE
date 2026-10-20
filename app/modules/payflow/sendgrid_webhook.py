"""SendGrid Event Webhook — signature verify + map events to communications."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.communication_service import PayflowCommunicationService

logger = logging.getLogger("app.comms.sendgrid_webhook")

router = APIRouter(tags=["Webhooks · SendGrid"])


def _verify_signature(raw_body: bytes, signature: str | None, timestamp: str | None) -> bool:
    """Verify SendGrid signed Event Webhook when verification key is configured.

    Empty SENDGRID_WEBHOOK_VERIFICATION_KEY → accept (local/dev). Set the key from
    SendGrid Event Webhook settings for production.
    """
    key = (settings.SENDGRID_WEBHOOK_VERIFICATION_KEY or "").strip()
    if not key:
        return True
    if not signature or not timestamp:
        return False
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.exceptions import InvalidSignature

        # SendGrid provides a public key; accept PEM or raw base64 DER.
        pem = key
        if "BEGIN PUBLIC KEY" not in key:
            der = base64.b64decode(key)
            pem_bytes = (
                b"-----BEGIN PUBLIC KEY-----\n"
                + base64.encodebytes(der)
                + b"-----END PUBLIC KEY-----\n"
            )
            public_key = load_pem_public_key(pem_bytes)
        else:
            public_key = load_pem_public_key(pem.encode("utf-8"))

        payload = timestamp.encode("utf-8") + raw_body
        sig = base64.b64decode(signature)
        public_key.verify(sig, payload, ec.ECDSA(hashes.SHA256()))  # type: ignore[attr-defined]
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001
        logger.exception("SendGrid webhook signature verification failed")
        return False


def _parse_event_time(ev: dict[str, Any]) -> datetime | None:
    ts = ev.get("timestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _custom_arg(ev: dict[str, Any], key: str) -> str | None:
    # SendGrid may nest custom args at top level or under unique_args / custom_args.
    for bag in (ev, ev.get("unique_args") or {}, ev.get("custom_args") or {}):
        if isinstance(bag, dict) and bag.get(key) is not None:
            return str(bag.get(key))
    return None


@router.post("/webhooks/sendgrid")
async def sendgrid_event_webhook(
    request: Request,
    x_twilio_email_event_webhook_signature: str | None = Header(default=None),
    x_twilio_email_event_webhook_timestamp: str | None = Header(default=None),
):
    raw = await request.body()
    if not _verify_signature(
        raw,
        x_twilio_email_event_webhook_signature,
        x_twilio_email_event_webhook_timestamp,
    ):
        raise HTTPException(status_code=401, detail="Invalid SendGrid webhook signature")

    try:
        events = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if not isinstance(events, list):
        events = [events]

    db: Session = SessionLocal()
    processed = 0
    duplicates = 0
    unmatched = 0
    try:
        svc = PayflowCommunicationService(db)
        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_type = str(ev.get("event") or "").lower()
            if not event_type:
                continue
            cid_raw = _custom_arg(ev, "communication_id")
            code = _custom_arg(ev, "communication_code")
            try:
                cid = int(cid_raw) if cid_raw else None
            except ValueError:
                cid = None
            provider_message_id = (
                ev.get("sg_message_id") or ev.get("smtp-id") or ev.get("sg_message_id")
            )
            if isinstance(provider_message_id, str) and "." in provider_message_id:
                # SendGrid often appends .filter; keep prefix for match flexibility.
                provider_message_id = provider_message_id.split(".", 1)[0]
            provider_event_id = None
            if ev.get("sg_event_id"):
                provider_event_id = str(ev.get("sg_event_id"))
            elif ev.get("sg_message_id") and event_type:
                provider_event_id = f"{ev.get('sg_message_id')}:{event_type}:{ev.get('timestamp')}"

            result = svc.apply_provider_event(
                event_type=event_type,
                occurred_at=_parse_event_time(ev),
                communication_id=cid,
                communication_code=code,
                provider_message_id=str(provider_message_id) if provider_message_id else None,
                provider_event_id=provider_event_id,
                url=ev.get("url"),
                payload={k: ev.get(k) for k in ("email", "reason", "status", "url", "useragent") if k in ev},
                commit=False,
            )
            if result.get("duplicate"):
                duplicates += 1
            elif result.get("matched"):
                processed += 1
            else:
                unmatched += 1
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("SendGrid webhook processing failed")
        raise HTTPException(status_code=500, detail="Webhook processing failed") from None
    finally:
        db.close()

    return {
        "ok": True,
        "processed": processed,
        "duplicates": duplicates,
        "unmatched": unmatched,
    }
