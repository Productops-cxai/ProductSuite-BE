"""SendGrid domain authentication (whitelabel) — uses a client API key."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("app.email.sendgrid_domain_auth")

_BASE = "https://api.sendgrid.com/v3"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }


def _normalize_dns_records(raw: dict[str, Any] | None) -> list[dict[str, str]]:
    """Flatten SendGrid dns object into UI-friendly rows."""
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, str]] = []
    for kind, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        out.append(
            {
                "type": str(meta.get("type") or "cname"),
                "host": str(meta.get("host") or ""),
                "data": str(meta.get("data") or ""),
                "valid": str(meta.get("valid") or ""),
                "kind": str(kind),
            }
        )
    return out


def list_domains(api_key: str) -> list[dict[str, Any]]:
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(f"{_BASE}/whitelabel/domains", headers=_headers(api_key))
    if resp.status_code != 200:
        raise RuntimeError(f"sendgrid_list_domains_{resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data if isinstance(data, list) else []


def find_domain(api_key: str, domain: str) -> dict[str, Any] | None:
    domain_l = (domain or "").strip().lower()
    for row in list_domains(api_key):
        if str(row.get("domain") or "").strip().lower() == domain_l:
            return row
    return None


def create_domain(api_key: str, domain: str) -> dict[str, Any]:
    payload = {
        "domain": (domain or "").strip().lower(),
        "automatic_security": True,
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"{_BASE}/whitelabel/domains",
            headers=_headers(api_key),
            json=payload,
        )
    if resp.status_code not in (200, 201):
        # Already exists → fetch
        if resp.status_code in (400, 409):
            existing = find_domain(api_key, domain)
            if existing:
                return existing
        raise RuntimeError(f"sendgrid_create_domain_{resp.status_code}: {resp.text[:400]}")
    return resp.json()


def get_domain(api_key: str, domain_id: str | int) -> dict[str, Any]:
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            f"{_BASE}/whitelabel/domains/{domain_id}",
            headers=_headers(api_key),
        )
    if resp.status_code != 200:
        raise RuntimeError(f"sendgrid_get_domain_{resp.status_code}: {resp.text[:300]}")
    return resp.json()


def validate_domain(api_key: str, domain_id: str | int) -> dict[str, Any]:
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"{_BASE}/whitelabel/domains/{domain_id}/validate",
            headers=_headers(api_key),
        )
    if resp.status_code != 200:
        raise RuntimeError(f"sendgrid_validate_domain_{resp.status_code}: {resp.text[:300]}")
    return resp.json()


def ensure_domain_auth(api_key: str, domain: str) -> dict[str, Any]:
    """Create or reuse domain auth; return normalized status + DNS records."""
    domain_l = (domain or "").strip().lower()
    if not domain_l or "." not in domain_l:
        raise ValueError("Invalid domain")

    row = find_domain(api_key, domain_l)
    if row is None:
        row = create_domain(api_key, domain_l)

    domain_id = row.get("id")
    dns = _normalize_dns_records(row.get("dns") if isinstance(row.get("dns"), dict) else None)
    valid = bool(row.get("valid"))
    return {
        "sendgrid_domain_id": str(domain_id) if domain_id is not None else None,
        "domain": domain_l,
        "valid": valid,
        "dns_records": dns,
        "raw": row,
    }


def refresh_domain_status(api_key: str, sendgrid_domain_id: str) -> dict[str, Any]:
    """Validate + re-fetch domain; return status snapshot."""
    validation = validate_domain(api_key, sendgrid_domain_id)
    row = get_domain(api_key, sendgrid_domain_id)
    dns = _normalize_dns_records(row.get("dns") if isinstance(row.get("dns"), dict) else None)
    valid = bool(row.get("valid")) or bool((validation or {}).get("valid"))
    return {
        "sendgrid_domain_id": str(sendgrid_domain_id),
        "domain": str(row.get("domain") or ""),
        "valid": valid,
        "dns_records": dns,
        "validation": validation,
        "raw": row,
    }
