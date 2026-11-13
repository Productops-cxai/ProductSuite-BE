"""The last gaps of "AI considers historical email performance":
- AI-05: an email is weak only when it is weak in each period that has enough of its emails;
- AI-07: reviewer decisions on earlier proposals reach the AI that writes (planner, writer, new proposals);
- AI-09: an accepted / edited proposal shows how its text has done since."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.modules.payflow.services import email_subject_ai
from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService
from app.modules.payflow.services.email_tracking import record_message_version
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.message_brief import _results_lines
from app.modules.payflow.services.workflow_suggestion.message_prompt import _performance_lines
from tests.test_email_subject_performance import (  # noqa: F401 (fixtures)
    FRIENDLY,
    NOW,
    OPTIONS,
    admin,
    history,
    make_email,
    make_payment,
    weak_workflow,
)

OLD = NOW - timedelta(days=60)  # before the last 30 days
OLD_BODY = "Old body {{payment_link}}"


@pytest.fixture(autouse=True)
def small_minimum(monkeypatch):
    """Three delivered emails are enough to measure in these tests (100 in production)."""
    monkeypatch.setattr(settings, "EMAIL_SUBJECT_MIN_DELIVERIES", 3)
    email_subject_ai._INSIGHT_CACHE.clear()


def earlier_period(db, history, weak_workflow, *, weak_step_paid):  # noqa: F811
    """60 days ago: the comparable subject (OPTIONS) converts 2 of 3; the weak step (m2) converts
    `weak_step_paid` of 3."""
    for i, account in enumerate(history.accounts[:3]):
        peer = make_email(db, account, OPTIONS, strategy=history.strategy, stage="Second reminder", sent_at=OLD)
        own = make_email(db, account, FRIENDLY, strategy=weak_workflow, stage="Second reminder", sent_at=OLD,
                         step_id="m2")
        if i < 2:
            make_payment(db, account, peer)
        if i < weak_step_paid:
            make_payment(db, account, own)
    db.commit()


def m2(db, admin, weak_workflow):  # noqa: F811
    proposals = PayflowEmailSubjectAiService(db).analyze_workflow(admin, weak_workflow.id)["proposals"]
    return next(p for p in proposals if p["step_id"] == "m2")


# --------------------------------------------------------------------------- AI-05: several periods


def test_weak_in_both_periods_stays_weak(db_session, admin, history, weak_workflow):  # noqa: F811
    earlier_period(db_session, history, weak_workflow, weak_step_paid=0)
    proposal = m2(db_session, admin, weak_workflow)
    assert proposal["recommendation"] == "Replace"
    assert "It was weak both in the last 30 days and before that." in proposal["explanation"]


def test_weak_in_one_period_only_is_kept(db_session, admin, history, weak_workflow):  # noqa: F811
    earlier_period(db_session, history, weak_workflow, weak_step_paid=3)  # it did well before
    proposal = m2(db_session, admin, weak_workflow)
    assert proposal["recommendation"] == "Retain" and proposal["proposed"] is None
    assert "not weak in every period: it was in line with comparable templates before that" in proposal["explanation"]
    # The daily check does not raise it either.
    assert PayflowEmailSubjectAiService(db_session).scan_low_performers() == {"workflows": 0, "proposals": 0}


def test_one_period_of_history_is_judged_as_before(db_session, admin, weak_workflow):  # noqa: F811
    assert m2(db_session, admin, weak_workflow)["recommendation"] == "Replace"  # all of it in the last 30 days


# --------------------------------------------------------------------------- AI-07: decisions as feedback


def test_reviewer_decisions_become_lessons_for_the_ai(db_session, admin, weak_workflow):  # noqa: F811
    service = PayflowEmailSubjectAiService(db_session)
    service.decide(admin, m2(db_session, admin, weak_workflow)["id"], action="reject", note="Too close to the old wording")
    rejected = (f'- [Second reminder · Payment Reminder] REJECTED: "{OPTIONS}" (the reviewer kept "{FRIENDLY}") '
                "- reviewer's note: Too close to the old wording")
    assert service.decision_lessons(weak_workflow.client_id) == [rejected]

    service.decide(admin, m2(db_session, admin, weak_workflow)["id"], action="accept")
    lessons = service.decision_lessons(weak_workflow.client_id)
    assert lessons == [f'- [Second reminder · Payment Reminder] ACCEPTED: "{OPTIONS}" instead of "{FRIENDLY}"', rejected]

    # The planner and the writer read them with the measured results.
    context = service.performance_context(weak_workflow.client_id)
    assert context["decisions"] == lessons
    assert "Reviewer decisions on earlier AI suggestions (newest first):\n" + "\n".join(lessons) in _results_lines(context)
    assert "write like what they accepted or how they edited, not like what they rejected" in _performance_lines(context)


def test_new_proposals_are_written_with_the_earlier_decisions(db_session, admin, weak_workflow):  # noqa: F811
    service = PayflowEmailSubjectAiService(db_session)
    service.decide(admin, m2(db_session, admin, weak_workflow)["id"], action="reject", note="Too pushy")
    seen = []

    def writer(client, *args, **kwargs):
        seen.append(client)
        raise ServiceUnavailableError("AI is off in tests")  # the proposal falls back to measured history

    with patch.object(message_writer, "write_messages", side_effect=writer):
        m2(db_session, admin, weak_workflow)
    assert seen and "reviewer's note: Too pushy" in seen[0]["email_performance"]["decisions"][0]


# --------------------------------------------------------------------------- AI-09: results since the change


def test_an_accepted_proposal_shows_how_its_text_has_done_since(client, admin_token, db_session, admin, history,  # noqa: F811
                                                                 weak_workflow):
    service = PayflowEmailSubjectAiService(db_session)
    accepted = service.decide(admin, m2(db_session, admin, weak_workflow)["id"], action="accept")
    assert accepted["result"] == {"status": "Not sent yet", "message_version_id": None, "metrics": None}

    # The new text goes out (the same text fingerprint every sent email is recorded with).
    version = record_message_version(db_session, client_id=weak_workflow.client_id, channel="Email",
                                     subject_template=OPTIONS, body_template=OLD_BODY)
    sent = [make_email(db_session, account, OPTIONS, strategy=weak_workflow, stage="Second reminder", step_id="m2",
                       version_id=version.id) for account in history.accounts[:2]]
    make_payment(db_session, history.accounts[0], sent[0])
    db_session.commit()
    result = service.list_proposals(admin, strategy_id=weak_workflow.id)["items"][0]["result"]
    assert (result["status"], result["message_version_id"]) == ("Measuring", version.id)  # 2 of 3 delivered

    make_email(db_session, history.accounts[2], OPTIONS, strategy=weak_workflow, stage="Second reminder",
               step_id="m2", version_id=version.id)
    db_session.commit()
    listed = client.get("/payflow/email-proposals", headers={"Authorization": f"Bearer {admin_token}"}).json()
    result = next(item for item in listed["items"] if item["id"] == accepted["id"])["result"]
    assert result["status"] == "Measured"
    assert (result["metrics"]["delivered"], result["metrics"]["completed"]) == (3, 1)


def test_a_rejected_proposal_has_no_result(db_session, admin, weak_workflow):  # noqa: F811
    service = PayflowEmailSubjectAiService(db_session)
    assert service.decide(admin, m2(db_session, admin, weak_workflow)["id"], action="reject")["result"] is None
    assert m2(db_session, admin, weak_workflow)["result"] is None  # a pending one neither
