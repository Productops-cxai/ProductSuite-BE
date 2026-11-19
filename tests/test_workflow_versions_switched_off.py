"""What the team decided not to have (the per-version review and per-client segmentation settings of the
message-versions ticket) stays switched off:

- AI texts carry no review status, and approving a workflow never waits for each version to be reviewed one by one.
- The text checks on a person's edits are warnings only: they never block approval.
- The AI always writes versions for the fixed age groups, whatever segmentation settings a client has saved.
- The per-version review and segmentation settings APIs are not there.
"""

import json

from app.infrastructure.database.models import PayflowSegmentationConfigModel, PayflowStrategyModel
from app.modules.payflow.services.segmentation_config_service import default_segmentation_config
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from tests.test_workflow_audience_messages import _post_update, add_seniors, write_with
from tests.test_workflow_message_api import get, messages
from tests.test_workflow_regenerate import admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_ai_texts_carry_no_review_status(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    for config in messages(saved).values():
        assert "review_status" not in config
        assert all("review_status" not in version for version in config["versions"].values())
        assert config["versions"]["age_65_plus"]["segment"]["dimension"] == "age"  # which group it is for
    assert "variant_reviews" not in saved


def test_approval_never_waits_for_each_version_or_for_text_warnings(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, saved["id"])

    steps = json.loads(json.dumps(get(client, admin_token, saved["id"])["steps"]))
    step = next(s for s in steps if s["kind"] == "Communication")
    step["config"]["versions"]["age_65_plus"]["body"] += "\nWe may take legal action."  # a warning, not a block
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200 and response.json()["text_warnings"], response.json()

    approved = strategies.approve(admin, saved["id"])
    assert approved["status"] == "Approved"


def test_the_ai_ignores_a_clients_saved_segmentation_settings(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio, province_state="ON", country_code="CA")
    settings = default_segmentation_config()
    settings["dimensions"]["region"] = {"enabled": True, "values": []}
    settings["dimensions"]["country"] = {"enabled": True, "values": []}
    db_session.add(PayflowSegmentationConfigModel(client_id=portfolio["client_id"], config=settings))
    db_session.get(PayflowStrategyModel, ai_workflow["id"]).segmentation_snapshot = None  # nothing saved with it
    db_session.commit()

    saved = write_with(db_session, admin, ai_workflow["id"])
    for config in messages(saved).values():
        assert set(config["versions"]) == {"age_65_plus"}  # no "Region: Ontario" / "Country: Canada" versions


def test_the_review_and_segmentation_settings_apis_are_not_there(client, admin_token, portfolio, ai_workflow):  # noqa: F811,E501
    client_id = portfolio["client_id"]
    assert client.post(f"/payflow/workflows/{ai_workflow['id']}/versions/review",
                       json={"step_id": "s2", "audience": "main", "review_status": "Reviewed"},
                       headers=_auth(admin_token)).status_code in (404, 405)
    for path in (f"/payflow/clients/{client_id}/segmentation-config",
                 f"/payflow/clients/{client_id}/segmentation-config/history"):
        assert client.get(path, headers=_auth(admin_token)).status_code in (404, 405), path
    assert client.post(f"/payflow/clients/{client_id}/segmentation-config/update", json={"config": {}},
                       headers=_auth(admin_token)).status_code in (404, 405)
    assert "variant_reviews" not in client.get(f"/payflow/workflows/{ai_workflow['id']}/audiences",
                                               headers=_auth(admin_token)).json()
