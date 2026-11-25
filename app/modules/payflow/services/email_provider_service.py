"""Per-client SendGrid account credentials (encrypted at rest)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationAppError
from app.core.secrets_crypto import SecretsCryptoError, decrypt_with_key, encrypt_value
from app.infrastructure.database.models import (
    PayflowClientEmailProviderModel,
    PayflowClientModel,
)

logger = logging.getLogger("app.payflow.email_provider")


def encrypt_client_secret(plain: str) -> str:
    """Encrypt with ENV_MASTER_KEY (same as enc: .env secrets)."""
    return encrypt_value(plain)


def decrypt_client_secret(enc: str | None) -> str | None:
    if not enc:
        return None
    return decrypt_with_key(enc)


class PayflowEmailProviderService:
    def __init__(self, db: Session):
        self.db = db

    def _get_client(self, client_id: int) -> PayflowClientModel:
        client = self.db.get(PayflowClientModel, client_id)
        if client is None:
            raise NotFoundError("Client not found")
        return client

    def get_provider_row(self, client_id: int) -> PayflowClientEmailProviderModel | None:
        return (
            self.db.query(PayflowClientEmailProviderModel)
            .filter(PayflowClientEmailProviderModel.client_id == client_id)
            .first()
        )

    def get_status(self, client_id: int) -> dict[str, Any]:
        self._get_client(client_id)
        row = self.get_provider_row(client_id)
        if row is None:
            return {
                "configured": False,
                "provider": None,
                "is_active": False,
                "label": None,
                "has_api_key": False,
                "has_webhook_verification_key": False,
                "last_checked_at": None,
                "last_error": None,
            }
        return {
            "configured": True,
            "provider": row.provider,
            "is_active": bool(row.is_active),
            "label": row.label,
            "has_api_key": bool((row.api_key_enc or "").strip()),
            "has_webhook_verification_key": bool(
                (row.webhook_verification_key_enc or "").strip()
            ),
            "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
            "last_error": row.last_error,
        }

    def upsert(
        self,
        client_id: int,
        *,
        api_key: str | None = None,
        webhook_verification_key: str | None = None,
        label: str | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        self._get_client(client_id)
        row = self.get_provider_row(client_id)
        creating = row is None
        if creating:
            if not (api_key or "").strip():
                raise ValidationAppError("api_key is required when connecting SendGrid")
            row = PayflowClientEmailProviderModel(
                client_id=client_id,
                provider="sendgrid",
                api_key_enc=encrypt_client_secret(api_key.strip()),
                is_active=True,
            )
            self.db.add(row)

        if api_key is not None and api_key.strip():
            row.api_key_enc = encrypt_client_secret(api_key.strip())
        if webhook_verification_key is not None:
            stripped = webhook_verification_key.strip()
            row.webhook_verification_key_enc = (
                encrypt_client_secret(stripped) if stripped else None
            )
        if label is not None:
            row.label = (label.strip() or None)
        if is_active is not None:
            row.is_active = bool(is_active)

        row.last_error = None
        self.db.commit()
        self.db.refresh(row)
        logger.info(
            "client_email_provider_%s client_id=%s provider_id=%s",
            "created" if creating else "updated",
            client_id,
            row.id,
        )
        return self.get_status(client_id)

    def test_connection(self, client_id: int) -> dict[str, Any]:
        self._get_client(client_id)
        row = self.get_provider_row(client_id)
        if row is None or not row.is_active:
            raise ValidationAppError("SendGrid is not configured for this client")
        try:
            api_key = decrypt_client_secret(row.api_key_enc)
        except SecretsCryptoError as exc:
            raise ValidationAppError(str(exc)) from exc
        if not api_key:
            raise ValidationAppError("SendGrid API key is missing")

        ok = False
        err: str | None = None
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    "https://api.sendgrid.com/v3/user/account",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code in (200, 401, 403):
                # 401/403 still prove the endpoint responded; treat 200 as success.
                ok = resp.status_code == 200
                if not ok:
                    err = f"sendgrid_auth_{resp.status_code}"
            else:
                err = f"sendgrid_http_{resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:300]
            logger.exception("SendGrid test failed client_id=%s", client_id)

        row.last_checked_at = datetime.now(timezone.utc)
        row.last_error = None if ok else err
        self.db.commit()
        return {
            **self.get_status(client_id),
            "ok": ok,
            "error": err,
        }

    def delete(self, client_id: int) -> dict[str, Any]:
        self._get_client(client_id)
        row = self.get_provider_row(client_id)
        if row is None:
            return {"configured": False, "deleted": False}
        self.db.delete(row)
        self.db.commit()
        logger.info("client_email_provider_deleted client_id=%s", client_id)
        return {"configured": False, "deleted": True}

    def decrypt_api_key(self, client_id: int) -> str | None:
        row = self.get_provider_row(client_id)
        if row is None or not row.is_active:
            return None
        try:
            return decrypt_client_secret(row.api_key_enc)
        except SecretsCryptoError:
            logger.exception("Failed to decrypt API key client_id=%s", client_id)
            return None

    def decrypt_webhook_key(self, client_id: int) -> str | None:
        row = self.get_provider_row(client_id)
        if row is None or not row.is_active:
            return None
        try:
            return decrypt_client_secret(row.webhook_verification_key_enc)
        except SecretsCryptoError:
            logger.exception("Failed to decrypt webhook key client_id=%s", client_id)
            return None
