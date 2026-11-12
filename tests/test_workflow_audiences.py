"""Who a workflow's messages are for (audiences.py): age audiences that get their own version, and the
totals that describe them - code only, no AI."""

from datetime import date

from app.infrastructure.database.models import PayflowAccountModel
from app.modules.payflow.services.workflow_suggestion import audiences
from app.modules.payflow.services.workflow_suggestion.audiences import (
    audience_of,
    audience_profile,
    find_audiences,
    members_by_segment,
)
from app.modules.payflow.services.workflow_suggestion.context import load_segment_members
from app.modules.payflow.services.workflow_suggestion.segmenter import build_segments
from tests.test_workflow_regenerate import admin  # noqa: F401 (fixture)
from tests.test_workflow_segmenter import make_account, varied_accounts
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

TODAY = date(2026, 11, 12)


def born(age: int) -> date:
    """A date of birth that makes the account `age` years old on TODAY."""
    return date(TODAY.year - age, 6, 1)


def people(n: int, age: int | None, **overrides) -> list:
    return [make_account(date_of_birth=born(age) if age is not None else None, **overrides) for _ in range(n)]


def test_the_audience_comes_from_the_date_of_birth():
    on = lambda dob: audience_of(make_account(date_of_birth=dob), TODAY)  # noqa: E731
    assert on(date(2008, 11, 12)) == "age_18_34"  # 18 today
    assert on(date(2008, 11, 13)) is None  # 17 - no audience
    assert on(date(1991, 11, 13)) == "age_18_34"  # still 34
    assert on(date(1991, 11, 12)) == "age_35_64"  # 35 today
    assert on(date(1961, 11, 12)) == "age_65_plus"  # 65 today
    assert on(None) is None


def test_big_enough_audiences_get_a_version_and_the_rest_get_the_main_text():
    members = people(30, 25) + people(40, 70) + people(10, 45) + people(25, None)
    found = find_audiences(members, TODAY)
    assert [(a["key"], a["label"], a["accounts"]) for a in found["audiences"]] == [
        ("age_65_plus", "Age 65 and over", 40),
        ("age_18_34", "Age 18 – 34", 30),
    ]  # 35 – 64 has only 10 accounts
    assert found["audiences"][0]["profile"]["age"] == {"65 and over": 100}
    # The main text is written for the 35 left: 10 aged 35 – 64, 25 without a date of birth.
    assert found["main"]["accounts"] == 35
    assert found["main"]["profile"]["age"] == {"Unknown": 71, "35 – 64": 29}
    assert found["group"]["accounts"] == 105


def test_too_few_left_the_biggest_audience_is_served_by_the_main_text():
    # 65+ is almost everyone: no version, the main text is written for it.
    found = find_audiences(people(90, 70) + people(10, 25), TODAY)
    assert found["audiences"] == [] and found["main"]["accounts"] == 100

    # Only 15 would be left for the main text: 18 – 34 (biggest, first in order on a tie) takes it over.
    found = find_audiences(people(30, 25) + people(30, 70) + people(10, 45) + people(5, None), TODAY)
    assert [a["key"] for a in found["audiences"]] == ["age_65_plus"]
    assert found["main"]["accounts"] == 45 and found["main"]["profile"]["age"]["18 – 34"] == 67

    found = find_audiences(people(170, 70) + people(30, 25), TODAY)
    assert [a["key"] for a in found["audiences"]] == ["age_18_34"]
    assert found["main"]["accounts"] == 170 and found["main"]["profile"]["age"] == {"65 and over": 100}


def test_biggest_audiences_first_and_at_most_max_versions(monkeypatch):
    members = people(25, 25) + people(40, 70) + people(30, 50) + people(20, None)
    assert [a["key"] for a in find_audiences(members, TODAY)["audiences"]] == [
        "age_65_plus", "age_35_64", "age_18_34",
    ]
    monkeypatch.setattr(audiences, "MAX_VERSIONS", 2)
    found = find_audiences(members, TODAY)
    assert [a["key"] for a in found["audiences"]] == ["age_65_plus", "age_35_64"]
    assert found["main"]["accounts"] == 45  # 18 – 34 joins the main text


def test_small_groups_get_only_the_main_text():
    found = find_audiences(people(15, 70) + people(4, 25), TODAY)
    assert found["audiences"] == [] and found["main"]["accounts"] == 19
    assert find_audiences([], TODAY) == {"group": {"accounts": 0}, "audiences": [],
                                         "main": {"accounts": 0, "profile": {"accounts": 0}}}


def test_the_profile_describes_the_accounts_by_totals_only():
    accounts = [
        make_account(date_of_birth=born(70), province_state="ON", country_code="CA", language="EN",
                     outstanding_balance=2000.0, recovered_balance=100.0, days_past_due=40),
        make_account(date_of_birth=born(70), province_state="QC", country_code="ca", language="FR",
                     outstanding_balance=1000.0, days_past_due=50),
        make_account(date_of_birth=born(30), region="Maharashtra", country_code="IN", language="hi",
                     outstanding_balance=400.0, days_past_due=None),
        make_account(postal_code="T2P 1J9", language=None, outstanding_balance=600.0, days_past_due=30),
    ]
    profile = audience_profile(accounts, TODAY)
    assert profile == {
        "accounts": 4,
        "age": {"65 and over": 50, "18 – 34": 25, "Unknown": 25},
        "regions": {"Alberta": 25, "Maharashtra": 25, "Ontario": 25, "Quebec": 25},  # Alberta: from the postal code
        "countries": {"Canada": 50, "IN": 25, "Unknown": 25},
        "languages": {"EN": 25, "FR": 25, "HI": 25, "Unknown": 25},
        "avg_balance": 1000.0,
        "balance_bands": {"$500 – $1,500": 50, "$1,500 – $5,000": 25, "Under $500": 25},
        "partly_paid_pct": 25,
        "avg_days_past_due": 40,
    }
    assert audience_profile([], TODAY) == {"accounts": 0}


def test_suggest_members_are_the_accounts_each_segment_counts():
    accounts = varied_accounts()
    segments = build_segments(accounts)["segments"]
    members = members_by_segment(accounts, segments)
    assert {s["segment_id"]: s["stats"]["accounts"] for s in segments} == {k: len(v) for k, v in members.items()}
    everyone = [id(account) for group in members.values() for account in group]
    assert len(everyone) == len(set(everyone))  # every account in exactly one segment


def test_a_saved_workflows_members_are_its_scopes_eligible_accounts_in_its_segment(
    db_session, admin, portfolio  # noqa: F811
):
    workflow = {"client_id": portfolio["client_id"], "portfolio_id": portfolio["portfolio_id"],
                "segment": {"situation": "Active"}}
    members = load_segment_members(db_session, workflow)
    assert len(members) == 29  # 30 accounts, one of them Resolved
    assert load_segment_members(db_session, {**workflow, "segment": {"situation": "Payment Plan"}}) == []
    assert load_segment_members(db_session, {**workflow, "portfolio_id": 999999}) == []

    # Real accounts from the database: in the mixed portfolio's 50 Active accounts, 25 are 65 or older.
    mixed = {**workflow, "portfolio_id": portfolio["mixed_id"]}
    rows = db_session.query(PayflowAccountModel).filter(
        PayflowAccountModel.portfolio_id == portfolio["mixed_id"],
        PayflowAccountModel.collection_status == "Active",
        PayflowAccountModel.days_past_due == 10,
    )
    for row in rows:
        row.date_of_birth = born(70)
    db_session.commit()
    found = find_audiences(load_segment_members(db_session, mixed), TODAY)
    assert [(a["key"], a["accounts"]) for a in found["audiences"]] == [("age_65_plus", 25)]
    assert found["main"]["accounts"] == 25 and found["main"]["profile"]["age"] == {"Unknown": 100}
