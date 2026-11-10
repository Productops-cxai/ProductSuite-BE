"""Medium-gap coverage: contact-step normalize, purpose templates, HR review_reason."""

from app.modules.payflow.services.strategy_service import (
    CONTACT_CHANNEL,
    MESSAGE_PURPOSES,
    MESSAGE_TEMPLATES,
    _normalize_step,
)
from app.modules.payflow.services.workflow_contact_runtime import message_for_channel, template_for


def test_every_purpose_has_email_and_sms_templates():
    for purpose in MESSAGE_PURPOSES:
        email = template_for(purpose, "Email")
        sms = template_for(purpose, "SMS")
        assert email and email["channel"] == "Email" and email["purpose"] == purpose, purpose
        assert sms and sms["channel"] == "SMS" and sms["purpose"] == purpose, purpose


def test_normalize_strips_email_plus_sms_to_contact_step():
    step = _normalize_step(
        {
            "id": "s2",
            "kind": "Communication",
            "title": "Reminder",
            "channel": CONTACT_CHANNEL,
            "config": {
                "channel": CONTACT_CHANNEL,
                "purpose": "Payment Reminder",
                "template_id": "em-reminder",
                "subject": "Hi",
                "body": "Email body",
                "sms_body": "SMS body Reply STOP to opt out.",
                "amount": 0,
                "unit": "Days",
                "direction": "After",
                "reference_event": "Case Received",
            },
        }
    )
    assert step["channel"] is None
    assert "channel" not in step["config"]
    assert "template_id" not in step["config"]
    assert step["config"]["sms_body"].startswith("SMS body")


def test_normalize_does_not_revive_stale_top_level_channel_on_contact_step():
    step = _normalize_step(
        {
            "id": "s2",
            "kind": "Communication",
            "title": "Reminder",
            "channel": "Email",  # stale flat field
            "config": {
                "channel": None,
                "purpose": "Firm Reminder",
                "body": "Email",
                "sms_body": "SMS Reply STOP to opt out.",
                "amount": 0,
                "unit": "Days",
                "direction": "After",
                "reference_event": "Previous Action",
            },
        }
    )
    assert step["channel"] is None
    assert step["config"].get("channel") is None


def test_normalize_sets_review_reason_from_note():
    step = _normalize_step(
        {
            "id": "s9",
            "kind": "Human Review",
            "title": "Human review",
            "config": {"note": "Customer raised a dispute about the balance"},
        }
    )
    assert step["config"]["review_reason"] == "Customer dispute raised"


def test_contact_message_uses_purpose_template_when_own_text_missing():
    subject, body = message_for_channel(
        {"purpose": "Firm Reminder"}, channel="Email", purpose="Firm Reminder"
    )
    assert "Action required" in subject or "unpaid" in body.lower()
    _, sms = message_for_channel({}, channel="SMS", purpose="Firm Reminder")
    assert "Reply STOP to opt out." in sms
    assert any(t["id"] == "sms-firm" for t in MESSAGE_TEMPLATES)


def test_stored_segment_restores_email_allowed_for_regenerate():
    from app.modules.payflow.services.workflow_suggestion.service import _stored_segment

    segment = _stored_segment(
        {
            "segment": {"situation": "Active"},
            "ai_context": [
                {"label": "Segment", "value": "Active · early"},
                {"label": "Accounts", "value": "42"},
                {"label": "Avg outstanding", "value": "1,250.50 CAD"},
                {"label": "Avg days past due", "value": "37"},
                {"label": "Email consent", "value": "10%"},
                {"label": "Email allowed", "value": "85%"},
            ],
        }
    )
    assert segment["stats"]["accounts"] == 42
    assert segment["stats"]["avg_outstanding"] == 1250.5
    assert segment["stats"]["avg_days_past_due"] == 37
    assert segment["stats"]["email_consent_pct"] == 10
    assert segment["stats"]["email_allowed_pct"] == 85
