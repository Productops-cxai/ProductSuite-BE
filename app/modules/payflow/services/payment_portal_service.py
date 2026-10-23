"""Customer payment portal — resolve, eligibility, initiate, attempt status, Stripe outcomes."""

from __future__ import annotations

import logging
import math
import uuid
from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanModel,
)
from app.modules.payflow.services.payment_link_service import (
    PayflowPaymentLinkService,
    public_url_for_token,
)
from app.modules.payflow.services.stripe_checkout_service import (
    create_checkout_session,
    retrieve_checkout_session,
    retrieve_payment_intent,
    stripe_configured,
)
from app.shared.enums import (
    PayflowClientType,
    PayflowPaymentAttemptStatus,
    PayflowPaymentLinkStatus,
    PayflowPaymentOptionType,
)

logger = logging.getLogger("app.payflow.payment_portal")

_DEFAULT_PAYMENT_SETTINGS: dict[str, Any] = {
    "allow_pay_in_full": True,
    "allow_partial": True,
    "allow_payment_plan": True,
    "min_partial_amount": 10.0,
    "min_partial_percent": 5.0,
    # Stripe readiness (story BE-12): opt-in per client; currencies empty = use case currency only.
    "stripe_payments_enabled": True,
    "accepted_currencies": [],
    "payment_plan_options": [
        {"id": "plan_3", "label": "3 monthly instalments", "installments": 3},
        {"id": "plan_6", "label": "6 monthly instalments", "installments": 6},
    ],
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: float) -> float:
    return round(float(value or 0), 2)


class PayflowPaymentPortalService:
    def __init__(self, db: Session):
        self.db = db
        self.links = PayflowPaymentLinkService(db)

    def _append_attempt_event(
        self, row: PayflowPaymentAttemptModel, label: str, detail: str | None = None
    ) -> None:
        events = list(row.events or [])
        events.append({"at": _utcnow().isoformat(), "label": label, "detail": detail})
        row.events = events[-80:]

    def _client_settings(self, client: PayflowClientModel | None) -> dict[str, Any]:
        raw = dict(_DEFAULT_PAYMENT_SETTINGS)
        stored: dict[str, Any] = {}
        if client and isinstance(client.payment_settings, dict):
            stored = client.payment_settings
            raw.update({k: v for k, v in stored.items() if v is not None})
        # FE may send allow_plans; portal uses allow_payment_plan.
        if "allow_payment_plan" not in stored and "allow_plans" in raw:
            raw["allow_payment_plan"] = bool(raw.get("allow_plans"))
        return raw

    def _stripe_enabled_for_client(self, client: PayflowClientModel | None) -> tuple[bool, str | None]:
        """Per-client Stripe enablement + currency gate (does not assume every client is live)."""
        cfg = self._client_settings(client)
        if not bool(cfg.get("stripe_payments_enabled", True)):
            return False, "Online card payments are not enabled for this client"
        if not stripe_configured():
            env = (settings.APP_ENV or "").lower()
            if env in {"development", "dev", "local", "test"}:
                return True, None
            return False, "Online payments are not configured for this environment"
        return True, None

    def _currency_allowed(
        self, client: PayflowClientModel | None, currency: str
    ) -> tuple[bool, str | None]:
        cfg = self._client_settings(client)
        cur = (currency or "USD").strip().upper()
        accepted_raw = cfg.get("accepted_currencies") or []
        if isinstance(accepted_raw, str):
            accepted = [c.strip().upper() for c in accepted_raw.split(",") if c.strip()]
        elif isinstance(accepted_raw, list):
            accepted = [str(c).strip().upper() for c in accepted_raw if str(c).strip()]
        else:
            accepted = []
        if accepted and cur not in accepted:
            return (
                False,
                f"Currency {cur} is not enabled for online payments for this client",
            )
        # When no explicit list: still require a case/client currency (already on the attempt).
        return True, None

    _PREVIEW_TOKEN_PREFIX = "preview."

    @classmethod
    def _strip_preview_token(cls, token: str) -> tuple[str, bool]:
        if token.startswith(cls._PREVIEW_TOKEN_PREFIX):
            return token[len(cls._PREVIEW_TOKEN_PREFIX) :], True
        return token, False

    def _is_third_party(self, client: PayflowClientModel | None) -> bool:
        if not client or not client.client_type:
            return True
        key = str(client.client_type).strip().lower().replace(" ", "_").replace("-", "_")
        return key in {
            PayflowClientType.THIRD_PARTY.value,
            "thirdparty",
            "third_party",
        }

    @staticmethod
    def _absolute_asset_url(url: str | None) -> str | None:
        """Make /uploads/... absolute so portal FE + Stripe can load logos cross-origin."""
        raw = (url or "").strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://")):
            return raw
        base = (settings.PUBLIC_API_BASE or "").strip().rstrip("/")
        if not base:
            return raw  # relative — FE may still prefix via VITE_API_BASE_URL
        path = raw if raw.startswith("/") else f"/{raw}"
        return f"{base}{path}"

    def _brand_payload(self, client: PayflowClientModel | None) -> dict[str, Any]:
        third = self._is_third_party(client)
        client_label = (client.brand_name or client.name) if client else "Client"
        if third:
            return {
                "mode": "third_party",
                "name": settings.PAYFLOW_OPERATOR_BRAND_NAME or "PayFlow Collections",
                "logo_url": self._absolute_asset_url(
                    (settings.PAYFLOW_OPERATOR_LOGO_URL or None) or None
                ),
                "on_behalf_of": client_label,
            }
        return {
            "mode": "first_party",
            "name": client_label or "PayFlow",
            "logo_url": self._absolute_asset_url(client.logo_url if client else None),
            "on_behalf_of": None,
        }

    def _case_payment_eligible(self, account: PayflowAccountModel) -> tuple[bool, str | None]:
        ok, reason = self.links._case_eligible(account)
        if not ok:
            return ok, reason
        balance = _money(account.outstanding_balance)
        if balance <= 0:
            return False, "This account has no outstanding balance"
        return True, None

    def _build_options(
        self, account: PayflowAccountModel, client: PayflowClientModel | None
    ) -> list[dict[str, Any]]:
        settings_map = self._client_settings(client)
        balance = _money(account.outstanding_balance)
        currency = (account.currency_code or (client.currency_code if client else None)) or "USD"
        options: list[dict[str, Any]] = []

        if settings_map.get("allow_pay_in_full", True) and balance > 0:
            options.append(
                {
                    "id": PayflowPaymentOptionType.PAY_IN_FULL.value,
                    "type": PayflowPaymentOptionType.PAY_IN_FULL.value,
                    "label": "Pay in full",
                    "description": "Settle the full outstanding balance in one payment.",
                    "amount": balance,
                    "currency_code": currency,
                    "amount_editable": False,
                    "min_amount": balance,
                    "max_amount": balance,
                }
            )

        if settings_map.get("allow_partial", True) and balance > 0:
            min_amt = float(settings_map.get("min_partial_amount") or 10.0)
            pct = float(settings_map.get("min_partial_percent") or 0)
            if pct > 0:
                min_amt = max(min_amt, _money(balance * (pct / 100.0)))
            min_amt = min(min_amt, balance)
            if min_amt < balance:
                options.append(
                    {
                        "id": PayflowPaymentOptionType.PARTIAL.value,
                        "type": PayflowPaymentOptionType.PARTIAL.value,
                        "label": "Pay a partial amount",
                        "description": f"Pay any amount between {min_amt:.2f} and {balance:.2f} {currency}.",
                        "amount": None,
                        "currency_code": currency,
                        "amount_editable": True,
                        "min_amount": min_amt,
                        "max_amount": balance,
                    }
                )

        min_plan = float(settings_map.get("min_plan_amount") or 0)
        if settings_map.get("allow_payment_plan", True) and balance > 0 and balance >= min_plan:
            plans = settings_map.get("payment_plan_options") or []
            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                installments = int(plan.get("installments") or 0)
                if installments < 2:
                    continue
                first = _money(math.ceil((balance / installments) * 100) / 100)
                first = min(first, balance)
                plan_id = str(plan.get("id") or f"plan_{installments}")
                frequency = str(plan.get("frequency") or "monthly")
                start = _utcnow().date()
                schedule = []
                remaining = balance
                for i in range(1, installments + 1):
                    if i == 1:
                        amt = first
                    elif i == installments:
                        amt = _money(remaining)
                    else:
                        amt = _money(remaining / (installments - i + 1))
                    remaining = _money(remaining - amt)
                    due = self._add_months(start, i - 1)
                    schedule.append(
                        {
                            "sequence": i,
                            "amount": amt,
                            "due_date": due.isoformat(),
                            "frequency": frequency,
                        }
                    )
                options.append(
                    {
                        "id": plan_id,
                        "type": PayflowPaymentOptionType.PAYMENT_PLAN.value,
                        "label": str(plan.get("label") or f"{installments} instalments"),
                        "description": (
                            f"{installments} {frequency} payments totalling {balance:.2f} {currency}. "
                            f"Today’s charge: {first:.2f} {currency}. Future instalments are not "
                            "auto-charged."
                        ),
                        "amount": first,
                        "currency_code": currency,
                        "amount_editable": False,
                        "min_amount": first,
                        "max_amount": first,
                        "installments": installments,
                        "plan_id": plan_id,
                        "frequency": frequency,
                        "total_payable": balance,
                        "installment_amount": first,
                        "schedule": schedule,
                    }
                )

        # Explicit pay-next for an already-active plan (manual instalment, not auto-charge).
        try:
            from app.modules.payflow.services.payment_outcome_service import (
                PayflowPaymentOutcomeService,
            )

            outcome = PayflowPaymentOutcomeService(self.db)
            outcome.refresh_overdue_plans(account.id)
            for plan in (
                self.db.query(PayflowPaymentPlanModel)
                .filter(
                    PayflowPaymentPlanModel.account_id == account.id,
                    PayflowPaymentPlanModel.status.in_(["Active", "Overdue"]),
                )
                .order_by(PayflowPaymentPlanModel.id.desc())
                .all()
            ):
                serialized = outcome.serialize_plan(plan)
                next_inst = next(
                    (
                        i
                        for i in (serialized.get("installments") or [])
                        if i.get("status") in {"Pending", "Overdue", "Failed"}
                    ),
                    None,
                )
                if not next_inst:
                    continue
                amt = _money(next_inst.get("amount") or plan.installment_amount)
                amt = min(amt, balance)
                if amt <= 0:
                    continue
                options.append(
                    {
                        "id": f"pay_next_{plan.public_id}",
                        "type": PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
                        "label": "Pay next instalment",
                        "description": (
                            f"Pay the next scheduled instalment"
                            f"{' due ' + next_inst['due_date'] if next_inst.get('due_date') else ''}"
                            f" for {serialized.get('label') or plan.public_id}. "
                            "Future instalments are not auto-charged."
                        ),
                        "amount": amt,
                        "currency_code": currency,
                        "amount_editable": False,
                        "min_amount": amt,
                        "max_amount": amt,
                        "plan_id": plan.public_id,
                        "installment_sequence": next_inst.get("sequence"),
                    }
                )
                break
        except Exception:  # noqa: BLE001
            logger.exception("failed to build pay-next instalment option")

        return options

    @staticmethod
    def _add_months(d: date, months: int) -> date:
        month = d.month - 1 + months
        year = d.year + month // 12
        month = month % 12 + 1
        day = min(d.day, monthrange(year, month)[1])
        return date(year, month, day)

    def resolve_portal(self, token: str, *, preview: bool = False) -> dict[str, Any]:
        """Public portal payload — branding, balance, options, block reasons.

        When preview=True (or token is preview-prefixed), skips access/visit side effects
        and returns a preview token that cannot initiate Checkout.
        """
        real_token, preview_from_token = self._strip_preview_token(token)
        preview_mode = bool(preview or preview_from_token)
        row = (
            self.db.query(PayflowPaymentLinkModel)
            .options(
                joinedload(PayflowPaymentLinkModel.account).joinedload(PayflowAccountModel.client),
                joinedload(PayflowPaymentLinkModel.client),
            )
            .filter(PayflowPaymentLinkModel.token == real_token)
            .first()
        )
        if not row:
            raise NotFoundError("Payment link not found")

        self.links._refresh_expiry(row)
        account = row.account
        if not account:
            raise NotFoundError("Account not found")
        client = account.client or row.client

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
            ok, reason = self._case_payment_eligible(account)
            if not ok:
                usable = False
                block_reason = reason

        if not preview_mode:
            row.access_count = int(row.access_count or 0) + 1
            row.last_accessed_at = _utcnow()
            self.links._append_event(
                row, "Accessed", "usable" if usable else (block_reason or "blocked")
            )
            # Distinct portal visit on originating communication (vs email link click).
            if row.communication_id:
                try:
                    from app.modules.payflow.services.communication_service import (
                        PayflowCommunicationService,
                    )

                    PayflowCommunicationService(self.db).record_portal_visit(
                        row.communication_id,
                        detail="Customer opened payment portal",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to record portal visit on communication")
            self.db.commit()
        else:
            # Preview may still need overdue refresh for accurate options (no commit of link access).
            self.db.flush()

        balance = _money(account.outstanding_balance)
        currency = (account.currency_code or (client.currency_code if client else None)) or "USD"
        # Preview still shows options for staff UX even when link is blocked, when balance > 0.
        options = (
            self._build_options(account, client)
            if usable or (preview_mode and balance > 0)
            else []
        )
        stripe_ok, stripe_msg = self._stripe_enabled_for_client(client)
        currency_ok, currency_msg = self._currency_allowed(client, currency)
        env_ok = stripe_configured() or (settings.APP_ENV or "").lower() in {
            "development",
            "dev",
            "local",
            "test",
        }
        provider_ready = bool(env_ok and stripe_ok and currency_ok)
        provider_message = None
        if preview_mode:
            provider_ready = False
            provider_message = "Preview only — payment cannot be started"
        elif not stripe_ok:
            provider_message = stripe_msg
        elif not currency_ok:
            provider_message = currency_msg
        elif not env_ok:
            provider_message = "Online payments are not configured for this environment"

        active_plans: list[dict[str, Any]] = []
        try:
            from app.modules.payflow.services.payment_outcome_service import (
                PayflowPaymentOutcomeService,
            )

            outcome = PayflowPaymentOutcomeService(self.db)
            outcome.refresh_overdue_plans(account.id)
            for plan in (
                self.db.query(PayflowPaymentPlanModel)
                .filter(
                    PayflowPaymentPlanModel.account_id == account.id,
                    PayflowPaymentPlanModel.status.in_(["Active", "Overdue"]),
                )
                .order_by(PayflowPaymentPlanModel.id.desc())
                .all()
            ):
                active_plans.append(outcome.serialize_plan(plan))
        except Exception:  # noqa: BLE001
            logger.exception("failed to load payment plans for portal")

        response_token = (
            f"{self._PREVIEW_TOKEN_PREFIX}{real_token}" if preview_mode else real_token
        )
        return {
            "usable": usable and not preview_mode,
            "status": row.status,
            "block_reason": (
                "Staff preview — checkout is disabled"
                if preview_mode
                else block_reason
            ),
            "expires_at": row.expires_at,
            "token": response_token,
            "link_ref": row.token[:8],
            "customer": {
                "display_name": account.customer_first_name
                or (account.customer_name or "").split(" ")[0]
                or "Customer",
            },
            "case": {
                "account_reference": account.account_reference,
                "original_balance": float(account.original_balance or 0),
                "outstanding_balance": balance,
                "recovered_balance": float(account.recovered_balance or 0),
                "currency_code": currency,
                "collection_status": account.collection_status,
            },
            "brand": self._brand_payload(client),
            "payment_options": options,
            "active_payment_plans": active_plans,
            "preview_mode": preview_mode,
            "provider": {
                "name": "stripe" if stripe_configured() else "mock",
                "ready": bool(provider_ready and usable and options and not preview_mode),
                "message": provider_message,
            },
        }

    def _resolve_amount(
        self,
        *,
        option: dict[str, Any],
        requested_amount: float | None,
        balance: float,
    ) -> float:
        opt_type = option["type"]
        if opt_type == PayflowPaymentOptionType.PAY_IN_FULL.value:
            return balance
        if opt_type in {
            PayflowPaymentOptionType.PAYMENT_PLAN.value,
            PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
        }:
            return _money(option["amount"])
        # partial
        if requested_amount is None:
            raise ValidationAppError("Partial payment amount is required")
        amount = _money(requested_amount)
        min_a = _money(option["min_amount"])
        max_a = _money(option["max_amount"])
        if amount < min_a:
            raise ValidationAppError(f"Amount must be at least {min_a:.2f}")
        if amount > max_a:
            raise ValidationAppError(f"Amount cannot exceed {max_a:.2f}")
        return amount

    def initiate(
        self,
        token: str,
        *,
        option_id: str,
        amount: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        real_token, is_preview = self._strip_preview_token(token)
        if is_preview:
            raise ValidationAppError("Checkout cannot be started in preview mode")
        row = (
            self.db.query(PayflowPaymentLinkModel)
            .options(
                joinedload(PayflowPaymentLinkModel.account).joinedload(PayflowAccountModel.client),
                joinedload(PayflowPaymentLinkModel.client),
            )
            .filter(PayflowPaymentLinkModel.token == real_token)
            .first()
        )
        if not row:
            raise NotFoundError("Payment link not found")
        self.links._refresh_expiry(row)
        if row.status != PayflowPaymentLinkStatus.ACTIVE.value:
            raise ValidationAppError(
                "This payment link is no longer available"
                if row.status != PayflowPaymentLinkStatus.COMPLETED.value
                else "Payment for this link was already completed"
            )

        account = row.account
        if not account:
            raise NotFoundError("Account not found")
        ok, reason = self._case_payment_eligible(account)
        if not ok:
            raise ValidationAppError(reason or "Case is not eligible for payment")

        # Recheck live balance — prevent stale / overpayment.
        balance = _money(account.outstanding_balance)
        if balance <= 0:
            raise ValidationAppError("This account has no outstanding balance")

        client = account.client or row.client
        options = self._build_options(account, client)
        option = next((o for o in options if o["id"] == option_id), None)
        if not option:
            raise ValidationAppError("Selected payment option is not available")

        # Recompute option amounts against current balance.
        if option["type"] == PayflowPaymentOptionType.PAY_IN_FULL.value:
            option = {**option, "amount": balance, "min_amount": balance, "max_amount": balance}
        elif option["type"] == PayflowPaymentOptionType.PARTIAL.value:
            option = {**option, "max_amount": balance}
            if option["min_amount"] > balance:
                raise ValidationAppError("Partial payment is not available for this balance")

        charge_amount = self._resolve_amount(
            option=option, requested_amount=amount, balance=balance
        )
        if charge_amount > balance:
            raise ValidationAppError("Payment amount exceeds current outstanding balance")

        if idempotency_key:
            existing = (
                self.db.query(PayflowPaymentAttemptModel)
                .filter(PayflowPaymentAttemptModel.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return self.serialize_attempt(existing)

        # Avoid duplicate open attempts for same link+option+amount in a short window.
        open_statuses = {
            PayflowPaymentAttemptStatus.INITIATED.value,
            PayflowPaymentAttemptStatus.PROCESSING.value,
        }
        recent = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(
                PayflowPaymentAttemptModel.payment_link_id == row.id,
                PayflowPaymentAttemptModel.option_type == option["type"],
                PayflowPaymentAttemptModel.amount == charge_amount,
                PayflowPaymentAttemptModel.status.in_(list(open_statuses)),
            )
            .order_by(PayflowPaymentAttemptModel.id.desc())
            .first()
        )
        if recent and recent.checkout_url:
            return self.serialize_attempt(recent)

        currency = (account.currency_code or (client.currency_code if client else None)) or "USD"
        stripe_ok, stripe_msg = self._stripe_enabled_for_client(client)
        if not stripe_ok:
            raise ValidationAppError(stripe_msg or "Online payments are not available")
        currency_ok, currency_msg = self._currency_allowed(client, currency)
        if not currency_ok:
            raise ValidationAppError(currency_msg or "Currency not supported for online payment")
        public_id = uuid.uuid4().hex
        use_stripe = stripe_configured()
        attempt = PayflowPaymentAttemptModel(
            public_id=public_id,
            payment_link_id=row.id,
            client_id=row.client_id,
            account_id=account.id,
            option_type=option["type"],
            plan_id=option.get("plan_id"),
            amount=charge_amount,
            currency_code=currency,
            status=PayflowPaymentAttemptStatus.INITIATED.value,
            provider="stripe" if use_stripe else "mock",
            idempotency_key=idempotency_key,
            balance_at_initiation=balance,
            events=[],
        )
        self.db.add(attempt)
        self.db.flush()
        self._append_attempt_event(
            attempt,
            "Initiated",
            f"{option['type']} {charge_amount:.2f} {currency}",
        )

        fe = (settings.FRONTEND_URL or "").rstrip("/")
        success_url = (
            f"{fe}/pay/{real_token}/result?attempt={public_id}&outcome=success"
            if fe
            else f"/pay/{real_token}/result?attempt={public_id}&outcome=success"
        )
        cancel_url = (
            f"{fe}/pay/{real_token}/result?attempt={public_id}&outcome=cancel"
            if fe
            else f"/pay/{real_token}/result?attempt={public_id}&outcome=cancel"
        )
        brand = self._brand_payload(client)

        try:
            if use_stripe:
                session = create_checkout_session(
                    amount=charge_amount,
                    currency=currency,
                    success_url=success_url + "&session_id={CHECKOUT_SESSION_ID}",
                    cancel_url=cancel_url,
                    client_reference_id=public_id,
                    metadata={
                        "attempt_public_id": public_id,
                        "payment_link_token": real_token[:48],
                        "account_id": str(account.id),
                        "client_id": str(row.client_id),
                        "option_type": option["type"],
                    },
                    brand_name=brand.get("name"),
                    brand_logo_url=brand.get("logo_url"),
                    brand_subtitle=brand.get("on_behalf_of"),
                    customer_email=(account.email or None),
                )
                attempt.stripe_session_id = session.get("id")
                attempt.checkout_url = session.get("url")
                pi = session.get("payment_intent")
                if isinstance(pi, str):
                    attempt.stripe_payment_intent_id = pi
                attempt.provider_payload = {"session_id": session.get("id")}
                attempt.status = PayflowPaymentAttemptStatus.PROCESSING.value
                self._append_attempt_event(attempt, "Checkout Created", attempt.stripe_session_id)
            else:
                # Dev mock — no card data; customer confirms on our page.
                mock_url = (
                    f"{fe}/pay/{real_token}/checkout?attempt={public_id}"
                    if fe
                    else f"/pay/{real_token}/checkout?attempt={public_id}"
                )
                attempt.checkout_url = mock_url
                attempt.status = PayflowPaymentAttemptStatus.PROCESSING.value
                self._append_attempt_event(attempt, "Mock Checkout Ready", None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("checkout create failed attempt=%s", public_id)
            attempt.status = PayflowPaymentAttemptStatus.FAILED.value
            attempt.error_message = str(exc)[:500]
            self._append_attempt_event(attempt, "Checkout Failed", attempt.error_message)
            self._raise_ops_exception(attempt, attempt.error_message or "Checkout failed")
            self.db.commit()
            try:
                from app.modules.payflow.services.notification_service import (
                    PayflowNotificationService,
                )

                PayflowNotificationService(self.db).notify_payment_failed(
                    client_id=attempt.client_id,
                    account_id=attempt.account_id,
                    customer_name=account.customer_name or "Customer",
                    account_reference=account.account_reference or str(account.id),
                    attempt_public_id=attempt.public_id,
                    detail=attempt.error_message,
                )
            except Exception:  # noqa: BLE001
                logger.exception("checkout failed notify failed attempt=%s", public_id)
            raise ValidationAppError(
                "Unable to start payment. Please try again or contact support."
            ) from exc

        try:
            from app.modules.payflow.services.communication_service import (
                PayflowCommunicationService,
            )

            PayflowCommunicationService(self.db).mark_payment_event(
                account.id, event="initiated", commit=False
            )
        except Exception:  # noqa: BLE001
            logger.exception("mark payment initiated failed account_id=%s", account.id)

        self.db.commit()
        self.db.refresh(attempt)
        return self.serialize_attempt(attempt)

    def serialize_attempt(self, row: PayflowPaymentAttemptModel) -> dict[str, Any]:
        return {
            "id": row.public_id,
            "status": row.status,
            "option_type": row.option_type,
            "plan_id": row.plan_id,
            "amount": _money(row.amount),
            "currency_code": row.currency_code,
            "provider": row.provider,
            "checkout_url": row.checkout_url,
            "error_message": row.error_message,
            "balance_at_initiation": row.balance_at_initiation,
            "stripe_session_id": row.stripe_session_id,
            "reference": row.stripe_payment_intent_id or row.stripe_session_id or row.public_id[:12],
            "confirmed": row.status == PayflowPaymentAttemptStatus.SUCCEEDED.value,
            "events": list(row.events or []),
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }

    def get_attempt_public(self, token: str, attempt_public_id: str) -> dict[str, Any]:
        real_token, _ = self._strip_preview_token(token)
        link = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(PayflowPaymentLinkModel.token == real_token)
            .first()
        )
        if not link:
            raise NotFoundError("Payment link not found")
        row = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(
                PayflowPaymentAttemptModel.public_id == attempt_public_id,
                PayflowPaymentAttemptModel.payment_link_id == link.id,
            )
            .first()
        )
        if not row:
            raise NotFoundError("Payment attempt not found")
        # Stuck Processing → pull latest from Stripe (webhook may be delayed).
        if row.status == PayflowPaymentAttemptStatus.PROCESSING.value and stripe_configured():
            try:
                self.reconcile_attempt_from_stripe(row)
                self.db.refresh(row)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "stripe reconcile failed attempt=%s", attempt_public_id
                )
        return self.serialize_attempt(row)

    def reconcile_attempt_from_stripe(self, attempt: PayflowPaymentAttemptModel) -> bool:
        """Sync attempt from Stripe Session/PI. Returns True if status changed."""
        if attempt.status == PayflowPaymentAttemptStatus.SUCCEEDED.value:
            return False
        session_payload = None
        if attempt.stripe_session_id:
            session_payload = retrieve_checkout_session(attempt.stripe_session_id)
        if session_payload:
            pi = session_payload.get("payment_intent")
            if isinstance(pi, str) and pi:
                attempt.stripe_payment_intent_id = attempt.stripe_payment_intent_id or pi
            payment_status = (session_payload.get("payment_status") or "").lower()
            status = (session_payload.get("status") or "").lower()
            amount_total = session_payload.get("amount_total")
            if amount_total is not None:
                try:
                    stripe_amt = float(amount_total) / 100.0
                    if abs(stripe_amt - _money(attempt.amount)) > 0.009:
                        self._raise_processing_discrepancy(
                            attempt,
                            f"Stripe amount {stripe_amt:.2f} ≠ attempt {_money(attempt.amount):.2f}",
                            diagnostic_ref=str(attempt.stripe_session_id),
                        )
                        self.db.commit()
                        return False
                except (TypeError, ValueError):
                    pass
            if payment_status in {"paid", "no_payment_required"} or (
                status == "complete" and payment_status == "paid"
            ):
                self.apply_succeeded(
                    attempt,
                    provider_ref=attempt.stripe_payment_intent_id
                    or attempt.stripe_session_id,
                    commit=True,
                )
                return True
            if status == "expired":
                attempt.status = PayflowPaymentAttemptStatus.EXPIRED.value
                self._append_attempt_event(attempt, "Expired", "Reconcile from Stripe")
                self.db.commit()
                return True

        if attempt.stripe_payment_intent_id:
            pi_payload = retrieve_payment_intent(attempt.stripe_payment_intent_id)
            if pi_payload:
                pi_status = (pi_payload.get("status") or "").lower()
                if pi_status == "succeeded":
                    self.apply_succeeded(
                        attempt,
                        provider_ref=attempt.stripe_payment_intent_id,
                        commit=True,
                    )
                    return True
                if pi_status in {"canceled", "cancelled"}:
                    self.apply_failed(
                        attempt, error="PaymentIntent canceled (reconcile)", commit=True
                    )
                    return True
                if pi_status == "requires_payment_method":
                    # Customer abandoned / card failed before capture.
                    pass
        return False

    def cancel_attempt(self, token: str, attempt_public_id: str) -> dict[str, Any]:
        real_token, is_preview = self._strip_preview_token(token)
        if is_preview:
            raise ValidationAppError("Checkout cannot be started in preview mode")
        link = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(PayflowPaymentLinkModel.token == real_token)
            .first()
        )
        if not link:
            raise NotFoundError("Payment link not found")
        row = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(
                PayflowPaymentAttemptModel.public_id == attempt_public_id,
                PayflowPaymentAttemptModel.payment_link_id == link.id,
            )
            .first()
        )
        if not row:
            raise NotFoundError("Payment attempt not found")
        if row.status in {
            PayflowPaymentAttemptStatus.SUCCEEDED.value,
            PayflowPaymentAttemptStatus.FAILED.value,
            PayflowPaymentAttemptStatus.CANCELLED.value,
            PayflowPaymentAttemptStatus.EXPIRED.value,
        }:
            return self.serialize_attempt(row)
        row.status = PayflowPaymentAttemptStatus.CANCELLED.value
        self._append_attempt_event(row, "Cancelled", "Customer returned without paying")
        self.db.commit()
        self.db.refresh(row)
        return self.serialize_attempt(row)

    def simulate_mock_outcome(
        self, token: str, attempt_public_id: str, *, outcome: str
    ) -> dict[str, Any]:
        """Dev-only path when Stripe keys are unset. Never stores card details."""
        if stripe_configured():
            raise ValidationAppError("Mock outcomes are disabled when Stripe is configured")
        if (settings.APP_ENV or "").lower() not in {"development", "dev", "local", "test"}:
            raise ValidationAppError("Mock payment outcomes are only available in development")

        link = (
            self.db.query(PayflowPaymentLinkModel)
            .filter(PayflowPaymentLinkModel.token == token)
            .first()
        )
        if not link:
            raise NotFoundError("Payment link not found")
        row = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(
                PayflowPaymentAttemptModel.public_id == attempt_public_id,
                PayflowPaymentAttemptModel.payment_link_id == link.id,
            )
            .first()
        )
        if not row:
            raise NotFoundError("Payment attempt not found")
        if row.provider != "mock":
            raise ValidationAppError("Attempt is not a mock checkout")

        outcome_norm = (outcome or "").strip().lower()
        if outcome_norm == "success":
            self.apply_succeeded(row, provider_ref=f"mock_{row.public_id[:12]}", commit=True)
        elif outcome_norm in {"fail", "failed", "failure"}:
            self.apply_failed(row, error="Mock payment failed", commit=True)
        elif outcome_norm in {"cancel", "cancelled", "canceled"}:
            row.status = PayflowPaymentAttemptStatus.CANCELLED.value
            self._append_attempt_event(row, "Cancelled", "Mock cancel")
            self.db.commit()
        else:
            raise ValidationAppError("outcome must be success, failed, or cancel")
        self.db.refresh(row)
        return self.serialize_attempt(row)

    def _raise_ops_exception(self, attempt: PayflowPaymentAttemptModel, reason: str) -> None:
        try:
            from app.modules.payflow.services.operational_exception_service import (
                PayflowOperationalExceptionService,
            )
            from app.shared.enums import PayflowOperationalExceptionType

            PayflowOperationalExceptionService(self.db).raise_exception(
                exception_type=PayflowOperationalExceptionType.PAYMENT_PROCESSING_ISSUE.value,
                reason=reason[:500],
                client_id=attempt.client_id,
                account_id=attempt.account_id,
                source_entity_type="payment_attempt",
                source_entity_id=attempt.public_id,
                severity="High",
                retriable=True,
                diagnostic_ref=attempt.stripe_session_id or attempt.public_id,
                link=f"/payflow/cases/{attempt.account_id}",
                notify=True,
                commit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to raise payment ops exception")

    def _raise_processing_discrepancy(
        self,
        attempt: PayflowPaymentAttemptModel,
        reason: str,
        *,
        diagnostic_ref: str | None = None,
    ) -> None:
        try:
            from app.modules.payflow.services.operational_exception_service import (
                PayflowOperationalExceptionService,
            )
            from app.shared.enums import PayflowOperationalExceptionType

            PayflowOperationalExceptionService(self.db).raise_exception(
                exception_type=PayflowOperationalExceptionType.PROCESSING_DISCREPANCY.value,
                reason=reason[:500],
                client_id=attempt.client_id,
                account_id=attempt.account_id,
                source_entity_type="payment_attempt",
                source_entity_id=attempt.public_id,
                severity="High",
                retriable=False,
                diagnostic_ref=diagnostic_ref or attempt.stripe_session_id or attempt.public_id,
                link=f"/payflow/cases/{attempt.account_id}",
                notify=True,
                commit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to raise processing_discrepancy")

    def apply_succeeded(
        self,
        attempt: PayflowPaymentAttemptModel,
        *,
        provider_ref: str | None = None,
        commit: bool = True,
    ) -> None:
        from app.modules.payflow.services.payment_outcome_service import (
            PayflowPaymentOutcomeService,
        )

        # Row lock reduces concurrent double-apply from webhook races.
        locked = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(PayflowPaymentAttemptModel.id == attempt.id)
            .with_for_update()
            .first()
        )
        if locked is not None:
            attempt = locked

        outcome = PayflowPaymentOutcomeService(self.db)
        ref = provider_ref or attempt.stripe_payment_intent_id
        # Ledger exists for this attempt OR same provider_ref → never cut balance again.
        existing = outcome._txn_by_attempt(attempt.id)
        if not existing and ref:
            existing = outcome._txn_by_provider_ref(ref)
        if existing:
            attempt.status = PayflowPaymentAttemptStatus.SUCCEEDED.value
            attempt.completed_at = attempt.completed_at or _utcnow()
            if ref:
                attempt.stripe_payment_intent_id = attempt.stripe_payment_intent_id or ref
            if commit:
                self.db.commit()
            else:
                self.db.flush()
            return

        account = self.db.get(PayflowAccountModel, attempt.account_id)
        amount = _money(attempt.amount)
        prev = _money(account.outstanding_balance) if account else 0.0
        new_bal = prev
        if account:
            new_bal = max(0.0, _money(prev - amount))
            account.outstanding_balance = new_bal
            account.recovered_balance = _money((account.recovered_balance or 0) + amount)
            account.last_payment_amount = amount
            account.last_payment_date = _utcnow().date()
            account.paid_amount = _money((account.paid_amount or 0) + amount)
            if new_bal <= 0 or attempt.option_type == PayflowPaymentOptionType.PAY_IN_FULL.value:
                account.collection_status = "Resolved"
            elif attempt.option_type in {
                PayflowPaymentOptionType.PAYMENT_PLAN.value,
                PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
            }:
                account.collection_status = "Payment Plan"
            # Partial keeps Active / existing status unless settled.

        attempt.status = PayflowPaymentAttemptStatus.SUCCEEDED.value
        attempt.completed_at = _utcnow()
        if ref:
            attempt.stripe_payment_intent_id = attempt.stripe_payment_intent_id or ref
        self._append_attempt_event(attempt, "Succeeded", ref)

        txn = None
        plan = None
        if account:
            txn = outcome.record_confirmed_payment(
                attempt,
                account,
                amount=amount,
                balance_before=prev,
                balance_after=new_bal,
                provider_ref=ref or attempt.stripe_payment_intent_id,
            )
            if attempt.option_type == PayflowPaymentOptionType.PAYMENT_PLAN.value:
                plan = outcome.ensure_payment_plan_for_attempt(
                    attempt,
                    account,
                    first_txn=txn,
                    balance_at_plan_start=prev,
                )
            elif attempt.option_type == PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value:
                plan = (
                    self.db.query(PayflowPaymentPlanModel)
                    .filter(
                        PayflowPaymentPlanModel.account_id == account.id,
                        PayflowPaymentPlanModel.public_id == attempt.plan_id,
                    )
                    .first()
                )
                if plan is None:
                    plan = (
                        self.db.query(PayflowPaymentPlanModel)
                        .filter(
                            PayflowPaymentPlanModel.account_id == account.id,
                            PayflowPaymentPlanModel.status.in_(["Active", "Overdue"]),
                        )
                        .order_by(PayflowPaymentPlanModel.id.desc())
                        .first()
                    )
                if plan is not None:
                    outcome._apply_payment_to_plan(plan, attempt, txn)
            outcome._append_account_timeline(
                account,
                "Payment Confirmed",
                f"{amount:.2f} {attempt.currency_code} · {attempt.option_type} · {txn.public_id}",
            )

        # Stop collection links only on full settlement — not on partial/plan first instalment.
        if account and new_bal <= 0:
            self.links.mark_completed_for_account(attempt.account_id, commit=False)

        try:
            from app.modules.payflow.services.workflow_execution_service import (
                WorkflowExecutionService,
            )

            WorkflowExecutionService(self.db).on_payment_event(
                attempt.account_id,
                {
                    "amount": amount,
                    # verified money received (comm payment_completed) — not only full settlement
                    "completed": True,
                    "case_settled": new_bal <= 0,
                    "partial": attempt.option_type == PayflowPaymentOptionType.PARTIAL.value,
                    "payment_plan": attempt.option_type
                    in {
                        PayflowPaymentOptionType.PAYMENT_PLAN.value,
                        PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
                    },
                    "plan_id": plan.public_id if plan else None,
                    "status": "succeeded",
                    "source": "stripe" if attempt.provider == "stripe" else "mock",
                    "attempt_id": attempt.public_id,
                    "transaction_id": txn.public_id if txn else None,
                    "payment_date": _utcnow().date().isoformat(),
                },
                commit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("workflow payment event failed")

        if commit:
            self.db.commit()
        else:
            self.db.flush()

        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            customer = account.customer_name if account else "Customer"
            ref = account.account_reference if account else str(attempt.account_id)
            PayflowNotificationService(self.db).notify_payment_received(
                client_id=attempt.client_id,
                account_id=attempt.account_id,
                customer_name=customer,
                account_reference=ref,
                amount=amount,
                currency_code=attempt.currency_code or "USD",
                attempt_public_id=attempt.public_id,
                option_type=attempt.option_type,
                resolved=new_bal <= 0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("payment received notify failed attempt=%s", attempt.public_id)

    def apply_failed(
        self,
        attempt: PayflowPaymentAttemptModel,
        *,
        error: str | None = None,
        commit: bool = True,
    ) -> None:
        if attempt.status == PayflowPaymentAttemptStatus.SUCCEEDED.value:
            return
        if attempt.status == PayflowPaymentAttemptStatus.FAILED.value:
            return
        attempt.status = PayflowPaymentAttemptStatus.FAILED.value
        attempt.error_message = (error or attempt.error_message or "Payment failed")[:500]
        attempt.completed_at = _utcnow()
        self._append_attempt_event(attempt, "Failed", attempt.error_message)
        self._raise_ops_exception(attempt, attempt.error_message or "Payment failed")
        if commit:
            self.db.commit()
        else:
            self.db.flush()

        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            account = self.db.get(PayflowAccountModel, attempt.account_id)
            customer = account.customer_name if account else "Customer"
            ref = account.account_reference if account else str(attempt.account_id)
            PayflowNotificationService(self.db).notify_payment_failed(
                client_id=attempt.client_id,
                account_id=attempt.account_id,
                customer_name=customer,
                account_reference=ref,
                attempt_public_id=attempt.public_id,
                detail=attempt.error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("payment failed notify failed attempt=%s", attempt.public_id)

    def handle_stripe_event(self, event: dict[str, Any]) -> dict[str, Any]:
        et = str(event.get("type") or "")
        data_object = (event.get("data") or {}).get("object") or {}

        # Refunds / chargebacks — adjust ledger without deleting originals.
        if et in {"charge.refunded", "refund.created", "charge.dispute.funds_withdrawn"}:
            try:
                from app.modules.payflow.services.payment_outcome_service import (
                    PayflowPaymentOutcomeService,
                )

                pi = data_object.get("payment_intent")
                charge_id = data_object.get("id")
                amount_refunded = data_object.get("amount_refunded") or data_object.get("amount")
                amt = None
                if amount_refunded is not None:
                    amt = float(amount_refunded) / 100.0
                ref = None
                if isinstance(pi, str) and pi:
                    ref = pi
                elif charge_id:
                    # Find txn by provider_ref containing charge or use charge id.
                    ref = str(charge_id)
                direction = "reversal" if "dispute" in et else "refund"
                if ref:
                    adj = PayflowPaymentOutcomeService(self.db).apply_refund_or_reversal(
                        original_provider_ref=ref,
                        amount=amt,
                        direction=direction,
                        provider_ref=f"{et}:{data_object.get('id')}",
                        notes=f"Stripe {et}",
                        commit=True,
                    )
                    return {
                        "matched": True,
                        "type": et,
                        "adjustment": adj.public_id,
                        "status": adj.status,
                    }
            except Exception as exc:  # noqa: BLE001
                logger.exception("stripe refund/reversal handling failed")
                return {"matched": False, "reason": str(exc)[:200], "type": et}

        session_id = data_object.get("id") if data_object.get("object") == "checkout.session" else None
        if not session_id and "checkout.session" in et:
            session_id = data_object.get("id")

        pi_id = None
        if data_object.get("object") == "payment_intent":
            pi_id = data_object.get("id")
        else:
            pi_raw = data_object.get("payment_intent")
            if isinstance(pi_raw, str):
                pi_id = pi_raw
            elif isinstance(pi_raw, dict):
                pi_id = pi_raw.get("id")

        attempt: PayflowPaymentAttemptModel | None = None
        if session_id:
            attempt = (
                self.db.query(PayflowPaymentAttemptModel)
                .filter(PayflowPaymentAttemptModel.stripe_session_id == session_id)
                .first()
            )
        if not attempt and pi_id:
            attempt = (
                self.db.query(PayflowPaymentAttemptModel)
                .filter(PayflowPaymentAttemptModel.stripe_payment_intent_id == pi_id)
                .first()
            )
        if not attempt:
            meta = data_object.get("metadata") or {}
            public_id = meta.get("attempt_public_id") or data_object.get("client_reference_id")
            if public_id:
                attempt = (
                    self.db.query(PayflowPaymentAttemptModel)
                    .filter(PayflowPaymentAttemptModel.public_id == str(public_id))
                    .first()
                )
        if not attempt:
            logger.warning(
                "stripe event unmatched type=%s session=%s pi=%s",
                et,
                session_id,
                pi_id,
            )
            return {"matched": False, "reason": "attempt_not_found", "type": et}

        if pi_id:
            attempt.stripe_payment_intent_id = attempt.stripe_payment_intent_id or pi_id

        if et in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            # Only treat as paid when Stripe reports paid / complete.
            payment_status = (data_object.get("payment_status") or "").lower()
            status = (data_object.get("status") or "").lower()
            amount_total = data_object.get("amount_total")
            amount_mismatch = False
            if amount_total is not None:
                try:
                    stripe_amt = float(amount_total) / 100.0
                    expected = _money(attempt.amount)
                    if abs(stripe_amt - expected) > 0.009:
                        amount_mismatch = True
                        self._raise_processing_discrepancy(
                            attempt,
                            f"Stripe amount {stripe_amt:.2f} ≠ attempt {expected:.2f}",
                            diagnostic_ref=str(session_id or attempt.public_id),
                        )
                except (TypeError, ValueError):
                    pass
            if amount_mismatch:
                self.db.commit()
                return {
                    "matched": True,
                    "status": attempt.status,
                    "type": et,
                    "amount_mismatch": True,
                }
            # Require Stripe payment_status=paid (do not confirm on URL / session complete alone).
            if payment_status in {"paid", "no_payment_required"}:
                self.apply_succeeded(
                    attempt,
                    provider_ref=attempt.stripe_payment_intent_id or session_id,
                    commit=True,
                )
                return {"matched": True, "status": attempt.status, "type": et}
            attempt.status = PayflowPaymentAttemptStatus.PROCESSING.value
            self._append_attempt_event(
                attempt, "Processing", f"{et} payment_status={payment_status or status}"
            )
            self.db.commit()
            return {"matched": True, "status": attempt.status, "type": et}

        if et in {
            "checkout.session.expired",
            "checkout.session.async_payment_failed",
        }:
            if et.endswith("expired"):
                if attempt.status != PayflowPaymentAttemptStatus.SUCCEEDED.value:
                    attempt.status = PayflowPaymentAttemptStatus.EXPIRED.value
                    self._append_attempt_event(attempt, "Expired", et)
                    self.db.commit()
            else:
                self.apply_failed(attempt, error=f"Stripe event: {et}", commit=True)
            return {"matched": True, "status": attempt.status, "type": et}

        if et in {"payment_intent.payment_failed", "payment_intent.canceled"}:
            self.apply_failed(attempt, error=f"Stripe event: {et}", commit=True)
            return {"matched": True, "status": attempt.status, "type": et}

        if et == "payment_intent.succeeded":
            amount_received = data_object.get("amount_received") or data_object.get("amount")
            if amount_received is not None:
                try:
                    stripe_amt = float(amount_received) / 100.0
                    if abs(stripe_amt - _money(attempt.amount)) > 0.009:
                        self._raise_processing_discrepancy(
                            attempt,
                            f"PI amount {stripe_amt:.2f} ≠ attempt {_money(attempt.amount):.2f}",
                            diagnostic_ref=str(pi_id or attempt.public_id),
                        )
                        self.db.commit()
                        return {
                            "matched": True,
                            "status": attempt.status,
                            "type": et,
                            "amount_mismatch": True,
                        }
                except (TypeError, ValueError):
                    pass
            self.apply_succeeded(
                attempt, provider_ref=pi_id or data_object.get("id") or session_id, commit=True
            )
            return {"matched": True, "status": attempt.status, "type": et}

        self._append_attempt_event(attempt, "Stripe Event", et)
        self.db.commit()
        return {"matched": True, "ignored": True, "type": et, "status": attempt.status}


# Keep public_url helper importable for callers that only need URL composition.
__all__ = ["PayflowPaymentPortalService", "public_url_for_token"]
