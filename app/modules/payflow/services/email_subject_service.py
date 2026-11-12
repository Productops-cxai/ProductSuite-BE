"""Email subject performance: the Dashboard section, the subject details popup, and the history
the AI reads. Everything is counted from tracked rows (see email_tracking, payment_attribution):

- a subject = the subject as written on the workflow step (placeholders kept), whitespace-normalised;
- unique opens / clicks = emails with at least one open / click (repeated events add nothing);
- completed payments = successful payment transactions credited to the subject's emails
  (refunded / reversed ones are no longer "Succeeded", so they drop out);
- every rate uses Delivered as its denominator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, distinct, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowMessageVersionModel,
    PayflowPaymentTransactionModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.email_tracking import REMINDER_STAGES, normalize_subject
from app.modules.payflow.services.payment_attribution import METHOD_NONE
from app.modules.payflow.services.strategy_service import MESSAGE_PURPOSES
from app.shared.enums import PayflowCommStatus, PayflowPaymentTxnDirection, PayflowPaymentTxnStatus

C = PayflowCommunicationModel
T = PayflowPaymentTransactionModel

DATE_RANGES = ("30d", "7d", "today", "custom")
SORT_KEYS = (
    "subject",
    "sent",
    "delivered",
    "opens",
    "open_rate",
    "clicks",
    "click_rate",
    "payment_link_clicks",
    "initiated",
    "completed",
    "conversion",
    "variations",
)
MANUAL_LABEL = "Manual email"
# Seeing email performance is analytics: the user needs this permission for the client.
VIEW_PERMISSION = "view_analytics"
NO_STAGE_LABEL = "No reminder stage"


def min_deliveries() -> int:
    return max(1, int(settings.EMAIL_SUBJECT_MIN_DELIVERIES or 100))


@dataclass(frozen=True)
class SubjectFilters:
    client_ids: list[int] | None = None  # None = every client (platform-wide user)
    portfolio_ids: tuple[int, ...] = ()
    strategy_id: int | None = None
    reminder_stage: str | None = None
    purpose: str | None = None
    start: datetime | None = None  # inclusive
    end: datetime | None = None  # exclusive
    search: str | None = None


# One email counts once for each of these, however many events it had.
# Opened: at least one open that the provider did not flag as automatic (a mail app pre-load).
_OPENED = C.opens_count > func.coalesce(C.machine_opens_count, 0)
_CLICKED = or_(
    C.clicks_count > 0,
    C.last_clicked_url.isnot(None),
    C.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
)
_LINK_CLICKED = and_(
    C.payment_link.is_(True),
    or_(C.clicks_count > 0, C.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value),
)
# An open or a click proves delivery even when the provider's "delivered" event never came.
_DELIVERED = or_(C.delivered_at.isnot(None), _OPENED, _CLICKED)


def _count_if(condition: Any) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


class PayflowEmailSubjectService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    # ------------------------------------------------------------------ access / filters

    def _visible_client_ids(self, user: UserModel, permission: str = VIEW_PERMISSION) -> list[int] | None:
        """Clients whose email performance the user may see: the ones they are assigned to and hold
        `permission` for (None = every client, for a platform-wide user)."""
        if self.access.is_operations_admin(user):
            return None
        client_ids = list(self.access.build_context(user).get("client_ids") or [])
        return [client_id for client_id in client_ids if self.access.can(user, permission, client_id)]

    def build_filters(
        self,
        user: UserModel,
        *,
        date_range: str = "30d",
        date_from: date | None = None,
        date_to: date | None = None,
        client_id: int | None = None,
        portfolio_ids: list[int] | None = None,
        strategy_id: int | None = None,
        reminder_stage: str | None = None,
        purpose: str | None = None,
        search: str | None = None,
    ) -> SubjectFilters:
        """The user's selection, limited to the clients they are allowed to see."""
        allowed = self._visible_client_ids(user)
        if client_id is not None:
            if allowed is not None and client_id not in allowed:
                raise ForbiddenError("Client access denied")
            client_ids: list[int] | None = [client_id]
        else:
            client_ids = allowed

        key = (date_range or "30d").strip().lower()
        if key not in DATE_RANGES:
            raise ValidationAppError(f"date_range must be one of: {', '.join(DATE_RANGES)}")
        today = datetime.now(timezone.utc).date()
        if key == "custom":
            if not date_from or not date_to:
                raise ValidationAppError("date_from and date_to are required for a custom range")
            if date_from > date_to:
                raise ValidationAppError("Choose an end date on or after the start date")
            first, last = date_from, date_to
        else:
            first = today - timedelta(days={"30d": 29, "7d": 6, "today": 0}[key])
            last = today
        stage = (reminder_stage or "").strip() or None
        if stage and stage not in REMINDER_STAGES:
            raise ValidationAppError(f"reminder_stage must be one of: {', '.join(REMINDER_STAGES)}")
        return SubjectFilters(
            client_ids=client_ids,
            # Portfolios belong to one client: they only narrow a single-client selection.
            portfolio_ids=tuple(portfolio_ids or ()) if client_id is not None else (),
            strategy_id=strategy_id,
            reminder_stage=stage,
            purpose=(purpose or "").strip() or None,
            start=datetime(first.year, first.month, first.day, tzinfo=timezone.utc),
            end=datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1),
            search=normalize_subject(search) or None,
        )

    def _conditions(self, filters: SubjectFilters) -> list[Any]:
        conditions = [C.channel == "Email", C.subject_key.isnot(None), C.sent_at.isnot(None)]
        if filters.client_ids is not None:
            conditions.append(C.client_id.in_(filters.client_ids))
        if filters.portfolio_ids:
            conditions.append(C.portfolio_id.in_(filters.portfolio_ids))
        if filters.strategy_id is not None:
            conditions.append(C.strategy_id == filters.strategy_id)
        if filters.reminder_stage:
            conditions.append(C.reminder_stage == filters.reminder_stage)
        if filters.purpose:
            conditions.append(C.purpose == filters.purpose)
        if filters.start is not None:
            conditions.append(C.sent_at >= filters.start)
        if filters.end is not None:
            conditions.append(C.sent_at < filters.end)
        if filters.search:
            escaped = filters.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(C.subject_key.ilike(f"%{escaped}%", escape="\\"))
        return conditions

    # ------------------------------------------------------------------ aggregation

    def aggregate(
        self, filters: SubjectFilters, group_by: list[Any], *, extra: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Metrics per group. Each result: {"group": (values of group_by...), **metrics}."""
        paid = (
            self.db.query(
                T.attributed_communication_id.label("cid"),
                func.count(T.id).label("n"),
            )
            .filter(
                T.direction == PayflowPaymentTxnDirection.PAYMENT.value,
                T.status == PayflowPaymentTxnStatus.SUCCEEDED.value,
                T.attributed_communication_id.isnot(None),
            )
            .group_by(T.attributed_communication_id)
            .subquery()
        )
        rows = (
            self.db.query(
                *group_by,
                func.count(C.id),
                _count_if(_DELIVERED),
                _count_if(_OPENED),
                _count_if(_CLICKED),
                _count_if(_LINK_CLICKED),
                _count_if(C.payment_initiated.is_(True)),
                func.coalesce(func.sum(paid.c.n), 0),
                func.count(paid.c.cid),
                func.count(distinct(C.message_version_id)),
                _count_if(C.is_manual.is_(True)),
            )
            .outerjoin(paid, paid.c.cid == C.id)
            .filter(*self._conditions(filters), *(extra or []))
            .group_by(*group_by)
            .all()
        )
        width = len(group_by)
        result = []
        for row in rows:
            sent, delivered, opens, clicks, link_clicks, initiated, completed, paying, versions, manual = (
                int(value or 0) for value in row[width:]
            )
            result.append(
                {
                    "group": tuple(row[:width]),
                    "sent": sent,
                    "delivered": delivered,
                    "opens": opens,
                    "open_rate": _rate(opens, delivered),
                    "clicks": clicks,
                    "click_rate": _rate(clicks, delivered),
                    "payment_link_clicks": link_clicks,
                    "initiated": initiated,
                    "completed": completed,
                    "emails_with_payment": paying,
                    "conversion": _rate(completed, delivered),
                    # Emails typed by hand have no stored version: together they count as one.
                    "variations": versions + (1 if manual and manual == sent else 0) or 1,
                    "manual_sent": manual,
                    "low_sample": delivered < min_deliveries(),
                }
            )
        return result

    def client_ids_in(self, filters: SubjectFilters, *, subject: str | None = None) -> list[int]:
        """The clients that have emails in this selection (optionally: with this subject)."""
        extra = [C.subject_key == subject] if subject else []
        return [
            row[0]
            for row in self.db.query(C.client_id).filter(*self._conditions(filters), *extra).distinct().all()
        ]

    def subject_rows(self, filters: SubjectFilters) -> list[dict[str, Any]]:
        """One row per subject, most completed payments first."""
        rows = []
        for item in self.aggregate(filters, [C.subject_key]):
            subject = item.pop("group")[0]
            rows.append({"subject_key": subject, "subject": subject, **item})
        rows.sort(key=lambda r: (-r["completed"], r["subject"]))
        return rows

    def _names(self, model: Any, ids: set[int]) -> dict[int, str]:
        ids = {i for i in ids if i is not None}
        if not ids:
            return {}
        return {row.id: row.name for row in self.db.query(model).filter(model.id.in_(ids)).all()}

    def _contexts(self, filters: SubjectFilters) -> dict[str, dict[str, list[str]]]:
        """Per subject: its "Client · Stage" labels and its reminder stages."""
        groups = (
            self.db.query(C.subject_key, C.client_id, C.reminder_stage)
            .filter(*self._conditions(filters))
            .group_by(C.subject_key, C.client_id, C.reminder_stage)
            .all()
        )
        names = self._names(PayflowClientModel, {client_id for _, client_id, _ in groups})
        result: dict[str, dict[str, list[str]]] = {}
        for subject, client_id, stage in groups:
            entry = result.setdefault(subject, {"contexts": [], "stages": []})
            label = f"{names.get(client_id, client_id)} · {stage or MANUAL_LABEL}"
            if label not in entry["contexts"]:
                entry["contexts"].append(label)
            if stage and stage not in entry["stages"]:
                entry["stages"].append(stage)
        return result

    @staticmethod
    def leaders(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
        """The four KPI cards. Best conversion only ranks subjects with enough delivered emails."""

        def top(key: str, pool: list[dict[str, Any]]) -> dict[str, Any] | None:
            return max(pool, key=lambda r: (r[key], r["delivered"]), default=None)

        return {
            "opened": top("opens", rows),
            "clicked": top("clicks", rows),
            "payments": top("completed", rows),
            "conversion": top("conversion", [r for r in rows if not r["low_sample"]]),
        }

    def unattributed_payments(self, filters: SubjectFilters) -> int:
        """Successful payments in the period that no message could be credited with."""
        query = self.db.query(func.count(T.id)).filter(
            T.direction == PayflowPaymentTxnDirection.PAYMENT.value,
            T.status == PayflowPaymentTxnStatus.SUCCEEDED.value,
            T.attribution_method == METHOD_NONE,
        )
        if filters.client_ids is not None:
            query = query.filter(T.client_id.in_(filters.client_ids))
        if filters.portfolio_ids:
            query = query.join(PayflowAccountModel, PayflowAccountModel.id == T.account_id).filter(
                PayflowAccountModel.portfolio_id.in_(filters.portfolio_ids)
            )
        if filters.start is not None:
            query = query.filter(T.completed_at >= filters.start)
        if filters.end is not None:
            query = query.filter(T.completed_at < filters.end)
        return int(query.scalar() or 0)

    # ------------------------------------------------------------------ dashboard section

    def _filter_options(self, user: UserModel) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        clients = self.db.query(PayflowClientModel)
        portfolios = self.db.query(PayflowPortfolioModel)
        workflows = self.db.query(PayflowStrategyModel)
        if allowed is not None:
            clients = clients.filter(PayflowClientModel.id.in_(allowed))
            portfolios = portfolios.filter(PayflowPortfolioModel.client_id.in_(allowed))
            workflows = workflows.filter(PayflowStrategyModel.client_id.in_(allowed))
        return {
            "date_ranges": list(DATE_RANGES),
            "clients": [
                {"id": c.id, "name": c.name} for c in clients.order_by(PayflowClientModel.name.asc()).all()
            ],
            "portfolios": [
                {"id": p.id, "name": p.name, "client_id": p.client_id}
                for p in portfolios.order_by(PayflowPortfolioModel.name.asc()).all()
            ],
            "workflows": [
                {"id": s.id, "name": s.name, "client_id": s.client_id}
                for s in workflows.order_by(PayflowStrategyModel.name.asc()).all()
            ],
            "reminder_stages": list(REMINDER_STAGES),
            "purposes": list(MESSAGE_PURPOSES),
        }

    def _measurement(self) -> dict[str, Any]:
        return {
            "min_deliveries": min_deliveries(),
            "attribution_window_days": int(settings.PAYMENT_ATTRIBUTION_WINDOW_DAYS or 7),
            "attribution_rule": "last_payment_link_click",
            "rate_denominator": "delivered",
            "opens_are_estimated": True,
        }

    def get_section(
        self, user: UserModel, filters: SubjectFilters, *, sort: str = "completed", order: str = "desc"
    ) -> dict[str, Any]:
        if sort not in SORT_KEYS:
            raise ValidationAppError(f"sort must be one of: {', '.join(SORT_KEYS)}")
        rows = self.subject_rows(filters)
        contexts = self._contexts(filters)
        for row in rows:
            entry = contexts.get(row["subject_key"], {})
            row["contexts"] = entry.get("contexts", [])
            row["reminder_stages"] = entry.get("stages", [])
        leaders = self.leaders(rows)
        descending = (order or "desc").lower() != "asc"
        if sort == "subject":
            rows.sort(key=lambda r: r["subject"].lower(), reverse=descending)
        elif sort != "completed" or not descending:
            rows.sort(key=lambda r: r[sort], reverse=descending)
        return {
            "filter_options": self._filter_options(user),
            "measurement": self._measurement(),
            "leaders": leaders,
            "rows": rows,
            "total": len(rows),
            "unattributed_payments": self.unattributed_payments(filters),
        }

    # ------------------------------------------------------------------ subject details

    def _trend(self, filters: SubjectFilters, subject: str, period: str) -> list[dict[str, Any]]:
        day = func.date(C.sent_at)
        points: dict[str, dict[str, int]] = {}
        for item in self.aggregate(filters, [day], extra=[C.subject_key == subject]):
            when = date.fromisoformat(str(item["group"][0])[:10])
            if period == "weekly":
                when -= timedelta(days=when.weekday())  # week starts Monday
            point = points.setdefault(when.isoformat(), {"opens": 0, "clicks": 0, "payments": 0})
            point["opens"] += item["opens"]
            point["clicks"] += item["clicks"]
            point["payments"] += item["completed"]
        return [{"date": key, **points[key]} for key in sorted(points)]

    def _breakdown(self, filters: SubjectFilters, subject: str) -> dict[str, list[dict[str, Any]]]:
        own = [C.subject_key == subject]

        def lines(column: Any, labels: dict[Any, str], missing: str) -> list[dict[str, Any]]:
            items = self.aggregate(filters, [column], extra=own)
            items.sort(key=lambda i: (-i["completed"], -i["delivered"]))
            return [
                {
                    "id": item["group"][0],
                    "label": labels.get(item["group"][0]) or (str(item["group"][0]) if item["group"][0] else missing),
                    "delivered": item["delivered"],
                    "completed": item["completed"],
                    "conversion": item["conversion"],
                }
                for item in items
            ]

        client_ids = {r[0] for r in self.db.query(C.client_id).filter(*self._conditions(filters), *own).distinct()}
        strategy_ids = {r[0] for r in self.db.query(C.strategy_id).filter(*self._conditions(filters), *own).distinct()}
        return {
            "by_client": lines(C.client_id, self._names(PayflowClientModel, client_ids), "Unknown client"),
            "by_workflow": lines(C.strategy_id, self._names(PayflowStrategyModel, strategy_ids), MANUAL_LABEL),
            "by_stage": lines(C.reminder_stage, {}, NO_STAGE_LABEL),
        }

    def _variations(self, filters: SubjectFilters, subject: str) -> list[dict[str, Any]]:
        """The distinct bodies sent under this subject, each with its own results."""
        items = self.aggregate(filters, [C.message_version_id], extra=[C.subject_key == subject])
        ids = {item["group"][0] for item in items if item["group"][0] is not None}
        versions = (
            {v.id: v for v in self.db.query(PayflowMessageVersionModel).filter(PayflowMessageVersionModel.id.in_(ids))}
            if ids
            else {}
        )
        items.sort(key=lambda i: (i["group"][0] is None, i["group"][0] or 0))
        result = []
        for number, item in enumerate(items, start=1):
            version = versions.get(item.pop("group")[0])
            item.pop("variations", None)
            result.append(
                {
                    "id": version.id if version else None,
                    "label": f"Body version {number}" if version else MANUAL_LABEL,
                    "subject": version.subject_template if version else subject,
                    # The text as written on the workflow ({{placeholders}} kept): never a customer's copy.
                    "body": version.body_template if version else None,
                    "origin": version.origin if version else "Human Created",
                    "language": version.language if version else None,
                    "reminder_stage": version.reminder_stage if version else None,
                    "first_used_at": version.first_used_at if version else None,
                    "last_used_at": version.last_used_at if version else None,
                    **item,
                }
            )
        return result

    def get_trend(
        self, user: UserModel, filters: SubjectFilters, *, group_by: str = "overall", period: str = "daily"
    ) -> dict[str, Any]:
        """How the selection did over time: as a whole, or one line per client / workflow / reminder
        stage (the five groups with the most completed payments)."""
        group_by = (group_by or "overall").strip().lower()
        columns = {"overall": None, "client": C.client_id, "workflow": C.strategy_id, "stage": C.reminder_stage}
        if group_by not in columns:
            raise ValidationAppError("group_by must be overall, client, workflow or stage")
        period = (period or "daily").strip().lower()
        if period not in {"daily", "weekly"}:
            raise ValidationAppError("period must be daily or weekly")
        column = columns[group_by]
        day = func.date(C.sent_at)
        keys = ("delivered", "opens", "clicks", "completed")
        groups: dict[Any, dict[str, dict[str, int]]] = {}
        for item in self.aggregate(filters, [day] if column is None else [day, column]):
            when = date.fromisoformat(str(item["group"][0])[:10])
            if period == "weekly":
                when -= timedelta(days=when.weekday())  # week starts Monday
            group = item["group"][1] if column is not None else None
            point = groups.setdefault(group, {}).setdefault(when.isoformat(), dict.fromkeys(keys, 0))
            for key in keys:
                point[key] += item[key]

        if group_by == "client":
            labels = self._names(PayflowClientModel, set(groups))
        elif group_by == "workflow":
            labels = self._names(PayflowStrategyModel, set(groups))
        else:
            labels = {}
        missing = {"overall": "All emails in this selection", "workflow": MANUAL_LABEL, "stage": NO_STAGE_LABEL}
        dates = sorted({d for points in groups.values() for d in points})
        series = []
        for group, points in groups.items():
            totals = {key: sum(p[key] for p in points.values()) for key in keys}
            series.append(
                {
                    "id": group,
                    "label": labels.get(group) or (str(group) if group else missing.get(group_by, "Unknown")),
                    "totals": {**totals, "conversion": _rate(totals["completed"], totals["delivered"])},
                    # Every series has every date, so the lines share one time axis.
                    "points": [
                        {
                            "date": d,
                            "delivered": points.get(d, {}).get("delivered", 0),
                            "opens": points.get(d, {}).get("opens", 0),
                            "clicks": points.get(d, {}).get("clicks", 0),
                            "payments": points.get(d, {}).get("completed", 0),
                            "conversion": _rate(
                                points.get(d, {}).get("completed", 0), points.get(d, {}).get("delivered", 0)
                            ),
                        }
                        for d in dates
                    ],
                }
            )
        series.sort(key=lambda s: (-s["totals"]["completed"], -s["totals"]["delivered"], str(s["label"])))
        return {
            "group_by": group_by,
            "period": period,
            "dates": dates,
            "series": series[:5],
            "more_groups": max(0, len(series) - 5),
        }

    def get_details(
        self, user: UserModel, filters: SubjectFilters, subject_key: str, *, period: str = "daily"
    ) -> dict[str, Any]:
        period = (period or "daily").strip().lower()
        if period not in {"daily", "weekly"}:
            raise ValidationAppError("period must be daily or weekly")
        subject = normalize_subject(subject_key)
        filters = replace(filters, search=None)  # the popup compares against every subject in view
        rows = self.subject_rows(filters)
        row = next((r for r in rows if r["subject_key"] == subject), None)
        if row is None:
            raise NotFoundError("No emails with this subject in the selected filters")
        contexts = self._contexts(filters)
        stages = contexts.get(subject, {}).get("stages", [])
        similar = []
        for other in rows:
            other_stages = contexts.get(other["subject_key"], {}).get("stages", [])
            if other["subject_key"] != subject and set(other_stages) & set(stages):
                similar.append({**other, "reminder_stages": other_stages})
        funnel = [
            {"label": label, "value": row[key], "pct_of_sent": _rate(row[key], row["sent"])}
            for label, key in (
                ("Sent", "sent"),
                ("Delivered", "delivered"),
                ("Opened", "opens"),
                ("Clicked", "clicks"),
                ("Payment Initiated", "initiated"),
                ("Completed Payment", "completed"),
            )
        ]
        return {
            "subject_key": subject,
            "subject": subject,
            "reminder_stages": stages,
            "contexts": contexts.get(subject, {}).get("contexts", []),
            "summary": row,
            "funnel": funnel,
            "period": period,
            "trend": self._trend(filters, subject, period),
            "breakdown": self._breakdown(filters, subject),
            "variations": self._variations(filters, subject),
            "similar": similar,
            "measurement": self._measurement(),
        }

    # ------------------------------------------------------------------ history for the AI

    def history(
        self,
        client_id: int,
        *,
        strategy_id: int | None = None,
        reminder_stage: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """This client's past subjects for a reminder context, best first - never another client's.

        The narrowest scope that has history is used: the same workflow, then the client's other
        workflows at the same stage and purpose, then the same purpose at any stage.
        """
        base = SubjectFilters(client_ids=[client_id])
        scopes: list[tuple[str, SubjectFilters]] = []
        if strategy_id is not None and reminder_stage and purpose:
            scopes.append(
                (
                    "Same client, workflow, stage and purpose",
                    replace(base, strategy_id=strategy_id, reminder_stage=reminder_stage, purpose=purpose),
                )
            )
        if reminder_stage and purpose:
            note = " · no exact workflow history" if strategy_id is not None else ""
            scopes.append(
                (f"Same client, stage and purpose{note}", replace(base, reminder_stage=reminder_stage, purpose=purpose))
            )
        if purpose:
            scopes.append(("Same client and purpose · no history at this stage", replace(base, purpose=purpose)))
        for scope, filters in scopes:
            rows = self.subject_rows(filters)
            if rows:
                # Enough volume first, then conversion: a handful of lucky emails is not a result.
                rows.sort(key=lambda r: (r["low_sample"], -r["conversion"], -r["delivered"]))
                return {"scope": scope, "rows": rows, "filters": filters}
        return {
            "scope": "No relevant historical data for this client, stage and purpose",
            "rows": [],
            "filters": None,
        }
