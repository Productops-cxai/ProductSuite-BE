"""Stripe Checkout / PaymentIntent webhook — signature verify + attempt updates."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.payment_portal_service import PayflowPaymentPortalService
from app.modules.payflow.services.stripe_checkout_service import verify_and_parse_webhook

logger = logging.getLogger("app.payflow.stripe_webhook")

router = APIRouter(tags=["Webhooks · Stripe"])


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("Stripe-Signature") or request.headers.get("stripe-signature")
    try:
        event = verify_and_parse_webhook(raw, sig)
    except ValueError as exc:
        logger.warning("stripe webhook rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = SessionLocal()
    try:
        result = PayflowPaymentPortalService(db).handle_stripe_event(event)
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("stripe webhook processing failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Webhook processing failed") from exc
    finally:
        db.close()
