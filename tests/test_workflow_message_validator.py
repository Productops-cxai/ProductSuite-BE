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
            "s2: mentions a number of hours / days / weeks or a deadline - it differs per account, leave it out"
        ], duration


def test_deadlines_are_not_set():
    for deadline in ("open - pay within 48 hours", "open, you have a 48-hour window", "open - pay before the deadline"):
        body = SMS["body"].replace("is still open", f"is still {deadline}")
        assert check_message({"body": body}, brief("SMS"), []) == [
            "s2: mentions a number of hours / days / weeks or a deadline - it differs per account, leave it out"
        ], deadline


def test_typed_phone_numbers_are_refused():
    for phone in ("1-800-555-0199", "(416) 555-0199", "+1 416 555 0199", "4165550199"):
        body = SMS["body"].replace("is still open", f"is still open, call {phone}")
        found = check_message({"body": body}, brief("SMS"), [])
        assert "s2: types a phone number or another long number - leave it out" in found, phone


def test_typed_web_and_email_addresses_are_refused():
    for address in ("https://maple.ca/pay", "www.maple-energy.ca", "maple-energy.ca", "billing@maple.ca"):
        body = SMS["body"].replace("is still open", f"is still open, see {address}")
        assert check_message({"body": body}, brief("SMS"), []) == [
            "s2: types a web or email address - the only link allowed is {{payment_link}}"
        ], address


def test_legal_proceedings_and_agency_threats_are_refused():
    for threat in (
        "open or legal proceedings will start",
        "open or your account will be sent to a collection agency",
        "open or it may be passed on to a debt collector",
    ):
        body = SMS["body"].replace("is still open", f"is still {threat}")
        found = check_message({"body": body}, brief("SMS", tone="Firm"), [])
        assert len(found) == 1 and "sounds like a threat" in found[0], threat


def test_an_agency_may_say_who_it_is():
    """A collection agency writing for a client must say so - that is not a threat."""
    body = SMS["body"].replace(
        "{{brand_name}}:", "{{brand_name}}: we are a collection agency contacting you on behalf of Maple Energy."
    )
    assert check_message({"body": body}, brief("SMS"), []) == []


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
    long_subject = check_message({**EMAIL, "subject": "A reminder with information about your account " * 2}, brief(), [])
    assert long_subject == ["s2: subject must be one line of at most 60 characters"]
    short = check_message({**EMAIL, "body": "Hi {{first_name}}, pay:\n{{payment_link}}\n{{brand_name}}"}, brief(), [])
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


# --------------------------------------------------------------------------- other-language versions

from app.modules.payflow.services.workflow_suggestion.message_validator import check_translation  # noqa: E402

CONTACT = "Email + SMS"
SOURCE = {"subject": EMAIL["subject"], "body": EMAIL["body"], "sms_body": SMS["body"]}
FRENCH = {
    "subject": "Votre solde {{brand_name}}",
    "body": (
        "Bonjour {{first_name}},\n\nIci {{brand_name}} au sujet du compte {{account_reference}}. Un solde de "
        "{{outstanding_balance}} reste à payer. Vous pouvez payer en totalité ou choisir un plan sur votre page "
        "sécurisée :\n\n{{payment_link}}\n\nSi vous avez déjà payé, veuillez ignorer ce message.\n\n{{brand_name}}"
    ),
    "sms_body": "{{brand_name}} : {{outstanding_balance}} reste à payer. Payez ici : {{payment_link}} "
    "Répondez STOP pour vous désabonner.",
}
CHINESE_SMS = "{{brand_name}}：您尚有{{outstanding_balance}}未付。请在此付款：{{payment_link}} 回复STOP退订。"


def test_a_good_translation_passes_in_any_language():
    assert check_translation("s2", FRENCH, SOURCE, CONTACT) == []
    # No spaces between words: measured by length, not by word count.
    assert check_translation("s2", {"body": CHINESE_SMS}, {"body": SMS["body"]}, "SMS") == []


def test_every_part_of_the_original_is_translated():
    found = check_translation("s2", {"subject": FRENCH["subject"], "body": FRENCH["body"]}, SOURCE, CONTACT)
    assert found == ["s2 (sms_body): missing - translate it too"]


def test_variables_must_stay_exactly_the_same():
    renamed = {**FRENCH, "sms_body": FRENCH["sms_body"].replace("{{payment_link}}", "{{lien_de_paiement}}")}
    found = check_translation("s2", renamed, SOURCE, CONTACT)
    assert found == [
        "s2 (sms_body): must use exactly the variables of the original ({{brand_name}}, {{outstanding_balance}}, "
        "{{payment_link}}), written the same way - it uses {{brand_name}}, {{lien_de_paiement}}, "
        "{{outstanding_balance}}"
    ]


def test_new_numbers_are_refused_in_any_language():
    """The original was checked: a new number is an invented amount, date, phone number or deadline."""
    for added in ("dans les 48 heures", "au 1-800-555-0199", "avant le 15 mars", "soit 120 $"):
        sms = {**FRENCH, "sms_body": FRENCH["sms_body"].replace("Payez ici", f"Payez {added} ici")}
        found = check_translation("s2", sms, SOURCE, CONTACT)
        assert len(found) == 1 and "has numbers the original does not have" in found[0], added


def test_typed_links_untranslated_text_and_lost_stop_are_refused():
    link = {**FRENCH, "sms_body": FRENCH["sms_body"].replace("Payez ici :", "Voir maple-energy.ca ou")}
    assert any("types a web or email address" in e for e in check_translation("s2", link, SOURCE, CONTACT))
    same = {**FRENCH, "sms_body": SMS["body"]}
    assert "s2 (sms_body): is not translated" in check_translation("s2", same, SOURCE, CONTACT)
    no_stop = {**FRENCH, "sms_body": FRENCH["sms_body"].replace("Répondez STOP", "Répondez ARRÊT")}
    assert any("must keep the word STOP" in e for e in check_translation("s2", no_stop, SOURCE, CONTACT))


def test_a_translation_adds_or_leaves_out_nothing():
    short = {**FRENCH, "body": "Bonjour {{first_name}}, payez : {{payment_link}} {{brand_name}} {{account_reference}} "
                               "{{outstanding_balance}}"}
    assert any("times as long as the original" in e for e in check_translation("s2", short, SOURCE, CONTACT))


def test_the_opt_out_sentence_is_translated_too():
    """Live: the AI kept "Reply STOP to opt out." in English inside a French SMS."""
    english_opt_out = {**FRENCH, "sms_body": FRENCH["sms_body"].replace(
        "Répondez STOP pour vous désabonner.", "Reply STOP to opt out.")}
    assert "s2 (sms_body): translate the opt-out sentence 'Reply STOP to opt out.' too, keeping the word STOP" in (
        check_translation("s2", english_opt_out, SOURCE, CONTACT)
    )
    # Into English (a French client's English customers): the English sentence is the translation.
    french_source = {"body": FRENCH["sms_body"]}
    assert check_translation("s2", {"body": SMS["body"]}, french_source, "SMS") == []
