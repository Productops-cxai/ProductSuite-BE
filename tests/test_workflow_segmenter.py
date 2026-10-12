import random
from datetime import date
from types import SimpleNamespace

from app.modules.payflow.services.workflow_suggestion import config
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    ALL_BALANCES,
    ALL_STAGES,
    ANY_CONTACT,
    CONTACT_STATES,
    SITUATIONS,
    build_segments,
    filter_eligible,
    label_account,
    match_account_to_segment,
)


def make_account(**overrides):
    base = dict(
        account_status="OPEN",
        collection_status="Active",
        provincial_hold=False,
        outstanding_balance=300.0,
        recovered_balance=0.0,
        days_past_due=10,
        last_contact_date=None,
        last_email_sent_date=None,
        last_sms_sent_date=None,
        email="a@example.com",
        phone_mobile=None,
        email_consent=True,
        language="EN",
        postal_code="M5V 1A1",
        date_of_birth=None,
        date_listed=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def many(n, **overrides):
    return [make_account(**overrides) for _ in range(n)]


def varied_accounts(n=400):
    """Deterministic mix of situations, contact states, stages and balances."""
    statuses = ["Active"] * 6 + ["Promise to Pay", "Payment Plan"]
    dpds = [None, 0, 5, 20, 35, 45, 70, 95, 200]
    balances = [50, 250, 499.99, 500, 900, 1600, 4000, 7000, 25000]
    accounts = []
    for i in range(n):
        accounts.append(
            make_account(
                collection_status=statuses[i % len(statuses)],
                days_past_due=dpds[(i * 7) % len(dpds)],
                outstanding_balance=balances[(i * 3) % len(balances)],
                recovered_balance=100.0 if i % 5 == 0 else 0.0,
                last_contact_date=date(2026, 9, 1) if i % 3 == 0 else None,
                language="FR" if i % 4 == 0 else "EN",
            )
        )
    return accounts


def counts_by_match(accounts, segments):
    counts = {}
    for account in filter_eligible(accounts)[0]:
        match = match_account_to_segment(account, segments)
        counts[match["segment_id"]] = counts.get(match["segment_id"], 0) + 1
    return counts


def test_filter_excludes_accounts_that_need_no_workflow():
    accounts = [
        make_account(account_status="CLOSED"),
        make_account(collection_status="Resolved"),
        make_account(collection_status="Human Review"),
        make_account(provincial_hold=True),
        make_account(outstanding_balance=0),
        make_account(),
    ]
    eligible, excluded = filter_eligible(accounts)
    assert len(eligible) == 1
    assert excluded == {"closed": 1, "resolved": 1, "human_review": 1, "on_hold": 1, "no_balance": 1}


def test_label_account():
    assert label_account(make_account()) == ("Active", "1 – 29 days past due", "never_contacted", "Under $500")
    assert label_account(make_account(recovered_balance=50))[2] == "partly_paid"
    assert label_account(make_account(last_sms_sent_date=date(2026, 9, 1)))[2] == "contacted_no_payment"
    # Arrangements are one group each.
    assert label_account(make_account(collection_status="Promise to Pay")) == (
        "Promise to Pay", ALL_STAGES, ANY_CONTACT, ALL_BALANCES
    )
    # Unknown stage → later fields widened too, so keys stay nested.
    assert label_account(make_account(days_past_due=None))[1:] == (ALL_STAGES, ANY_CONTACT, ALL_BALANCES)


def test_large_groups_stay_specific():
    accounts = many(25) + many(25, days_past_due=40, outstanding_balance=800)
    result = build_segments(accounts, min_size=20)
    labels = [(s["segment"]["delinquency"], s["segment"]["balance_band"]) for s in result["segments"]]
    assert labels == [
        ("1 – 29 days past due", "Under $500"),
        ("30 – 59 days past due", "$500 – $1,500"),
    ]


def test_small_groups_widen_balance_first():
    accounts = many(25) + many(12, outstanding_balance=800) + many(10, outstanding_balance=2000)
    result = build_segments(accounts, min_size=20)
    segments = {(s["segment"]["delinquency"], s["segment"]["balance_band"]): s for s in result["segments"]}
    assert segments[("1 – 29 days past due", "Under $500")]["stats"]["accounts"] == 25
    assert segments[("1 – 29 days past due", ALL_BALANCES)]["stats"]["accounts"] == 22


def test_contact_state_widens_before_delinquency():
    contacted = date(2026, 9, 1)
    accounts = (
        many(12, days_past_due=40)
        + many(12, days_past_due=40, last_contact_date=contacted)
        + many(25)
    )
    result = build_segments(accounts, min_size=20)
    segments = {(s["segment"]["delinquency"], s["segment"]["contact_state"]): s for s in result["segments"]}
    assert segments[("30 – 59 days past due", ANY_CONTACT)]["stats"]["accounts"] == 24
    assert segments[("1 – 29 days past due", "never_contacted")]["stats"]["accounts"] == 25


def test_days_late_survives_the_segment_cap():
    """Regression: a realistic portfolio must keep its delinquency stages."""
    rng = random.Random(42)
    accounts = [
        make_account(
            collection_status=rng.choices(["Active", "Promise to Pay", "Payment Plan"], weights=[70, 12, 10])[0],
            days_past_due=rng.choices([5, 15, 25, 40, 55, 75, 120, 200], weights=[20, 18, 12, 14, 10, 10, 9, 7])[0],
            outstanding_balance=rng.uniform(80, 15000),
            recovered_balance=100.0 if rng.random() < 0.25 else 0.0,
            last_contact_date=date(2026, 9, 1) if rng.random() < 0.55 else None,
        )
        for _ in range(1000)
    ]
    result = build_segments(accounts)
    assert len(result["segments"]) <= config.MAX_SEGMENTS
    active_stages = {
        s["segment"]["delinquency"] for s in result["segments"] if s["segment"]["situation"] == "Active"
    }
    assert len(active_stages - {ALL_STAGES}) >= 3


def test_situations_are_never_merged():
    accounts = many(30) + many(3, collection_status="Promise to Pay")
    result = build_segments(accounts, min_size=20)
    situations = [s["segment"]["situation"] for s in result["segments"]]
    assert situations == ["Active", "Promise to Pay"]
    assert result["segments"][1]["stats"]["accounts"] == 3


def test_every_eligible_account_fits_exactly_its_own_segment():
    accounts = varied_accounts()
    # No cap, so specific segments and widened "All …" catch-alls coexist.
    result = build_segments(accounts, min_size=15, max_segments=50)
    stages = {s["segment"]["delinquency"] for s in result["segments"]}
    assert ALL_STAGES in stages and len(stages) > 1
    expected = {s["segment_id"]: s["stats"]["accounts"] for s in result["segments"]}
    assert counts_by_match(accounts, result["segments"]) == expected
    assert sum(expected.values()) == result["eligible_accounts"]


def test_max_segments_cap():
    accounts = varied_accounts()
    result = build_segments(accounts, min_size=1, max_segments=4)
    assert 0 < len(result["segments"]) <= 4
    expected = {s["segment_id"]: s["stats"]["accounts"] for s in result["segments"]}
    assert counts_by_match(accounts, result["segments"]) == expected


def test_same_input_gives_same_segments():
    accounts = varied_accounts()
    shuffled = accounts[:]
    random.Random(1).shuffle(shuffled)
    assert build_segments(accounts) == build_segments(shuffled)


def test_segment_values_are_catalog_labels():
    for segment in build_segments(varied_accounts(), min_size=10)["segments"]:
        values = segment["segment"]
        for field, labels in config.SEGMENT_FIELDS.items():
            assert values[field] in labels
        assert values["situation"] in SITUATIONS
        assert values["contact_state"] in CONTACT_STATES


def test_no_eligible_accounts():
    result = build_segments([make_account(account_status="CLOSED")])
    assert result["segments"] == []
    assert result["eligible_accounts"] == 0
    assert result["total_accounts"] == 1


def test_matcher_respects_ui_fields_and_prefers_most_specific():
    catch_all = {"segment_id": "ALL", "segment": {"situation": "Active"}}
    quebec_fr = {
        "segment_id": "QC",
        "segment": {"situation": "Active", "language": "French", "postal_region": "Quebec (H, J, G)"},
    }
    items = [catch_all, quebec_fr]
    assert match_account_to_segment(make_account(language="FR", postal_code="H2X 1Y4"), items) is quebec_fr
    assert match_account_to_segment(make_account(language="EN", postal_code="H2X 1Y4"), items) is catch_all
    assert match_account_to_segment(make_account(collection_status="Payment Plan"), items) is None


def test_matcher_age_and_tenure():
    today = date(2026, 10, 9)
    item = {"segment": {"age_band": "25 – 34", "tenure": "New customer (under 6 months)"}}
    fits = make_account(date_of_birth=date(1996, 1, 1), date_listed=date(2026, 6, 1))
    too_old = make_account(date_of_birth=date(1980, 1, 1), date_listed=date(2026, 6, 1))
    assert match_account_to_segment(fits, [item], today) is item
    assert match_account_to_segment(too_old, [item], today) is None
