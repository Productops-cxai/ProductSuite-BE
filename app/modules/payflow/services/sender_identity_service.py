"""Client From-address identities + DNS verify + send-time resolution."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAccountSenderOverrideModel,
    PayflowClientEmailProviderModel,
    PayflowClientModel,
    PayflowClientSenderIdentityModel,
    PayflowPaymentTransactionModel,
)
from app.infrastructure.email import sendgrid_domain_auth
from app.modules.payflow.services.email_provider_service import PayflowEmailProviderService
from app.shared.enums import PayflowClientType

logger = logging.getLogger("app.payflow.sender_identity")

_EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _domain_of(email: str) -> str:
    m = _EMAIL_RE.match((email or "").strip().lower())
    if not m:
        raise ValidationAppError("Invalid email address")
    return m.group(1).lower()


def _is_third_party(client: PayflowClientModel | None) -> bool:
    if not client or not client.client_type:
        return True
    key = str(client.client_type).strip().lower().replace(" ", "_").replace("-", "_")
    return key in {
        PayflowClientType.THIRD_PARTY.value,
        "thirdparty",
        "third_party",
    }


@dataclass
class ResolvedSender:
    from_email: str | None
    from_name: str | None
    identity_id: int | None
    api_key: str | None
    source: str  # platform | identity | legacy_email_from


class PayflowSenderIdentityService:
    def __init__(self, db: Session):
        self.db = db
        self.providers = PayflowEmailProviderService(db)

    def _client(self, client_id: int) -> PayflowClientModel:
        client = self.db.get(PayflowClientModel, client_id)
        if client is None:
            raise NotFoundError("Client not found")
        return client

    def _provider(self, client_id: int) -> PayflowClientEmailProviderModel:
        row = self.providers.get_provider_row(client_id)
        if row is None or not row.is_active:
            raise ValidationAppError("Connect this client's SendGrid account first")
        return row

    def _serialize(self, row: PayflowClientSenderIdentityModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "client_id": row.client_id,
            "email_provider_id": row.email_provider_id,
            "email": row.email,
            "display_name": row.display_name,
            "domain": row.domain,
            "is_enabled": bool(row.is_enabled),
            "is_primary": bool(row.is_primary),
            "verification_status": row.verification_status,
            "sendgrid_domain_id": row.sendgrid_domain_id,
            "dns_records": row.dns_records or [],
            "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
            "failover_order": int(row.failover_order or 100),
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def list_identities(self, client_id: int) -> list[dict[str, Any]]:
        self._client(client_id)
        self.backfill_from_client_email_from(client_id)
        rows = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(PayflowClientSenderIdentityModel.client_id == client_id)
            .order_by(
                PayflowClientSenderIdentityModel.is_primary.desc(),
                PayflowClientSenderIdentityModel.failover_order.asc(),
                PayflowClientSenderIdentityModel.id.asc(),
            )
            .all()
        )
        return [self._serialize(r) for r in rows]

    def list_domains(self, client_id: int) -> list[dict[str, Any]]:
        rows = self.list_identities(client_id)
        by_domain: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = r["domain"]
            if d not in by_domain:
                by_domain[d] = {
                    "domain": d,
                    "verification_status": r["verification_status"],
                    "sendgrid_domain_id": r["sendgrid_domain_id"],
                    "dns_records": r["dns_records"],
                    "emails": [],
                }
            by_domain[d]["emails"].append(r["email"])
            if r["verification_status"] == "verified":
                by_domain[d]["verification_status"] = "verified"
        return list(by_domain.values())

    def _clear_other_primaries(self, client_id: int, keep_id: int | None) -> None:
        q = self.db.query(PayflowClientSenderIdentityModel).filter(
            PayflowClientSenderIdentityModel.client_id == client_id,
            PayflowClientSenderIdentityModel.is_primary.is_(True),
        )
        if keep_id is not None:
            q = q.filter(PayflowClientSenderIdentityModel.id != keep_id)
        for row in q.all():
            row.is_primary = False

    def _mirror_primary_to_client(self, client: PayflowClientModel) -> None:
        primary = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client.id,
                PayflowClientSenderIdentityModel.is_primary.is_(True),
            )
            .first()
        )
        if primary is not None:
            client.email_from = primary.email
            if primary.display_name:
                client.sender_name = primary.display_name

    def add_identity(
        self,
        client_id: int,
        *,
        email: str,
        display_name: str | None = None,
        is_primary: bool | None = None,
        failover_order: int | None = None,
        user_id: uuid.UUID | None = None,
        ensure_dns: bool = True,
    ) -> dict[str, Any]:
        client = self._client(client_id)
        provider = self._provider(client_id)
        email_l = (email or "").strip().lower()
        domain = _domain_of(email_l)

        existing = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client_id,
                PayflowClientSenderIdentityModel.email == email_l,
            )
            .first()
        )
        if existing:
            raise ConflictError("This From email is already configured for the client")

        count = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(PayflowClientSenderIdentityModel.client_id == client_id)
            .count()
        )
        make_primary = bool(is_primary) if is_primary is not None else count == 0

        row = PayflowClientSenderIdentityModel(
            client_id=client_id,
            email_provider_id=provider.id,
            email=email_l,
            display_name=(display_name or "").strip() or None,
            domain=domain,
            is_enabled=True,
            is_primary=make_primary,
            verification_status="pending",
            failover_order=int(failover_order) if failover_order is not None else (count + 1) * 10,
            connected_by_user_id=user_id,
        )
        if make_primary:
            self._clear_other_primaries(client_id, None)
            row.is_primary = True

        self.db.add(row)
        self.db.flush()

        if ensure_dns:
            self._ensure_dns_for_row(row)

        if row.is_primary:
            self._mirror_primary_to_client(client)

        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def _ensure_dns_for_row(self, row: PayflowClientSenderIdentityModel) -> None:
        api_key = self.providers.decrypt_api_key(row.client_id)
        if not api_key:
            row.verification_status = "failed"
            row.last_error = "Missing SendGrid API key"
            return

        # Reuse DNS snapshot from another identity on the same domain when present.
        sibling = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == row.client_id,
                PayflowClientSenderIdentityModel.domain == row.domain,
                PayflowClientSenderIdentityModel.id != row.id,
                PayflowClientSenderIdentityModel.sendgrid_domain_id.isnot(None),
            )
            .first()
        )
        try:
            if sibling and sibling.sendgrid_domain_id:
                row.sendgrid_domain_id = sibling.sendgrid_domain_id
                row.dns_records = sibling.dns_records
                if sibling.verification_status == "verified":
                    row.verification_status = "verified"
                    row.last_verified_at = sibling.last_verified_at
                else:
                    row.verification_status = "dns_pending"
                row.last_error = None
                return

            snap = sendgrid_domain_auth.ensure_domain_auth(api_key, row.domain)
            row.sendgrid_domain_id = snap.get("sendgrid_domain_id")
            row.dns_records = snap.get("dns_records") or []
            if snap.get("valid"):
                row.verification_status = "verified"
                row.last_verified_at = _utcnow()
            else:
                row.verification_status = "dns_pending"
            row.last_error = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("domain auth failed client_id=%s domain=%s", row.client_id, row.domain)
            row.verification_status = "failed"
            row.last_error = str(exc)[:500]

    def update_identity(
        self,
        client_id: int,
        identity_id: int,
        *,
        display_name: str | None = None,
        failover_order: int | None = None,
    ) -> dict[str, Any]:
        row = self._get_identity(client_id, identity_id)
        if display_name is not None:
            row.display_name = display_name.strip() or None
        if failover_order is not None:
            row.failover_order = int(failover_order)
        if row.is_primary:
            self._mirror_primary_to_client(self._client(client_id))
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def toggle(self, client_id: int, identity_id: int, *, is_enabled: bool) -> dict[str, Any]:
        row = self._get_identity(client_id, identity_id)
        row.is_enabled = bool(is_enabled)
        if not row.is_enabled and row.is_primary:
            raise ValidationAppError("Cannot disable the primary From email; set another primary first")
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def set_primary(self, client_id: int, identity_id: int) -> dict[str, Any]:
        client = self._client(client_id)
        row = self._get_identity(client_id, identity_id)
        if row.verification_status != "verified":
            raise ValidationAppError("Only a verified From email can be primary")
        self._clear_other_primaries(client_id, row.id)
        row.is_primary = True
        row.is_enabled = True
        self._mirror_primary_to_client(client)
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def verify(self, client_id: int, identity_id: int) -> dict[str, Any]:
        row = self._get_identity(client_id, identity_id)
        api_key = self.providers.decrypt_api_key(client_id)
        if not api_key:
            raise ValidationAppError("Missing SendGrid API key")

        if not row.sendgrid_domain_id:
            self._ensure_dns_for_row(row)
            self.db.commit()
            self.db.refresh(row)
            if not row.sendgrid_domain_id:
                raise ValidationAppError(row.last_error or "Domain authentication failed")

        try:
            snap = sendgrid_domain_auth.refresh_domain_status(api_key, row.sendgrid_domain_id)
            row.dns_records = snap.get("dns_records") or row.dns_records
            if snap.get("valid"):
                row.verification_status = "verified"
                row.last_verified_at = _utcnow()
                row.last_error = None
                # Propagate verified status to siblings on same domain.
                siblings = (
                    self.db.query(PayflowClientSenderIdentityModel)
                    .filter(
                        PayflowClientSenderIdentityModel.client_id == client_id,
                        PayflowClientSenderIdentityModel.domain == row.domain,
                    )
                    .all()
                )
                for sib in siblings:
                    sib.sendgrid_domain_id = row.sendgrid_domain_id
                    sib.dns_records = row.dns_records
                    sib.verification_status = "verified"
                    sib.last_verified_at = row.last_verified_at
                    sib.last_error = None
            else:
                row.verification_status = "dns_pending"
                row.last_error = "DNS records not validated yet"
        except Exception as exc:  # noqa: BLE001
            row.verification_status = "failed"
            row.last_error = str(exc)[:500]
            raise ValidationAppError(row.last_error) from exc

        if row.is_primary:
            self._mirror_primary_to_client(self._client(client_id))
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def delete(self, client_id: int, identity_id: int) -> dict[str, Any]:
        client = self._client(client_id)
        row = self._get_identity(client_id, identity_id)
        was_primary = row.is_primary
        self.db.delete(row)
        self.db.flush()
        if was_primary:
            nxt = (
                self.db.query(PayflowClientSenderIdentityModel)
                .filter(PayflowClientSenderIdentityModel.client_id == client_id)
                .order_by(
                    PayflowClientSenderIdentityModel.failover_order.asc(),
                    PayflowClientSenderIdentityModel.id.asc(),
                )
                .first()
            )
            if nxt:
                nxt.is_primary = True
                nxt.is_enabled = True
                self._mirror_primary_to_client(client)
            else:
                # Leave legacy email_from as-is for transition.
                pass
        self.db.commit()
        return {"deleted": True, "id": identity_id}

    def _get_identity(self, client_id: int, identity_id: int) -> PayflowClientSenderIdentityModel:
        row = self.db.get(PayflowClientSenderIdentityModel, identity_id)
        if row is None or row.client_id != client_id:
            raise NotFoundError("Sender identity not found")
        return row

    def backfill_from_client_email_from(self, client_id: int) -> dict[str, Any] | None:
        """If client has email_from and no identities, create a pending primary row."""
        client = self._client(client_id)
        existing = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(PayflowClientSenderIdentityModel.client_id == client_id)
            .count()
        )
        if existing:
            return None
        email = (client.email_from or "").strip().lower()
        if not email or "@" not in email:
            return None
        provider = self.providers.get_provider_row(client_id)
        try:
            domain = _domain_of(email)
        except ValidationAppError:
            return None
        row = PayflowClientSenderIdentityModel(
            client_id=client_id,
            email_provider_id=provider.id if provider else None,
            email=email,
            display_name=(client.sender_name or client.brand_name or None),
            domain=domain,
            is_enabled=True,
            is_primary=True,
            verification_status="pending",
            failover_order=10,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    # --- resolution / payments / failover ---------------------------------

    def account_has_successful_payment(self, account_id: int) -> bool:
        row = (
            self.db.query(PayflowPaymentTransactionModel.id)
            .filter(
                PayflowPaymentTransactionModel.account_id == account_id,
                PayflowPaymentTransactionModel.direction == "payment",
                PayflowPaymentTransactionModel.status == "Succeeded",
            )
            .first()
        )
        return row is not None

    def get_active_override(
        self, account_id: int
    ) -> PayflowAccountSenderOverrideModel | None:
        return (
            self.db.query(PayflowAccountSenderOverrideModel)
            .filter(
                PayflowAccountSenderOverrideModel.account_id == account_id,
                PayflowAccountSenderOverrideModel.cleared_at.is_(None),
            )
            .first()
        )

    def resolve_for_account(
        self,
        *,
        client: PayflowClientModel | None,
        account: PayflowAccountModel | None,
    ) -> ResolvedSender:
        """Pick From + API key for a collection send."""
        # Third Party → platform SendGrid.
        if client is None or _is_third_party(client):
            return ResolvedSender(
                from_email=(settings.SENDGRID_FROM_EMAIL or "").strip() or None,
                from_name=(
                    (settings.PAYFLOW_OPERATOR_BRAND_NAME or "").strip()
                    or (settings.SENDGRID_FROM_NAME or "").strip()
                    or "PayFlow Collections"
                ),
                identity_id=None,
                api_key=(settings.SENDGRID_API_KEY or "").strip() or None,
                source="platform",
            )

        api_key = self.providers.decrypt_api_key(client.id)
        display_fallback = (
            (client.sender_name or "").strip()
            or (client.brand_name or "").strip()
            or (client.name or "").strip()
            or None
        )

        # Account override first.
        if account is not None:
            ov = self.get_active_override(account.id)
            if ov and ov.active_identity_id:
                ident = self.db.get(PayflowClientSenderIdentityModel, ov.active_identity_id)
                if (
                    ident
                    and ident.client_id == client.id
                    and ident.verification_status == "verified"
                ):
                    if not ident.is_enabled:
                        ident.is_enabled = True
                    return ResolvedSender(
                        from_email=ident.email,
                        from_name=(ident.display_name or display_fallback),
                        identity_id=ident.id,
                        api_key=api_key,
                        source="identity",
                    )

        # Primary verified+enabled.
        primary = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client.id,
                PayflowClientSenderIdentityModel.is_primary.is_(True),
                PayflowClientSenderIdentityModel.is_enabled.is_(True),
                PayflowClientSenderIdentityModel.verification_status == "verified",
            )
            .first()
        )
        if primary is not None and api_key:
            return ResolvedSender(
                from_email=primary.email,
                from_name=(primary.display_name or display_fallback),
                identity_id=primary.id,
                api_key=api_key,
                source="identity",
            )

        # Next enabled verified by failover order.
        nxt = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client.id,
                PayflowClientSenderIdentityModel.is_enabled.is_(True),
                PayflowClientSenderIdentityModel.verification_status == "verified",
            )
            .order_by(
                PayflowClientSenderIdentityModel.failover_order.asc(),
                PayflowClientSenderIdentityModel.id.asc(),
            )
            .first()
        )
        if nxt is not None and api_key:
            return ResolvedSender(
                from_email=nxt.email,
                from_name=(nxt.display_name or display_fallback),
                identity_id=nxt.id,
                api_key=api_key,
                source="identity",
            )

        provider = self.providers.get_provider_row(client.id)
        identity_count = (
            self.db.query(PayflowClientSenderIdentityModel.id)
            .filter(PayflowClientSenderIdentityModel.client_id == client.id)
            .count()
        )
        # Once client SendGrid / identities are in play, never silently fall back.
        if (provider and provider.is_active) or identity_count > 0:
            return ResolvedSender(
                from_email=None,
                from_name=display_fallback,
                identity_id=None,
                api_key=api_key,
                source="unresolved",
            )

        # Legacy scalar email_from + platform key (pre-migration clients only).
        legacy = (client.email_from or "").strip() or None
        return ResolvedSender(
            from_email=legacy,
            from_name=display_fallback,
            identity_id=None,
            api_key=api_key or ((settings.SENDGRID_API_KEY or "").strip() or None),
            source="legacy_email_from",
        )

    def clear_override_for_account(self, account_id: int) -> bool:
        """Clear active From failover override (e.g. after successful payment)."""
        ov = self.get_active_override(account_id)
        if ov is None:
            return False
        ov.cleared_at = _utcnow()
        self.db.flush()
        logger.info("from_override_cleared account_id=%s", account_id)
        return True

    def client_uses_sender_identities(self, client_id: int) -> bool:
        provider = self.providers.get_provider_row(client_id)
        if provider and provider.is_active:
            return True
        count = (
            self.db.query(PayflowClientSenderIdentityModel.id)
            .filter(PayflowClientSenderIdentityModel.client_id == client_id)
            .count()
        )
        return count > 0

    def apply_from_failover(
        self,
        *,
        account: PayflowAccountModel,
        blocked_identity_id: int | None,
        reason: str,
    ) -> dict[str, Any]:
        """After spam/unsub with no payment: point account at next From; enable it."""
        client_id = account.client_id
        blocked_ids = {blocked_identity_id} if blocked_identity_id else set()
        ov = self.get_active_override(account.id)
        if ov and ov.blocked_identity_id:
            blocked_ids.add(ov.blocked_identity_id)

        candidates = (
            self.db.query(PayflowClientSenderIdentityModel)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client_id,
                PayflowClientSenderIdentityModel.verification_status == "verified",
            )
            .order_by(
                PayflowClientSenderIdentityModel.failover_order.asc(),
                PayflowClientSenderIdentityModel.id.asc(),
            )
            .all()
        )
        nxt = next((c for c in candidates if c.id not in blocked_ids), None)
        if nxt is None:
            return {"failover": False, "reason": "no_alternate_identity"}

        nxt.is_enabled = True
        if ov is None:
            ov = PayflowAccountSenderOverrideModel(
                account_id=account.id,
                client_id=client_id,
            )
            self.db.add(ov)
        ov.blocked_identity_id = blocked_identity_id
        ov.active_identity_id = nxt.id
        ov.reason = reason
        ov.payment_gate = True
        ov.cleared_at = None
        self.db.flush()
        logger.info(
            "from_failover account_id=%s blocked=%s active=%s reason=%s",
            account.id,
            blocked_identity_id,
            nxt.id,
            reason,
        )
        return {
            "failover": True,
            "blocked_identity_id": blocked_identity_id,
            "active_identity_id": nxt.id,
            "active_email": nxt.email,
            "reason": reason,
        }

    def has_verified_primary(self, client_id: int) -> bool:
        row = (
            self.db.query(PayflowClientSenderIdentityModel.id)
            .filter(
                PayflowClientSenderIdentityModel.client_id == client_id,
                PayflowClientSenderIdentityModel.is_primary.is_(True),
                PayflowClientSenderIdentityModel.is_enabled.is_(True),
                PayflowClientSenderIdentityModel.verification_status == "verified",
            )
            .first()
        )
        return row is not None
