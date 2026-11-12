"""Limited ops repair for Email Subject Performance tracking (Epic 3).

Fills missing ``subject_key`` / ``subject_template`` on email communications from the
stored ``subject``, and attributes payment transactions that still have no
``attribution_method``. Does **not** invent SendGrid opens/clicks or replay history
from before the feature was deployed.

    venv/Scripts/python.exe scripts/backfill_email_subject_tracking.py
    venv/Scripts/python.exe scripts/backfill_email_subject_tracking.py --dry-run

Refuses to run when APP_ENV is production unless ``--force-production`` is passed.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.infrastructure.database.models import (  # noqa: E402
    PayflowCommunicationModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentTransactionModel,
)
from app.infrastructure.database.schema_sync import sync_schema  # noqa: E402
from app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from app.modules.payflow.services.email_tracking import subject_key  # noqa: E402
from app.modules.payflow.services.payment_attribution import attribute_payment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report counts only; do not commit")
    parser.add_argument(
        "--force-production",
        action="store_true",
        help="Allow running when APP_ENV is production",
    )
    args = parser.parse_args()

    env = (settings.APP_ENV or "").strip().lower()
    if env == "production" and not args.force_production:
        print("Refusing to run in production without --force-production")
        return 2

    sync_schema(engine)
    db = SessionLocal()
    keys_fixed = 0
    attributed = 0
    try:
        emails = (
            db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.channel == "Email",
                or_(
                    PayflowCommunicationModel.subject_key.is_(None),
                    PayflowCommunicationModel.subject_key == "",
                ),
                PayflowCommunicationModel.subject.isnot(None),
                PayflowCommunicationModel.subject != "",
            )
            .all()
        )
        for row in emails:
            raw = (row.subject or "").strip()
            if not raw:
                continue
            template = row.subject_template or raw
            key = subject_key(template) or subject_key(raw)
            if not key:
                continue
            row.subject_template = template[:512]
            row.subject_key = key[:512]
            keys_fixed += 1

        txns = (
            db.query(PayflowPaymentTransactionModel)
            .filter(
                or_(
                    PayflowPaymentTransactionModel.attribution_method.is_(None),
                    PayflowPaymentTransactionModel.attribution_method == "",
                )
            )
            .all()
        )
        for txn in txns:
            attempt = None
            if txn.payment_attempt_id:
                attempt = db.get(PayflowPaymentAttemptModel, txn.payment_attempt_id)
            if attempt is None:
                continue
            attribute_payment(db, txn, attempt)
            if txn.attribution_method:
                attributed += 1

        print(f"subject_key filled: {keys_fixed}")
        print(f"transactions attributed: {attributed}")
        if args.dry_run:
            db.rollback()
            print("dry-run: rolled back")
        else:
            db.commit()
            print("committed")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
