"""Case-level AI reassessment — recommendations stored on the account (not portfolio Suggest)."""

from __future__ import annotations

import json
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

from app.modules.payflow.services.workflow_condition_runtime import (
    payment_status,
    promise_to_pay_status,
)
from app.modules.payflow.services.workflow_suggestion.llm import call_llm

logger = logging.getLogger("app.payflow.case_reassess")

# What a reassessment may recommend. The first three are the treatments the fixed rules also give
# (kept word for word: a rejected recommendation is recognised by its text).
ESCALATE_ACTION = "Escalate for supervisor review"
REASSESS_ACTIONS = [
    "Friendly payment reminder",
    "Payment reminder with payment link",
    "Firm reminder with payment link",
    "Offer a payment plan",
    ESCALATE_ACTION,
]
NO_CHANNEL = "None"
REASSESS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "channel", "rationale", "confidence"],
    "properties": {
        "action": {"type": "string", "enum": REASSESS_ACTIONS},
        "channel": {"type": "string", "enum": ["Email", "SMS", NO_CHANNEL]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
    },
}
REASSESS_SYSTEM_PROMPT = f"""You reassess one overdue account for PayFlow, a collections platform, and recommend
its next treatment. A supervisor reads your recommendation; nothing is sent because of it alone.

Choose exactly one action: {", ".join(f'"{a}"' for a in REASSESS_ACTIONS)}.
- Earlier, lightly overdue accounts get the friendly reminder; later stages get firmer ones.
- "Offer a payment plan" fits a customer who shows they want to pay but cannot pay at once (part payments,
  a broken promise, a high balance) - never when a plan is already running.
- "{ESCALATE_ACTION}" when a person must decide: a dispute, repeated contact without any result, a plan that
  broke down, or when you are not sure.
- Never repeat a treatment a supervisor rejected for this account (see human_decisions).
- channel: "Email" or "SMS" - only one the account can be reached on - or "{NO_CHANNEL}" for an escalation.
- rationale: one or two plain sentences with the facts that decided it. No threats, no legal language.
- confidence: 0-100, how sure you are. Be honest: below 60 when the facts are thin or point both ways.
Answer with JSON only."""


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
        recommended = self._ai_propose(account)
        if recommended is not None:
            return recommended
        # The AI could not be asked (not configured, down, an unusable answer): the fixed rules.
        return {**self._rule_action(account, balance, dpd), "source": "rules"}

    def _ai_propose(self, account: PayflowAccountModel) -> dict[str, Any] | None:
        """Ask the AI for the next treatment of this one case; None when it cannot be used."""
        facts = self._case_facts(account)
        try:
            answer = call_llm(
                [
                    {"role": "system", "content": REASSESS_SYSTEM_PROMPT},
                    {"role": "user", "content": "Account:\n" + json.dumps(facts, ensure_ascii=False)},
                ],
                REASSESS_SCHEMA,
                quick=True,
            )
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the rules
            logger.warning("AI case reassessment unavailable for account_id=%s: %s", account.id, exc)
            return None
        action = str((answer or {}).get("action") or "").strip()
        rationale = str((answer or {}).get("rationale") or "").strip()
        if action not in REASSESS_ACTIONS or not rationale:
            logger.warning("AI case reassessment gave an unusable answer for account_id=%s", account.id)
            return None
        reachable = [name for name, ok in (("Email", facts["has_email"]), ("SMS", facts["has_mobile"])) if ok]
        channel = (answer.get("channel") or "").strip()
        if action == ESCALATE_ACTION:
            channel = None
        elif channel not in reachable:
            channel = reachable[0] if reachable else None
        try:
            confidence = max(0.0, min(100.0, float(answer.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        return {
            "action": action,
            "channel": channel,
            "rationale": rationale[:500],
            "confidence": confidence,
            "source": "ai",
        }

    def _case_facts(self, account: PayflowAccountModel) -> dict[str, Any]:
        """What the AI is told about the case - no name, address, email or phone number."""
        decisions = [
            {
                "decision": h.get("decision"),
                "proposed_action": h.get("proposed_action"),
                "final_action": h.get("final_action"),
            }
            for h in list(account.ai_decision_context or [])
            if h.get("kind") == "human_decision"
        ][-5:]
        return {
            "days_past_due": int(account.days_past_due or 0),
            "outstanding_balance": round(float(account.outstanding_balance or 0), 2),
            "original_balance": round(float(account.original_balance or 0), 2),
            "recovered_balance": round(float(account.recovered_balance or 0), 2),
            "collection_status": account.collection_status,
            "payment_status": payment_status(self.db, account),
            "promise_to_pay": promise_to_pay_status(account),
            "last_payment_date": account.last_payment_date.isoformat() if account.last_payment_date else None,
            "last_contact_date": account.last_contact_date.isoformat() if account.last_contact_date else None,
            "has_email": bool((account.email or "").strip()),
            "has_mobile": bool((account.phone_mobile or "").strip()),
            "human_decisions": decisions,
        }

    @staticmethod
    def _rule_action(account: PayflowAccountModel, balance: float, dpd: int) -> dict[str, Any]:
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
            confidence=rec.get("confidence"),
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
