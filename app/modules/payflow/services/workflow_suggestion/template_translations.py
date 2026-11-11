"""Translate the message templates into the other account languages — once, with the AI translator and checks.

Templates are sent when a message step has no text of its own. Run this when the backend allows a new
account language (crm_catalog.ACCOUNT_IMPORT_LANGUAGES), then have a speaker read the result:

    python -m app.modules.payflow.services.workflow_suggestion.template_translations         # every allowed language
    python -m app.modules.payflow.services.workflow_suggestion.template_translations FR ES   # only these

It updates workflow_contact_runtime.TEMPLATE_TRANSLATIONS_PATH; languages already there and not asked for are kept.
"""

from __future__ import annotations

import json
import sys

from app.modules.payflow.crm_catalog import ACCOUNT_IMPORT_LANGUAGES
from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
from app.modules.payflow.services.workflow_contact_runtime import TEMPLATE_TRANSLATIONS_PATH
from app.modules.payflow.services.workflow_suggestion.languages import extra_languages
from app.modules.payflow.services.workflow_suggestion.message_writer import translate_messages

SOURCE_LANGUAGE = "English"  # the templates are written in English


def translate_templates(codes: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    """{code: {template_id: {"subject", "body"}}} — one AI call per language, every template checked."""
    workflow = {
        "steps": [
            {"id": t["id"], "kind": "Communication", "config": {"channel": t["channel"], "purpose": t["purpose"]}}
            for t in MESSAGE_TEMPLATES
        ]
    }
    written = {t["id"]: {key: t[key] for key in ("subject", "body") if t.get(key)} for t in MESSAGE_TEMPLATES}
    client = {"name": "PayFlow", "language": SOURCE_LANGUAGE}
    return {code: translate_messages(client, workflow, written, code) for code in extra_languages(SOURCE_LANGUAGE, codes)}


def main(codes: list[str]) -> None:
    translations = translate_templates(codes or list(ACCOUNT_IMPORT_LANGUAGES))
    try:
        stored = json.loads(TEMPLATE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    stored.update(translations)
    TEMPLATE_TRANSLATIONS_PATH.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {', '.join(translations) or 'nothing'} to {TEMPLATE_TRANSLATIONS_PATH}")


if __name__ == "__main__":
    main(sys.argv[1:])
