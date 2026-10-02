from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowStrategyOrigin, PayflowStrategyStatus

STRATEGY_STEP_KINDS = [
    "Trigger",
    "Communication",
    "Wait",
    "Condition",
    "Payment Action",
    "Case Action",
    "Human Review",
    "AI Reassessment",
    "Outcome",
]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "strategy"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y")


def _step_stats(steps: list[dict]) -> dict[str, int]:
    return {
        "steps": len(steps),
        "branches": sum(1 for s in steps if s.get("kind") == "Condition"),
        "emails": sum(
            1
            for s in steps
            if s.get("kind") == "Communication" and s.get("channel") == "Email"
        ),
        "sms": sum(
            1 for s in steps if s.get("kind") == "Communication" and s.get("channel") == "SMS"
        ),
    }


class PayflowStrategyService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Strategy access denied")
        if not self.access.can(user, "view_workflows", client_id):
            raise ForbiddenError("View Workflows permission required")

    def _serialize(self, row: PayflowStrategyModel) -> dict[str, Any]:
        steps = list(row.steps or [])
        stats = _step_stats(steps)
        return {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "client_id": row.client_id,
            "client_code": row.client.code if row.client else None,
            "client_name": row.client.name if row.client else None,
            "portfolio_id": row.portfolio_id,
            "portfolio_name": row.portfolio.name if row.portfolio else None,
            "status": row.status,
            "origin": row.origin,
            "version": row.version,
            "summary": row.summary,
            "coverage": row.coverage,
            "segment": row.segment or {},
            "steps": steps,
            "stats": stats,
            "ai_context": list(row.ai_context or []),
            "versions": list(row.versions or []),
            "approved_by": row.approved_by,
            "approval_date": row.approval_date,
            "created_by": row.created_by,
            "last_updated_label": row.updated_at.strftime("%d %b %Y") if row.updated_at else None,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def list_strategies(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowStrategyModel).options(
            joinedload(PayflowStrategyModel.client),
            joinedload(PayflowStrategyModel.portfolio),
        )
        if allowed is not None:
            if not allowed:
                return self._empty()
            q = q.filter(PayflowStrategyModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowStrategyModel.client_id == client_id)
        if portfolio_id is not None:
            q = q.filter(PayflowStrategyModel.portfolio_id == portfolio_id)
        if status:
            q = q.filter(PayflowStrategyModel.status == status)

        rows = q.order_by(PayflowStrategyModel.name.asc()).all()
        if not self.access.is_operations_admin(user):
            rows = [
                r for r in rows if self.access.can(user, "view_workflows", r.client_id)
            ]
        if search:
            term = search.strip().lower()
            rows = [r for r in rows if term in (r.name or "").lower()]

        proposed = sum(1 for r in rows if r.status == PayflowStrategyStatus.AI_PROPOSED.value)
        return {
            "strategies": [self._serialize(r) for r in rows],
            "summary": {
                "total": len(rows),
                "ai_proposed": proposed,
                "active": sum(1 for r in rows if r.status == PayflowStrategyStatus.ACTIVE.value),
                "under_review": sum(
                    1 for r in rows if r.status == PayflowStrategyStatus.UNDER_REVIEW.value
                ),
            },
            "statuses": [s.value for s in PayflowStrategyStatus],
            "step_kinds": STRATEGY_STEP_KINDS,
        }

    def get_strategy(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        return self._serialize(row)

    def create_strategy(self, user: UserModel, payload: dict[str, Any]) -> dict[str, Any]:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationAppError("Strategy name is required")
        client_id = payload.get("client_id")
        if client_id is None:
            raise ValidationAppError("Client is required")
        client_id = int(client_id)
        self._assert_view(user, client_id)
        client = (
            self.db.query(PayflowClientModel).filter(PayflowClientModel.id == client_id).first()
        )
        if not client:
            raise NotFoundError("Client not found")

        portfolio_id = payload.get("portfolio_id")
        if portfolio_id is not None:
            portfolio = (
                self.db.query(PayflowPortfolioModel)
                .filter(
                    PayflowPortfolioModel.id == int(portfolio_id),
                    PayflowPortfolioModel.client_id == client_id,
                )
                .first()
            )
            if not portfolio:
                raise ValidationAppError("Portfolio not found for this client")
            portfolio_id = portfolio.id

        steps = payload.get("steps") or []
        if not isinstance(steps, list) or not steps:
            raise ValidationAppError("At least one step is required")

        status = payload.get("status") or PayflowStrategyStatus.UNDER_REVIEW.value
        if status not in {s.value for s in PayflowStrategyStatus}:
            raise ValidationAppError("Invalid status")

        code = self._unique_code(_slugify(name))
        row = PayflowStrategyModel(
            code=code,
            name=name,
            client_id=client_id,
            portfolio_id=portfolio_id,
            status=status,
            origin=PayflowStrategyOrigin.HUMAN_MODIFIED.value,
            version=1,
            summary=(payload.get("summary") or "").strip() or None,
            coverage=payload.get("coverage") or "Targeted segment",
            segment=payload.get("segment") or {},
            steps=steps,
            ai_context=payload.get("ai_context") or [],
            versions=[{"version": 1, "date": _stamp(), "note": "Workflow created"}],
            created_by=user.full_name,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        if row.status in {
            PayflowStrategyStatus.AI_PROPOSED.value,
            PayflowStrategyStatus.UNDER_REVIEW.value,
        }:
            try:
                from app.modules.payflow.services.notification_service import (
                    PayflowNotificationService,
                )

                PayflowNotificationService(self.db).notify_workflow_awaiting(
                    strategy_id=row.id,
                    strategy_name=row.name,
                    client_id=row.client_id,
                    actor_user_id=user.id,
                    send_mail=True,
                )
            except Exception:
                pass
        return self._serialize(self._get_row(row.id))

    def update_strategy(
        self, user: UserModel, strategy_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise ValidationAppError("Strategy name is required")
            row.name = name
        if "summary" in payload:
            row.summary = (payload.get("summary") or "").strip() or None
        if "segment" in payload and payload["segment"] is not None:
            row.segment = payload["segment"]
        if "steps" in payload and payload["steps"] is not None:
            if not isinstance(payload["steps"], list) or not payload["steps"]:
                raise ValidationAppError("At least one step is required")
            row.steps = payload["steps"]
        if "coverage" in payload:
            row.coverage = payload.get("coverage")
        row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
        if row.status == PayflowStrategyStatus.AI_PROPOSED.value:
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def save_draft(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        row.version = int(row.version or 1) + 1
        versions = list(row.versions or [])
        versions.insert(0, {"version": row.version, "date": _stamp(), "note": "Draft saved"})
        row.versions = versions
        if row.status == PayflowStrategyStatus.AI_PROPOSED.value:
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value
        row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def approve(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        row.status = PayflowStrategyStatus.ACTIVE.value
        row.approved_by = user.full_name
        row.approval_date = _stamp()
        row.version = int(row.version or 1) + 1
        versions = list(row.versions or [])
        versions.insert(
            0, {"version": row.version, "date": _stamp(), "note": f"Approved by {user.full_name}"}
        )
        row.versions = versions
        self.db.commit()
        self.db.refresh(row)
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).mark_entity_read(
                entity_type="workflow", entity_id=row.id
            )
        except Exception:
            pass
        return self._serialize(self._get_row(row.id))

    def reject(self, user: UserModel, strategy_id: int, *, note: str | None = None) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        row.status = PayflowStrategyStatus.AI_PROPOSED.value
        row.origin = PayflowStrategyOrigin.AI_PROPOSED.value
        versions = list(row.versions or [])
        versions.insert(
            0,
            {
                "version": row.version,
                "date": _stamp(),
                "note": note or f"Rejected / regeneration requested by {user.full_name}",
            },
        )
        row.versions = versions
        self.db.commit()
        self.db.refresh(row)
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).notify_workflow_awaiting(
                strategy_id=row.id,
                strategy_name=row.name,
                client_id=row.client_id,
                actor_user_id=user.id,
                send_mail=True,
            )
        except Exception:
            pass
        return self._serialize(self._get_row(row.id))

    def _get_row(self, strategy_id: int) -> PayflowStrategyModel:
        row = (
            self.db.query(PayflowStrategyModel)
            .options(
                joinedload(PayflowStrategyModel.client),
                joinedload(PayflowStrategyModel.portfolio),
            )
            .filter(PayflowStrategyModel.id == strategy_id)
            .first()
        )
        if not row:
            raise NotFoundError("Strategy not found")
        return row

    def _unique_code(self, base: str) -> str:
        code = base
        n = 2
        while self.db.query(PayflowStrategyModel).filter(PayflowStrategyModel.code == code).first():
            code = f"{base}-{n}"
            n += 1
        return code

    def _empty(self) -> dict[str, Any]:
        return {
            "strategies": [],
            "summary": {"total": 0, "ai_proposed": 0, "active": 0, "under_review": 0},
            "statuses": [s.value for s in PayflowStrategyStatus],
            "step_kinds": STRATEGY_STEP_KINDS,
        }
