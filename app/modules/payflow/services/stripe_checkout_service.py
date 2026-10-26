"""Stripe Checkout session create + off-session charges + webhook verify.

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

# Card + US bank (ACH) for portal + saved-method auto-charge.
DEFAULT_PAYMENT_METHOD_TYPES = ["card", "us_bank_account"]


def stripe_configured() -> bool:
    return bool((settings.STRIPE_SECRET_KEY or "").strip())


def _secret() -> str:
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    if not secret:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    return secret


def _norm_meta(metadata: dict[str, str] | None) -> dict[str, str]:
    return {k: str(v)[:500] for k, v in (metadata or {}).items()}


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
    save_payment_method: bool = False,
    payment_method_types: list[str] | None = None,
    stripe_customer_id: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session (mode=payment). Amount is major units."""
    secret = _secret()
    cents = max(1, int(round(float(amount) * 100)))
    currency_norm = (currency or "USD").strip().lower()
    product_name = f"{brand_name or 'PayFlow'} payment"
    meta = _norm_meta(metadata)
    logo = (brand_logo_url or "").strip()
    product_images = [logo] if logo.startswith(("http://", "https://")) else []
    submit_msg = None
    if brand_subtitle:
        submit_msg = f"Payment on behalf of {brand_subtitle}"[:120]
    pm_types = payment_method_types or DEFAULT_PAYMENT_METHOD_TYPES

    try:
        import stripe

        stripe.api_key = secret
        product_data: dict[str, Any] = {"name": product_name[:120]}
        if product_images:
            product_data["images"] = product_images[:8]
        pi_data: dict[str, Any] = {"metadata": meta}
        if save_payment_method:
            pi_data["setup_future_usage"] = "off_session"
        session_kwargs: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": client_reference_id[:200],
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
            "payment_intent_data": pi_data,
            "payment_method_types": pm_types,
        }
        if stripe_customer_id:
            session_kwargs["customer"] = stripe_customer_id
        else:
            if customer_email:
                session_kwargs["customer_email"] = customer_email
            if save_payment_method:
                session_kwargs["customer_creation"] = "always"
        if submit_msg:
            session_kwargs["custom_text"] = {"submit": {"message": submit_msg}}
        session = stripe.checkout.Session.create(**session_kwargs)
        return {
            "id": session.id,
            "url": session.url,
            "mode": "payment",
            "payment_intent": getattr(session, "payment_intent", None),
            "setup_intent": getattr(session, "setup_intent", None),
            "customer": getattr(session, "customer", None),
            "raw": dict(session) if hasattr(session, "keys") else {"id": session.id},
        }
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK checkout create failed; trying httpx")

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
    for i, pm in enumerate(pm_types):
        data.append((f"payment_method_types[{i}]", pm))
    if stripe_customer_id:
        data.append(("customer", stripe_customer_id))
    else:
        if customer_email:
            data.append(("customer_email", customer_email))
        if save_payment_method:
            data.append(("customer_creation", "always"))
    for i, img in enumerate(product_images[:8]):
        data.append((f"line_items[0][price_data][product_data][images][{i}]", img))
    if submit_msg:
        data.append(("custom_text[submit][message]", submit_msg))
    for k, v in meta.items():
        data.append((f"metadata[{k}]", v))
        data.append((f"payment_intent_data[metadata][{k}]", v))
    if save_payment_method:
        data.append(("payment_intent_data[setup_future_usage]", "off_session"))

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            auth=(secret, ""),
        )
    if res.status_code >= 400:
        raise RuntimeError(f"Stripe Checkout create failed: {res.text[:500]}")
    payload = res.json()
    return {
        "id": payload.get("id"),
        "url": payload.get("url"),
        "mode": "payment",
        "payment_intent": payload.get("payment_intent"),
        "setup_intent": payload.get("setup_intent"),
        "customer": payload.get("customer"),
        "raw": payload,
    }


def create_setup_checkout_session(
    *,
    currency: str,
    success_url: str,
    cancel_url: str,
    client_reference_id: str,
    metadata: dict[str, str],
    brand_name: str | None = None,
    brand_subtitle: str | None = None,
    customer_email: str | None = None,
    payment_method_types: list[str] | None = None,
    stripe_customer_id: str | None = None,
) -> dict[str, Any]:
    """Checkout mode=setup — save Card/Bank for later off-session charges (no charge now)."""
    secret = _secret()
    currency_norm = (currency or "USD").strip().lower()
    meta = _norm_meta(metadata)
    pm_types = payment_method_types or DEFAULT_PAYMENT_METHOD_TYPES
    submit_msg = None
    if brand_subtitle:
        submit_msg = f"Save payment method for {brand_name or brand_subtitle}"[:120]

    try:
        import stripe

        stripe.api_key = secret
        session_kwargs: dict[str, Any] = {
            "mode": "setup",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": client_reference_id[:200],
            "metadata": meta,
            "payment_method_types": pm_types,
            "currency": currency_norm,
            "setup_intent_data": {
                "metadata": meta,
            },
        }
        if stripe_customer_id:
            session_kwargs["customer"] = stripe_customer_id
        elif customer_email:
            session_kwargs["customer_email"] = customer_email
        if submit_msg:
            session_kwargs["custom_text"] = {"submit": {"message": submit_msg}}
        session = stripe.checkout.Session.create(**session_kwargs)
        return {
            "id": session.id,
            "url": session.url,
            "mode": "setup",
            "payment_intent": None,
            "setup_intent": getattr(session, "setup_intent", None),
            "customer": getattr(session, "customer", None),
            "raw": dict(session) if hasattr(session, "keys") else {"id": session.id},
        }
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK setup checkout failed; trying httpx")

    data: list[tuple[str, str]] = [
        ("mode", "setup"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("client_reference_id", client_reference_id[:200]),
        ("currency", currency_norm),
    ]
    for i, pm in enumerate(pm_types):
        data.append((f"payment_method_types[{i}]", pm))
    if stripe_customer_id:
        data.append(("customer", stripe_customer_id))
    elif customer_email:
        data.append(("customer_email", customer_email))
    if submit_msg:
        data.append(("custom_text[submit][message]", submit_msg))
    for k, v in meta.items():
        data.append((f"metadata[{k}]", v))
        data.append((f"setup_intent_data[metadata][{k}]", v))

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            auth=(secret, ""),
        )
    if res.status_code >= 400:
        raise RuntimeError(f"Stripe setup Checkout create failed: {res.text[:500]}")
    payload = res.json()
    return {
        "id": payload.get("id"),
        "url": payload.get("url"),
        "mode": "setup",
        "payment_intent": None,
        "setup_intent": payload.get("setup_intent"),
        "customer": payload.get("customer"),
        "raw": payload,
    }


def create_off_session_payment_intent(
    *,
    amount: float,
    currency: str,
    customer_id: str,
    payment_method_id: str,
    metadata: dict[str, str],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Charge a saved payment method off-session (worker auto-deduct)."""
    secret = _secret()
    cents = max(1, int(round(float(amount) * 100)))
    currency_norm = (currency or "USD").strip().lower()
    meta = _norm_meta(metadata)
    cust = (customer_id or "").strip()
    pm = (payment_method_id or "").strip()
    if not cust or not pm:
        raise RuntimeError("stripe_customer_id and stripe_payment_method_id are required")

    try:
        import stripe

        stripe.api_key = secret
        kwargs: dict[str, Any] = {
            "amount": cents,
            "currency": currency_norm,
            "customer": cust,
            "payment_method": pm,
            "off_session": True,
            "confirm": True,
            "metadata": meta,
        }
        request_opts: dict[str, Any] = {}
        if idempotency_key:
            request_opts["idempotency_key"] = idempotency_key[:255]
        intent = stripe.PaymentIntent.create(**kwargs, **request_opts)
        return {
            "id": intent.id,
            "status": getattr(intent, "status", None),
            "customer": getattr(intent, "customer", None),
            "payment_method": getattr(intent, "payment_method", None),
            "raw": dict(intent) if hasattr(intent, "keys") else {"id": intent.id},
        }
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK off-session PI create failed; trying httpx")

    data: list[tuple[str, str]] = [
        ("amount", str(cents)),
        ("currency", currency_norm),
        ("customer", cust),
        ("payment_method", pm),
        ("off_session", "true"),
        ("confirm", "true"),
    ]
    for k, v in meta.items():
        data.append((f"metadata[{k}]", v))
    headers: dict[str, str] = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:255]

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            "https://api.stripe.com/v1/payment_intents",
            data=data,
            auth=(secret, ""),
            headers=headers,
        )
    if res.status_code >= 400:
        raise RuntimeError(f"Stripe off-session PaymentIntent failed: {res.text[:500]}")
    payload = res.json()
    return {
        "id": payload.get("id"),
        "status": payload.get("status"),
        "customer": payload.get("customer"),
        "payment_method": payload.get("payment_method"),
        "raw": payload,
    }


def extract_session_payment_method(session_payload: dict[str, Any] | None) -> dict[str, str | None]:
    """Pull customer + payment_method (+ type) from an expanded Checkout Session."""
    session = session_payload or {}
    customer = session.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    pm = session.get("payment_method")
    pm_type = None
    if isinstance(pm, dict):
        pm_type = pm.get("type")
        pm = pm.get("id")
    # payment mode often exposes PM on payment_intent
    if not pm:
        pi = session.get("payment_intent")
        if isinstance(pi, dict):
            pm = pi.get("payment_method")
            if isinstance(pm, dict):
                pm_type = pm.get("type") or pm_type
                pm = pm.get("id")
            if not customer:
                customer = pi.get("customer")
                if isinstance(customer, dict):
                    customer = customer.get("id")
    # setup mode exposes PM on setup_intent
    if not pm:
        si = session.get("setup_intent")
        if isinstance(si, dict):
            pm = si.get("payment_method")
            if isinstance(pm, dict):
                pm_type = pm.get("type") or pm_type
                pm = pm.get("id")
            if not customer:
                customer = si.get("customer")
                if isinstance(customer, dict):
                    customer = customer.get("id")
    return {
        "customer_id": str(customer) if customer else None,
        "payment_method_id": str(pm) if pm else None,
        "payment_method_type": str(pm_type) if pm_type else None,
    }


def retrieve_checkout_session(
    session_id: str,
    *,
    expand: list[str] | None = None,
) -> dict[str, Any] | None:
    """Fetch Checkout Session from Stripe (reconcile stuck Processing attempts)."""
    secret = (settings.STRIPE_SECRET_KEY or "").strip()
    sid = (session_id or "").strip()
    if not secret or not sid:
        return None
    expand_fields = expand or [
        "payment_intent",
        "payment_intent.payment_method",
        "setup_intent",
        "setup_intent.payment_method",
    ]
    try:
        import stripe

        stripe.api_key = secret
        session = stripe.checkout.Session.retrieve(sid, expand=expand_fields)
        return dict(session) if hasattr(session, "keys") else session.to_dict()  # type: ignore[attr-defined]
    except ImportError:
        pass
    except Exception:
        logger.exception("stripe SDK session retrieve failed session=%s", sid)
        return None
    try:
        params = [("expand[]", f) for f in expand_fields]
        with httpx.Client(timeout=20.0) as client:
            res = client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{sid}",
                params=params,
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
        intent = stripe.PaymentIntent.retrieve(pi, expand=["payment_method"])
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
                params=[("expand[]", "payment_method")],
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
