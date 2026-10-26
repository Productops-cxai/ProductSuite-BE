"""AI evaluation export — workflow version history for AI proposal performance."""

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
    amount = cfg.get("amount", step.get("amount"))
    unit = str(cfg.get("unit") or step.get("unit") or "").strip()
    reference = str(cfg.get("reference_event") or step.get("reference_event") or "").strip()
    direction = str(cfg.get("direction") or step.get("direction") or "After").strip()
    timing = str(step.get("timing") or "").strip()
    if not timing and amount is not None and unit:
        timing = f"{amount} {unit} {direction}" + (f" {reference}" if reference else "")
    elif not timing and reference:
        timing = f"Immediately when {reference.lower()} occurs" if amount in (None, 0, "0") else reference

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


def _status_bucket(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s in {"active", "approved"}:
        return "approved"
    if "reject" in s:
        return "rejected"
    if "modif" in s or s in {"under review", "ai proposed", "draft"}:
        return "in_review"
    return "other"


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
        ai_versions = [r for r in versions if _is_ai_origin(str(r.get("origin") or ""))]
        # One latest row per strategy for outcome rates.
        latest_by_strategy: dict[Any, dict[str, Any]] = {}
        for r in versions:
            sid = r.get("strategy_id")
            if sid is None:
                continue
            prev = latest_by_strategy.get(sid)
            if prev is None or int(r.get("version") or 0) > int(prev.get("version") or 0):
                latest_by_strategy[sid] = r
        approved = 0
        rejected = 0
        in_review = 0
        for r in latest_by_strategy.values():
            bucket = _status_bucket(str(r.get("status") or ""))
            if bucket == "approved":
                approved += 1
            elif bucket == "rejected":
                rejected += 1
            elif bucket == "in_review":
                in_review += 1
        decided = approved + rejected
        with_changes = sum(1 for r in versions if r.get("changes_summary"))
        return {
            "workflow_versions": len(versions),
            "ai_origin_versions": len(ai_versions),
            "human_created_versions": len(versions) - len(ai_versions),
            "human_reviews": 0,
            "hr_approved": approved,
            "hr_rejected": rejected,
            "hr_modified": with_changes,
            "hr_held": in_review,
            "hr_decided": decided,
            "approval_rate_pct": round((approved / decided) * 100, 1) if decided else None,
            "rejection_rate_pct": round((rejected / decided) * 100, 1) if decided else None,
            "modify_rate_pct": round((with_changes / len(versions)) * 100, 1) if versions else None,
            "avg_confidence": None,
            "workflows": len(latest_by_strategy),
            "versions_with_changes": with_changes,
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

        # Oldest→newest within each strategy so we can attach prior changes context.
        by_strategy: dict[int, list[PayflowStrategyVersionModel]] = {}
        for v in versions:
            by_strategy.setdefault(int(v.strategy_id), []).append(v)
        for sid, items in by_strategy.items():
            items.sort(key=lambda x: (int(x.version or 0), int(x.id or 0)))

        client_cache: dict[int, PayflowClientModel | None] = {}

        def client_for(cid: int | None) -> PayflowClientModel | None:
            if not cid:
                return None
            if cid not in client_cache:
                client_cache[cid] = self.db.get(PayflowClientModel, cid)
            return client_cache[cid]

        rows: list[dict[str, Any]] = []
        # Emit newest-first overall (matches previous preview order).
        for v in versions:
            strategy = v.strategy
            client = client_for(strategy.client_id if strategy else None)
            # Only the version's own snapshot — never fall back to live strategy.steps
            # (that made every version look identical).
            steps = v.steps if isinstance(v.steps, list) else None
            segment = v.segment if isinstance(v.segment, dict) else None
            segment_bits = []
            if isinstance(segment, dict):
                for key in ("label", "name", "dpd_band", "balance_band", "channel"):
                    if segment.get(key):
                        segment_bits.append(f"{key}={segment.get(key)}")
            step_snapshots = (
                [
                    {
                        **(s if isinstance(s, dict) else {}),
                        **_step_snapshot_label(s if isinstance(s, dict) else {}, i),
                    }
                    for i, s in enumerate(steps)
                ]
                if steps
                else None
            )
            siblings = by_strategy.get(int(v.strategy_id), [])
            prev = None
            for item in siblings:
                if int(item.id or 0) == int(v.id or 0):
                    break
                prev = item
            prior_changes = _text_list(prev.changes) if prev is not None else None
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
                    "prior_changes_summary": prior_changes,
                    "steps_detail": step_snapshots,
                    "segment_detail": segment,
                    "changes_detail": v.changes if isinstance(v.changes, list) else None,
                    "steps_snapshot_missing": not bool(steps),
                    "export_reference": export_ref,
                }
            )

        logger.info(
            "ai_eval_export user=%s ref=%s versions=%s",
            getattr(user, "id", None),
            export_ref,
            len(versions),
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
                "workflows": performance.get("workflows") or 0,
                "human_reviews": 0,
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
