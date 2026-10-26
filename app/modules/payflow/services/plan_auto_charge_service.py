"""Worker-driven off-session charges for due payment-plan instalments."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
)
from app.modules.payflow.services.stripe_checkout_service import (
    create_off_session_payment_intent,
    stripe_configured,
)
from app.shared.enums import (
    PayflowPaymentAttemptStatus,
    PayflowPaymentInstallmentStatus,
    PayflowPaymentLinkStatus,
    PayflowPaymentOptionType,
    PayflowPaymentPlanStatus,
)

logger = logging.getLogger("app.payflow.plan_auto_charge")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: float) -> float:
    return round(float(value or 0), 2)


class PayflowPlanAutoChargeService:
    def __init__(self, db: Session):
        self.db = db

    def charge_due_installments(self, *, limit: int = 50) -> dict[str, Any]:
        """Find due Pending/Overdue instalments and create off-session PaymentIntents."""
        if not stripe_configured():
            return {"charged": 0, "skipped": 0, "failed": 0, "reason": "stripe_not_configured"}

        today = _utcnow().date()
        rows = (
            self.db.query(PayflowPaymentPlanInstallmentModel)
            .join(PayflowPaymentPlanModel)
            .options(joinedload(PayflowPaymentPlanInstallmentModel.plan))
            .filter(
                PayflowPaymentPlanInstallmentModel.status.in_(
                    [
                        PayflowPaymentInstallmentStatus.PENDING.value,
                        PayflowPaymentInstallmentStatus.OVERDUE.value,
                    ]
                ),
                PayflowPaymentPlanInstallmentModel.due_date <= today,
                PayflowPaymentPlanModel.status.in_(
                    [
                        PayflowPaymentPlanStatus.ACTIVE.value,
                        PayflowPaymentPlanStatus.OVERDUE.value,
                    ]
                ),
                PayflowPaymentPlanModel.auto_charge_enabled.is_(True),
                PayflowPaymentPlanModel.stripe_customer_id.isnot(None),
                PayflowPaymentPlanModel.stripe_payment_method_id.isnot(None),
            )
            .order_by(PayflowPaymentPlanInstallmentModel.due_date.asc())
            .limit(max(1, min(int(limit or 50), 200)))
            .all()
        )

        charged = 0
        skipped = 0
        failed = 0
        for inst in rows:
            try:
                result = self._charge_one(inst)
                if result == "charged":
                    charged += 1
                elif result == "failed":
                    failed += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.exception(
                    "auto-charge installment failed plan_id=%s seq=%s",
                    inst.plan_id,
                    inst.sequence,
                )
                self.db.rollback()

        return {"charged": charged, "skipped": skipped, "failed": failed, "examined": len(rows)}

    def _charge_one(self, inst: PayflowPaymentPlanInstallmentModel) -> str:
        plan = inst.plan or self.db.get(PayflowPaymentPlanModel, inst.plan_id)
        if plan is None:
            return "skipped"
        if not plan.auto_charge_enabled:
            return "skipped"
        if not plan.stripe_customer_id or not plan.stripe_payment_method_id:
            return "skipped"

        # Idempotency: skip if open/succeeded attempt already exists for this instalment.
        open_statuses = {
            PayflowPaymentAttemptStatus.INITIATED.value,
            PayflowPaymentAttemptStatus.PROCESSING.value,
            PayflowPaymentAttemptStatus.SUCCEEDED.value,
        }
        existing = (
            self.db.query(PayflowPaymentAttemptModel)
            .filter(
                PayflowPaymentAttemptModel.account_id == plan.account_id,
                PayflowPaymentAttemptModel.plan_id == plan.public_id,
                PayflowPaymentAttemptModel.option_type
                == PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
                PayflowPaymentAttemptModel.status.in_(list(open_statuses)),
            )
            .order_by(PayflowPaymentAttemptModel.id.desc())
            .limit(20)
            .all()
        )
        for row in existing:
            payload = row.provider_payload if isinstance(row.provider_payload, dict) else {}
            if int(payload.get("installment_sequence") or 0) == int(inst.sequence):
                return "skipped"

        account = self.db.get(PayflowAccountModel, plan.account_id)
        if not account:
            return "skipped"
        balance = _money(account.outstanding_balance)
        amount = min(_money(inst.amount), balance)
        if amount <= 0:
            return "skipped"

        link = None
        if plan.payment_link_id:
            link = self.db.get(PayflowPaymentLinkModel, plan.payment_link_id)
        if link is None:
            link = (
                self.db.query(PayflowPaymentLinkModel)
                .filter(
                    PayflowPaymentLinkModel.account_id == plan.account_id,
                    PayflowPaymentLinkModel.status == PayflowPaymentLinkStatus.ACTIVE.value,
                )
                .order_by(PayflowPaymentLinkModel.id.desc())
                .first()
            )
        if link is None:
            logger.warning(
                "auto-charge skipped — no payment link plan=%s account=%s",
                plan.public_id,
                plan.account_id,
            )
            return "skipped"

        public_id = uuid.uuid4().hex
        attempt = PayflowPaymentAttemptModel(
            public_id=public_id,
            payment_link_id=link.id,
            client_id=plan.client_id,
            account_id=plan.account_id,
            option_type=PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
            plan_id=plan.public_id,
            amount=amount,
            currency_code=plan.currency_code or account.currency_code or "USD",
            status=PayflowPaymentAttemptStatus.PROCESSING.value,
            provider="stripe",
            idempotency_key=f"autocharge_{plan.public_id}_{inst.sequence}_{inst.due_date.isoformat()}",
            balance_at_initiation=balance,
            events=[
                {
                    "at": _utcnow().isoformat(),
                    "label": "Auto-charge Initiated",
                    "detail": f"instalment #{inst.sequence}",
                }
            ],
            provider_payload={
                "auto_charge": True,
                "installment_sequence": inst.sequence,
                "plan_public_id": plan.public_id,
            },
        )
        self.db.add(attempt)
        self.db.flush()

        try:
            intent = create_off_session_payment_intent(
                amount=amount,
                currency=attempt.currency_code,
                customer_id=plan.stripe_customer_id,
                payment_method_id=plan.stripe_payment_method_id,
                metadata={
                    "attempt_public_id": public_id,
                    "plan_id": plan.public_id,
                    "installment_sequence": str(inst.sequence),
                    "option_type": PayflowPaymentOptionType.PAY_NEXT_INSTALMENT.value,
                    "auto_charge": "1",
                    "account_id": str(plan.account_id),
                    "client_id": str(plan.client_id),
                },
                idempotency_key=attempt.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            attempt.status = PayflowPaymentAttemptStatus.FAILED.value
            attempt.error_message = str(exc)[:500]
            attempt.completed_at = _utcnow()
            from app.modules.payflow.services.payment_outcome_service import (
                PayflowPaymentOutcomeService,
            )

            PayflowPaymentOutcomeService(self.db).record_auto_charge_failure(
                plan, installment=inst, detail=attempt.error_message
            )
            self.db.commit()
            return "failed"

        attempt.stripe_payment_intent_id = intent.get("id")
        payload = dict(attempt.provider_payload or {})
        payload["payment_intent"] = intent.get("id")
        payload["pi_status"] = intent.get("status")
        attempt.provider_payload = payload
        self.db.commit()

        # Immediate success (common for cards); ACH may stay processing until webhook.
        status = str(intent.get("status") or "").lower()
        if status == "succeeded":
            from app.modules.payflow.services.payment_portal_service import (
                PayflowPaymentPortalService,
            )

            PayflowPaymentPortalService(self.db).apply_succeeded(
                attempt, provider_ref=attempt.stripe_payment_intent_id, commit=True
            )
            return "charged"
        if status in {"requires_payment_method", "canceled"}:
            from app.modules.payflow.services.payment_portal_service import (
                PayflowPaymentPortalService,
            )

            PayflowPaymentPortalService(self.db).apply_failed(
                attempt, error=f"Off-session status={status}", commit=True
            )
            return "failed"
        return "charged"
