"""Compliance rule catalogue: APIs, validation, lifecycle, versions, audit, jurisdictions."""

import pytest

from app.infrastructure.database.models import (
    PayflowComplianceAuditModel,
    PayflowComplianceRuleModel,
    PayflowComplianceRuleVersionModel,
)
from app.modules.payflow.services.compliance.rule_service import seed_compliance_library
from tests.test_security_gaps import _auth, scoped_token, two_clients  # noqa: F401  (fixtures)

BASE = "/payflow/compliance"


def payload(**over):
    body = {
        "name": "Ontario contact hours",
        "rule_type": "JUR-01",
        "description": "Mon–Sat 7am–9pm, Sun 1–5pm",
        "legal_reference": "Ontario Collection and Debt Settlement Services Act",
        "regulatory_authority": "Ontario Ministry of Public and Business Service Delivery",
        "country": "Canada",
        "state": "Ontario",
        "channel": "All Channels",
        "enforcement": "Mandatory",
        "action": "Reschedule",
        "effective_from": "2026-01-01",
        "conditions": [{"field": "Outside Permitted Hours", "operator": "Equals", "value": "Yes"}],
        "settings": {
            "start": "07:00", "end": "21:00", "days": [0, 1, 2, 3, 4, 5, 6],
            "sunday_start": "13:00", "sunday_end": "17:00",
        },
    }
    body.update(over)
    return body


def create(client, token, **over):
    response = client.post(f"{BASE}/rules", json=payload(**over), headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def activate(client, token, rule_id):
    submitted = client.post(f"{BASE}/rules/{rule_id}/submit", json={}, headers=_auth(token))
    assert submitted.status_code == 200, submitted.text
    approved = client.post(f"{BASE}/rules/{rule_id}/approve", json={}, headers=_auth(token))
    assert approved.status_code == 200, approved.text
    return approved.json()


# ------------------------------------------------------------------ AC1 / AC2


def test_rule_is_saved_as_a_draft_and_read_back_without_loss(client, admin_token):
    created = create(client, admin_token)

    fetched = client.get(f"{BASE}/rules/{created['id']}", headers=_auth(admin_token)).json()

    assert fetched["status"] == "Draft"
    assert fetched["version"] == 1
    assert fetched["code"].startswith("CMP-")
    assert fetched["category"] == "Permitted Hours"  # from the rule type
    for key in ("name", "rule_type", "legal_reference", "country", "state", "action", "enforcement"):
        assert fetched[key] == payload()[key]
    assert fetched["settings"]["sunday_start"] == "13:00"
    assert fetched["conditions"][0]["field"] == "Outside Permitted Hours"
    assert fetched["effective_from"] == "2026-01-01"


def test_rules_can_be_filtered_sorted_and_paged(client, admin_token):
    create(client, admin_token, name="B hours")
    create(client, admin_token, name="A hours", state="Quebec")
    create(client, admin_token, name="C opt-out", rule_type="JUR-09", action="Suppress",
           conditions=[{"field": "Opt-Out", "operator": "Equals", "value": "Yes"}])

    by_state = client.get(f"{BASE}/rules?state=Quebec", headers=_auth(admin_token)).json()
    assert [r["name"] for r in by_state["rules"]] == ["A hours"]

    by_type = client.get(f"{BASE}/rules?rule_type=JUR-09", headers=_auth(admin_token)).json()
    assert by_type["total"] == 1

    page = client.get(
        f"{BASE}/rules?sort=name&order=asc&limit=2&offset=0", headers=_auth(admin_token)
    ).json()
    assert [r["name"] for r in page["rules"]] == ["A hours", "B hours"]
    assert page["total"] == 3

    search = client.get(f"{BASE}/rules?search=opt-out", headers=_auth(admin_token)).json()
    assert [r["name"] for r in search["rules"]] == ["C opt-out"]


def test_update_needs_a_reason_and_keeps_the_previous_version(client, admin_token, db_session):
    rule = create(client, admin_token)

    no_reason = client.post(
        f"{BASE}/rules/{rule['id']}/update", json={"name": "Renamed"}, headers=_auth(admin_token)
    )
    assert no_reason.status_code == 400

    updated = client.post(
        f"{BASE}/rules/{rule['id']}/update",
        json={"name": "Ontario contact hours (2027)", "reason": "Regulation amended"},
        headers=_auth(admin_token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    history = client.get(f"{BASE}/rules/{rule['id']}/history", headers=_auth(admin_token)).json()
    versions = {v["version"]: v for v in history["versions"]}
    assert versions[1]["snapshot"]["name"] == "Ontario contact hours"
    assert versions[2]["snapshot"]["name"] == "Ontario contact hours (2027)"
    assert versions[2]["change_reason"] == "Regulation amended"

    audit = (
        db_session.query(PayflowComplianceAuditModel)
        .filter_by(rule_id=rule["id"], event="Rule modified")
        .one()
    )
    assert audit.before["name"] == "Ontario contact hours"
    assert audit.after["name"] == "Ontario contact hours (2027)"
    assert audit.actor_name == "Platform Super Admin"


def test_duplicate_makes_a_new_draft_copy(client, admin_token):
    rule = create(client, admin_token)
    activate(client, admin_token, rule["id"])

    copy = client.post(f"{BASE}/rules/{rule['id']}/duplicate", json={}, headers=_auth(admin_token))

    assert copy.status_code == 200
    body = copy.json()
    assert body["id"] != rule["id"]
    assert body["status"] == "Draft"
    assert body["version"] == 1
    assert body["name"] == "Ontario contact hours (copy)"


# ------------------------------------------------------------------ lifecycle


def test_full_lifecycle_submit_approve_deactivate_activate(client, admin_token):
    rule = create(client, admin_token)
    headers = _auth(admin_token)

    assert client.post(f"{BASE}/rules/{rule['id']}/submit", json={}, headers=headers).json()[
        "status"
    ] == "Pending Approval"
    approved = client.post(f"{BASE}/rules/{rule['id']}/approve", json={}, headers=headers).json()
    assert approved["status"] == "Active"
    assert approved["approved_by"] == "Platform Super Admin"
    off = client.post(
        f"{BASE}/rules/{rule['id']}/deactivate", json={"reason": "Under legal review"}, headers=headers
    ).json()
    assert off["status"] == "Inactive"
    assert client.post(f"{BASE}/rules/{rule['id']}/activate", json={}, headers=headers).json()[
        "status"
    ] == "Active"


def test_rejected_rule_goes_back_to_draft_with_the_reason(client, admin_token):
    rule = create(client, admin_token)
    headers = _auth(admin_token)
    client.post(f"{BASE}/rules/{rule['id']}/submit", json={}, headers=headers)

    rejected = client.post(
        f"{BASE}/rules/{rule['id']}/reject", json={"reason": "Citation is wrong"}, headers=headers
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "Draft"
    assert rejected.json()["rejection_reason"] == "Citation is wrong"


def test_draft_cannot_be_activated_without_approval(client, admin_token):
    rule = create(client, admin_token)
    response = client.post(f"{BASE}/rules/{rule['id']}/activate", json={}, headers=_auth(admin_token))
    assert response.status_code == 400


def test_every_status_change_is_audited(client, admin_token, db_session):
    rule = create(client, admin_token)
    activate(client, admin_token, rule["id"])

    events = [
        row.event
        for row in db_session.query(PayflowComplianceAuditModel)
        .filter_by(rule_id=rule["id"])
        .order_by(PayflowComplianceAuditModel.id.asc())
        .all()
    ]
    assert events == [
        "Rule created",
        "Status: Draft → Pending Approval",
        "Status: Pending Approval → Active",
    ]


# ------------------------------------------------------------------ AC4 validation


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"legal_reference": ""}, "legal"),
        ({"settings": {"start": "21:00", "end": "07:00", "days": [1, 2]}}, "start"),
        ({"settings": {"start": "07:00", "end": "21:00", "days": []}}, "day"),
        ({"effective_from": "2027-01-01", "effective_until": "2026-01-01"}, "expiry"),
        ({"effective_from": None}, "effective"),
        ({"action": "Allow"}, "action"),
    ],
)
def test_invalid_configuration_cannot_be_submitted_for_activation(client, admin_token, change, message):
    rule = create(client, admin_token, **change)

    response = client.post(f"{BASE}/rules/{rule['id']}/submit", json={}, headers=_auth(admin_token))

    assert response.status_code == 400
    assert message in response.json()["detail"].lower()


def test_frequency_rule_needs_a_threshold_and_a_rolling_period(client, admin_token):
    rule = create(
        client, admin_token, rule_type="JUR-04", action="Block",
        conditions=[{"field": "Contact Attempts", "operator": "Greater Than", "value": "", "period": 0}],
        settings={},
    )
    response = client.post(f"{BASE}/rules/{rule['id']}/submit", json={}, headers=_auth(admin_token))
    assert response.status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"country": "France"},
        {"country": "Canada", "state": "Texas"},
        {"rule_type": "JUR-99"},
        {"channel": "Carrier pigeon"},
        {"action": "Explode"},
        {"conditions": [{"field": "Shoe Size", "operator": "Equals", "value": "9"}]},
        {"name": ""},
        {"settings": {"start": "25:00", "end": "26:00", "days": [1]}},  # not a time
        {"settings": {"start": "07:00", "end": "21:00", "days": [9]}},  # not a weekday
    ],
)
def test_unsupported_values_are_refused_on_save(client, admin_token, change):
    response = client.post(f"{BASE}/rules", json=payload(**change), headers=_auth(admin_token))
    assert response.status_code in (400, 422)


def test_two_active_rules_for_the_same_scope_may_not_overlap(client, admin_token):
    first = create(client, admin_token)
    activate(client, admin_token, first["id"])
    second = create(client, admin_token, name="Same thing again")
    client.post(f"{BASE}/rules/{second['id']}/submit", json={}, headers=_auth(admin_token))

    response = client.post(f"{BASE}/rules/{second['id']}/approve", json={}, headers=_auth(admin_token))

    assert response.status_code == 409
    assert first["code"] in response.json()["detail"]


def test_a_later_version_may_follow_once_the_first_has_an_end_date(client, admin_token):
    first = create(client, admin_token, effective_until="2026-12-31")
    activate(client, admin_token, first["id"])
    second = create(client, admin_token, name="From 2027", effective_from="2027-01-01")

    assert activate(client, admin_token, second["id"])["status"] == "Active"


# ------------------------------------------------------------------ AC20 authorization


def test_user_without_governance_permission_cannot_read_or_change_rules(client, admin_token, scoped_token):
    rule = create(client, admin_token)
    headers = _auth(scoped_token)

    assert client.get(f"{BASE}/rules", headers=headers).status_code == 403
    assert client.post(f"{BASE}/rules", json=payload(), headers=headers).status_code == 403
    assert client.post(f"{BASE}/rules/{rule['id']}/approve", json={}, headers=headers).status_code == 403
    assert client.get(f"{BASE}/audit", headers=headers).status_code == 403


def test_requests_without_a_login_are_refused(client):
    assert client.get(f"{BASE}/rules").status_code == 401
    assert client.post(f"{BASE}/evaluate", json={}).status_code == 401


# ------------------------------------------------------------------ AC3 jurisdictions


def test_jurisdiction_api_returns_the_states_of_a_country(client, admin_token):
    countries = client.get(f"{BASE}/jurisdictions", headers=_auth(admin_token)).json()
    assert [c["name"] for c in countries["countries"]] == ["United States", "Canada"]

    canada = client.get(f"{BASE}/jurisdictions?country=Canada", headers=_auth(admin_token)).json()
    ontario = next(s for s in canada["states"] if s["name"] == "Ontario")
    assert ontario == {"code": "ON", "name": "Ontario", "timezone": "America/Toronto"}
    assert len(canada["states"]) == 13

    ny = client.get(
        f"{BASE}/jurisdictions?country=US&state=NY", headers=_auth(admin_token)
    ).json()
    assert ny["regions"][0]["name"] == "New York City"


def test_unknown_country_is_not_found(client, admin_token):
    response = client.get(f"{BASE}/jurisdictions?country=Atlantis", headers=_auth(admin_token))
    assert response.status_code == 404


def test_catalog_lists_all_25_rule_types(client, admin_token):
    catalog = client.get(f"{BASE}/catalog", headers=_auth(admin_token)).json()
    assert len(catalog["rule_types"]) == 25
    assert catalog["rule_types"][0]["code"] == "JUR-01"


# ------------------------------------------------------------------ holidays


def test_holiday_calendar_can_be_maintained(client, admin_token):
    headers = _auth(admin_token)
    created = client.post(
        f"{BASE}/holidays",
        json={"name": "Canada Day", "date": "2026-07-01", "recurring": True, "country": "Canada"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    holiday_id = created.json()["id"]

    listed = client.get(f"{BASE}/holidays?country=Canada", headers=headers).json()
    assert [h["name"] for h in listed["holidays"]] == ["Canada Day"]

    client.post(f"{BASE}/holidays/{holiday_id}/update", json={"name": "Canada Day (Fête)"}, headers=headers)
    assert client.get(f"{BASE}/holidays", headers=headers).json()["holidays"][0]["name"] == "Canada Day (Fête)"

    assert client.post(f"{BASE}/holidays/{holiday_id}/delete", json={}, headers=headers).status_code == 200
    assert client.get(f"{BASE}/holidays", headers=headers).json()["holidays"] == []


# ------------------------------------------------------------------ evaluate (rule testing)


def test_evaluate_api_simulates_a_communication(client, admin_token, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    rule = create(
        client, admin_token, name="Canada opt-out", rule_type="JUR-09", action="Suppress", state="",
        conditions=[{"field": "Opt-Out", "operator": "Equals", "value": "Yes"}], settings={},
    )
    activate(client, admin_token, rule["id"])

    body = {
        "client_id": mine.id,
        "channel": "Email",
        "at": "2026-11-09T15:00:00Z",
        "jurisdiction": {"country": "Canada", "state": "Ontario"},
        "opted_out": True,
        "record": True,
    }
    response = client.post(f"{BASE}/evaluate", json=body, headers=_auth(admin_token))

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["decision"] == "BLOCK"
    assert result["matches"][0]["rule_code"] == rule["code"]
    assert result["matches"][0]["version"] >= 1
    assert result["reason_codes"] == ["OPT_OUT"]
    recorded = db_session.query(PayflowComplianceAuditModel).filter_by(entity_type="evaluation").one()
    assert recorded.event == "Execution check: BLOCK"


def test_evaluate_api_without_jurisdiction_asks_for_review(client, admin_token, two_clients):
    mine, _other, _portfolio = two_clients
    rule = create(client, admin_token, rule_type="JUR-09", action="Suppress", state="",
                  conditions=[{"field": "Opt-Out", "operator": "Equals", "value": "Yes"}], settings={})
    activate(client, admin_token, rule["id"])

    response = client.post(
        f"{BASE}/evaluate",
        json={"client_id": mine.id, "channel": "Email", "at": "2026-11-09T15:00:00Z"},
        headers=_auth(admin_token),
    )

    assert response.json()["decision"] == "REVIEW_REQUIRED"


# ------------------------------------------------------------------ library seed


def test_jurisdiction_library_is_seeded_as_drafts_and_only_once(db_session):
    first = seed_compliance_library(db_session)
    second = seed_compliance_library(db_session)

    rules = db_session.query(PayflowComplianceRuleModel).all()
    assert first["rules"] == len(rules) > 40
    assert second["rules"] == 0
    assert {r.status for r in rules} == {"Draft"}  # nothing is enforced until activated
    assert all(r.is_library for r in rules)
    ontario = next(r for r in rules if r.code == "JL-CA-ON")
    assert ontario.settings["sunday_start"] == "13:00"
    assert db_session.query(PayflowComplianceRuleVersionModel).count() == len(rules)
