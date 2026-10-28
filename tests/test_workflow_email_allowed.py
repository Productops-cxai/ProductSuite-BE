"""Unknown email consent must not push every workflow to SMS.

Account files without a consent column leave email_consent empty. The worker still emails those
accounts (it blocks only an explicit "No"), so the AI is told who can get email (email_allowed_pct),
not how many said "Yes" (email_consent_pct).
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import build_system_prompt, build_user_prompt
from app.modules.payflow.services.workflow_suggestion.segmenter import _stats
from tests.test_workflow_suggestion_api import _suggest, _url, fake_llm, portfolio  # noqa: F401 (fixture)

CLIENT = {"name": "Northstar", "client_type": "third_party", "ai_mode": "supervised_ai"}
LIMITS = {"channels": ["Email", "SMS"], "max_messages_per_7_days": None, "min_steps": 3, "max_steps": 15,
          "max_communications": 5}


def accounts(*consents, email="a@b.com"):
    return [
        SimpleNamespace(outstanding_balance=300, days_past_due=10, language="EN", email=email,
                        phone_mobile="+15550001111", email_consent=consent)
        for consent in consents
    ]


def test_unknown_consent_counts_as_allowed_like_the_worker():
    stats = _stats(accounts(None, None, True, False))
    assert stats["email_allowed_pct"] == 75  # unknown, unknown, yes — not the explicit "No"
    assert stats["email_consent_pct"] == 25  # unchanged: recorded "Yes" only


def test_no_email_address_is_never_allowed():
    assert _stats(accounts(None, True, email=None))["email_allowed_pct"] == 0


def test_design_ai_sees_email_allowed_not_recorded_consent():
    segment = {"segment_id": "A", "label": "Active", "stats": _stats(accounts(None, None))}
    prompt = build_user_prompt(CLIENT, "Residential", [segment])
    assert '"email_allowed_pct": 100' in prompt and '"with_email_pct": 100' in prompt
    assert "email_consent_pct" not in prompt
    assert "few accounts that can get email (low email allowed) = favour SMS" in build_system_prompt(LIMITS)


def test_regenerate_stats_drop_the_consent_line_too():
    """Regenerate rebuilds stats from the saved ai_context labels."""
    stats = {"Accounts": "29", "Email consent": "0%", "Email allowed": "100%"}
    prompt = build_user_prompt(CLIENT, "Residential", [{"segment_id": "A", "label": "Active", "stats": stats}])
    assert '"Email allowed": "100%"' in prompt and "Email consent" not in prompt


def test_preview_and_saved_workflow_show_email_allowed(client, admin_token, portfolio):  # noqa: F811
    headers = {"Authorization": f"Bearer {admin_token}"}
    stats = client.get(_url(portfolio["client_id"], portfolio["portfolio_id"]), headers=headers).json()["segments"][0]["stats"]
    assert "email_allowed_pct" in stats and "email_consent_pct" in stats  # additive: old field kept

    call, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        [workflow] = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"]).json()["workflows"]
    labels = [item["label"] for item in workflow["ai_context"]]
    assert labels.index("Email allowed") == labels.index("Email consent") + 1
