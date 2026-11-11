"""Auto-suggest for uncovered accounts, Suggest as a background job, delivery report wait,
AI case reassessment and the eval report's rejected count."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAiSuggestJobModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
    PayflowStrategyVersionModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    UserModel,
)
from app.modules.payflow.services import ai_suggest_job_service, case_reassessment_service
from app.modules.payflow.services.ai_eval_export_service import _status_bucket
from app.modules.payflow.services.ai_suggest_job_service import TRIGGER_MANUAL, AiSuggestJobService
from app.modules.payflow.services.case_reassessment_service import CaseReassessmentService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_suggestion.gates import ALL_COVERED_REASON, uncovered_accounts
from app.shared.enums import PayflowCommStatus, PayflowWorkflowRunStatus

import pytest

_SEQ = 0
BIG = {"balance_band": "$5,000 and over"}


@pytest.fixture()
def platform_admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


def _client(db):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"aig-c-{_SEQ}", name=f"AI Gap Client {_SEQ}", status="active", governance_rules=[])
    db.add(client)
    db.flush()
    return client


def _account(db, client, *, outstanding=500.0, **values):
    global _SEQ
    _SEQ += 1
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"AIG-{_SEQ}",
        case_reference=f"CASE-AIG-{_SEQ}",
        original_balance=outstanding,
        outstanding_balance=outstanding,
        collection_status="Active",
        email="a@example.com",
        **values,
    )
    db.add(account)
    db.flush()
    return account


def _workflow(db, client, *, segment=None, status="AI Proposed", source="AI Generated", steps=None):
    global _SEQ
    _SEQ += 1
    strategy = PayflowStrategyModel(
        code=f"aig-wf-{_SEQ}", name=f"AIG WF {_SEQ}", client_id=client.id, status=status, source=source,
        segment=segment, steps=steps or [{"id": "s1", "kind": "Trigger", "title": "Start"}], entry_node_id="s1",
    )
    db.add(strategy)
    db.flush()
    return strategy


def _history(db, strategy, *, status, note):
    db.add(
        PayflowStrategyVersionModel(
            strategy_id=strategy.id, version=1, event_date="2026-11-12", note=note, status=status,
            steps=strategy.steps,
        )
    )
    db.flush()


# --------------------------------------------------------------------------- uncovered accounts (gap 3)


def test_accounts_that_fit_no_workflow_are_uncovered(db_session):
    client = _client(db_session)
    small, big = _account(db_session, client, outstanding=300.0), _account(db_session, client, outstanding=8000.0)
    _workflow(db_session, client, segment=BIG)
    resolved = _account(db_session, client, outstanding=300.0)
    resolved.collection_status = "Resolved"  # not eligible: never "uncovered"

    assert uncovered_accounts(db_session, client.id, None, [small, big, resolved]) == [small]


def test_rejected_and_inactive_workflows_cover_nothing(db_session):
    client = _client(db_session)
    big = _account(db_session, client, outstanding=8000.0)
    rejected = _workflow(db_session, client, segment=BIG)
    _history(db_session, rejected, status="AI Proposed", note="Rejected / regeneration requested by Sam")
    _workflow(db_session, client, segment=BIG, status="Inactive")
    assert uncovered_accounts(db_session, client.id, None, [big]) == [big]

    # Edited after the rejection: it is being worked on again, so it covers its segment.
    _history(db_session, rejected, status="AI Proposed", note="Steps updated")
    assert uncovered_accounts(db_session, client.id, None, [big]) == []


def test_auto_suggest_is_queued_again_only_for_uncovered_accounts(db_session):
    """Before: one AI workflow in the scope (even a rejected one) stopped auto-suggest for good."""
    client = _client(db_session)
    _account(db_session, client, outstanding=8000.0)
    _workflow(db_session, client, segment=BIG)
    db_session.commit()
    jobs = AiSuggestJobService(db_session)

    assert jobs.enqueue(client.id, None, trigger="import") is None  # everything is covered

    _account(db_session, client, outstanding=300.0)  # arrives in a segment without a workflow
    db_session.commit()
    job = jobs.enqueue(client.id, None, trigger="import")
    assert job is not None and job.status == "pending"


def test_the_automatic_job_designs_workflows_only_for_the_uncovered_accounts(db_session, platform_admin):
    client = _client(db_session)
    _account(db_session, client, outstanding=8000.0)
    _account(db_session, client, outstanding=300.0)
    _workflow(db_session, client, segment=BIG)
    job = PayflowAiSuggestJobModel(
        client_id=client.id, portfolio_id=None, trigger="import", status="pending",
        requested_by_user_id=platform_admin.id,
    )
    db_session.add(job)
    db_session.commit()

    with patch.object(
        ai_suggest_job_service.WorkflowSuggestionService, "suggest_workflows", return_value={"workflows": [{"id": 7}]}
    ) as suggest:
        AiSuggestJobService(db_session).process(job.id)

    db_session.refresh(job)
    assert job.status == "done" and job.workflow_ids == [7]
    assert suggest.call_args.kwargs == {"only_uncovered": True}
    assert job.segments_count == 1  # the small account's segment only


def test_the_automatic_job_is_skipped_when_every_account_is_covered(db_session, platform_admin):
    client = _client(db_session)
    _account(db_session, client, outstanding=8000.0)
    _workflow(db_session, client, segment=BIG)
    job = PayflowAiSuggestJobModel(
        client_id=client.id, portfolio_id=None, trigger="import", status="pending",
        requested_by_user_id=platform_admin.id,
    )
    db_session.add(job)
    db_session.commit()

    AiSuggestJobService(db_session).process(job.id)

    db_session.refresh(job)
    assert job.status == "skipped" and job.skip_reason == ALL_COVERED_REASON


# --------------------------------------------------------------------------- Suggest as a job (gap 11)


def test_manual_suggest_starts_a_background_job_even_when_ai_workflows_exist(db_session, platform_admin):
    client = _client(db_session)
    _account(db_session, client, outstanding=8000.0)
    _workflow(db_session, client, segment=BIG)
    db_session.commit()
    jobs = AiSuggestJobService(db_session)

    job, started = jobs.start_manual(platform_admin, client.id, None)
    assert started and job.trigger == TRIGGER_MANUAL and job.status == "pending"
    assert job.requested_by_user_id == platform_admin.id

    again, started_again = jobs.start_manual(platform_admin, client.id, None)  # pressed twice
    assert not started_again and again.id == job.id

    with patch.object(
        ai_suggest_job_service.WorkflowSuggestionService, "suggest_workflows", return_value={"workflows": []}
    ) as suggest:
        jobs.process(job.id)
    assert suggest.call_args.kwargs == {"only_uncovered": False}  # a person's Suggest designs every segment


def test_manual_suggest_says_at_once_why_it_cannot_run(db_session, platform_admin):
    from app.core.exceptions import ValidationAppError

    client = _client(db_session)  # no accounts
    db_session.commit()
    with pytest.raises(ValidationAppError, match="No accounts"):
        AiSuggestJobService(db_session).start_manual(platform_admin, client.id, None)


# --------------------------------------------------------------------------- delivery report (gap 7)

DELIVERY_STEPS = [
    {"id": "s1", "kind": "Condition", "title": "Email delivered?",
     "config": {"attribute": "Email Delivery", "operator": "Equals", "value": "Delivered"}, "yes": "s2", "no": "s3"},
    {"id": "s2", "kind": "Case Action", "title": "Delivered", "config": {"action": "Flag"}},
    {"id": "s3", "kind": "Case Action", "title": "Not delivered", "config": {"action": "Flag"}},
]


def _run_with_email(db, *, status, sent_minutes_ago=0):
    client = _client(db)
    account = _account(db, client)
    strategy = _workflow(db, client, status="Active", steps=DELIVERY_STEPS)
    comm = PayflowCommunicationModel(
        code=f"EM-{account.id}", client_id=client.id, account_id=account.id, channel="Email",
        purpose="Payment Reminder", status=status, balance=500.0,
        sent_at=datetime.now(timezone.utc) - timedelta(minutes=sent_minutes_ago),
    )
    run = PayflowWorkflowRunModel(
        account_id=account.id, strategy_id=strategy.id, client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value, current_step_id="s1", strategy_version=1,
    )
    db.add_all([comm, run])
    db.commit()
    return account, run, comm


def _pending(db, run):
    rows = db.query(PayflowWorkflowJobModel).filter_by(run_id=run.id, status="pending").all()
    return [(row.step_id, (row.payload or {}).get("after_comm_delay", False)) for row in rows]


def test_delivery_condition_waits_for_the_report_then_takes_the_delivered_branch(db_session):
    """Before: checked right after the send, the email was never "Delivered" yet → always the no branch."""
    _, run, comm = _run_with_email(db_session, status=PayflowCommStatus.SENT.value)
    engine = WorkflowExecutionService(db_session)

    engine.execute_step(run.id, "s1")
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.WAITING.value
    assert _pending(db_session, run) == [("s1", True)]  # the check when the wait is over

    # "Sent" again, or a payment: not a delivery report, nothing moves.
    assert engine.on_communication_event(comm.id, PayflowCommStatus.SENT.value)["advanced"] == 0
    assert _pending(db_session, run) == [("s1", True)]

    comm.status = PayflowCommStatus.DELIVERED.value
    db_session.commit()
    assert engine.on_communication_event(comm.id, PayflowCommStatus.DELIVERED.value)["advanced"] == 1
    assert _pending(db_session, run) == [("s2", False)]  # delivered branch; the scheduled check is cancelled


def test_delivery_condition_decides_on_what_is_known_when_no_report_comes(db_session):
    _, run, _ = _run_with_email(db_session, status=PayflowCommStatus.SENT.value)
    engine = WorkflowExecutionService(db_session)
    engine.execute_step(run.id, "s1")

    engine.execute_step(run.id, "s1", after_comm_delay=True)  # the scheduled check

    assert ("s3", False) in _pending(db_session, run)  # not delivered
    rows = db_session.query(PayflowWorkflowStepRunModel).filter_by(run_id=run.id, step_id="s1").all()
    assert sorted(row.status for row in rows) == ["done", "done"]  # nothing left "scheduled"


def test_delivery_condition_does_not_wait_when_the_report_is_in_or_overdue(db_session):
    for status, minutes_ago, branch in (
        (PayflowCommStatus.DELIVERED.value, 0, "s2"),
        (PayflowCommStatus.FAILED.value, 0, "s3"),
        (PayflowCommStatus.SENT.value, 60, "s3"),  # sent an hour ago: no report is coming
    ):
        _, run, _ = _run_with_email(db_session, status=status, sent_minutes_ago=minutes_ago)
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")
        assert _pending(db_session, run) == [(branch, False)], status


# --------------------------------------------------------------------------- AI reassessment (gap 12)


def _reassess(db, answer=None, *, error=None, **account_values):
    client = _client(db)
    account = _account(db, client, days_past_due=75, phone_mobile="+15551234567", **account_values)
    db.commit()
    fake = patch.object(case_reassessment_service, "call_llm", return_value=answer, side_effect=error)
    with fake as call:
        result = CaseReassessmentService(db).run_reassessment(account, trigger="manual_case_suggest")
    return account, result, call


def test_reassessment_is_decided_by_the_ai(db_session):
    answer = {"action": "Offer a payment plan", "channel": "SMS", "rationale": "High balance, part paid.",
              "confidence": 82}
    account, result, call = _reassess(db_session, answer)

    rec = result["recommendation"]
    assert rec["action"] == "Offer a payment plan" and rec["channel"] == "SMS"
    assert rec["source"] == "ai" and rec["confidence"] == 82.0
    review = db_session.get(PayflowHumanReviewModel, result["review_id"])
    assert review.proposed_action == "Offer a payment plan" and review.confidence == 82.0
    assert call.call_args.kwargs == {"quick": True}  # never waits out a rate limit inside a workflow step
    sent = call.call_args.args[0][1]["content"]
    assert '"days_past_due": 75' in sent
    assert "Test User" not in sent and "a@example.com" not in sent and "5551234567" not in sent  # no personal data


def test_reassessment_falls_back_to_the_rules_when_the_ai_cannot_be_used(db_session):
    from app.core.exceptions import ServiceUnavailableError

    for answer, error in (
        (None, ServiceUnavailableError("down")),
        ({"action": "Take them to court", "channel": "Email", "rationale": "x", "confidence": 99}, None),
    ):
        _, result, _ = _reassess(db_session, answer, error=error)
        rec = result["recommendation"]
        assert rec["action"] == "Firm reminder with payment link" and rec["source"] == "rules"
        assert "confidence" not in rec  # no made-up confidence


def test_reassessment_never_recommends_a_channel_the_account_does_not_have(db_session):
    answer = {"action": "Friendly payment reminder", "channel": "SMS", "rationale": "Early.", "confidence": 70}
    _, result, _ = _reassess_email_only(db_session, answer)
    assert result["recommendation"]["channel"] == "Email"


def _reassess_email_only(db, answer):
    client = _client(db)
    account = _account(db, client, days_past_due=10)  # email only
    db.commit()
    with patch.object(case_reassessment_service, "call_llm", return_value=answer) as call:
        result = CaseReassessmentService(db).run_reassessment(account, trigger="manual_case_suggest")
    return account, result, call


# --------------------------------------------------------------------------- eval report (gap 12)


def test_eval_report_counts_rejected_workflows():
    """Reject puts a workflow back to "AI Proposed": before, a rejection was never counted, so the
    approval rate was always 100%."""
    assert _status_bucket("AI Proposed", "Rejected / regeneration requested by Sam") == "rejected"
    assert _status_bucket("Rejected", "Regenerated as #12") == "rejected"
    assert _status_bucket("AI Proposed", "AI suggested") == "in_review"
    assert _status_bucket("Active", "Activated") == "approved"


def test_suggest_jobs_endpoint_answers_at_once_with_the_job(client, admin_token, db_session):
    payflow_client = _client(db_session)
    _account(db_session, payflow_client, outstanding=8000.0)
    db_session.commit()
    headers = {"Authorization": f"Bearer {admin_token}"}

    with patch.object(ai_suggest_job_service, "process_ai_suggest_jobs") as background:
        response = client.post(
            "/payflow/workflows/suggest-jobs", json={"client_id": payflow_client.id}, headers=headers
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["started"] is True
    assert body["job"]["status"] == "pending" and body["job"]["trigger"] == TRIGGER_MANUAL
    background.assert_called_once_with([body["job"]["id"]])  # the AI runs after the answer is sent

    job = client.get(f"/payflow/ai-suggest-jobs/{body['job']['id']}", headers=headers)
    assert job.status_code == 200 and job.json()["id"] == body["job"]["id"]
    assert client.get("/payflow/ai-suggest-jobs/999999", headers=headers).status_code == 404
