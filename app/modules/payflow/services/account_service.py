from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowCollectionStatus

COLLECTION_WORKFLOWS = [
    "Early Stage Collection",
    "Progressive Reminder",
    "Promise-to-Pay Follow-Up",
    "Payment Plan Monitoring",
    "Escalated Collection",
]


class PayflowAccountService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def _assert_account_permission(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Account access denied")
        can_accounts = self.access.can(user, "view_customer_accounts", client_id)
        can_cases = self.access.can(user, "view_collection_cases", client_id)
        if not (can_accounts or can_cases):
            raise ForbiddenError("Account access denied")

    def _serialize(self, row: PayflowAccountModel) -> dict[str, Any]:
        client = row.client
        portfolio = row.portfolio
        return {
            "id": row.id,
            "client_id": row.client_id,
            "client_code": client.code if client else None,
            "client_name": client.name if client else None,
            "portfolio_id": row.portfolio_id,
            "portfolio_name": portfolio.name if portfolio else None,
            "customer_name": row.customer_name,
            "account_reference": row.account_reference,
            "case_reference": row.case_reference,
            "original_balance": float(row.original_balance or 0),
            "outstanding_balance": float(row.outstanding_balance or 0),
            "recovered_balance": float(row.recovered_balance or 0),
            "collection_status": row.collection_status,
            "current_workflow": row.current_workflow,
            "last_action": row.last_action,
            "next_action": row.next_action,
            "human_review": bool(row.human_review),
            "timeline": list(row.timeline or []),
            "portfolio_code": portfolio.code if portfolio else None,
            "customer_first_name": row.customer_first_name,
            "customer_last_name": row.customer_last_name,
            "date_of_birth": row.date_of_birth.isoformat() if row.date_of_birth else None,
            "age_group": row.age_group,
            "employment_status": row.employment_status,
            "income_band": row.income_band,
            "education_level": row.education_level,
            "customer_segment": row.customer_segment,
            "address_line1": row.address_line1,
            "city": row.city,
            "province_state": row.province_state,
            "postal_code": row.postal_code,
            "country_code": row.country_code,
            "region": row.region,
            "email": row.email,
            "phone_mobile": row.phone_mobile,
            "phone_work": row.phone_work,
            "language": row.language,
            "currency_code": row.currency_code,
            "fee_amount": float(row.fee_amount) if row.fee_amount is not None else None,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "days_past_due": row.days_past_due,
            "last_payment_amount": float(row.last_payment_amount)
            if row.last_payment_amount is not None
            else None,
            "last_payment_date": row.last_payment_date.isoformat() if row.last_payment_date else None,
            "last_payment_is_ptp": row.last_payment_is_ptp,
            "ptp_code": row.ptp_code,
            "ptp_amount": float(row.ptp_amount) if row.ptp_amount is not None else None,
            "ptp_due_date": row.ptp_due_date.isoformat() if row.ptp_due_date else None,
            "account_status": row.account_status,
            "account_category": row.account_category,
            "negative_balance_reason": row.negative_balance_reason,
            "crm_case_id": row.crm_case_id,
            "debtor_id": row.debtor_id,
            "client_reference_number": row.client_reference_number,
            "product_code": row.product_code,
            "date_listed": row.date_listed.isoformat() if row.date_listed else None,
            "last_email_sent_date": row.last_email_sent_date.isoformat()
            if row.last_email_sent_date
            else None,
            "last_sms_sent_date": row.last_sms_sent_date.isoformat()
            if row.last_sms_sent_date
            else None,
            "last_contact_date": row.last_contact_date.isoformat()
            if row.last_contact_date
            else None,
            "provincial_hold": row.provincial_hold,
            "hold_days": row.hold_days,
            "email_consent": row.email_consent,
            "source_updated_at": row.source_updated_at.isoformat()
            if row.source_updated_at
            else None,
            "last_crm_refresh_at": row.last_crm_refresh_at.isoformat()
            if row.last_crm_refresh_at
            else None,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def list_accounts(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        status: str | None = None,
        workflow: str | None = None,
        human_review: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowAccountModel).options(
            joinedload(PayflowAccountModel.client),
            joinedload(PayflowAccountModel.portfolio),
        )
        if allowed is not None:
            if not allowed:
                return {
                    "accounts": [],
                    "intake": self._empty_intake(),
                    "workflows": COLLECTION_WORKFLOWS,
                    "statuses": [s.value for s in PayflowCollectionStatus],
                }
            q = q.filter(PayflowAccountModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_account_permission(user, client_id)
            q = q.filter(PayflowAccountModel.client_id == client_id)
        if portfolio_id is not None:
            q = q.filter(PayflowAccountModel.portfolio_id == portfolio_id)
        if status:
            q = q.filter(PayflowAccountModel.collection_status == status)
        if workflow:
            q = q.filter(PayflowAccountModel.current_workflow == workflow)
        if human_review == "Yes":
            q = q.filter(PayflowAccountModel.human_review.is_(True))
        elif human_review == "No":
            q = q.filter(PayflowAccountModel.human_review.is_(False))
        if search:
            term = f"%{search.strip()}%"
            q = q.filter(
                (PayflowAccountModel.customer_name.ilike(term))
                | (PayflowAccountModel.account_reference.ilike(term))
                | (PayflowAccountModel.case_reference.ilike(term))
                | (PayflowAccountModel.email.ilike(term))
            )

        rows = q.order_by(PayflowAccountModel.customer_name.asc()).all()
        # Permission filter for supervisors when listing across clients
        if not self.access.is_operations_admin(user):
            rows = [
                r
                for r in rows
                if self.access.can(user, "view_customer_accounts", r.client_id)
                or self.access.can(user, "view_collection_cases", r.client_id)
            ]

        from app.modules.payflow.services.import_service import PayflowImportService

        intake = PayflowImportService(self.db).latest_account_intake()
        if client_id is not None:
            scoped = [r for r in rows if r.client_id == client_id]
            intake = {**intake, "accounts_in_files": len(scoped), "files": 1 if scoped else 0}

        return {
            "accounts": [self._serialize(r) for r in rows],
            "intake": intake,
            "workflows": COLLECTION_WORKFLOWS,
            "statuses": [s.value for s in PayflowCollectionStatus],
        }

    def get_account(self, user: UserModel, account_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowAccountModel)
            .options(
                joinedload(PayflowAccountModel.client),
                joinedload(PayflowAccountModel.portfolio),
            )
            .filter(PayflowAccountModel.id == account_id)
            .first()
        )
        if not row:
            raise NotFoundError("Account not found")
        self._assert_account_permission(user, row.client_id)
        return self._serialize(row)

    def _empty_intake(self) -> dict[str, Any]:
        return {
            "files": 0,
            "latest_received_at": "—",
            "latest_assigned_at": "—",
            "accounts_in_files": 0,
        }

    def _intake_summary(self, client_ids: list[int], account_count: int) -> dict[str, Any]:
        if not client_ids:
            return self._empty_intake()
        clients = (
            self.db.query(PayflowClientModel)
            .filter(PayflowClientModel.id.in_(client_ids))
            .all()
        )
        # Illustrative intake strip — mirrors Lovable until real file ingest exists.
        return {
            "files": len(clients),
            "latest_received_at": "12 Sep 2026, 06:15 ET",
            "latest_assigned_at": "12 Sep 2026, 07:02 ET",
            "accounts_in_files": account_count,
        }
