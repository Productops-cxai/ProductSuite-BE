"""Stripe Checkout session create + webhook signature verification.

Uses the official `stripe` package when available; falls back to httpx + HMAC
so local environments without the package installed still work for mock mode.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("app.payflow.stripe")


def stripe_configured() -> bool:
    return bool((settings.STRIPE_SECRET_KEY or "").strip())


def create_checkout_session(
    *,
    amount: float,
    currency: str,
    success_url: str,
    cancel_url: str,
    client_reference_id: str,
    metadata: dict[str, str],
    brand_name: str | None = None,
    customer_email: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session. Amount is major currency units (e.g. 12.50)."""
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    if not secret:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")

    cents = max(1, int(round(float(amount) * 100)))
    currency_norm = (currency or "USD").strip().lower()
    product_name = f"{brand_name or 'PayFlow'} payment"

    try:
        import stripe

        stripe.api_key = secret
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=client_reference_id[:200],
            customer_email=customer_email or None,
            line_items=[
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency_norm,
                        "unit_amount": cents,
                        "product_data": {"name": product_name[:120]},
                    },
                }
            ],
            metadata={k: str(v)[:500] for k, v in (metadata or {}).items()},
        )
        return {
            "id": session.id,
            "url": session.url,
            "payment_intent": getattr(session, "payment_intent", None),
            "raw": dict(session) if hasattr(session, "keys") else {"id": session.id},
        }
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK checkout create failed; trying httpx")

    # httpx fallback (form-encoded Stripe API)
    data: list[tuple[str, str]] = [
        ("mode", "payment"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("client_reference_id", client_reference_id[:200]),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", currency_norm),
        ("line_items[0][price_data][unit_amount]", str(cents)),
        ("line_items[0][price_data][product_data][name]", product_name[:120]),
    ]
    if customer_email:
        data.append(("customer_email", customer_email))
    for i, (k, v) in enumerate((metadata or {}).items()):
        data.append((f"metadata[{k}]", str(v)[:500]))

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            auth=(secret, ""),
        )
    if res.status_code >= 400:
        detail = res.text[:500]
        raise RuntimeError(f"Stripe Checkout create failed: {detail}")
    payload = res.json()
    return {
        "id": payload.get("id"),
        "url": payload.get("url"),
        "payment_intent": payload.get("payment_intent"),
        "raw": payload,
    }


def verify_and_parse_webhook(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Verify Stripe-Signature and return event dict. Empty secret → accept (local/dev)."""
    secret = (settings.STRIPE_WEBHOOK_SECRET or "").strip()
    if not secret:
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid webhook JSON") from exc

    if not sig_header:
        raise ValueError("Missing Stripe-Signature header")

    try:
        import stripe

        event = stripe.Webhook.construct_event(payload, sig_header, secret)
        return dict(event) if hasattr(event, "keys") else event.to_dict()  # type: ignore[attr-defined]
    except ImportError:
        pass
    except Exception as exc:
        # Fall through to manual verify; re-raise if that also fails.
        logger.debug("stripe SDK webhook verify failed: %s", exc)

    _verify_signature_manual(payload, sig_header, secret)
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid webhook JSON") from exc


def _verify_signature_manual(payload: bytes, sig_header: str, secret: str) -> None:
    parts = {}
    for item in sig_header.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts.setdefault(k.strip(), []).append(v.strip())
    timestamp = (parts.get("t") or [None])[0]
    signatures = parts.get("v1") or []
    if not timestamp or not signatures:
        raise ValueError("Malformed Stripe-Signature")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise ValueError("Invalid Stripe timestamp") from exc
    if abs(time.time() - ts) > 300:
        raise ValueError("Stripe webhook timestamp outside tolerance")
    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise ValueError("Stripe webhook signature mismatch")
