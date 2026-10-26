"""Verified payment ledger, balance allocation, payment plans, refunds/reversals."""

from __future__ import annotations

import logging
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
    PayflowPaymentTransactionModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import (
    PayflowPaymentInstallmentStatus,
    PayflowPaymentOptionType,
    PayflowPaymentPlanStatus,
    PayflowPaymentTxnDirection,
    PayflowPaymentTxnStatus,
)

logger = logging.getLogger("app.payflow.payment_outcome")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: float) -> float:
    return round(float(value or 0), 2)


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _add_period(d: date, index: int, frequency: str) -> date:
    from app.modules.payflow.services.plan_schedule import add_period

    return add_period(d, index, frequency)


class PayflowPaymentOutcomeService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_case_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Payment history access denied")
        if not (
            self.access.can(user, "view_collection_cases", client_id)
            or self.access.can(user, "view_customer_accounts", client_id)
        ):
            raise ForbiddenError("View Cases / Accounts permission required")

    def _append_account_timeline(self, account: PayflowAccountModel, label: str, detail: str) -> None:
        events = list(account.timeline or [])
        events.append(
            {
                "at": _utcnow().isoformat(),
                "label": label,
                "detail": detail,
                "source": "payment",
            }
        )
        account.timeline = events[-80:]

    def _txn_by_attempt(self, attempt_id: int) -> PayflowPaymentTransactionModel | None:
        return (
            self.db.query(PayflowPaymentTransactionModel)
            .filter(
                PayflowPaymentTransactionModel.payment_attempt_id == attempt_id,
                PayflowPaymentTransactionModel.direction
                == PayflowPaymentTxnDirection.PAYMENT.value,
                PayflowPaymentTransactionModel.status
                == PayflowPaymentTxnStatus.SUCCEEDED.value,
            )
            .first()
        )

    def _txn_by_provider_ref(self, provider_ref: str | None) -> PayflowPaymentTransactionModel | None:
        if not provider_ref:
            return None
        return (
            self.db.query(PayflowPaymentTransactionModel)
            .filter(PayflowPaymentTransactionModel.provider_ref == provider_ref)
            .first()
        )

    def record_confirmed_payment(
        self,
        attempt: PayflowPaymentAttemptModel,
        account: PayflowAccountModel,
        *,
        amount: float,
        balance_before: float,
        balance_after: float,
        provider_ref: str | None,
    ) -> PayflowPaymentTransactionModel:
        """Idempotent ledger row for a succeeded attempt. Does not change balances."""
        existing = self._txn_by_attempt(attempt.id)
        if existing:
            return existing
        if provider_ref:
            by_ref = self._txn_by_provider_ref(provider_ref)
            if by_ref:
                return by_ref

        public_id = f"txn_{uuid.uuid4().hex[:20]}"
        # Ensure unique provider_ref when missing.
        ref = provider_ref or f"{attempt.provider}_{attempt.public_id}"
        if self._txn_by_provider_ref(ref):
            ref = f"{ref}_{public_id[-6:]}"

        txn = PayflowPaymentTransactionModel(
            public_id=public_id,
            client_id=attempt.client_id,
            account_id=attempt.account_id,
            payment_attempt_id=attempt.id,
            direction=PayflowPaymentTxnDirection.PAYMENT.value,
            status=PayflowPaymentTxnStatus.SUCCEEDED.value,
            option_type=attempt.option_type,
            amount=_money(amount),
            currency_code=attempt.currency_code or account.currency_code or "USD",
            provider=attempt.provider,
            provider_ref=ref,
            method_label="Stripe Checkout" if attempt.provider == "stripe" else "Mock checkout",
            balance_before=_money(balance_before),
            balance_after=_money(balance_after),
            recovered_delta=_money(amount),
            events=[{"at": _utcnow().isoformat(), "label": "Recorded", "detail": "Verified payment"}],
            completed_at=_utcnow(),
        )
        self.db.add(txn)
        self.db.flush()
        return txn

    def _plan_quote_from_attempt(self, attempt: PayflowPaymentAttemptModel) -> dict[str, Any]:
        payload = attempt.provider_payload if isinstance(attempt.provider_payload, dict) else {}
        quote = payload.get("plan_quote")
        return quote if isinstance(quote, dict) else {}

    def attach_stripe_payment_method(
        self,
        plan: PayflowPaymentPlanModel,
        *,
        customer_id: str | None,
        payment_method_id: str | None,
        payment_method_type: str | None = None,
    ) -> None:
        if customer_id:
            plan.stripe_customer_id = str(customer_id)[:255]
        if payment_method_id:
            plan.stripe_payment_method_id = str(payment_method_id)[:255]
        if payment_method_type:
            plan.payment_method_type = str(payment_method_type)[:32]
        if plan.stripe_customer_id and plan.stripe_payment_method_id:
            plan.auto_charge_enabled = True

    def ensure_payment_plan_for_attempt(
        self,
        attempt: PayflowPaymentAttemptModel,
        account: PayflowAccountModel,
        *,
        first_txn: PayflowPaymentTransactionModel | None,
        balance_at_plan_start: float,
        setup_only: bool = False,
        stripe_customer_id: str | None = None,
        stripe_payment_method_id: str | None = None,
        payment_method_type: str | None = None,
    ) -> PayflowPaymentPlanModel | None:
        if attempt.option_type != PayflowPaymentOptionType.PAYMENT_PLAN.value:
            return None
        existing = (
            self.db.query(PayflowPaymentPlanModel)
            .filter(
                PayflowPaymentPlanModel.account_id == account.id,
                PayflowPaymentPlanModel.status.in_(
                    [
                        PayflowPaymentPlanStatus.ACTIVE.value,
                        PayflowPaymentPlanStatus.OVERDUE.value,
                    ]
                ),
            )
            .order_by(PayflowPaymentPlanModel.id.desc())
            .first()
        )
        if existing:
            if first_txn is not None and not setup_only:
                self._apply_payment_to_plan(existing, attempt, first_txn)
            self.attach_stripe_payment_method(
                existing,
                customer_id=stripe_customer_id,
                payment_method_id=stripe_payment_method_id,
                payment_method_type=payment_method_type,
            )
            return existing

        quote = self._plan_quote_from_attempt(attempt)
        schedule = list(quote.get("schedule") or [])
        frequency = str(quote.get("frequency") or "monthly").strip().lower()
        if frequency not in {"daily", "weekly", "monthly"}:
            frequency = "monthly"

        if schedule:
            installments_n = len(schedule)
            installment_amt = _money(quote.get("installment_amount") or attempt.amount or 0)
            total = _money(quote.get("total_payable") or balance_at_plan_start)
            try:
                y, m, d = [int(x) for x in str(quote.get("start_date")).split("-")[:3]]
                start = date(y, m, d)
            except (TypeError, ValueError, AttributeError):
                start = _utcnow().date()
        else:
            # Legacy fallback: fixed N from plan_id like plan_6
            installments_n = 3
            if attempt.plan_id and attempt.plan_id.startswith("plan_"):
                try:
                    installments_n = max(2, int(attempt.plan_id.rsplit("_", 1)[-1]))
                except ValueError:
                    pass
            total = _money(balance_at_plan_start) or _money(attempt.amount * installments_n)
            installment_amt = _money(attempt.amount) if attempt.amount else _money(total / installments_n)
            start = _utcnow().date()
            remaining = total
            schedule = []
            for i in range(1, installments_n + 1):
                if i == installments_n:
                    amt = _money(remaining)
                else:
                    amt = min(installment_amt, _money(remaining))
                    remaining = _money(remaining - amt)
                schedule.append(
                    {
                        "sequence": i,
                        "amount": amt,
                        "due_date": _add_period(start, i - 1, frequency).isoformat(),
                    }
                )

        paid_first = (not setup_only) and _money(attempt.amount) > 0 and first_txn is not None
        amount_paid = _money(attempt.amount) if paid_first else 0.0
        next_due = None
        if schedule:
            if paid_first and len(schedule) > 1:
                next_due = date.fromisoformat(str(schedule[1]["due_date"])[:10])
            elif not paid_first:
                next_due = date.fromisoformat(str(schedule[0]["due_date"])[:10])

        plan = PayflowPaymentPlanModel(
            public_id=f"plan_{uuid.uuid4().hex[:16]}",
            client_id=attempt.client_id,
            account_id=account.id,
            payment_link_id=attempt.payment_link_id,
            plan_option_id=attempt.plan_id,
            label=str(quote.get("label") or f"{installments_n} {frequency} instalments"),
            status=PayflowPaymentPlanStatus.ACTIVE.value,
            installments_total=installments_n,
            installment_amount=installment_amt,
            frequency=frequency,
            currency_code=attempt.currency_code or "USD",
            total_payable=total,
            amount_paid=amount_paid,
            start_date=start,
            next_due_date=next_due,
            auto_charge_enabled=True,
            consecutive_charge_failures=0,
            events=[
                {
                    "at": _utcnow().isoformat(),
                    "label": "Created",
                    "detail": (
                        "Payment method saved — first charge on start date"
                        if setup_only
                        else "First instalment paid"
                    ),
                }
            ],
        )
        self.attach_stripe_payment_method(
            plan,
            customer_id=stripe_customer_id,
            payment_method_id=stripe_payment_method_id,
            payment_method_type=payment_method_type,
        )
        self.db.add(plan)
        self.db.flush()

        for row in schedule:
            seq = int(row.get("sequence") or 0)
            amt = _money(row.get("amount") or 0)
            try:
                due = date.fromisoformat(str(row.get("due_date"))[:10])
            except (TypeError, ValueError):
                due = _add_period(start, max(0, seq - 1), frequency)
            is_first_paid = paid_first and seq == 1
            inst = PayflowPaymentPlanInstallmentModel(
                plan_id=plan.id,
                sequence=seq,
                amount=amt,
                due_date=due,
                status=(
                    PayflowPaymentInstallmentStatus.PAID.value
                    if is_first_paid
                    else PayflowPaymentInstallmentStatus.PENDING.value
                ),
                paid_at=_utcnow() if is_first_paid else None,
                payment_attempt_id=attempt.id if is_first_paid else None,
                payment_transaction_public_id=first_txn.public_id if is_first_paid and first_txn else None,
            )
            self.db.add(inst)
        self.db.flush()
        if first_txn is not None:
            first_txn.payment_plan_id = plan.id
        account.collection_status = "Payment Plan"
        return plan

    def record_auto_charge_failure(
        self,
        plan: PayflowPaymentPlanModel,
        *,
        installment: PayflowPaymentPlanInstallmentModel | None,
        detail: str | None = None,
    ) -> None:
        plan.consecutive_charge_failures = int(plan.consecutive_charge_failures or 0) + 1
        if installment is not None:
            installment.status = PayflowPaymentInstallmentStatus.FAILED.value
        events = list(plan.events or [])
        events.append(
            {
                "at": _utcnow().isoformat(),
                "label": "Auto-charge Failed",
                "detail": (detail or "Off-session charge failed")[:240],
            }
        )
        if plan.consecutive_charge_failures >= 3:
            plan.auto_charge_enabled = False
            events.append(
                {
                    "at": _utcnow().isoformat(),
                    "label": "Auto-charge Paused",
                    "detail": "Paused after 3 consecutive failures — customer can pay next instalment manually",
                }
            )
            try:
                from app.modules.payflow.services.workflow_execution_service import (
                    WorkflowExecutionService,
                )

                WorkflowExecutionService(self.db).on_payment_event(
                    plan.account_id,
                    {
                        "payment_plan": True,
                        "plan_id": plan.public_id,
                        "status": "auto_charge_paused",
                        "source": "stripe",
                    },
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception("workflow auto-charge pause event failed plan=%s", plan.public_id)
        plan.events = events[-60:]

    def _apply_payment_to_plan(
        self,
        plan: PayflowPaymentPlanModel,
        attempt: PayflowPaymentAttemptModel,
        txn: PayflowPaymentTransactionModel,
    ) -> None:
        # Prefer installment_sequence from attempt provider_payload when present.
        payload = attempt.provider_payload if isinstance(attempt.provider_payload, dict) else {}
        seq = payload.get("installment_sequence")
        inst = None
        if seq is not None:
            try:
                inst = (
                    self.db.query(PayflowPaymentPlanInstallmentModel)
                    .filter(
                        PayflowPaymentPlanInstallmentModel.plan_id == plan.id,
                        PayflowPaymentPlanInstallmentModel.sequence == int(seq),
                        PayflowPaymentPlanInstallmentModel.status.in_(
                            [
                                PayflowPaymentInstallmentStatus.PENDING.value,
                                PayflowPaymentInstallmentStatus.OVERDUE.value,
                                PayflowPaymentInstallmentStatus.FAILED.value,
                            ]
                        ),
                    )
                    .first()
                )
            except (TypeError, ValueError):
                inst = None
        if inst is None:
            inst = (
                self.db.query(PayflowPaymentPlanInstallmentModel)
                .filter(
                    PayflowPaymentPlanInstallmentModel.plan_id == plan.id,
                    PayflowPaymentPlanInstallmentModel.status.in_(
                        [
                            PayflowPaymentInstallmentStatus.PENDING.value,
                            PayflowPaymentInstallmentStatus.OVERDUE.value,
                            PayflowPaymentInstallmentStatus.FAILED.value,
                        ]
                    ),
                )
                .order_by(PayflowPaymentPlanInstallmentModel.sequence.asc())
                .first()
            )
        if not inst:
            return
        inst.status = PayflowPaymentInstallmentStatus.PAID.value
        inst.paid_at = _utcnow()
        inst.payment_attempt_id = attempt.id
        inst.payment_transaction_public_id = txn.public_id
        plan.amount_paid = _money((plan.amount_paid or 0) + attempt.amount)
        plan.consecutive_charge_failures = 0
        txn.payment_plan_id = plan.id
        txn.installment_id = inst.id
        next_pending = (
            self.db.query(PayflowPaymentPlanInstallmentModel)
            .filter(
                PayflowPaymentPlanInstallmentModel.plan_id == plan.id,
                PayflowPaymentPlanInstallmentModel.status.in_(
                    [
                        PayflowPaymentInstallmentStatus.PENDING.value,
                        PayflowPaymentInstallmentStatus.OVERDUE.value,
                        PayflowPaymentInstallmentStatus.FAILED.value,
                    ]
                ),
            )
            .order_by(PayflowPaymentPlanInstallmentModel.sequence.asc())
            .first()
        )
        if next_pending:
            plan.next_due_date = next_pending.due_date
            plan.status = PayflowPaymentPlanStatus.ACTIVE.value
        else:
            plan.status = PayflowPaymentPlanStatus.COMPLETED.value
            plan.next_due_date = None
            plan.completed_at = _utcnow()
            plan.auto_charge_enabled = False
        events = list(plan.events or [])
        events.append(
            {
                "at": _utcnow().isoformat(),
                "label": "Instalment Paid",
                "detail": f"#{inst.sequence} · {attempt.amount:.2f}",
            }
        )
        plan.events = events[-60:]

    def refresh_overdue_plans(self, account_id: int | None = None) -> int:
        """Mark missed instalments Overdue; emit workflow event. No auto-charge."""
        today = _utcnow().date()
        q = self.db.query(PayflowPaymentPlanInstallmentModel).filter(
            PayflowPaymentPlanInstallmentModel.status
            == PayflowPaymentInstallmentStatus.PENDING.value,
            PayflowPaymentPlanInstallmentModel.due_date < today,
        )
        if account_id is not None:
            q = q.join(PayflowPaymentPlanModel).filter(
                PayflowPaymentPlanModel.account_id == account_id
            )
        rows = q.all()
        touched_plans: set[int] = set()
        for inst in rows:
            inst.status = PayflowPaymentInstallmentStatus.OVERDUE.value
            touched_plans.add(inst.plan_id)
        for plan_id in touched_plans:
            plan = self.db.get(PayflowPaymentPlanModel, plan_id)
            if not plan:
                continue
            plan.status = PayflowPaymentPlanStatus.OVERDUE.value
            events = list(plan.events or [])
            events.append(
                {
                    "at": _utcnow().isoformat(),
                    "label": "Instalment Overdue",
                    "detail": "Missed due date — workflow reassessment required",
                }
            )
            plan.events = events[-60:]
            try:
                from app.modules.payflow.services.workflow_execution_service import (
                    WorkflowExecutionService,
                )

                WorkflowExecutionService(self.db).on_payment_event(
                    plan.account_id,
                    {
                        "status": "plan_missed",
                        "completed": False,
                        "plan_id": plan.public_id,
                        "reason": "Payment plan instalment overdue",
                    },
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception("plan overdue workflow event failed plan=%s", plan_id)
        if rows:
            self.db.flush()
        return len(rows)

    def apply_refund_or_reversal(
        self,
        *,
        original_provider_ref: str | None = None,
        original_txn_public_id: str | None = None,
        amount: float | None = None,
        direction: str = "refund",
        provider_ref: str | None = None,
        notes: str | None = None,
        commit: bool = True,
    ) -> PayflowPaymentTransactionModel:
        """Create adjustment row and restore outstanding balance. Never deletes original."""
        original = None
        if original_txn_public_id:
            original = (
                self.db.query(PayflowPaymentTransactionModel)
                .filter(PayflowPaymentTransactionModel.public_id == original_txn_public_id)
                .first()
            )
        elif original_provider_ref:
            original = self._txn_by_provider_ref(original_provider_ref)
        if not original:
            raise NotFoundError("Original payment transaction not found")
        if original.direction != PayflowPaymentTxnDirection.PAYMENT.value:
            raise ValidationAppError("Can only refund/reverse a payment transaction")

        adj_ref = provider_ref or f"{direction}_{original.public_id}_{uuid.uuid4().hex[:8]}"
        existing_adj = self._txn_by_provider_ref(adj_ref)
        if existing_adj:
            return existing_adj

        amt = _money(amount if amount is not None else original.amount)
        if amt <= 0 or amt > _money(original.amount) + 0.001:
            raise ValidationAppError("Invalid refund/reversal amount")

        account = self.db.get(PayflowAccountModel, original.account_id)
        if not account:
            raise NotFoundError("Account not found")

        before = _money(account.outstanding_balance)
        after = _money(before + amt)
        account.outstanding_balance = after
        account.recovered_balance = max(0.0, _money((account.recovered_balance or 0) - amt))
        if (account.collection_status or "").lower() in {"resolved", "closed", "paid"}:
            account.collection_status = "Active"

        direction_norm = (
            PayflowPaymentTxnDirection.REFUND.value
            if direction == "refund"
            else PayflowPaymentTxnDirection.REVERSAL.value
        )
        status = (
            PayflowPaymentTxnStatus.REFUNDED.value
            if direction_norm == PayflowPaymentTxnDirection.REFUND.value
            else PayflowPaymentTxnStatus.REVERSED.value
        )
        # Mark original as refunded/reversed for display (keep amount history).
        if direction_norm == PayflowPaymentTxnDirection.REFUND.value:
            original.status = PayflowPaymentTxnStatus.REFUNDED.value
        else:
            original.status = PayflowPaymentTxnStatus.REVERSED.value

        adj = PayflowPaymentTransactionModel(
            public_id=f"txn_{uuid.uuid4().hex[:20]}",
            client_id=original.client_id,
            account_id=original.account_id,
            payment_attempt_id=original.payment_attempt_id,
            payment_plan_id=original.payment_plan_id,
            installment_id=original.installment_id,
            direction=direction_norm,
            status=status,
            option_type=original.option_type,
            amount=amt,
            currency_code=original.currency_code,
            provider=original.provider,
            provider_ref=adj_ref,
            method_label=original.method_label,
            balance_before=before,
            balance_after=after,
            recovered_delta=_money(-amt),
            related_transaction_public_id=original.public_id,
            notes=notes,
            events=[{"at": _utcnow().isoformat(), "label": direction_norm.title(), "detail": notes}],
            completed_at=_utcnow(),
        )
        self.db.add(adj)
        self._append_account_timeline(
            account,
            f"Payment {direction_norm.title()}",
            f"{amt:.2f} {original.currency_code} · ref {adj.public_id}",
        )
        if commit:
            self.db.commit()
            self.db.refresh(adj)
        else:
            self.db.flush()
        return adj

    def serialize_txn(self, row: PayflowPaymentTransactionModel) -> dict[str, Any]:
        return {
            "id": row.public_id,
            "status": row.status,
            "direction": row.direction,
            "option_type": row.option_type,
            "amount": _money(row.amount),
            "currency_code": row.currency_code,
            "provider": row.provider,
            "provider_ref": row.provider_ref,
            "method": row.method_label,
            "reference": row.public_id,
            "balance_before": row.balance_before,
            "balance_after": row.balance_after,
            "recovered_delta": row.recovered_delta,
            "related_transaction_id": row.related_transaction_public_id,
            "payment_attempt_id": row.payment_attempt_id,
            "payment_plan_id": row.payment_plan_id,
            "notes": row.notes,
            "completed_at": row.completed_at,
            "created_at": row.created_at,
            "is_confirmed": row.status == PayflowPaymentTxnStatus.SUCCEEDED.value
            and row.direction == PayflowPaymentTxnDirection.PAYMENT.value,
        }

    def serialize_plan(self, plan: PayflowPaymentPlanModel) -> dict[str, Any]:
        self.refresh_overdue_plans(plan.account_id)
        self.db.refresh(plan)
        installments = (
            self.db.query(PayflowPaymentPlanInstallmentModel)
            .filter(PayflowPaymentPlanInstallmentModel.plan_id == plan.id)
            .order_by(PayflowPaymentPlanInstallmentModel.sequence.asc())
            .all()
        )
        return {
            "id": plan.public_id,
            "status": plan.status,
            "label": plan.label,
            "plan_option_id": plan.plan_option_id,
            "installments_total": plan.installments_total,
            "installment_amount": _money(plan.installment_amount),
            "frequency": plan.frequency,
            "currency_code": plan.currency_code,
            "total_payable": _money(plan.total_payable),
            "amount_paid": _money(plan.amount_paid),
            "amount_remaining": max(0.0, _money(plan.total_payable - (plan.amount_paid or 0))),
            "start_date": plan.start_date.isoformat() if plan.start_date else None,
            "next_due_date": plan.next_due_date.isoformat() if plan.next_due_date else None,
            "auto_charge_enabled": bool(getattr(plan, "auto_charge_enabled", False)),
            "payment_method_type": getattr(plan, "payment_method_type", None),
            "consecutive_charge_failures": int(
                getattr(plan, "consecutive_charge_failures", 0) or 0
            ),
            "has_saved_payment_method": bool(
                getattr(plan, "stripe_customer_id", None)
                and getattr(plan, "stripe_payment_method_id", None)
            ),
            "installments": [
                {
                    "sequence": i.sequence,
                    "amount": _money(i.amount),
                    "due_date": i.due_date.isoformat() if i.due_date else None,
                    "status": i.status,
                    "paid_at": i.paid_at,
                    "transaction_id": i.payment_transaction_public_id,
                }
                for i in installments
            ],
            "events": list(plan.events or []),
            "created_at": plan.created_at,
            "completed_at": plan.completed_at,
        }

    def list_account_payments(self, user: UserModel, account_id: int) -> dict[str, Any]:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            raise NotFoundError("Account not found")
        self._assert_case_view(user, account.client_id)
        self.refresh_overdue_plans(account_id)

        txns = (
            self.db.query(PayflowPaymentTransactionModel)
            .filter(PayflowPaymentTransactionModel.account_id == account_id)
            .order_by(PayflowPaymentTransactionModel.id.desc())
            .all()
        )
        attempts = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(PayflowPaymentAttemptModel.account_id == account_id)
            .order_by(PayflowPaymentAttemptModel.id.desc())
            .limit(50)
            .all()
        )
        plans = (
            self.db.query(PayflowPaymentPlanModel)
            .filter(PayflowPaymentPlanModel.account_id == account_id)
            .order_by(PayflowPaymentPlanModel.id.desc())
            .all()
        )
        return {
            "account_id": account_id,
            "balances": {
                "original_balance": _money(account.original_balance),
                "outstanding_balance": _money(account.outstanding_balance),
                "recovered_balance": _money(account.recovered_balance),
                "currency_code": account.currency_code or "USD",
                "collection_status": account.collection_status,
            },
            "transactions": [self.serialize_txn(t) for t in txns],
            "attempts": [
                {
                    "id": a.public_id,
                    "status": a.status,
                    "option_type": a.option_type,
                    "plan_id": a.plan_id,
                    "amount": _money(a.amount),
                    "currency_code": a.currency_code,
                    "provider": a.provider,
                    "reference": a.stripe_payment_intent_id
                    or a.stripe_session_id
                    or a.public_id[:12],
                    "confirmed": a.status == "Succeeded",
                    "error_message": a.error_message,
                    "created_at": a.created_at,
                    "completed_at": a.completed_at,
                }
                for a in attempts
            ],
            "payment_plans": [self.serialize_plan(p) for p in plans],
            "total_transactions": len(txns),
        }
