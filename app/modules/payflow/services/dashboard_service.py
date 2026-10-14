from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowNotificationModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.integration_service import PayflowIntegrationService
from app.shared.enums import (
    PayflowClientStatus,
    PayflowCollectionStatus,
    PayflowCommChannel,
    PayflowCommStatus,
    PayflowReviewPriority,
    PayflowReviewStatus,
)

DATE_BUCKETS = {
    "today": {"Today"},
    "7d": {"Today", "Yesterday", "Last 7 days"},
    "30d": {"Today", "Yesterday", "Last 7 days", "Last 30 days"},
    "qtd": {"Today", "Yesterday", "Last 7 days", "Last 30 days", "Quarter to date"},
}

# Statuses that have progressed at least to this funnel stage.
FUNNEL_STAGES: list[tuple[str, str, set[str]]] = [
    (
        "01",
        "Sent",
        {
            PayflowCommStatus.SENT.value,
            PayflowCommStatus.DELIVERED.value,
            PayflowCommStatus.OPENED_READ.value,
            PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
        },
    ),
    (
        "02",
        "Delivered",
        {
            PayflowCommStatus.DELIVERED.value,
            PayflowCommStatus.OPENED_READ.value,
            PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
        },
    ),
    (
        "03",
        "Opened / Read",
        {
            PayflowCommStatus.OPENED_READ.value,
            PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
        },
    ),
    (
        "04",
        "Clicked",
        {PayflowCommStatus.PAYMENT_LINK_CLICKED.value},
    ),
]


def _rel_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} min ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = seconds // 86400
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


class PayflowDashboardService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def get_dashboard(
        self,
        user: UserModel,
        *,
        date_range: str = "today",
        client_id: int | None = None,
        channel: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        if allowed is not None and not allowed:
            return self._empty(user)

        clients_q = self.db.query(PayflowClientModel)
        accounts_q = self.db.query(PayflowAccountModel).options(
            joinedload(PayflowAccountModel.client),
        )
        reviews_q = self.db.query(PayflowHumanReviewModel).options(
            joinedload(PayflowHumanReviewModel.account),
            joinedload(PayflowHumanReviewModel.client),
        )
        comms_q = self.db.query(PayflowCommunicationModel).options(
            joinedload(PayflowCommunicationModel.account),
            joinedload(PayflowCommunicationModel.client),
        )

        if allowed is not None:
            clients_q = clients_q.filter(PayflowClientModel.id.in_(allowed))
            accounts_q = accounts_q.filter(PayflowAccountModel.client_id.in_(allowed))
            reviews_q = reviews_q.filter(PayflowHumanReviewModel.client_id.in_(allowed))
            comms_q = comms_q.filter(PayflowCommunicationModel.client_id.in_(allowed))

        # Full filter catalogs (never shrink when a filter is applied).
        catalog_clients = clients_q.order_by(PayflowClientModel.name.asc()).all()
        catalog_comms = comms_q.all()
        catalog_accounts = accounts_q.all()
        catalog_channels = sorted({c.channel for c in catalog_comms if c.channel})
        if not catalog_channels:
            catalog_channels = [
                c.value for c in PayflowCommChannel if c != PayflowCommChannel.WHATSAPP
            ]
        catalog_workflows = sorted(
            {
                *(a.current_workflow for a in catalog_accounts if a.current_workflow),
                *(c.workflow_name for c in catalog_comms if c.workflow_name),
            }
        )

        if client_id is not None:
            if allowed is not None and client_id not in allowed:
                return self._empty(user)
            clients_q = clients_q.filter(PayflowClientModel.id == client_id)
            accounts_q = accounts_q.filter(PayflowAccountModel.client_id == client_id)
            reviews_q = reviews_q.filter(PayflowHumanReviewModel.client_id == client_id)
            comms_q = comms_q.filter(PayflowCommunicationModel.client_id == client_id)

        clients = clients_q.order_by(PayflowClientModel.name.asc()).all()
        accounts = accounts_q.all()
        reviews = reviews_q.all()
        integ = PayflowIntegrationService(self.db).list_integrations(
            user, client_id=client_id
        )
        integration_issues = int((integ.get("summary") or {}).get("attention") or 0)

        date_set = DATE_BUCKETS.get(date_range) or DATE_BUCKETS["today"]
        # Apply date/channel/workflow only to metrics — catalogs stay full above.
        all_scoped_comms = comms_q.order_by(PayflowCommunicationModel.id.desc()).all()
        comms = [
            c
            for c in all_scoped_comms
            if (c.date_bucket or "Today") in date_set
            and (not channel or c.channel == channel)
            and (not workflow or c.workflow_name == workflow)
        ]

        if not self.access.is_operations_admin(user):
            reviews = [
                r
                for r in reviews
                if self.access.can(user, "view_human_reviews", r.client_id)
            ]
            accounts = [
                a
                for a in accounts
                if self.access.can(user, "view_customer_accounts", a.client_id)
                or self.access.can(user, "view_collection_cases", a.client_id)
            ]
            comms = [
                c for c in comms if self.access.can(user, "view_communications", c.client_id)
            ]

        active_clients = [c for c in clients if c.status == PayflowClientStatus.ACTIVE.value]
        active_cases = [
            a for a in accounts if a.collection_status == PayflowCollectionStatus.ACTIVE.value
        ]
        recovered_total = sum(float(a.recovered_balance or 0) for a in accounts)
        awaiting = [r for r in reviews if r.status == PayflowReviewStatus.AWAITING_REVIEW.value]
        high_priority = sum(
            1 for r in awaiting if r.priority == PayflowReviewPriority.HIGH.value
        )
        failed_comms = sum(1 for c in comms if c.status == PayflowCommStatus.FAILED.value)
        # Partial payments: recovered something but still outstanding
        partial_payments = sum(
            1
            for a in accounts
            if float(a.recovered_balance or 0) > 0 and float(a.outstanding_balance or 0) > 0
        )
        paid_in_full = sum(
            1 for a in accounts if a.collection_status == PayflowCollectionStatus.RESOLVED.value
        )
        payment_plans = sum(
            1 for a in accounts if a.collection_status == PayflowCollectionStatus.PAYMENT_PLAN.value
        )
        # Failed payments — no dedicated entity; surface failed payment-purpose comms
        failed_payments = sum(
            1
            for c in comms
            if c.status == PayflowCommStatus.FAILED.value
            and "payment" in (c.purpose or "").lower()
        )

        kpis = [
            {
                "id": "active_clients",
                "label": "Active Clients",
                "value": len(active_clients),
                "display": str(len(active_clients)),
                "hint": f"{len(clients)} total in view",
                "tone": "neutral",
                "href": "/payflow/clients?status=active",
            },
            {
                "id": "accounts",
                "label": "Accounts Under Collection",
                "value": len(accounts),
                "display": f"{len(accounts):,}",
                "hint": f"{len(active_cases):,} active cases",
                "tone": "neutral",
                "href": "/payflow/cases",
            },
            {
                "id": "active_cases",
                "label": "Active Collection Cases",
                "value": len(active_cases),
                "display": f"{len(active_cases):,}",
                "hint": f"{sum(1 for a in accounts if a.human_review)} flagged for review",
                "tone": "neutral",
                "href": "/payflow/cases?status=Active",
            },
            {
                "id": "recovered",
                "label": "Amount Recovered",
                "value": recovered_total,
                "display": _money(recovered_total),
                "hint": f"{paid_in_full} paid in full",
                "tone": "primary",
                "href": "/payflow/cases?status=Resolved",
            },
            {
                "id": "reviews",
                "label": "Human Reviews Pending",
                "value": len(awaiting),
                "display": str(len(awaiting)),
                "hint": f"{high_priority} high priority · open queue",
                "tone": "neutral",
                "href": "/payflow/review?status=Awaiting%20Review",
            },
        ]

        attention = [
            {
                "id": "reviews",
                "label": f"Human Reviews Pending · {len(awaiting)}",
                "count": len(awaiting),
                "tone": "peach",
                "href": "/payflow/review?status=Awaiting%20Review",
            },
            {
                "id": "failed_comms",
                "label": f"Failed Communications · {failed_comms}",
                "count": failed_comms,
                "tone": "amber",
                "href": "/payflow/comms?status=Failed",
            },
            {
                "id": "failed_payments",
                "label": f"Failed Payments · {failed_payments}",
                "count": failed_payments,
                "tone": "coral",
                "href": "/payflow/comms?status=Failed",
            },
            {
                "id": "integrations",
                "label": f"Integration Issues · {integration_issues}",
                "count": integration_issues,
                "tone": "rose",
                "href": "/payflow/system-mapping",
            },
        ]

        funnel = self._build_funnel(comms, paid_in_full, payment_plans)

        outcomes = [
            {
                "id": "amount_recovered",
                "label": "Amount Recovered",
                "value": recovered_total,
                "display": _money(recovered_total),
                "href": "/payflow/cases?status=Resolved",
            },
            {
                "id": "paid_in_full",
                "label": "Accounts Paid in Full",
                "value": paid_in_full,
                "display": str(paid_in_full),
                "href": "/payflow/cases?status=Resolved",
            },
            {
                "id": "payment_plans",
                "label": "Active Payment Plans",
                "value": payment_plans,
                "display": str(payment_plans),
                "href": "/payflow/cases?status=Payment%20Plan",
            },
            {
                "id": "partial",
                "label": "Partial Payments",
                "value": partial_payments,
                "display": str(partial_payments),
                "href": "/payflow/cases",
            },
            {
                "id": "failed_payments",
                "label": "Failed Payments",
                "value": failed_payments,
                "display": str(failed_payments),
                "href": "/payflow/comms?status=Failed",
            },
        ]

        clients_attention = self._clients_needing_attention(clients, accounts, awaiting)
        activity = self._recent_activity(user)

        role = (self.access.build_context(user).get("role") or {}).get("name") or "PayFlow"
        return {
            "description": f"{role} view · {len(accounts):,} accounts in scope",
            "filters": {
                "date_range": date_range,
                "client_id": client_id,
                "channel": channel,
                "workflow": workflow,
            },
            "clients": [
                {"id": c.id, "name": c.name, "code": c.code, "status": c.status}
                for c in catalog_clients
            ],
            "channels": catalog_channels,
            "workflows": catalog_workflows,
            "kpis": kpis,
            "attention": attention,
            "funnel": funnel,
            "outcomes": outcomes,
            "clients_attention": clients_attention,
            "activity": activity,
        }

    def _build_funnel(
        self,
        comms: list[PayflowCommunicationModel],
        paid_in_full: int,
        payment_plans: int,
    ) -> list[dict[str, Any]]:
        counts = []
        for step, label, statuses in FUNNEL_STAGES:
            counts.append((step, label, sum(1 for c in comms if c.status in statuses), False))
        counts.append(("05", "Payment Initiated", payment_plans, False))
        counts.append(("06", "Paid", paid_in_full, True))

        base = counts[0][2] or 1
        prev = counts[0][2]
        out: list[dict[str, Any]] = []
        for i, (step, label, value, paid) in enumerate(counts):
            rate = None if i == 0 else (f"{(value / prev * 100):.1f}%" if prev else "0%")
            drop = None if i == 0 else f"−{max(0, prev - value):,}"
            bar = min(100.0, (value / base) * 100) if base else 0
            out.append(
                {
                    "step": step,
                    "label": label,
                    "value": value,
                    "display": f"{value:,}",
                    "rate": rate,
                    "drop": drop,
                    "bar": round(bar, 1),
                    "paid": paid,
                }
            )
            prev = value if i < 4 else prev  # keep drop relative within comms then to payment
            if i >= 4:
                prev = value
        return out

    def _clients_needing_attention(
        self,
        clients: list[PayflowClientModel],
        accounts: list[PayflowAccountModel],
        awaiting: list[PayflowHumanReviewModel],
    ) -> list[dict[str, Any]]:
        by_client: dict[int, dict[str, Any]] = {}
        for c in clients:
            by_client[c.id] = {
                "client_id": c.id,
                "name": c.name,
                "reviews": 0,
                "flagged_accounts": 0,
                "detail": "",
                "badge": "",
                "tone": "tan",
                "href": f"/payflow/clients/{c.id}",
            }
        for r in awaiting:
            if r.client_id in by_client:
                by_client[r.client_id]["reviews"] += 1
        for a in accounts:
            if a.client_id in by_client and a.human_review:
                by_client[a.client_id]["flagged_accounts"] += 1

        rows = []
        for item in by_client.values():
            reviews = item["reviews"]
            flagged = item["flagged_accounts"]
            if reviews == 0 and flagged == 0:
                continue
            if reviews >= 5:
                item["tone"] = "rose"
                item["detail"] = f"{reviews} escalated reviews awaiting supervisor decision"
            elif reviews > 0:
                item["tone"] = "amber"
                item["detail"] = f"{reviews} open human review{'s' if reviews != 1 else ''}"
            else:
                item["tone"] = "tan"
                item["detail"] = f"{flagged} account{'s' if flagged != 1 else ''} flagged for review"
            item["badge"] = f"{reviews} review{'s' if reviews != 1 else ''}"
            rows.append(item)

        rows.sort(key=lambda x: (-x["reviews"], -x["flagged_accounts"], x["name"]))
        return rows[:8]

    def _recent_activity(self, user: UserModel) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PayflowNotificationModel)
            .filter(PayflowNotificationModel.user_id == user.id)
            .order_by(PayflowNotificationModel.created_at.desc())
            .limit(12)
            .all()
        )
        return [
            {
                "id": n.id,
                "text": n.body or n.title,
                "when": _rel_time(n.created_at),
                "href": n.link or "/payflow",
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in rows
        ]

    def _empty(self, user: UserModel) -> dict[str, Any]:
        role = (self.access.build_context(user).get("role") or {}).get("name") or "PayFlow"
        return {
            "description": f"{role} view · 0 accounts in scope",
            "filters": {"date_range": "today", "client_id": None, "channel": None, "workflow": None},
            "clients": [],
            "channels": [
                c.value for c in PayflowCommChannel if c != PayflowCommChannel.WHATSAPP
            ],
            "workflows": [],
            "kpis": [
                {
                    "id": "active_clients",
                    "label": "Active Clients",
                    "value": 0,
                    "display": "0",
                    "hint": "0 total in view",
                    "tone": "neutral",
                    "href": "/payflow/clients",
                },
                {
                    "id": "accounts",
                    "label": "Accounts Under Collection",
                    "value": 0,
                    "display": "0",
                    "hint": None,
                    "tone": "neutral",
                    "href": "/payflow/cases",
                },
                {
                    "id": "active_cases",
                    "label": "Active Collection Cases",
                    "value": 0,
                    "display": "0",
                    "hint": None,
                    "tone": "neutral",
                    "href": "/payflow/cases?status=Active",
                },
                {
                    "id": "recovered",
                    "label": "Amount Recovered",
                    "value": 0,
                    "display": "$0",
                    "hint": None,
                    "tone": "primary",
                    "href": "/payflow/cases",
                },
                {
                    "id": "reviews",
                    "label": "Human Reviews Pending",
                    "value": 0,
                    "display": "0",
                    "hint": "open queue",
                    "tone": "neutral",
                    "href": "/payflow/review",
                },
            ],
            "attention": [],
            "funnel": [],
            "outcomes": [],
            "clients_attention": [],
            "activity": [],
        }


def _money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"
