"""Customer email preference (unsubscribe / resubscribe) — signed tokens, no login.

Token is HMAC over account_id (JWT_SECRET). Used in collection email footers and
List-Unsubscribe one-click. Does not affect SMS opt-out or payment links.

Reasons + comment are recorded when the client has the Allow Unsubscribe Option
governance rule in force; one-click and legacy POSTs without a body still work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.core.request_context import get_frontend_url
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowEmailUnsubscribeEventModel,
    PayflowRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowRuleStatus

logger = logging.getLogger("app.comms.email_pref")

_TOKEN_PREFIX = "ep1"

# Catalog for the preference-page checklist (codes stored on the event).
EMAIL_UNSUBSCRIBE_REASONS: list[dict[str, str]] = [
    {"code": "too_many", "label": "Too many emails / contacting too often"},
    {"code": "prefer_other_channel", "label": "Prefer a different channel (SMS, phone, or mail)"},
    {"code": "already_paid", "label": "Already paid or arranged a plan"},
    {"code": "wrong_person", "label": "Not the right person / wrong contact"},
    {"code": "dispute", "label": "Dispute this debt / don't believe I owe this"},
    {"code": "attorney", "label": "Working with an attorney or agency"},
    {"code": "not_helpful", "label": "Content not helpful or relevant"},
    {"code": "prefer_no_email", "label": "Prefer not to receive collection emails"},
    {"code": "other", "label": "Other"},
]

# Selecting any of these flags the unsubscribe for Operations review (does not delay opt-out).
UNSUBSCRIBE_REVIEW_REASON_CODES = frozenset(
    {
        "already_paid",
        "wrong_person",
        "dispute",
        "attorney",
    }
)

_REASON_CODES = {r["code"] for r in EMAIL_UNSUBSCRIBE_REASONS}
_REASON_LABELS = {r["code"]: r["label"] for r in EMAIL_UNSUBSCRIBE_REASONS}

INCLUDE_UNSUBSCRIBE_ACTION = "Include Unsubscribe Option"
ALLOW_UNSUBSCRIBE_RULE_CODE = "allow-unsubscribe-option"


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
    except Exception:  # noqa: BLE001
        return None
    if prefix != _TOKEN_PREFIX:
        return None
    try:
        aid = int(aid_s)
    except ValueError:
        return None
    secret = (settings.JWT_SECRET or "").encode("utf-8")
    expected = hmac.new(
        secret, f"email_pref:{aid}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    return aid


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
    return f"{get_frontend_url()}/api/payflow/public/email-preferences/{token}/unsubscribe"


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


def reason_labels(codes: list[str] | None) -> list[str]:
    out: list[str] = []
    for code in codes or []:
        label = _REASON_LABELS.get(str(code))
        if label:
            out.append(label)
        elif code:
            out.append(str(code))
    return out


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

    def client_has_unsubscribe_option(self, account: PayflowAccountModel) -> bool:
        """True when Allow Unsubscribe Option is Active and selected for this client."""
        from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService

        matched = RuleRuntimeService(self.db).evaluate(
            account, channel="Email", for_communication=True
        )
        return any(m.get("action") == INCLUDE_UNSUBSCRIBE_ACTION for m in matched)

    def should_include_email_unsubscribe(self, account: PayflowAccountModel) -> bool:
        """Whether collection emails should carry unsubscribe links.

        Legacy-safe: while the seeded Allow Unsubscribe Option rule is not Active
        yet, keep the previous always-on footer. Once that rule is Active, only
        clients who selected it get the option.
        """
        if self.client_has_unsubscribe_option(account):
            return True
        seed = (
            self.db.query(PayflowRuleModel)
            .filter(PayflowRuleModel.code == ALLOW_UNSUBSCRIBE_RULE_CODE)
            .first()
        )
        if seed is None or seed.status != PayflowRuleStatus.ACTIVE.value:
            return True
        return False

    def _latest_event(self, account_id: int) -> PayflowEmailUnsubscribeEventModel | None:
        return (
            self.db.query(PayflowEmailUnsubscribeEventModel)
            .filter(PayflowEmailUnsubscribeEventModel.account_id == account_id)
            .order_by(PayflowEmailUnsubscribeEventModel.id.desc())
            .first()
        )

    def _serialize_event(self, event: PayflowEmailUnsubscribeEventModel | None) -> dict[str, Any] | None:
        if event is None:
            return None
        codes = [str(c) for c in (event.reasons or []) if c]
        return {
            "id": event.id,
            "reasons": codes,
            "reason_labels": reason_labels(codes),
            "comment": event.comment,
            "source": event.source,
            "review_required": bool(getattr(event, "review_required", False)),
            "unsubscribed_at": event.created_at.isoformat() if event.created_at else None,
        }

    @staticmethod
    def _review_required_for(reasons: list[str] | None) -> bool:
        return bool(UNSUBSCRIBE_REVIEW_REASON_CODES.intersection(reasons or []))

    def _serialize_public(self, account: PayflowAccountModel) -> dict[str, Any]:
        consented = account.email_consent is not False
        collect = self.client_has_unsubscribe_option(account)
        latest = self._latest_event(account.id) if not consented else None
        return {
            "account_reference": account.account_reference or str(account.id),
            "brand_name": self._brand_name(account),
            "email_masked": _mask_email(account.email),
            "subscribed": consented,
            "email_consent": account.email_consent,
            # Additive fields — old clients ignore them.
            "unsubscribe_option_enabled": collect,
            "collect_reasons": collect,
            "reason_options": list(EMAIL_UNSUBSCRIBE_REASONS) if collect else [],
            "last_unsubscribe": self._serialize_event(latest),
        }

    def resolve_public(self, token: str) -> dict[str, Any]:
        account_id = parse_email_preference_token(token)
        if account_id is None:
            raise NotFoundError("Invalid or expired preference link")
        return self._serialize_public(self._load_account(account_id))

    def _normalize_reasons(self, reasons: list[str] | None) -> list[str]:
        if not reasons:
            return []
        cleaned: list[str] = []
        for raw in reasons:
            code = str(raw or "").strip()
            if not code:
                continue
            if code not in _REASON_CODES:
                raise ValidationAppError(f"Unknown unsubscribe reason: {code}")
            if code not in cleaned:
                cleaned.append(code)
        return cleaned

    def _record_event(
        self,
        account: PayflowAccountModel,
        *,
        reasons: list[str] | None,
        comment: str | None,
        source: str,
    ) -> PayflowEmailUnsubscribeEventModel:
        text = (comment or "").strip() or None
        if text and len(text) > 2000:
            raise ValidationAppError("Comment must be at most 2000 characters")
        reason_list = list(reasons or [])
        event = PayflowEmailUnsubscribeEventModel(
            account_id=account.id,
            client_id=account.client_id,
            reasons=reason_list,
            comment=text,
            source=source,
            review_required=self._review_required_for(reason_list),
            account_reference=account.account_reference or str(account.id),
            customer_name=(account.customer_name or "").strip() or None,
            email_masked=_mask_email(account.email),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def unsubscribe_public(
        self,
        token: str,
        *,
        reasons: list[str] | None = None,
        comment: str | None = None,
        source: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        account_id = parse_email_preference_token(token)
        if account_id is None:
            raise NotFoundError("Invalid or expired preference link")
        account = self._load_account(account_id)
        cleaned = self._normalize_reasons(reasons)
        text = (comment or "").strip() or None
        # Reasons + comment are always optional (journey: confirm unsubscribe without them).
        resolved_source = (source or "").strip() or (
            "one_click" if not cleaned and not text else "preference_page"
        )

        if account.email_consent is not False:
            account.email_consent = False
            logger.info("email unsubscribed account_id=%s source=%s", account.id, resolved_source)
        # Always record an event so staff have a trail (including one-click).
        self._record_event(
            account,
            reasons=cleaned or (["prefer_no_email"] if resolved_source == "one_click" else []),
            comment=text,
            source=resolved_source,
        )
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
        reasons: list[str] | None = None,
        comment: str | None = None,
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
        previous = account.email_consent
        account.email_consent = email_consent
        if email_consent is False and previous is not False:
            cleaned = self._normalize_reasons(reasons)
            self._record_event(
                account,
                reasons=cleaned,
                comment=comment,
                source="staff",
            )
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        from app.modules.payflow.services.account_service import PayflowAccountService

        return PayflowAccountService(self.db).get_account(user, account_id)

    def latest_for_account(self, user: UserModel, account_id: int) -> dict[str, Any] | None:
        account = self._load_account(account_id)
        if not self.access.is_operations_admin(user):
            if not self.access.can_see_client(user, account.client_id):
                raise ForbiddenError("Account access denied")
            if not self.access.can(user, "view_communications", account.client_id):
                raise ForbiddenError("View Communications permission required")
        return self._serialize_event(self._latest_event(account_id))

    def list_unsubscribes(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        search: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        review_required: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Latest unsubscribe event per currently unsubscribed account."""
        if client_id is not None:
            if not self.access.can(user, "view_communications", client_id):
                raise ForbiddenError("View Communications permission required")
        elif not self.access.can(user, "view_communications"):
            raise ForbiddenError("View Communications permission required")

        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))

        # Latest event id per account.
        latest_id = (
            self.db.query(
                PayflowEmailUnsubscribeEventModel.account_id.label("account_id"),
                func.max(PayflowEmailUnsubscribeEventModel.id).label("event_id"),
            )
            .group_by(PayflowEmailUnsubscribeEventModel.account_id)
            .subquery()
        )
        q = (
            self.db.query(PayflowEmailUnsubscribeEventModel, PayflowAccountModel, PayflowClientModel)
            .join(latest_id, PayflowEmailUnsubscribeEventModel.id == latest_id.c.event_id)
            .join(
                PayflowAccountModel,
                PayflowAccountModel.id == PayflowEmailUnsubscribeEventModel.account_id,
            )
            .join(PayflowClientModel, PayflowClientModel.id == PayflowAccountModel.client_id)
            .filter(PayflowAccountModel.email_consent.is_(False))
        )
        if client_id is not None:
            q = q.filter(PayflowAccountModel.client_id == int(client_id))
        if review_required is True:
            q = q.filter(PayflowEmailUnsubscribeEventModel.review_required.is_(True))
        elif review_required is False:
            q = q.filter(PayflowEmailUnsubscribeEventModel.review_required.is_(False))
        if date_from is not None:
            q = q.filter(PayflowEmailUnsubscribeEventModel.created_at >= date_from)
        if date_to is not None:
            q = q.filter(PayflowEmailUnsubscribeEventModel.created_at <= date_to)
        if search:
            term = f"%{search.strip()}%"
            q = q.filter(
                (PayflowAccountModel.customer_name.ilike(term))
                | (PayflowAccountModel.account_reference.ilike(term))
                | (PayflowAccountModel.email.ilike(term))
                | (PayflowClientModel.name.ilike(term))
            )

        if not self.access.is_operations_admin(user):
            ctx = self.access.build_context(user)
            allowed = list(ctx.get("client_ids") or [])
            q = q.filter(PayflowAccountModel.client_id.in_(allowed or [-1]))

        total = q.count()
        rows = (
            q.order_by(PayflowEmailUnsubscribeEventModel.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        items: list[dict[str, Any]] = []
        for event, account, client in rows:
            codes = [str(c) for c in (event.reasons or []) if c]
            items.append(
                {
                    "event_id": event.id,
                    "account_id": account.id,
                    "client_id": client.id,
                    "client_name": client.name,
                    "account_reference": account.account_reference or str(account.id),
                    "customer_name": account.customer_name,
                    "email_masked": event.email_masked or _mask_email(account.email),
                    "outstanding_balance": float(account.outstanding_balance or 0),
                    "reasons": codes,
                    "reason_labels": reason_labels(codes),
                    "comment": event.comment,
                    "source": event.source,
                    "review_required": bool(getattr(event, "review_required", False)),
                    "unsubscribed_at": event.created_at.isoformat() if event.created_at else None,
                }
            )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "reason_options": list(EMAIL_UNSUBSCRIBE_REASONS),
            "review_reason_codes": sorted(UNSUBSCRIBE_REVIEW_REASON_CODES),
        }
