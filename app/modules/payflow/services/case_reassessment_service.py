"""Case-level AI reassessment — recommendations stored on the account (not portfolio Suggest)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
)
from app.shared.enums import (
    PayflowAiMode,
    PayflowCollectionStatus,
    PayflowReviewStatus,
)

logger = logging.getLogger("app.payflow.case_reassess")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _utcnow().strftime("%Y-%m-%d %H:%M")


class CaseReassessmentService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def append_context(account: PayflowAccountModel, entry: dict[str, Any]) -> None:
        history = list(account.ai_decision_context or [])
        history.append(entry)
        account.ai_decision_context = history[-40:]

    def record_human_decision(
        self,
        account: PayflowAccountModel,
        *,
        review: PayflowHumanReviewModel,
        decision: str,
        final_action: str,
        actor_name: str,
    ) -> None:
        self.append_context(
            account,
            {
                "kind": "human_decision",
                "at": _utcnow().isoformat(),
                "review_id": review.id,
                "decision": decision,
                "proposed_action": review.proposed_action,
                "final_action": final_action,
                "guidance": review.guidance,
                "rejection_reason": review.rejection_reason,
                "actor": actor_name,
            },
        )

    def run_reassessment(
        self,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel | None = None,
        *,
        trigger: str = "workflow_ai_reassessment",
        note: str | None = None,
    ) -> dict[str, Any]:
        """
        Produce a structured case recommendation under governance.
        Does not call portfolio AI Suggest / AiSuggestJob.
        """
        client = self.db.get(PayflowClientModel, account.client_id)
        history = list(account.ai_decision_context or [])
        last_reject = next(
            (
                h
                for h in reversed(history)
                if h.get("kind") == "human_decision" and h.get("decision") == "reject"
            ),
            None,
        )
        last_hold = next(
            (
                h
                for h in reversed(history)
                if h.get("kind") == "human_decision" and h.get("decision") == "hold"
            ),
            None,
        )
        last_modify = next(
            (
                h
                for h in reversed(history)
                if h.get("kind") == "human_decision" and h.get("decision") == "modify"
            ),
            None,
        )

        # Respect explicit hold — do not recommend further outreach until released.
        if account.human_review and account.collection_status == (
            PayflowCollectionStatus.HUMAN_REVIEW.value
        ):
            if last_hold or True:
                rec = {
                    "action": "Keep on Human Review / Hold",
                    "channel": None,
                    "rationale": "Case remains on human review or hold; no new outreach recommended.",
                    "respects_prior_rejection": False,
                    "blocked_by_hold": True,
                }
                return self._persist(account, strategy, trigger, note, rec, open_review=False)

        # Respect recent rejection of the same proposed treatment unless balance/status changed.
        rejected_action = (last_reject or {}).get("proposed_action") or (
            last_reject or {}
        ).get("final_action")
        recommended_action = self._propose_action(account, last_modify=last_modify)
        respects_rejection = False
        if rejected_action and recommended_action["action"] == rejected_action:
            # Material change: balance drop or status change → allow alternate.
            recommended_action = {
                "action": "Escalate for supervisor review",
                "channel": None,
                "rationale": (
                    f"Prior recommendation '{rejected_action}' was rejected; "
                    "escalating instead of repeating the same treatment."
                ),
                "respects_prior_rejection": True,
                "blocked_by_hold": False,
            }
            respects_rejection = True
        else:
            recommended_action["respects_prior_rejection"] = False
            recommended_action["blocked_by_hold"] = False

        ai_mode = (client.ai_mode if client else None) or PayflowAiMode.SUPERVISED_AI.value
        supervised = ai_mode == PayflowAiMode.SUPERVISED_AI.value
        open_review = supervised and not recommended_action.get("blocked_by_hold")

        result = self._persist(
            account,
            strategy,
            trigger,
            note,
            recommended_action,
            open_review=open_review,
        )
        result["respects_prior_rejection"] = respects_rejection
        result["ai_mode"] = ai_mode
        return result

    def _propose_action(
        self,
        account: PayflowAccountModel,
        *,
        last_modify: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if last_modify and last_modify.get("final_action"):
            return {
                "action": str(last_modify["final_action"]),
                "channel": None,
                "rationale": "Continuing the human-modified treatment approved on the prior review.",
            }
        balance = float(account.outstanding_balance or 0)
        dpd = int(account.days_past_due or 0)
        if balance <= 0:
            return {
                "action": "Close as resolved — no outstanding balance",
                "channel": None,
                "rationale": "Outstanding balance is zero or negative.",
            }
        if dpd >= 60:
            return {
                "action": "Firm reminder with payment link",
                "channel": "Email",
                "rationale": f"Account is {dpd} days past due with ${balance:,.2f} outstanding.",
            }
        if dpd >= 30:
            return {
                "action": "Payment reminder with payment link",
                "channel": "SMS" if account.phone_mobile else "Email",
                "rationale": f"Mid-stage delinquency ({dpd} DPD); prefer reachable channel.",
            }
        return {
            "action": "Friendly payment reminder",
            "channel": "Email" if account.email else "SMS",
            "rationale": "Early-stage collection; soft tone with clear payment path.",
        }

    def _persist(
        self,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel | None,
        trigger: str,
        note: str | None,
        rec: dict[str, Any],
        *,
        open_review: bool,
    ) -> dict[str, Any]:
        entry = {
            "kind": "ai_recommendation",
            "at": _utcnow().isoformat(),
            "trigger": trigger,
            "strategy_id": strategy.id if strategy else None,
            "note": note,
            **rec,
        }
        self.append_context(account, entry)
        account.next_action = str(rec.get("action") or "AI reassessment complete")
        account.last_action = f"AI reassessment · {_stamp()}"
        events = list(account.timeline or [])
        events.append(
            {
                "label": "AI reassessment",
                "detail": rec.get("action"),
                "at": _stamp(),
                "rationale": rec.get("rationale"),
            }
        )
        account.timeline = events[-40:]

        review_id = None
        if open_review and not rec.get("blocked_by_hold"):
            review_id = self._ensure_review(account, rec, strategy)

        return {
            "recommendation": rec,
            "review_id": review_id,
            "context_entries": len(account.ai_decision_context or []),
        }

    def _ensure_review(
        self,
        account: PayflowAccountModel,
        rec: dict[str, Any],
        strategy: PayflowStrategyModel | None,
    ) -> int | None:
        open_row = (
            self.db.query(PayflowHumanReviewModel)
            .filter(
                PayflowHumanReviewModel.account_id == account.id,
                PayflowHumanReviewModel.status.in_(
                    [
                        PayflowReviewStatus.AWAITING_REVIEW.value,
                        PayflowReviewStatus.ON_HOLD.value,
                    ]
                ),
            )
            .order_by(PayflowHumanReviewModel.id.desc())
            .first()
        )
        if open_row:
            open_row.proposed_action = str(rec.get("action") or open_row.proposed_action)
            rationale = str(rec.get("rationale") or "")
            if rationale:
                expl = list(open_row.explanation or [])
                expl.append(f"AI reassessment: {rationale}")
                open_row.explanation = expl[-20:]
            hist = list(open_row.history or [])
            hist.append(
                {
                    "at": _stamp(),
                    "event": "AI reassessment updated review",
                    "detail": rationale[:240] if rationale else None,
                }
            )
            open_row.history = hist[-40:]
            account.human_review = True
            if account.collection_status != PayflowCollectionStatus.HUMAN_REVIEW.value:
                account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
            return open_row.id

        from uuid import uuid4

        code = f"HR-{uuid4().hex[:8].upper()}"
        rationale = str(rec.get("rationale") or "AI reassessment recommendation")
        row = PayflowHumanReviewModel(
            code=code,
            client_id=account.client_id,
            account_id=account.id,
            strategy_id=strategy.id if strategy else None,
            status=PayflowReviewStatus.AWAITING_REVIEW.value,
            reason="AI reassessment",
            proposed_action=str(rec.get("action") or "Reassess collection treatment"),
            explanation=[f"Why: {rationale}"],
            history=[
                {
                    "at": _stamp(),
                    "event": "AI reassessment created review",
                }
            ],
            days_past_due=int(account.days_past_due or 0),
        )
        self.db.add(row)
        self.db.flush()
        account.human_review = True
        account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
        return row.id
