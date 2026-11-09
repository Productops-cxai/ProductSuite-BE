"""Customer email preference (unsubscribe / resubscribe) — signed tokens, no DB table.

Token is HMAC over account_id (JWT_SECRET). Used in collection email footers and
List-Unsubscribe one-click. Does not affect SMS opt-out or payment links.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.core.request_context import get_frontend_url
from app.infrastructure.database.models import PayflowAccountModel, UserModel
from app.modules.payflow.services.access_context_service import AccessContextService

logger = logging.getLogger("app.comms.email_pref")

_TOKEN_PREFIX = "ep1"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def make_email_preference_token(account_id: int) -> str:
    """Opaque URL-safe token bound to account_id."""
    aid = int(account_id)
    secret = (settings.JWT_SECRET or "").encode("utf-8")
    sig = hmac.new(secret, f"email_pref:{aid}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    raw = f"{_TOKEN_PREFIX}.{aid}.{sig}".encode("utf-8")
    return _b64url(raw)


def parse_email_preference_token(token: str) -> int | None:
    try:
        raw = _b64url_decode((token or "").strip()).decode("utf-8")
        prefix, aid_s, sig = raw.split(".", 2)
        if prefix != _TOKEN_PREFIX:
            return None
        aid = int(aid_s)
        secret = (settings.JWT_SECRET or "").encode("utf-8")
        expected = hmac.new(
            secret, f"email_pref:{aid}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(expected, sig):
            return None
        return aid
    except Exception:  # noqa: BLE001
        return None


def email_preference_page_url(account_id: int) -> str:
    """Customer-facing page (confirm unsubscribe / resubscribe)."""
    token = make_email_preference_token(account_id)
    return f"{get_frontend_url()}/email-preferences/{token}"


def email_unsubscribe_one_click_url(account_id: int) -> str:
    """HTTPS endpoint for List-Unsubscribe / RFC 8058 one-click POST."""
    token = make_email_preference_token(account_id)
    public = (settings.PUBLIC_API_BASE or "").strip().rstrip("/")
    if public:
        return f"{public}/payflow/public/email-preferences/{token}/unsubscribe"
    # Local / missing PUBLIC_API_BASE — still emit a relative API path via frontend proxy if any.
    return f"{get_frontend_url()}/api/payflow/public/email-preferences/{token}/unsubscribe"


class PayflowEmailPreferenceService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _load_account(self, account_id: int) -> PayflowAccountModel:
        row = (
            self.db.query(PayflowAccountModel)
            .options(joinedload(PayflowAccountModel.client))
            .filter(PayflowAccountModel.id == account_id)
            .first()
        )
        if not row:
            raise NotFoundError("Account not found")
        return row

    def _brand_name(self, account: PayflowAccountModel) -> str:
        client = account.client
        if client is None:
            return (settings.SENDGRID_FROM_NAME or "PayFlow").strip()
        return (
            (client.brand_name or "").strip()
            or (client.sender_name or "").strip()
            or (client.name or "").strip()
            or (settings.SENDGRID_FROM_NAME or "PayFlow").strip()
        )

    def _serialize_public(self, account: PayflowAccountModel) -> dict[str, Any]:
        consented = account.email_consent is not False
        return {
            "account_reference": account.account_reference or str(account.id),
            "brand_name": self._brand_name(account),
            "email_masked": _mask_email(account.email),
            "subscribed": consented,
            "email_consent": account.email_consent,
        }

    def resolve_public(self, token: str) -> dict[str, Any]:
        account_id = parse_email_preference_token(token)
        if account_id is None:
            raise NotFoundError("Invalid or expired preference link")
        return self._serialize_public(self._load_account(account_id))

    def unsubscribe_public(self, token: str, *, commit: bool = True) -> dict[str, Any]:
        account_id = parse_email_preference_token(token)
        if account_id is None:
            raise NotFoundError("Invalid or expired preference link")
        account = self._load_account(account_id)
        if account.email_consent is not False:
            account.email_consent = False
            logger.info("email unsubscribed account_id=%s", account.id)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self._serialize_public(account)

    def resubscribe_public(self, token: str, *, commit: bool = True) -> dict[str, Any]:
        account_id = parse_email_preference_token(token)
        if account_id is None:
            raise NotFoundError("Invalid or expired preference link")
        account = self._load_account(account_id)
        if account.email_consent is not True:
            account.email_consent = True
            logger.info("email resubscribed account_id=%s", account.id)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self._serialize_public(account)

    def set_email_consent(
        self,
        user: UserModel,
        account_id: int,
        *,
        email_consent: bool,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Ops toggle — requires send_communications (or operations admin)."""
        account = self._load_account(account_id)
        if not self.access.is_operations_admin(user):
            if not self.access.can_see_client(user, account.client_id):
                raise ForbiddenError("Account access denied")
            if not self.access.can(user, "send_communications", account.client_id):
                raise ForbiddenError("Send Communications permission required")
        if not isinstance(email_consent, bool):
            raise ValidationAppError("email_consent must be true or false")
        account.email_consent = email_consent
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        from app.modules.payflow.services.account_service import PayflowAccountService

        return PayflowAccountService(self.db).get_account(user, account_id)


def _mask_email(email: str | None) -> str | None:
    text = (email or "").strip()
    if not text or "@" not in text:
        return None
    local, _, domain = text.partition("@")
    if len(local) <= 2:
        masked_local = (local[:1] + "*") if local else "*"
    else:
        masked_local = local[0] + ("*" * min(6, len(local) - 2)) + local[-1]
    return f"{masked_local}@{domain}"
