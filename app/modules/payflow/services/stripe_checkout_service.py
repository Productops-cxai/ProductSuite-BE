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
    brand_logo_url: str | None = None,
    brand_subtitle: str | None = None,
    customer_email: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session. Amount is major currency units (e.g. 12.50)."""
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    if not secret:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")

    cents = max(1, int(round(float(amount) * 100)))
    currency_norm = (currency or "USD").strip().lower()
    product_name = f"{brand_name or 'PayFlow'} payment"
    meta = {k: str(v)[:500] for k, v in (metadata or {}).items()}
    logo = (brand_logo_url or "").strip()
    product_images = [logo] if logo.startswith(("http://", "https://")) else []
    submit_msg = None
    if brand_subtitle:
        submit_msg = f"Payment on behalf of {brand_subtitle}"[:120]

    try:
        import stripe

        stripe.api_key = secret
        product_data: dict[str, Any] = {"name": product_name[:120]}
        if product_images:
            product_data["images"] = product_images[:8]
        session_kwargs: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": client_reference_id[:200],
            "customer_email": customer_email or None,
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency_norm,
                        "unit_amount": cents,
                        "product_data": product_data,
                    },
                }
            ],
            "metadata": meta,
            # Copy metadata onto PaymentIntent so payment_intent.* webhooks match.
            "payment_intent_data": {"metadata": meta},
        }
        if submit_msg:
            session_kwargs["custom_text"] = {
                "submit": {"message": submit_msg},
            }
        session = stripe.checkout.Session.create(**session_kwargs)
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
    for i, img in enumerate(product_images[:8]):
        data.append((f"line_items[0][price_data][product_data][images][{i}]", img))
    if submit_msg:
        data.append(("custom_text[submit][message]", submit_msg))
    for k, v in meta.items():
        data.append((f"metadata[{k}]", v))
        data.append((f"payment_intent_data[metadata][{k}]", v))

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


def retrieve_checkout_session(session_id: str) -> dict[str, Any] | None:
    """Fetch Checkout Session from Stripe (reconcile stuck Processing attempts)."""
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    sid = (session_id or "").strip()
    if not secret or not sid:
        return None
    try:
        import stripe

        stripe.api_key = secret
        session = stripe.checkout.Session.retrieve(sid)
        return dict(session) if hasattr(session, "keys") else session.to_dict()  # type: ignore[attr-defined]
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK session retrieve failed session=%s", sid)
        return None
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{sid}",
                auth=(secret, ""),
            )
        if res.status_code >= 400:
            logger.warning("stripe session retrieve HTTP %s: %s", res.status_code, res.text[:200])
            return None
        return res.json()
    except Exception:
        logger.exception("stripe httpx session retrieve failed session=%s", sid)
        return None


def retrieve_payment_intent(payment_intent_id: str) -> dict[str, Any] | None:
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    pi = (payment_intent_id or "").strip()
    if not secret or not pi:
        return None
    try:
        import stripe

        stripe.api_key = secret
        intent = stripe.PaymentIntent.retrieve(pi)
        return dict(intent) if hasattr(intent, "keys") else intent.to_dict()  # type: ignore[attr-defined]
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK PI retrieve failed pi=%s", pi)
        return None
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(
                f"https://api.stripe.com/v1/payment_intents/{pi}",
                auth=(secret, ""),
            )
        if res.status_code >= 400:
            return None
        return res.json()
    except Exception:
        logger.exception("stripe httpx PI retrieve failed pi=%s", pi)
        return None


def verify_and_parse_webhook(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Verify Stripe-Signature and return event dict.

    Production requires STRIPE_WEBHOOK_SECRET. Dev/local may accept unsigned payloads
    when the secret is unset (mock/testing only).
    """
    secret = (settings.STRIPE_WEBHOOK_SECRET or "").strip()
    env = (settings.APP_ENV or "").strip().lower()
    if not secret:
        if env == "production":
            raise ValueError("STRIPE_WEBHOOK_SECRET is required in production")
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
