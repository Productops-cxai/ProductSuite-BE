"""AI evaluation export — workflow versions + human-review audits (CSV/JSON)."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
    PayflowStrategyVersionModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService

logger = logging.getLogger("app.payflow.ai_eval_export")

CSV_COLUMNS = [
    "row_type",
    "client_id",
    "client_code",
    "client_name",
    "strategy_id",
    "strategy_code",
    "strategy_name",
    "version",
    "origin",
    "status",
    "event_date",
    "note",
    "approved_by",
    "reviewer_note",
    "approval_date",
    "steps_count",
    "segment_summary",
    "changes_summary",
    "review_id",
    "review_code",
    "review_status",
    "priority",
    "proposed_action",
    "final_action",
    "decision_outcome",
    "confidence",
    "rejection_reason",
    "guidance",
    "condition_text",
    "observed_value",
    "explanation",
    "account_id",
    "decided_at",
    "export_reference",
]


def _text_list(value: Any, *, limit: int = 8) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:limit]:
            if isinstance(item, dict):
                parts.append(
                    str(
                        item.get("label")
                        or item.get("text")
                        or item.get("detail")
                        or item.get("event")
                        or item
                    )
                )
            else:
                parts.append(str(item))
        return " | ".join(p for p in parts if p) or None
    if isinstance(value, dict):
        return json.dumps(value, default=str)[:800]
    return str(value)[:800]


def _step_snapshot_label(step: dict[str, Any], index: int) -> dict[str, Any]:
    """Human-readable step line from strategy step shape (title/kind/purpose)."""
    cfg = step.get("config") if isinstance(step.get("config"), dict) else {}
    kind = str(step.get("kind") or step.get("type") or "").strip()
    title = str(
        step.get("title")
        or step.get("name")
        or step.get("label")
        or step.get("purpose")
        or cfg.get("purpose")
        or cfg.get("action")
        or kind
        or ""
    ).strip()
    channel = str(step.get("channel") or cfg.get("channel") or "").strip()
    purpose = str(step.get("purpose") or cfg.get("purpose") or "").strip()
    action = str(cfg.get("action") or step.get("action") or "").strip()
    timing = str(step.get("timing") or "").strip()
    meta_parts = [
        p
        for p in (
            kind if kind and kind != title else "",
            channel,
            purpose if purpose and purpose != title else "",
            action if action and action not in {title, purpose} else "",
            timing,
        )
        if p
    ]
    if not title:
        title = meta_parts[0] if meta_parts else f"Step {index + 1}"
        meta_parts = meta_parts[1:]
    return {
        "index": index + 1,
        "id": step.get("id"),
        "kind": kind or None,
        "title": title,
        "channel": channel or None,
        "purpose": purpose or None,
        "timing": timing or None,
        "meta": " · ".join(meta_parts) if meta_parts else None,
        "label": f"{title}" + (f" · {' · '.join(meta_parts)}" if meta_parts else ""),
    }


def _is_ai_origin(origin: str | None) -> bool:
    if not origin:
        return False
    key = origin.lower()
    return "ai" in key or "suggest" in key or "generated" in key


class AiEvalExportService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_export(self, user: UserModel) -> None:
        if self.access.is_operations_admin(user):
            return
        if self.access.can(user, "view_workflows") or self.access.can(user, "view_analytics"):
            return
        raise ForbiddenError("Workflows or Analytics permission required")

    def _scoped_client_ids(self, user: UserModel, client_id: int | None) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return [client_id] if client_id else None
        allowed = list(self.access.build_context(user).get("client_ids") or [])
        if client_id:
            if client_id not in allowed:
                raise ForbiddenError("Client not in your scope")
            return [client_id]
        return allowed

    def _performance(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        versions = [r for r in rows if r.get("row_type") == "workflow_version"]
        reviews = [r for r in rows if r.get("row_type") == "human_review"]
        ai_versions = [r for r in versions if _is_ai_origin(str(r.get("origin") or ""))]
        approved = sum(
            1
            for r in reviews
            if "approv" in str(r.get("review_status") or "").lower()
            or "approv" in str(r.get("decision_outcome") or "").lower()
        )
        rejected = sum(
            1
            for r in reviews
            if "reject" in str(r.get("review_status") or "").lower()
            or "reject" in str(r.get("decision_outcome") or "").lower()
            or r.get("rejection_reason")
        )
        modified = sum(
            1
            for r in reviews
            if "modif" in str(r.get("review_status") or "").lower()
            or "modif" in str(r.get("decision_outcome") or "").lower()
        )
        held = sum(
            1
            for r in reviews
            if "hold" in str(r.get("review_status") or "").lower()
            or "hold" in str(r.get("decision_outcome") or "").lower()
        )
        decided = approved + rejected + modified
        confidences = [
            float(r["confidence"])
            for r in reviews
            if r.get("confidence") is not None and str(r.get("confidence")).strip() != ""
        ]
        avg_confidence = (
            round(sum(confidences) / len(confidences), 3) if confidences else None
        )
        return {
            "workflow_versions": len(versions),
            "ai_origin_versions": len(ai_versions),
            "human_created_versions": len(versions) - len(ai_versions),
            "human_reviews": len(reviews),
            "hr_approved": approved,
            "hr_rejected": rejected,
            "hr_modified": modified,
            "hr_held": held,
            "hr_decided": decided,
            "approval_rate_pct": round((approved / decided) * 100, 1) if decided else None,
            "rejection_rate_pct": round((rejected / decided) * 100, 1) if decided else None,
            "modify_rate_pct": round((modified / decided) * 100, 1) if decided else None,
            "avg_confidence": avg_confidence,
        }

    def _rows(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        limit: int = 2000,
    ) -> tuple[list[dict[str, Any]], str]:
        self._assert_export(user)
        scoped = self._scoped_client_ids(user, client_id)
        export_ref = f"AIEVAL-{uuid4().hex[:12].upper()}"
        if scoped is not None and len(scoped) == 0:
            return [], export_ref

        versions_q = (
            self.db.query(PayflowStrategyVersionModel)
            .join(
                PayflowStrategyModel,
                PayflowStrategyModel.id == PayflowStrategyVersionModel.strategy_id,
            )
            .options(joinedload(PayflowStrategyVersionModel.strategy))
            .order_by(PayflowStrategyVersionModel.id.desc())
        )
        if scoped is not None:
            versions_q = versions_q.filter(PayflowStrategyModel.client_id.in_(scoped))
        # History table can store multiple status snapshots for the same version
        # number (approve → activate, etc.). For eval export keep one row per
        # (strategy_id, version) — newest history id wins.
        raw_versions = versions_q.limit(max(limit * 4, limit)).all()
        seen_ver: set[tuple[int, int]] = set()
        versions: list[PayflowStrategyVersionModel] = []
        for v in raw_versions:
            key = (int(v.strategy_id), int(v.version or 0))
            if key in seen_ver:
                continue
            seen_ver.add(key)
            versions.append(v)
            if len(versions) >= limit:
                break

        reviews_q = (
            self.db.query(PayflowHumanReviewModel)
            .options(
                joinedload(PayflowHumanReviewModel.client),
                joinedload(PayflowHumanReviewModel.strategy),
                joinedload(PayflowHumanReviewModel.audit_events),
            )
            .order_by(PayflowHumanReviewModel.id.desc())
        )
        if scoped is not None:
            reviews_q = reviews_q.filter(PayflowHumanReviewModel.client_id.in_(scoped))
        reviews = reviews_q.limit(limit).all()

        client_cache: dict[int, PayflowClientModel | None] = {}

        def client_for(cid: int | None) -> PayflowClientModel | None:
            if not cid:
                return None
            if cid not in client_cache:
                client_cache[cid] = self.db.get(PayflowClientModel, cid)
            return client_cache[cid]

        rows: list[dict[str, Any]] = []
        for v in versions:
            strategy = v.strategy
            client = client_for(strategy.client_id if strategy else None)
            steps = v.steps if isinstance(v.steps, list) else (strategy.steps if strategy else None)
            if not isinstance(steps, list) or not steps:
                steps = strategy.steps if strategy and isinstance(strategy.steps, list) else None
            segment = v.segment if isinstance(v.segment, dict) else (
                strategy.segment if strategy and isinstance(strategy.segment, dict) else None
            )
            segment_bits = []
            if isinstance(segment, dict):
                for key in ("label", "name", "dpd_band", "balance_band", "channel"):
                    if segment.get(key):
                        segment_bits.append(f"{key}={segment.get(key)}")
            step_snapshots = (
                [
                    {**(s if isinstance(s, dict) else {}), **_step_snapshot_label(s if isinstance(s, dict) else {}, i)}
                    for i, s in enumerate(steps)
                ]
                if isinstance(steps, list)
                else None
            )
            rows.append(
                {
                    "row_type": "workflow_version",
                    "client_id": strategy.client_id if strategy else None,
                    "client_code": client.code if client else None,
                    "client_name": client.name if client else None,
                    "strategy_id": v.strategy_id,
                    "strategy_code": strategy.code if strategy else None,
                    "strategy_name": strategy.name if strategy else None,
                    "version": v.version,
                    "origin": v.origin or (strategy.origin if strategy else None),
                    "status": v.status or (strategy.status if strategy else None),
                    "event_date": v.event_date,
                    "note": v.note,
                    "approved_by": v.approved_by or (strategy.approved_by if strategy else None),
                    "reviewer_note": v.reviewer_note,
                    "approval_date": v.approval_date
                    or (strategy.approval_date if strategy else None),
                    "steps_count": len(steps) if isinstance(steps, list) else None,
                    "segment_summary": ", ".join(segment_bits) if segment_bits else None,
                    "changes_summary": _text_list(v.changes),
                    "steps_detail": step_snapshots,
                    "segment_detail": segment,
                    "changes_detail": v.changes if isinstance(v.changes, list) else None,
                    "review_id": None,
                    "review_code": None,
                    "review_status": None,
                    "priority": None,
                    "proposed_action": None,
                    "final_action": None,
                    "decision_outcome": None,
                    "confidence": None,
                    "rejection_reason": None,
                    "guidance": None,
                    "condition_text": None,
                    "observed_value": None,
                    "explanation": None,
                    "account_id": None,
                    "audit_trail": None,
                    "decided_at": None,
                    "export_reference": export_ref,
                }
            )

        for r in reviews:
            client = r.client or client_for(r.client_id)
            strategy = r.strategy
            audits = []
            for ev in list(r.audit_events or [])[-12:]:
                audits.append(
                    {
                        "event": ev.event,
                        "detail": ev.detail,
                        "actor": ev.actor_name,
                        "before_status": ev.before_status,
                        "after_status": ev.after_status,
                        "at": ev.created_at.isoformat() if ev.created_at else None,
                    }
                )
            explanation = _text_list(r.explanation)
            rows.append(
                {
                    "row_type": "human_review",
                    "client_id": r.client_id,
                    "client_code": client.code if client else None,
                    "client_name": client.name if client else None,
                    "strategy_id": r.strategy_id,
                    "strategy_code": strategy.code if strategy else None,
                    "strategy_name": strategy.name if strategy else None,
                    "version": r.workflow_version,
                    "origin": strategy.origin if strategy else None,
                    "status": r.status,
                    "event_date": r.created_at.isoformat() if r.created_at else None,
                    "note": r.reason,
                    "approved_by": None,
                    "reviewer_note": r.guidance,
                    "approval_date": None,
                    "steps_count": None,
                    "segment_summary": None,
                    "changes_summary": None,
                    "steps_detail": None,
                    "segment_detail": None,
                    "changes_detail": None,
                    "review_id": r.id,
                    "review_code": r.code,
                    "review_status": r.status,
                    "priority": r.priority,
                    "proposed_action": r.proposed_action,
                    "final_action": r.final_action,
                    "decision_outcome": r.final_action or r.status,
                    "confidence": r.confidence,
                    "rejection_reason": r.rejection_reason,
                    "guidance": r.guidance,
                    "condition_text": r.condition_text,
                    "observed_value": r.observed_value,
                    "explanation": explanation,
                    "explanation_detail": r.explanation
                    if isinstance(r.explanation, list)
                    else None,
                    "account_id": r.account_id,
                    "audit_trail": audits,
                    "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                    "export_reference": export_ref,
                }
            )

        logger.info(
            "ai_eval_export user=%s ref=%s versions=%s reviews=%s",
            getattr(user, "id", None),
            export_ref,
            len(versions),
            len(reviews),
        )
        return rows, export_ref

    def preview(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        rows, export_ref = self._rows(user, client_id=client_id, limit=limit)
        performance = self._performance(rows)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "export_reference": export_ref,
            "totals": {
                "row_count": len(rows),
                "workflow_versions": performance["workflow_versions"],
                "human_reviews": performance["human_reviews"],
            },
            "performance": performance,
            "rows": rows[:200],
        }

    def export_csv(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
    ) -> tuple[str, bytes]:
        rows, export_ref = self._rows(user, client_id=client_id, limit=5000)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in CSV_COLUMNS})
        filename = f"ai-eval-{export_ref}.csv"
        return filename, buf.getvalue().encode("utf-8-sig")

    def export_json(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
    ) -> tuple[str, bytes]:
        rows, export_ref = self._rows(user, client_id=client_id, limit=5000)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "export_reference": export_ref,
            "performance": self._performance(rows),
            "rows": rows,
        }
        filename = f"ai-eval-{export_ref}.json"
        return filename, json.dumps(payload, default=str, indent=2).encode("utf-8")
