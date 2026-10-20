"""Secure account-linked payment links — opaque tokens, expiry, revoke, access audit."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowPaymentLinkModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowPaymentLinkStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ttl() -> timedelta:
    hours = max(1, int(getattr(settings, "PAYMENT_LINK_TTL_HOURS", 336) or 336))
    return timedelta(hours=hours)


def public_url_for_token(token: str) -> str:
    base = (settings.WORKFLOW_PAYMENT_LINK_BASE or "").rstrip("/")
    if not base:
        return f"/pay/{token}"
    return f"{base}/{token}"


class PayflowPaymentLinkService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_case_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Payment link access denied")
        if not (
            self.access.can(user, "view_collection_cases", client_id)
            or self.access.can(user, "view_customer_accounts", client_id)
            or self.access.can(user, "view_communications", client_id)
        ):
            raise ForbiddenError("View Cases / Accounts permission required")

    def _append_event(self, row: PayflowPaymentLinkModel, label: str, detail: str | None = None) -> None:
        events = list(row.events or [])
        events.append({"at": _utcnow().isoformat(), "label": label, "detail": detail})
        row.events = events[-60:]

    def _refresh_expiry(self, row: PayflowPaymentLinkModel) -> PayflowPaymentLinkModel:
        if row.status == PayflowPaymentLinkStatus.ACTIVE.value and row.expires_at <= _utcnow():
            row.status = PayflowPaymentLinkStatus.EXPIRED.value
            self._append_event(row, "Expired", "Past expiry window")
        return row

    def _case_eligible(self, account: PayflowAccountModel) -> tuple[bool, str | None]:
        if account.provincial_hold:
            return False, "Account is on provincial hold"
        status = (account.collection_status or "").strip().lower()
        if status in {"resolved", "closed", "ineligible", "paid"}:
            return False, f"Case status '{account.collection_status}' prevents payment"
        return True, None

    def public_url(self, row: PayflowPaymentLinkModel) -> str:
        return public_url_for_token(row.token)

    def serialize(self, row: PayflowPaymentLinkModel) -> dict[str, Any]:
        self._refresh_expiry(row)
        account = row.account
        comm = row.communication
        return {
            "id": row.id,
            "token": row.token,
            "url": self.public_url(row),
            "status": row.status,
            "client_id": row.client_id,
            "account_id": row.account_id,
            "account_reference": account.account_reference if account else None,
            "customer_name": account.customer_name if account else None,
            "communication_id": row.communication_id,
            "communication_code": comm.code if comm else None,
            "communication_channel": comm.channel if comm else None,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "revoked_by": row.revoked_by,
            "completed_at": row.completed_at,
            "access_count": int(row.access_count or 0),
            "clicks_count": int(row.clicks_count or 0),
            "last_accessed_at": row.last_accessed_at,
            "last_clicked_at": row.last_clicked_at,
            "events": list(row.events or []),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "is_usable": row.status == PayflowPaymentLinkStatus.ACTIVE.value
            and row.expires_at > _utcnow(),
        }

    def ensure_active_link(
        self,
        account: PayflowAccountModel,
        *,
        communication_id: int | None = None,
        commit: bool = False,
    ) -> PayflowPaymentLinkModel:
        """Return existing Active (non-expired) link or create one. Prevents duplicate actives."""
        ok, reason = self._case_eligible(account)
        if not ok:
            raise ValidationAppError(reason or "Case is not eligible for a payment link")

        existing = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(
                PayflowPaymentLinkModel.account_id == account.id,
                PayflowPaymentLinkModel.status == PayflowPaymentLinkStatus.ACTIVE.value,
            )
            .order_by(PayflowPaymentLinkModel.id.desc())
            .first()
        )
        if existing:
            self._refresh_expiry(existing)
            if existing.status == PayflowPaymentLinkStatus.ACTIVE.value:
                if communication_id and not existing.communication_id:
                    existing.communication_id = communication_id
                if commit:
                    self.db.commit()
                    self.db.refresh(existing)
                else:
                    self.db.flush()
                return existing

        token = secrets.token_urlsafe(32)[:48]
        # Extremely unlikely collision; retry once.
        if (
            self.db.query(PayflowPaymentLinkModel.id)
            .filter(PayflowPaymentLinkModel.token == token)
            .first()
        ):
            token = secrets.token_urlsafe(32)[:48]

        row = PayflowPaymentLinkModel(
            token=token,
            client_id=account.client_id,
            account_id=account.id,
            communication_id=communication_id,
            status=PayflowPaymentLinkStatus.ACTIVE.value,
            expires_at=_utcnow() + _ttl(),
            events=[],
        )
        self.db.add(row)
        self.db.flush()
        self._append_event(row, "Created", "Secure payment link generated")
        if commit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def list_for_account(self, user: UserModel, account_id: int) -> dict[str, Any]:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            raise NotFoundError("Account not found")
        self._assert_case_view(user, account.client_id)
        rows = (
            self.db.query(PayflowPaymentLinkModel)
            .options(
                joinedload(PayflowPaymentLinkModel.account),
                joinedload(PayflowPaymentLinkModel.communication),
            )
            .filter(PayflowPaymentLinkModel.account_id == account_id)
            .order_by(PayflowPaymentLinkModel.id.desc())
            .all()
        )
        for r in rows:
            self._refresh_expiry(r)
        self.db.flush()
        return {
            "payment_links": [self.serialize(r) for r in rows],
            "total": len(rows),
            "active_count": sum(
                1 for r in rows if r.status == PayflowPaymentLinkStatus.ACTIVE.value
            ),
        }

    def get_link(self, user: UserModel, link_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowPaymentLinkModel)
            .options(
                joinedload(PayflowPaymentLinkModel.account),
                joinedload(PayflowPaymentLinkModel.communication),
            )
            .filter(PayflowPaymentLinkModel.id == link_id)
            .first()
        )
        if not row:
            raise NotFoundError("Payment link not found")
        self._assert_case_view(user, row.client_id)
        self._refresh_expiry(row)
        self.db.flush()
        return self.serialize(row)

    def revoke(self, user: UserModel, link_id: int) -> dict[str, Any]:
        row = self.db.get(PayflowPaymentLinkModel, link_id)
        if not row:
            raise NotFoundError("Payment link not found")
        self._assert_case_view(user, row.client_id)
        if not self.access.is_operations_admin(user) and not self.access.can(
            user, "send_communications", row.client_id
        ):
            # Allow revoke with same permission used for outbound send / ops.
            if not self.access.can(user, "view_collection_cases", row.client_id):
                raise ForbiddenError("Not authorized to revoke payment links")
        self._refresh_expiry(row)
        if row.status in {
            PayflowPaymentLinkStatus.REVOKED.value,
            PayflowPaymentLinkStatus.COMPLETED.value,
        }:
            return self.serialize(row)
        if row.status == PayflowPaymentLinkStatus.EXPIRED.value:
            raise ValidationAppError("Expired links cannot be revoked")
        row.status = PayflowPaymentLinkStatus.REVOKED.value
        row.revoked_at = _utcnow()
        row.revoked_by = user.email or str(user.id)
        self._append_event(row, "Revoked", f"by {row.revoked_by}")
        self.db.commit()
        self.db.refresh(row)
        return self.serialize(row)

    def record_click_by_url(self, url: str | None, *, commit: bool = False) -> None:
        """Best-effort click bump when provider webhook URL contains our token."""
        if not url:
            return
        base = (settings.WORKFLOW_PAYMENT_LINK_BASE or "").rstrip("/")
        token = None
        if base and url.startswith(base + "/"):
            token = url[len(base) + 1 :].split("?", 1)[0].split("#", 1)[0]
        elif "/pay/" in url:
            token = url.rsplit("/pay/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
        if not token or len(token) < 16:
            return
        row = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(PayflowPaymentLinkModel.token == token)
            .first()
        )
        if not row:
            return
        self._refresh_expiry(row)
        if row.status != PayflowPaymentLinkStatus.ACTIVE.value:
            return
        row.clicks_count = int(row.clicks_count or 0) + 1
        row.last_clicked_at = _utcnow()
        self._append_event(row, "Link Clicked", None)
        if commit:
            self.db.commit()
        else:
            self.db.flush()

    def mark_completed_for_account(self, account_id: int, *, commit: bool = False) -> None:
        rows = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(
                PayflowPaymentLinkModel.account_id == account_id,
                PayflowPaymentLinkModel.status == PayflowPaymentLinkStatus.ACTIVE.value,
            )
            .all()
        )
        now = _utcnow()
        for row in rows:
            row.status = PayflowPaymentLinkStatus.COMPLETED.value
            row.completed_at = now
            self._append_event(row, "Payment Completed", "Associated payment event")
        if commit:
            self.db.commit()
        else:
            self.db.flush()

    def resolve_public(self, token: str) -> dict[str, Any]:
        """Portal resolve — no JWT. Validates status, expiry, and case eligibility.

        Does not alter outstanding balance or mark payment completed.
        """
        row = (
            self.db.query(PayflowPaymentLinkModel)
            .options(
                joinedload(PayflowPaymentLinkModel.account).joinedload(PayflowAccountModel.client),
                joinedload(PayflowPaymentLinkModel.client),
            )
            .filter(PayflowPaymentLinkModel.token == token)
            .first()
        )
        if not row:
            raise NotFoundError("Payment link not found")

        self._refresh_expiry(row)
        account = row.account
        if not account:
            raise NotFoundError("Account not found")

        usable = row.status == PayflowPaymentLinkStatus.ACTIVE.value
        block_reason: str | None = None
        if row.status == PayflowPaymentLinkStatus.EXPIRED.value:
            block_reason = "This payment link has expired"
            usable = False
        elif row.status == PayflowPaymentLinkStatus.REVOKED.value:
            block_reason = "This payment link has been revoked"
            usable = False
        elif row.status == PayflowPaymentLinkStatus.COMPLETED.value:
            block_reason = "Payment for this link was already completed"
            usable = False
        else:
            ok, reason = self._case_eligible(account)
            if not ok:
                usable = False
                block_reason = reason

        # Access audit — never marks payment completed / never changes balance.
        row.access_count = int(row.access_count or 0) + 1
        row.last_accessed_at = _utcnow()
        self._append_event(
            row,
            "Accessed",
            "usable" if usable else (block_reason or "blocked"),
        )
        self.db.commit()

        client = account.client or row.client
        return {
            "usable": usable,
            "status": row.status,
            "block_reason": block_reason,
            "expires_at": row.expires_at,
            "customer": {
                "display_name": account.customer_first_name
                or (account.customer_name or "").split(" ")[0]
                or "Customer",
            },
            "case": {
                "outstanding_balance": float(account.outstanding_balance or 0),
                "currency_code": account.currency_code or "USD",
            },
            "brand": {
                "name": (client.brand_name or client.name) if client else "PayFlow",
                "logo_url": client.logo_url if client else None,
            },
            # Opaque reference only — never CRM account_reference / debtor ids.
            "link_ref": row.token[:8],
        }

    def url_for_communication(
        self, communication: PayflowCommunicationModel, account: PayflowAccountModel | None
    ) -> str | None:
        if not communication.payment_link or not account:
            return None
        link = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(PayflowPaymentLinkModel.communication_id == communication.id)
            .order_by(PayflowPaymentLinkModel.id.desc())
            .first()
        )
        if not link:
            link = (
                self.db.query(PayflowPaymentLinkModel)
                .filter(
                    PayflowPaymentLinkModel.account_id == account.id,
                    PayflowPaymentLinkModel.status == PayflowPaymentLinkStatus.ACTIVE.value,
                )
                .order_by(PayflowPaymentLinkModel.id.desc())
                .first()
            )
        if not link:
            return None
        self._refresh_expiry(link)
        if link.status != PayflowPaymentLinkStatus.ACTIVE.value:
            return None
        return self.public_url(link)
