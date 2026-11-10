"""Compliance evaluation engine: every applicable restriction counts, nothing is assumed allowed."""

from datetime import datetime, timedelta, timezone

from app.modules.payflow.services.compliance.engine import (
    ContactEvent,
    EvaluationInput,
    Jurisdiction,
    evaluate,
    jurisdiction_matches,
)

ONTARIO = Jurisdiction(country="Canada", state="Ontario", timezone="America/Toronto")
NEW_YORK = Jurisdiction(country="United States", state="New York", timezone="America/New_York")
# Monday 9 Nov 2026, 15:00 UTC = 10:00 in Toronto / New York.
MONDAY_MORNING = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)

_SEQ = 0


def rule(**over):
    global _SEQ
    _SEQ += 1
    base = {
        "id": _SEQ,
        "code": f"CMP-T{_SEQ:03d}",
        "name": f"Rule {_SEQ}",
        "version": 1,
        "category": "Consent & Opt-Out",
        "rule_type": "JUR-09",
        "enforcement": "Mandatory",
        "status": "Active",
        "action": "Block",
        "channel": "All Channels",
        "logic": "AND",
        "priority": 100,
        "country": "Canada",
        "state": "",
        "region": "",
        "city": "",
        "postal": "",
        "client_id": None,
        "collector_type": "",
        "debt_type": "",
        "effective_from": None,
        "effective_until": None,
        "conditions": [],
        "settings": {},
    }
    base.update(over)
    return base


def cond(field, value, operator="Equals", period=7):
    return {"field": field, "operator": operator, "value": value, "period": period}


def facts(**over):
    base = dict(
        client_id=1,
        account_id=10,
        jurisdiction=ONTARIO,
        channel="Email",
        at=MONDAY_MORNING,
        consent=True,
        opted_out=False,
    )
    base.update(over)
    return EvaluationInput(**base)


OPT_OUT = dict(conditions=[cond("Opt-Out", "Yes")], action="Suppress")
HOURS_9_TO_6 = dict(
    category="Permitted Hours",
    rule_type="JUR-01",
    action="Reschedule",
    conditions=[cond("Outside Permitted Hours", "Yes")],
    settings={"start": "09:00", "end": "18:00", "days": [1, 2, 3, 4, 5]},
)


def test_nothing_applicable_means_allow():
    result = evaluate([], [], facts())
    assert result.decision == "ALLOW"
    assert result.matches == []


def test_opt_out_blocks_and_names_the_rule_and_version():
    r = rule(**OPT_OUT, version=3)
    result = evaluate([r], [], facts(opted_out=True))
    assert result.decision == "BLOCK"
    assert [(m["rule_code"], m["version"]) for m in result.matches] == [(r["code"], 3)]
    assert result.matches[0]["reason_code"] == "OPT_OUT"


def test_broader_and_narrower_restrictions_are_both_enforced():
    country = rule(**OPT_OUT)
    province = rule(**HOURS_9_TO_6, state="Ontario")
    late = MONDAY_MORNING + timedelta(hours=12)  # 22:00 Toronto
    result = evaluate([country, province], [], facts(opted_out=True, at=late))
    assert result.decision == "BLOCK"
    assert {m["rule_code"] for m in result.matches} == {country["code"], province["code"]}


def test_an_allow_rule_never_overrides_a_prohibition():
    block = rule(**OPT_OUT)
    client_allow = rule(action="Allow", client_id=1, state="Ontario", conditions=[cond("Opt-Out", "Yes")])
    result = evaluate([block, client_allow], [], facts(opted_out=True))
    assert result.decision == "BLOCK"


def test_rule_for_one_channel_does_not_touch_another():
    sms_only = rule(**OPT_OUT, channel="SMS")
    assert evaluate([sms_only], [], facts(opted_out=True, channel="Email")).decision == "ALLOW"
    assert evaluate([sms_only], [], facts(opted_out=True, channel="SMS")).decision == "BLOCK"


def test_another_clients_rule_is_never_evaluated():
    theirs = rule(**OPT_OUT, client_id=2)
    result = evaluate([theirs], [], facts(opted_out=True))
    assert result.decision == "ALLOW"
    assert result.evaluated == []


def test_draft_and_not_yet_effective_rules_are_ignored():
    draft = rule(**OPT_OUT, status="Draft")
    future = rule(**OPT_OUT, effective_from=(MONDAY_MORNING + timedelta(days=30)).date())
    expired = rule(**OPT_OUT, effective_until=(MONDAY_MORNING - timedelta(days=1)).date())
    assert evaluate([draft, future, expired], [], facts(opted_out=True)).decision == "ALLOW"


def test_rule_becomes_enforceable_on_its_effective_date():
    starts_today = rule(**OPT_OUT, effective_from=MONDAY_MORNING.date())
    assert evaluate([starts_today], [], facts(opted_out=True)).decision == "BLOCK"


def test_outside_hours_reschedules_to_the_next_permitted_local_time():
    hours = rule(**HOURS_9_TO_6, state="Ontario")
    friday_night = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)  # Fri 20:00 Toronto
    result = evaluate([hours], [], facts(at=friday_night))
    assert result.decision == "RESCHEDULE"
    # Next permitted: Monday 09:00 Toronto = 14:00 UTC (weekend excluded).
    assert result.next_permitted_at == datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)


def test_hours_use_the_customers_time_zone_not_the_servers():
    hours = rule(**HOURS_9_TO_6, country="United States")
    at = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)  # 10:00 New York, 07:00 Los Angeles
    east = facts(jurisdiction=NEW_YORK, at=at)
    west = facts(
        jurisdiction=Jurisdiction("United States", "California", timezone="America/Los_Angeles"), at=at
    )
    assert evaluate([hours], [], east).decision == "ALLOW"
    assert evaluate([hours], [], west).decision == "RESCHEDULE"


def test_sunday_has_its_own_window_without_changing_saturday():
    hours = rule(
        category="Permitted Hours",
        rule_type="JUR-01",
        action="Reschedule",
        state="Ontario",
        conditions=[cond("Outside Permitted Hours", "Yes")],
        settings={
            "start": "07:00", "end": "21:00", "days": [0, 1, 2, 3, 4, 5, 6],
            "sunday_start": "13:00", "sunday_end": "17:00",
        },
    )
    saturday_10 = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    sunday_10 = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
    sunday_14 = datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)
    assert evaluate([hours], [], facts(at=saturday_10)).decision == "ALLOW"
    assert evaluate([hours], [], facts(at=sunday_10)).decision == "RESCHEDULE"
    assert evaluate([hours], [], facts(at=sunday_14)).decision == "ALLOW"


def test_holiday_holds_the_message_until_the_next_working_day():
    holiday_rule = rule(
        category="Holidays & Restricted Dates",
        rule_type="JUR-03",
        action="Reschedule",
        conditions=[cond("Restricted Date", "Yes")],
    )
    holidays = [{"date": "2026-12-25", "recurring": True, "country": "Canada", "state": ""}]
    christmas = datetime(2027, 12, 25, 15, 0, tzinfo=timezone.utc)  # recurring: next year too
    result = evaluate([holiday_rule], holidays, facts(at=christmas))
    assert result.decision == "RESCHEDULE"
    assert result.next_permitted_at.date().isoformat() == "2027-12-26"


def test_rolling_contact_limit_blocks_at_the_threshold_and_ignores_older_contacts():
    limit = rule(
        category="Frequency",
        rule_type="JUR-04",
        action="Block",
        conditions=[cond("Contact Attempts", "2", "Greater Than", period=7)],
        settings={"combined": True},
    )
    recent = [
        ContactEvent(MONDAY_MORNING - timedelta(days=d), "Email", "attempted", 10) for d in (1, 2, 3)
    ]
    old = [ContactEvent(MONDAY_MORNING - timedelta(days=9), "Email", "attempted", 10)]
    assert evaluate([limit], [], facts(history=recent)).decision == "BLOCK"
    assert evaluate([limit], [], facts(history=recent[:2] + old)).decision == "ALLOW"


def test_per_channel_limit_does_not_count_other_channels():
    limit = rule(
        category="Frequency",
        rule_type="JUR-04",
        action="Block",
        channel="SMS",
        conditions=[cond("Contact Attempts", "1", "Greater Than", period=7)],
        settings={"combined": False},
    )
    history = [
        ContactEvent(MONDAY_MORNING - timedelta(days=1), "Email", "attempted", 10),
        ContactEvent(MONDAY_MORNING - timedelta(days=2), "Email", "attempted", 10),
        ContactEvent(MONDAY_MORNING - timedelta(days=3), "SMS", "attempted", 10),
    ]
    assert evaluate([limit], [], facts(channel="SMS", history=history)).decision == "ALLOW"


def test_cooling_period_after_a_conversation():
    cooling = rule(
        category="Frequency",
        rule_type="JUR-06",
        action="Block",
        conditions=[cond("Days Since Conversation", "7", "Less Than")],
    )
    talked = facts(last_conversation_at=MONDAY_MORNING - timedelta(days=2))
    long_ago = facts(last_conversation_at=MONDAY_MORNING - timedelta(days=10))
    never = facts()
    assert evaluate([cooling], [], talked).decision == "BLOCK"
    assert evaluate([cooling], [], long_ago).decision == "ALLOW"
    assert evaluate([cooling], [], never).decision == "ALLOW"


def test_suppression_flags_block_whatever_the_channel():
    cease = rule(
        category="Contact Restrictions",
        rule_type="JUR-10",
        action="Suppress",
        conditions=[cond("Cease Communication", "Yes")],
    )
    result = evaluate([cease], [], facts(flags={"Cease Communication"}))
    assert result.decision == "BLOCK"
    assert result.matches[0]["reason_code"] == "CEASE_COMMUNICATION"


def test_review_actions_require_review_and_say_so():
    attorney = rule(
        category="Contact Restrictions",
        rule_type="JUR-11",
        action="Require Approval",
        conditions=[cond("Legal Representation", "Yes")],
    )
    result = evaluate([attorney], [], facts(flags={"Legal Representation"}))
    assert result.decision == "REVIEW_REQUIRED"
    assert result.requires_review is True


def test_block_outranks_review_which_outranks_reschedule():
    block = rule(**OPT_OUT)
    review = rule(action="Flag for Review", conditions=[cond("Disputed Debt", "Yes")])
    hours = rule(**HOURS_9_TO_6)
    night = MONDAY_MORNING + timedelta(hours=12)
    everything = facts(opted_out=True, flags={"Disputed Debt"}, at=night)
    assert evaluate([block, review, hours], [], everything).decision == "BLOCK"
    assert evaluate([review, hours], [], everything).decision == "REVIEW_REQUIRED"
    assert evaluate([hours], [], everything).decision == "RESCHEDULE"


def test_advisory_rule_is_reported_as_a_warning_and_does_not_block():
    advisory = rule(**OPT_OUT, enforcement="Advisory")
    result = evaluate([advisory], [], facts(opted_out=True))
    assert result.decision == "ALLOW"
    assert [w["rule_code"] for w in result.warnings] == [advisory["code"]]


def test_client_can_switch_off_an_optional_rule_but_not_a_mandatory_one():
    optional = rule(**OPT_OUT, enforcement="Client Configurable")
    mandatory = rule(**OPT_OUT)
    off = {optional["id"], mandatory["id"]}
    result = evaluate([optional, mandatory], [], facts(opted_out=True), disabled_rule_ids=off)
    assert result.decision == "BLOCK"
    assert [m["rule_code"] for m in result.matches] == [mandatory["code"]]


def test_missing_jurisdiction_is_review_required_when_rules_are_in_force():
    r = rule(**OPT_OUT)
    nowhere = facts(jurisdiction=Jurisdiction(country="", state="", timezone=""))
    result = evaluate([r], [], nowhere)
    assert result.decision == "REVIEW_REQUIRED"
    assert result.matches[0]["reason_code"] == "JURISDICTION_UNKNOWN"


def test_missing_time_zone_is_review_required_for_time_based_rules():
    hours = rule(**HOURS_9_TO_6)
    no_tz = facts(jurisdiction=Jurisdiction(country="Canada", state="", timezone=""))
    assert evaluate([hours], [], no_tz).decision == "REVIEW_REQUIRED"


def test_consent_expires_after_the_configured_days():
    consent = rule(
        category="Consent & Opt-Out",
        rule_type="JUR-08",
        action="Block",
        conditions=[cond("Consent", "No")],
        settings={"consent_expiry_days": 365},
    )
    fresh = facts(consent=True, consent_date=MONDAY_MORNING - timedelta(days=30))
    stale = facts(consent=True, consent_date=MONDAY_MORNING - timedelta(days=400))
    assert evaluate([consent], [], fresh).decision == "ALLOW"
    assert evaluate([consent], [], stale).decision == "BLOCK"


def test_unknown_consent_is_not_treated_as_consent():
    consent = rule(action="Block", rule_type="JUR-08", conditions=[cond("Consent", "No")])
    assert evaluate([consent], [], facts(consent=None)).decision == "BLOCK"


def test_required_disclosure_text_must_be_in_the_message():
    disclosure = rule(
        category="Content",
        rule_type="JUR-20",
        action="Require Approval",
        settings={"required_text": "This is an attempt to collect a debt"},
    )
    without = facts(message_text="Please pay your balance.")
    with_text = facts(message_text="Hello. This is an attempt to collect a debt. Please pay.")
    assert evaluate([disclosure], [], without).decision == "REVIEW_REQUIRED"
    assert evaluate([disclosure], [], with_text).decision == "ALLOW"


def test_customer_inconvenient_times_are_respected():
    preference = rule(
        category="Contact Restrictions",
        rule_type="JUR-22",
        action="Reschedule",
        conditions=[cond("Outside Customer Preference", "Yes")],
    )
    busy = facts(inconvenient_times=[{"days": [1], "start": "09:00", "end": "12:00"}])
    result = evaluate([preference], [], busy)  # Monday 10:00 local
    assert result.decision == "RESCHEDULE"
    assert result.next_permitted_at == datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc)


def test_collector_and_debt_type_narrow_a_rule():
    agency_only = rule(**OPT_OUT, collector_type="Third Party", debt_type="Consumer")
    first_party = facts(opted_out=True, collector_type="First Party", debt_type="Consumer")
    third_party = facts(opted_out=True, collector_type="Third Party", debt_type="Consumer")
    assert evaluate([agency_only], [], first_party).decision == "ALLOW"
    assert evaluate([agency_only], [], third_party).decision == "BLOCK"


def test_jurisdiction_scope_matching():
    target = Jurisdiction(country="Canada", state="Ontario", city="Toronto", postal="M5V 2T6")
    assert jurisdiction_matches({"country": "Canada"}, target)
    assert jurisdiction_matches({"country": "canada", "state": "ONTARIO"}, target)
    assert not jurisdiction_matches({"country": "Canada", "state": "Quebec"}, target)
    assert jurisdiction_matches({"country": "Canada", "postal": "M5V"}, target)
    assert jurisdiction_matches({"country": "Canada", "postal": "M4A..M6Z"}, target)
    assert not jurisdiction_matches({"country": "Canada", "postal": "K1A..K9Z"}, target)
    assert not jurisdiction_matches({"country": "Canada", "city": "Ottawa"}, target)
    assert jurisdiction_matches({}, target)


def test_every_applicable_rule_is_listed_as_evaluated():
    a = rule(**OPT_OUT)
    b = rule(**HOURS_9_TO_6, state="Ontario")
    other_province = rule(**OPT_OUT, state="Quebec")
    result = evaluate([a, b, other_province], [], facts())
    assert result.decision == "ALLOW"
    assert sorted(result.evaluated) == sorted([a["code"], b["code"]])
