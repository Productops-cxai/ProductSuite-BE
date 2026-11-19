"""Email communication history export + retention helpers.

Operational search/export hide rows past each client's retention window (from sent_at).
Rows are kept in the DB until deletion policy is confirmed (FR-05).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowPortfolioModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService

DEFAULT_RETENTION_DAYS = 30
RETENTION_PRESETS = (30, 60, 90, 180)


def normalize_retention_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("communication_retention_days must be an integer") from exc
    if days < 1:
        raise ValidationAppError("communication_retention_days must be at least 1")
    if days > 3650:
        raise ValidationAppError("communication_retention_days cannot exceed 3650")
    return days


def retention_days_of(client: PayflowClientModel | None) -> int:
    if client is None:
        return DEFAULT_RETENTION_DAYS
    raw = getattr(client, "communication_retention_days", None)
    if raw is None:
        settings = getattr(client, "communication_settings", None) or {}
        if isinstance(settings, dict) and settings.get("retention_days") is not None:
            try:
                return max(1, int(settings["retention_days"]))
            except (TypeError, ValueError):
                return DEFAULT_RETENTION_DAYS
        return DEFAULT_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def retention_cutoff(days: int, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - timedelta(days=days)


def sent_timestamp(row: PayflowCommunicationModel) -> datetime | None:
    """Prefer sent_at; fall back to created_at for older rows."""
    value = row.sent_at or row.created_at
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def within_retention(row: PayflowCommunicationModel, client: PayflowClientModel | None = None) -> bool:
    when = sent_timestamp(row)
    if when is None:
        return True
    days = retention_days_of(client or row.client)
    return when >= retention_cutoff(days)


def format_display_datetime(value: datetime | str | None) -> str:
    """Human-readable timestamp for export HTML / summaries."""
    if value is None:
        return "—"
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return "—"
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone()
    return local.strftime("%d %b %Y · %H:%M")


def assert_portfolio_scope(
    db: Session,
    *,
    portfolio_id: int,
    client_id: int | None = None,
) -> PayflowPortfolioModel:
    """Ensure portfolio exists and (when client is set) belongs to that client."""
    portfolio = db.get(PayflowPortfolioModel, int(portfolio_id))
    if portfolio is None:
        raise ValidationAppError("Portfolio not found")
    if client_id is not None and int(portfolio.client_id) != int(client_id):
        raise ValidationAppError("Portfolio does not belong to the selected client")
    return portfolio


def apply_portfolio_filter(q: Any, portfolio_id: int) -> Any:
    """Match rows stamped with portfolio_id or (legacy) account.portfolio_id."""
    return q.outerjoin(
        PayflowAccountModel,
        PayflowCommunicationModel.account_id == PayflowAccountModel.id,
    ).filter(
        or_(
            PayflowCommunicationModel.portfolio_id == portfolio_id,
            (
                PayflowCommunicationModel.portfolio_id.is_(None)
                & (PayflowAccountModel.portfolio_id == portfolio_id)
            ),
        )
    )


class CommunicationExportService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can(user, "view_communications", client_id):
            raise ForbiddenError("View Communications permission required")

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        return list(self.access.build_context(user).get("client_ids") or [])

    def query_rows(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        account_id: int | None = None,
        status: str | None = None,
        channel: str | None = None,
        purpose: str | None = None,
        workflow: str | None = None,
        search: str | None = None,
        recipient_email: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        include_expired: bool = False,
        limit: int | None = None,
    ) -> list[PayflowCommunicationModel]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowCommunicationModel).options(
            joinedload(PayflowCommunicationModel.client),
            joinedload(PayflowCommunicationModel.account),
        )
        if allowed is not None:
            if not allowed:
                return []
            q = q.filter(PayflowCommunicationModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowCommunicationModel.client_id == client_id)
        if portfolio_id is not None:
            assert_portfolio_scope(self.db, portfolio_id=int(portfolio_id), client_id=client_id)
            q = apply_portfolio_filter(q, int(portfolio_id))
        if account_id is not None:
            q = q.filter(PayflowCommunicationModel.account_id == account_id)
        if status:
            q = q.filter(PayflowCommunicationModel.status == status)
        if channel:
            q = q.filter(PayflowCommunicationModel.channel == channel)
        if purpose:
            q = q.filter(PayflowCommunicationModel.purpose == purpose)
        if recipient_email:
            term = recipient_email.strip().lower()
            q = q.filter(PayflowCommunicationModel.recipient.ilike(f"%{term}%"))

        # Date range on sent timestamp (fallback created_at in Python for null sent_at).
        if date_from is not None:
            q = q.filter(
                (PayflowCommunicationModel.sent_at >= date_from)
                | (
                    (PayflowCommunicationModel.sent_at.is_(None))
                    & (PayflowCommunicationModel.created_at >= date_from)
                )
            )
        if date_to is not None:
            q = q.filter(
                (PayflowCommunicationModel.sent_at <= date_to)
                | (
                    (PayflowCommunicationModel.sent_at.is_(None))
                    & (PayflowCommunicationModel.created_at <= date_to)
                )
            )

        rows = q.order_by(
            PayflowCommunicationModel.sent_at.asc().nullslast(),
            PayflowCommunicationModel.id.asc(),
        ).all()

        if not self.access.is_operations_admin(user):
            rows = [r for r in rows if self.access.can(user, "view_communications", r.client_id)]

        if workflow:
            rows = [r for r in rows if (r.workflow_name or "") == workflow]

        if search:
            term = search.strip().lower()
            filtered: list[PayflowCommunicationModel] = []
            for r in rows:
                hay = " ".join(
                    [
                        r.code or "",
                        str(r.id),
                        r.recipient or "",
                        r.subject or "",
                        (r.account.customer_name if r.account else "") or "",
                        (r.account.account_reference if r.account else "") or "",
                        (r.account.case_reference if r.account else "") or "",
                        (r.client.name if r.client else "") or "",
                        r.purpose or "",
                        r.channel or "",
                        r.workflow_name or "",
                        r.status or "",
                    ]
                ).lower()
                if term in hay:
                    filtered.append(r)
            rows = filtered

        if not include_expired:
            rows = [r for r in rows if within_retention(r)]

        if limit is not None:
            rows = rows[: max(0, limit)]
        return rows

    def build_report(
        self,
        user: UserModel,
        *,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        rows = self.query_rows(
            user,
            client_id=filters.get("client_id"),
            portfolio_id=filters.get("portfolio_id"),
            account_id=filters.get("account_id"),
            status=filters.get("status"),
            channel=filters.get("channel") or "Email",
            purpose=filters.get("purpose"),
            workflow=filters.get("workflow"),
            search=filters.get("search"),
            recipient_email=filters.get("recipient_email"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
            include_expired=False,
            limit=int(filters.get("limit") or 5000),
        )
        # Export defaults to Email channel for this story; allow override.
        if filters.get("channel") is None:
            rows = [r for r in rows if (r.channel or "") == "Email"]

        client_name = None
        portfolio_name = None
        if filters.get("client_id"):
            client = self.db.get(PayflowClientModel, int(filters["client_id"]))
            client_name = client.name if client else None
        if filters.get("portfolio_id"):
            portfolio = self.db.get(PayflowPortfolioModel, int(filters["portfolio_id"]))
            portfolio_name = portfolio.name if portfolio else None
            if client_name is None and portfolio is not None:
                parent = self.db.get(PayflowClientModel, int(portfolio.client_id))
                client_name = parent.name if parent else None

        generated_at = datetime.now(timezone.utc)
        emails = [self._email_block(r) for r in rows]
        report = {
            "title": "Email Communication History",
            "client_name": client_name or "All Clients",
            "portfolio_name": portfolio_name,
            "recipient_email": filters.get("recipient_email") or None,
            "date_from": filters.get("date_from").isoformat() if filters.get("date_from") else None,
            "date_to": filters.get("date_to").isoformat() if filters.get("date_to") else None,
            "total_emails": len(emails),
            "generated_at": generated_at.isoformat(),
            "emails": emails,
        }
        self._audit_export(user, report, filters)
        return report

    def render_html(self, report: dict[str, Any]) -> str:
        parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            "<title>Email Communication History</title>",
            "<style>",
            "body{font-family:Segoe UI,Arial,sans-serif;color:#111;margin:24px;}",
            "h1{font-size:20px;margin:0 0 8px}",
            ".meta{font-size:12px;color:#444;margin-bottom:18px}",
            ".email{border:1px solid #ddd;border-radius:8px;padding:14px;margin:0 0 14px}",
            ".email h2{font-size:14px;margin:0 0 8px}",
            ".row{font-size:12px;margin:2px 0}",
            ".body{white-space:pre-wrap;font-size:12px;margin-top:10px;line-height:1.45}",
            "@media print{.email{break-inside:avoid}}",
            "</style></head><body>",
            f"<h1>{_esc(report.get('title') or 'Email Communication History')}</h1>",
            "<div class='meta'>",
            f"<div>Client: {_esc(report.get('client_name'))}</div>",
        ]
        if report.get("portfolio_name"):
            parts.append(f"<div>Sub-Client / Portfolio: {_esc(report['portfolio_name'])}</div>")
        if report.get("recipient_email"):
            parts.append(f"<div>Recipient: {_esc(report['recipient_email'])}</div>")
        parts.append(
            "<div>Date range: "
            f"{_esc(format_display_datetime(report.get('date_from')) if report.get('date_from') else 'Any')}"
            " – "
            f"{_esc(format_display_datetime(report.get('date_to')) if report.get('date_to') else 'Any')}"
            "</div>"
        )
        parts.append(f"<div>Total emails: {int(report.get('total_emails') or 0)}</div>")
        parts.append(
            f"<div>Generated: {_esc(format_display_datetime(report.get('generated_at')))}</div>"
        )
        parts.append("</div>")
        for email in report.get("emails") or []:
            parts.append("<section class='email'>")
            parts.append(f"<h2>{_esc(email.get('subject') or '(no subject)')}</h2>")
            parts.append(
                f"<div class='row'>Sent: {_esc(format_display_datetime(email.get('sent_at')))}</div>"
            )
            parts.append(f"<div class='row'>From: {_esc(email.get('from_email'))}</div>")
            parts.append(f"<div class='row'>To: {_esc(email.get('to_email'))}</div>")
            parts.append(f"<div class='row'>Status: {_esc(email.get('status'))}</div>")
            if email.get("account_reference"):
                parts.append(
                    f"<div class='row'>Account: {_esc(email.get('account_reference'))}"
                    f" · {_esc(email.get('client_name'))}</div>"
                )
            parts.append(f"<div class='body'>{_esc(email.get('body') or '')}</div>")
            parts.append("</section>")
        parts.append("</body></html>")
        return "".join(parts)

    def _email_block(self, row: PayflowCommunicationModel) -> dict[str, Any]:
        when = sent_timestamp(row)
        body = "\n".join(str(line) for line in (row.body_lines or []) if line is not None)
        return {
            "id": row.id,
            "code": row.code,
            "sent_at": when.isoformat() if when else None,
            "from_email": (row.client.email_from if row.client else None) or None,
            "to_email": row.recipient,
            "subject": row.subject,
            "status": row.status,
            "body": body,
            "body_lines": list(row.body_lines or []),
            "client_name": row.client.name if row.client else None,
            "portfolio_id": row.portfolio_id,
            "account_reference": row.account.account_reference if row.account else None,
            "customer_name": row.account.customer_name if row.account else None,
            "channel": row.channel,
            "purpose": row.purpose,
            "workflow_name": row.workflow_name,
        }

    def _audit_export(self, user: UserModel, report: dict[str, Any], filters: dict[str, Any]) -> None:
        # Lightweight audit: append a notification-style note via logger until a dedicated
        # export-audit table is introduced.
        import logging

        logging.getLogger("payflow.comms.export").info(
            "user=%s client_id=%s portfolio_id=%s total=%s recipient=%s",
            getattr(user, "email", None) or getattr(user, "id", None),
            filters.get("client_id"),
            filters.get("portfolio_id"),
            report.get("total_emails"),
            filters.get("recipient_email"),
        )


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
