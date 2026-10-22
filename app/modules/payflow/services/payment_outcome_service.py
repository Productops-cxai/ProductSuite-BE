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

    def ensure_payment_plan_for_attempt(
        self,
        attempt: PayflowPaymentAttemptModel,
        account: PayflowAccountModel,
        *,
        first_txn: PayflowPaymentTransactionModel,
        balance_at_plan_start: float,
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
            # Apply this payment to next unpaid installment if possible.
            self._apply_payment_to_plan(existing, attempt, first_txn)
            return existing

        installments_n = 3
        # Infer from attempt.plan_id like plan_6
        if attempt.plan_id and attempt.plan_id.startswith("plan_"):
            try:
                installments_n = max(2, int(attempt.plan_id.split("_", 1)[1]))
            except ValueError:
                pass
        # Prefer amount * n ≈ original plan total from balance at start + already paid first
        total = _money(balance_at_plan_start)
        if total <= 0:
            total = _money(attempt.amount * installments_n)
        installment_amt = _money(attempt.amount)
        # Rebuild equal-ish schedule using charged first amount
        start = _utcnow().date()
        plan = PayflowPaymentPlanModel(
            public_id=f"plan_{uuid.uuid4().hex[:16]}",
            client_id=attempt.client_id,
            account_id=account.id,
            payment_link_id=attempt.payment_link_id,
            plan_option_id=attempt.plan_id,
            label=f"{installments_n} monthly instalments",
            status=PayflowPaymentPlanStatus.ACTIVE.value,
            installments_total=installments_n,
            installment_amount=installment_amt,
            frequency="monthly",
            currency_code=attempt.currency_code or "USD",
            total_payable=total,
            amount_paid=_money(attempt.amount),
            start_date=start,
            next_due_date=_add_months(start, 1),
            events=[{"at": _utcnow().isoformat(), "label": "Created", "detail": "First instalment paid"}],
        )
        self.db.add(plan)
        self.db.flush()

        # Schedule: first paid today; remaining pending (no auto-charge).
        paid_first = _money(attempt.amount)
        leftover = max(0.0, _money(total - paid_first))
        for i in range(1, installments_n + 1):
            if i == 1:
                amt = paid_first
                due = start
                status = PayflowPaymentInstallmentStatus.PAID.value
                paid_at = _utcnow()
                att_id = attempt.id
                txn_pid = first_txn.public_id
            else:
                left_slots = installments_n - i + 1
                if i == installments_n:
                    amt = leftover
                else:
                    amt = _money(leftover / left_slots) if left_slots else 0.0
                    leftover = _money(leftover - amt)
                due = _add_months(start, i - 1)
                status = PayflowPaymentInstallmentStatus.PENDING.value
                paid_at = None
                att_id = None
                txn_pid = None
            inst = PayflowPaymentPlanInstallmentModel(
                plan_id=plan.id,
                sequence=i,
                amount=amt,
                due_date=due,
                status=status,
                paid_at=paid_at,
                payment_attempt_id=att_id,
                payment_transaction_public_id=txn_pid,
            )
            self.db.add(inst)
        self.db.flush()
        first_txn.payment_plan_id = plan.id
        return plan

    def _apply_payment_to_plan(
        self,
        plan: PayflowPaymentPlanModel,
        attempt: PayflowPaymentAttemptModel,
        txn: PayflowPaymentTransactionModel,
    ) -> None:
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
            "auto_charge_enabled": False,
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
