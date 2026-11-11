"""Follow-up fixes: "Last Customer Response" timing, "Offer Payment Plan", eval confidence."""

from datetime import date, datetime, timedelta, timezone

from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowWorkflowWorkerLogModel,
    UserModel,
)
from app.modules.payflow.services.ai_eval_export_service import AiEvalExportService
from app.modules.payflow.services.workflow_condition_runtime import last_customer_response_at
from app.modules.payflow.services.workflow_execution_service import (
    PLAN_OFFER_NOT_POSSIBLE,
    WorkflowExecutionService,
)
from tests.test_engine_worker_gaps import _case_action, _last_step_run, _setup, _wait
from tests.test_workflow_condition_runtime import _plan

OFFER = [{"id": "s1", "kind": "Payment Action", "title": "Offer plan", "config": {"action": "Offer Payment Plan"}}]


# --------------------------------------------------------------------------- "Last Customer Response"


def test_last_customer_response_is_not_our_own_message(db_session):
    """Before: the account's last contact date, which every message PayFlow sends sets."""
    account, strategy, run = _setup(db_session, [_case_action("Flag")], last_contact_date=date.today() - timedelta(days=30))
    assert last_customer_response_at(account) is None
    cfg = _wait("Last Customer Response", amount=3)

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    # No reply on record: the full 3 days from now (before: 30 days ago + 3 = at once).
    assert abs((when - (datetime.now(timezone.utc) + timedelta(days=3))).total_seconds()) < 5


def test_last_customer_response_uses_the_recorded_reply(db_session):
    account, strategy, run = _setup(db_session, [_case_action("Flag")])
    replied = datetime.now(timezone.utc) - timedelta(days=1)
    account.timeline = [
        {"label": "Inbound SMS", "detail": "customer reply", "at": (replied - timedelta(days=5)).isoformat()},
        {"label": "Payment Reminder", "detail": "Email sent", "at": datetime.now(timezone.utc).isoformat()},
        {"label": "Inbound SMS", "detail": "customer reply", "at": replied.isoformat()},
        {"label": "Customer response", "detail": "no time recorded"},  # unusable: skipped
    ]
    cfg = _wait("Last Customer Response", amount=3)

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    assert abs((when - (replied + timedelta(days=3))).total_seconds()) < 5  # the newest reply


# --------------------------------------------------------------------------- "Offer Payment Plan"


def _offer(db, **setup):
    account, _, run = _setup(db, OFFER, **setup)
    return account, run


def test_offer_payment_plan_prepares_the_page_where_the_plan_is_set_up(db_session):
    account, run = _offer(db_session)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert _last_step_run(db_session, run, "s1").result_json["plan_offer"] == "available"
    assert not account.human_review and account.collection_status == "Active"
    assert any("available" in (item.get("detail") or "") for item in account.timeline)


def test_offer_payment_plan_is_flagged_when_the_client_does_not_allow_plans(db_session):
    """Before: the timeline said a plan was offered although the customer could never set one up."""
    account, run = _offer(db_session)
    db_session.get(PayflowClientModel, account.client_id).payment_settings = {"allow_payment_plan": False}
    db_session.commit()

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert _last_step_run(db_session, run, "s1").result_json["plan_offer"] == "not_allowed"
    assert account.human_review is True
    warning = db_session.query(PayflowWorkflowWorkerLogModel).filter_by(
        event_type="plan_offer_not_possible", account_id=account.id
    ).one()
    assert warning.level == "warn" and PLAN_OFFER_NOT_POSSIBLE


def test_offer_payment_plan_does_nothing_for_a_customer_already_on_a_plan(db_session):
    account, run = _offer(db_session)
    _plan(db_session, account, "Active", ("Paid", "Pending"))
    db_session.commit()

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert _last_step_run(db_session, run, "s1").result_json["plan_offer"] == "already_on_plan"
    assert not account.human_review


# --------------------------------------------------------------------------- eval report confidence


def _review(db, account, confidence):
    db.add(
        PayflowHumanReviewModel(
            code=f"HR-{account.id}-{confidence}", client_id=account.client_id, account_id=account.id,
            status="Awaiting Review", reason="Low decision confidence", proposed_action="Review",
            confidence=confidence,
        )
    )


def test_eval_report_shows_the_real_average_confidence(db_session):
    admin = db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()
    account, _, _ = _setup(db_session, [_case_action("Flag")])
    other, _, _ = _setup(db_session, [_case_action("Flag")])
    service = AiEvalExportService(db_session)

    empty = service.preview(admin, client_id=account.client_id)["performance"]
    assert empty["human_reviews"] == 0 and empty["avg_confidence"] is None  # nothing made up

    _review(db_session, account, 80.0)
    _review(db_session, account, 60.0)
    _review(db_session, account, None)  # a review without a confidence is counted, not averaged
    _review(db_session, other, 10.0)  # another client: not in this report
    db_session.commit()

    report = service.preview(admin, client_id=account.client_id)
    assert report["performance"]["avg_confidence"] == 70.0
    assert report["performance"]["human_reviews"] == 3 and report["performance"]["reviews_with_confidence"] == 2
    assert report["totals"]["human_reviews"] == 3
