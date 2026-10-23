"""CRM payment reconciliation report — preview + CSV export (no CRM writeback)."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentTransactionModel,
    PayflowPortfolioModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowPaymentTxnDirection, PayflowPaymentTxnStatus

logger = logging.getLogger("app.payflow.payment_recon")

CSV_COLUMNS = [
    "payflow_client_id",
    "payflow_client_code",
    "crm_client_number",
    "portfolio_id",
    "portfolio_code",
    "crm_account_id",
    "payflow_case_id",
    "account_reference",
    "payflow_transaction_id",
    "payment_provider",
    "provider_transaction_id",
    "payment_datetime",
    "transaction_type",
    "payment_status",
    "payment_method",
    "currency",
    "payment_amount",
    "balance_before",
    "balance_after",
    "current_outstanding_balance",
    "payment_link_or_attempt_ref",
    "reconciliation_export_reference",
]


class PaymentReconciliationService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_report(self, user: UserModel) -> None:
        if self.access.is_operations_admin(user):
            return
        if self.access.can(user, "view_analytics") or self.access.can(
            user, "view_collection_cases"
        ):
            return
        raise ForbiddenError("Analytics or Collection Cases permission required")

    def _scoped_client_ids(self, user: UserModel, client_id: int | None) -> list[int] | None:
        """None = no restriction (ops admin all); empty = no access."""
        if self.access.is_operations_admin(user):
            if client_id:
                return [client_id]
            return None
        allowed = list(self.access.build_context(user).get("client_ids") or [])
        if client_id:
            if client_id not in allowed:
                raise ForbiddenError("Client not in your scope")
            return [client_id]
        return allowed

    def preview(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        self._assert_report(user)
        export_ref = f"RECON-{uuid4().hex[:12].upper()}"
        rows, totals = self._query_rows(
            user,
            client_id=client_id,
            portfolio_id=portfolio_id,
            date_from=date_from,
            date_to=date_to,
            status=status,
            limit=limit,
            export_ref=export_ref,
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "export_reference": export_ref,
            "filters": {
                "client_id": client_id,
                "portfolio_id": portfolio_id,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "status": status,
            },
            "totals": totals,
            "row_count": len(rows),
            "rows": rows,
        }

    def export_csv(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        status: str | None = None,
    ) -> tuple[str, str]:
        """Returns (filename, csv_text)."""
        self._assert_report(user)
        export_ref = f"RECON-{uuid4().hex[:12].upper()}"
        rows, _totals = self._query_rows(
            user,
            client_id=client_id,
            portfolio_id=portfolio_id,
            date_from=date_from,
            date_to=date_to,
            status=status,
            limit=50_000,
            export_ref=export_ref,
        )
        logger.info(
            "payment_recon_export user=%s ref=%s rows=%s filters=%s",
            getattr(user, "id", None),
            export_ref,
            len(rows),
            {
                "client_id": client_id,
                "portfolio_id": portfolio_id,
                "date_from": date_from,
                "date_to": date_to,
                "status": status,
            },
        )
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        filename = f"payflow_payment_reconciliation_{export_ref}.csv"
        return filename, buf.getvalue()

    def _query_rows(
        self,
        user: UserModel,
        *,
        client_id: int | None,
        portfolio_id: int | None,
        date_from: datetime | None,
        date_to: datetime | None,
        status: str | None,
        limit: int,
        export_ref: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        scope = self._scoped_client_ids(user, client_id)
        if scope is not None and len(scope) == 0:
            return [], {
                "transaction_count": 0,
                "payment_amount_total": 0.0,
                "refund_amount_total": 0.0,
            }

        q = (
            self.db.query(PayflowPaymentTransactionModel)
            .options(
                joinedload(PayflowPaymentTransactionModel.client),
                joinedload(PayflowPaymentTransactionModel.account).joinedload(
                    PayflowAccountModel.portfolio
                ),
            )
            .order_by(PayflowPaymentTransactionModel.id.desc())
        )
        if scope is not None:
            q = q.filter(PayflowPaymentTransactionModel.client_id.in_(scope))
        if portfolio_id:
            q = q.join(PayflowAccountModel).filter(
                PayflowAccountModel.portfolio_id == portfolio_id
            )
        if status:
            q = q.filter(PayflowPaymentTransactionModel.status == status.strip())
        if date_from:
            q = q.filter(
                (PayflowPaymentTransactionModel.completed_at >= date_from)
                | (
                    PayflowPaymentTransactionModel.completed_at.is_(None)
                    & (PayflowPaymentTransactionModel.created_at >= date_from)
                )
            )
        if date_to:
            q = q.filter(
                (PayflowPaymentTransactionModel.completed_at <= date_to)
                | (
                    PayflowPaymentTransactionModel.completed_at.is_(None)
                    & (PayflowPaymentTransactionModel.created_at <= date_to)
                )
            )

        txns = q.limit(max(1, min(limit, 50_000))).all()
        rows: list[dict[str, Any]] = []
        pay_total = 0.0
        refund_total = 0.0

        attempt_ids = {t.payment_attempt_id for t in txns if t.payment_attempt_id}
        attempts: dict[int, PayflowPaymentAttemptModel] = {}
        if attempt_ids:
            for a in (
                self.db.query(PayflowPaymentAttemptModel)
                .filter(PayflowPaymentAttemptModel.id.in_(attempt_ids))
                .all()
            ):
                attempts[a.id] = a

        link_by_attempt: dict[int, str] = {}
        for aid, attempt in attempts.items():
            if attempt.payment_link_id:
                link = self.db.get(PayflowPaymentLinkModel, attempt.payment_link_id)
                if link:
                    tok = (link.token or "")[:12]
                    link_by_attempt[aid] = f"link:{link.id}:{tok}" if tok else f"link:{link.id}"

        for t in txns:
            client: PayflowClientModel | None = t.client
            account: PayflowAccountModel | None = t.account
            portfolio: PayflowPortfolioModel | None = (
                account.portfolio if account else None
            )
            stamp = t.completed_at or t.created_at
            attempt_ref = ""
            if t.payment_attempt_id and t.payment_attempt_id in attempts:
                attempt_ref = attempts[t.payment_attempt_id].public_id or ""
            link_ref = link_by_attempt.get(t.payment_attempt_id or -1) or ""
            combined_ref = " · ".join(x for x in (link_ref, attempt_ref) if x)

            direction = (t.direction or PayflowPaymentTxnDirection.PAYMENT.value).lower()
            amt = float(t.amount or 0)
            if direction == PayflowPaymentTxnDirection.PAYMENT.value and t.status == (
                PayflowPaymentTxnStatus.SUCCEEDED.value
            ):
                pay_total += amt
            elif direction in {"refund", "reversal", "adjustment"}:
                refund_total += amt

            rows.append(
                {
                    "payflow_client_id": t.client_id,
                    "payflow_client_code": client.code if client else "",
                    "crm_client_number": (client.crm_client_number if client else None) or "",
                    "portfolio_id": portfolio.id if portfolio else "",
                    "portfolio_code": portfolio.code if portfolio else "",
                    "crm_account_id": (account.crm_case_id if account else None)
                    or (account.debtor_id if account else None)
                    or "",
                    "payflow_case_id": account.id if account else "",
                    "account_reference": account.account_reference if account else "",
                    "payflow_transaction_id": t.public_id,
                    "payment_provider": t.provider or "",
                    "provider_transaction_id": t.provider_ref or "",
                    "payment_datetime": stamp.isoformat() if stamp else "",
                    "transaction_type": t.direction or "payment",
                    "payment_status": t.status or "",
                    "payment_method": t.method_label or "",
                    "currency": t.currency_code or "USD",
                    "payment_amount": round(amt, 2),
                    "balance_before": t.balance_before,
                    "balance_after": t.balance_after,
                    "current_outstanding_balance": (
                        float(account.outstanding_balance or 0) if account else None
                    ),
                    "payment_link_or_attempt_ref": combined_ref,
                    "reconciliation_export_reference": export_ref,
                }
            )

        totals = {
            "transaction_count": len(rows),
            "payment_amount_total": round(pay_total, 2),
            "refund_amount_total": round(refund_total, 2),
        }
        return rows, totals
