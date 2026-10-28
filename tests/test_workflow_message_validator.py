"""Code checks on AI-written message text."""

from app.modules.payflow.services.workflow_suggestion.message_validator import check_message, render_sample

EMAIL = {
    "subject": "Your {{brand_name}} balance",
    "body": (
        "Hi {{first_name}},\n\nThis is {{brand_name}} about account {{account_reference}}. A balance of "
        "{{outstanding_balance}} is still open. You can pay in full or set up a plan on your secure page:\n\n"
        "{{payment_link}}\n\nIf you have already paid, please ignore this message.\n\n{{brand_name}}"
    ),
}
SMS = {"body": "{{brand_name}}: {{outstanding_balance}} is still open. Pay here: {{payment_link}} Reply STOP to opt out."}
FINAL_SMS = {
    "body": "{{brand_name}}: final reminder - {{outstanding_balance}} is still open. Next, your account goes to "
    "our team for review. Pay: {{payment_link}} Reply STOP to opt out."
}


def brief(channel="Email", tone="Clear", promise_ok=False):
    return {"step_id": "s2", "channel": channel, "tone": tone, "promise_ok": promise_ok}


def test_good_email_and_sms_pass():
    assert check_message(EMAIL, brief(), []) == []
    assert check_message(SMS, brief("SMS"), []) == []


def test_render_sample_fills_variables():
    assert render_sample("Hi {{first_name}}, {{ outstanding_balance }}") == "Hi Alexandra, $12,450.00"


def test_only_known_variables():
    found = check_message({**SMS, "body": SMS["body"] + " {{due_date}}"}, brief("SMS"), [])
    assert found == ["s2: {{due_date}} is not an allowed variable"]


def test_promise_variables_only_where_a_promise_exists():
    sms = {"body": "{{brand_name}}: {{promise_amount}} is due {{promise_date}}. Pay: {{payment_link}} Reply STOP to opt out."}
    assert check_message(sms, brief("SMS", promise_ok=True), []) == []
    assert len(check_message(sms, brief("SMS"), [])) == 2


def test_placeholders_and_broken_variables():
    for body in ("Hi [Name], pay: {{payment_link}} {{brand_name}} STOP", "Hi {first_name} {{payment_link}} {{brand_name}} STOP"):
        assert any("placeholder" in e for e in check_message({"body": body}, brief("SMS"), []))


def test_brand_always_and_link_unless_neutral():
    sms = {"body": "Please update your contact details. Reply STOP to opt out."}
    assert len(check_message(sms, brief("SMS", tone="Firm"), [])) == 2  # no brand, no link
    assert check_message(sms, brief("SMS", tone="Neutral"), []) == ["s2: must include {{brand_name}} so the customer knows who is writing"]


def test_typed_amounts_and_dates_are_refused():
    for typed in ("$120", "120 dollars", "Nov 3", "2026-11-03", "03/11/2026"):
        body = SMS["body"].replace("is still open", f"is due ({typed})")
        assert any("types an amount or a date" in e for e in check_message({"body": body}, brief("SMS"), [])), typed
    for fine in ("available 24/7", "choose 1 of 3 options"):
        body = SMS["body"].replace("is still open", fine)
        assert check_message({"body": body}, brief("SMS"), []) == [], fine


def test_threats_are_refused():
    body = SMS["body"].replace("is still open", "is open or we go to court")
    assert check_message({"body": body}, brief("SMS", tone="Firm"), []) == [
        "s2: 'court' sounds like a threat - remove it"
    ]


def test_hinted_consequences_are_refused():
    for hint in ("to avoid further action", "to keep your service uninterrupted", "before we disconnect you"):
        body = SMS["body"].replace("is still open", f"is still open, pay {hint}")
        found = check_message({"body": body}, brief("SMS", tone="Firm"), [])
        assert len(found) == 1 and "sounds like a threat" in found[0], hint


def test_days_overdue_are_not_stated():
    for duration in ("now 30-59 days past due", "overdue for over 90 days", "late by 2 weeks"):
        body = SMS["body"].replace("is still open", f"is {duration}")
        assert check_message({"body": body}, brief("SMS"), []) == [
            "s2: mentions a number of days / weeks - it differs per account, leave it out"
        ], duration


def test_final_wording_only_and_always_in_a_final_notice():
    final_only = {"body": SMS["body"].replace("is still open", "is still open. This is your final reminder")}
    assert check_message(final_only, brief("SMS", tone="Firm"), []) == [
        "s2: says 'final' / 'last' but its tone is Firm"
    ]
    assert check_message(FINAL_SMS, brief("SMS", tone="Final notice"), []) == []
    assert check_message(SMS, brief("SMS", tone="Final notice"), []) == [
        "s2: a Final notice must say it is the final reminder",
        "s2: a Final notice must say the account will then be passed to our team for review",
    ]


def test_final_notice_must_name_the_review_in_english_only():
    final_only = {"body": SMS["body"].replace("is still open", "is still open. This is your final reminder")}
    assert check_message(final_only, brief("SMS", tone="Final notice"), []) == [
        "s2: a Final notice must say the account will then be passed to our team for review"
    ]
    assert check_message(final_only, {**brief("SMS", tone="Final notice"), "english": False}, []) == []


def test_email_needs_a_short_subject_and_a_real_body():
    assert check_message({**EMAIL, "subject": ""}, brief(), []) == ["s2: an email needs a subject"]
    long_subject = check_message({**EMAIL, "subject": "Important information about your account " * 2}, brief(), [])
    assert long_subject == ["s2: subject must be one line of at most 60 characters"]
    short = check_message({**EMAIL, "body": "Hi {{first_name}}, pay: {{payment_link}} {{brand_name}}"}, brief(), [])
    assert short == ["s2: email body has 7 words - write 30 to 200"]  # counted with sample values


def test_sms_length_and_stop():
    long_sms = {"body": SMS["body"].replace("is still open.", "is still open. " + "Please pay. " * 25)}
    assert any("characters with values filled in" in e for e in check_message(long_sms, brief("SMS"), []))
    no_stop = {"body": SMS["body"].replace(" Reply STOP to opt out.", "")}
    assert check_message(no_stop, brief("SMS"), []) == ["s2: SMS must end with 'Reply STOP to opt out.'"]


def test_no_copy_paste_between_messages():
    assert check_message(SMS, brief("SMS"), [SMS["body"]]) == [
        "s2: is the same text as another message - each message must be different"
    ]
