"""Sample data for the Email Subject Performance feature (development / demo only).

Tracking only starts when the feature is deployed, so a fresh database shows an empty section.
This script adds ONE clearly named sample client with a workflow, 30 days of tracked emails and
their credited payments, so the Dashboard section, the subject popup, the AI insights, the
workflow-editor suggestions and the AI proposals can all be tried.

    venv/Scripts/python.exe scripts/seed_email_subject_samples.py            # add (or refresh)
    venv/Scripts/python.exe scripts/seed_email_subject_samples.py --remove   # take it all out

It is never run by the application. Everything it creates belongs to the sample client
(code "sample-email-performance"), and --remove deletes exactly that. No real customer data:
names and addresses are placeholders. It refuses to run when APP_ENV is production.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.infrastructure.database.models import (  # noqa: E402
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationEventModel,
    PayflowCommunicationModel,
    PayflowMessageVersionModel,
    PayflowPaymentTransactionModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
)
from app.infrastructure.database.schema_sync import sync_schema  # noqa: E402
from app.infrastructure.database.session import Base, SessionLocal, engine  # noqa: E402
from app.modules.payflow.services.email_tracking import content_hash, subject_key  # noqa: E402

CLIENT_CODE = "sample-email-performance"
CLIENT_NAME = "Sample Client (Email Performance)"
DAYS = 30
ACCOUNTS = 80

FOOTER = "\n\nIf you have already paid, thank you.\n\n{{brand_name}}"
STANDARD = (
    "Hi {{first_name}},\n\nYour account {{account_reference}} has an outstanding balance of "
    "{{outstanding_balance}}. You can review your balance and the available payment options securely "
    "here: {{payment_link}}" + FOOTER
)
SUPPORT = (
    "Hi {{first_name}},\n\nYour account {{account_reference}} has an outstanding balance of "
    "{{outstanding_balance}}. If you need more time, we can help you spread it with a payment plan: "
    "{{payment_link}}" + FOOTER
)

# subject, step id, reminder stage, purpose, emails per day, % opened, % clicked, % initiated, % paid
SUBJECTS = [
    ("A friendly reminder about your balance", "s1", "First reminder", "Payment Reminder", 9, 62, 24, 17, 11),
    ("A friendly reminder about your outstanding balance", "s2", "Second reminder", "Payment Reminder", 8, 85, 9, 5, 2),
    ("Your payment options are ready when you are", "s2", "Second reminder", "Payment Reminder", 8, 68, 40, 30, 18),
    ("A reminder: your account {{account_reference}} still needs attention", "s3", "Final reminder", "Firm Reminder", 6, 55, 28, 18, 9),
    ("Spread your balance with a payment plan", None, "Payment plan", "Payment Plan Reminder", 5, 72, 34, 24, 12),
    ("Your promised payment is coming up", None, "Promise to pay", "Promise-to-Pay Reminder", 5, 75, 38, 27, 15),
]
# A promising subject with too few emails to rank (shows "Low sample").
THIN = ("Let's find a payment option that works for you", "s2", "Second reminder", "Payment Reminder")


def _step(step_id: str, title: str, purpose: str, subject: str, body: str, nxt: str | None, origin: str) -> dict:
    return {
        "id": step_id,
        "kind": "Communication",
        "title": title,
        "origin": "Human Created",
        "disabled": False,
        "config": {
            "channel": "Email",
            "purpose": purpose,
            "subject": subject,
            "body": body,
            "content_origin": origin,
            "reference_event": "Previous Action",
            "amount": 3,
            "unit": "Days",
            "direction": "After",
        },
        "next": nxt,
        "yes": None,
        "no": None,
        "channel": "Email",
        "purpose": purpose,
    }


def remove(db) -> bool:
    client = db.query(PayflowClientModel).filter(PayflowClientModel.code == CLIENT_CODE).first()
    if client is None:
        return False
    # Using the sample in the app adds rows of its own (notifications, workflow versions, reviews,
    # payment links, ...). So every table is cleaned, children first: rows of this client, and rows
    # that point to a row of this client (e.g. a communication's events).
    client_table = PayflowClientModel.__table__
    owned = {t.name for t in Base.metadata.sorted_tables if "client_id" in t.c and t is not client_table}
    for table in reversed(Base.metadata.sorted_tables):
        if table is client_table:
            continue
        if table.name in owned:
            db.execute(table.delete().where(table.c.client_id == client.id))
            continue
        for column in table.c:
            for key in column.foreign_keys:
                parent = key.column.table
                if parent.name in owned:
                    parent_ids = select(key.column).where(parent.c.client_id == client.id)
                    db.execute(table.delete().where(column.in_(parent_ids)))
    db.execute(client_table.delete().where(client_table.c.id == client.id))
    db.commit()
    return True


def seed(db) -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    client = PayflowClientModel(code=CLIENT_CODE, name=CLIENT_NAME, status="active")
    db.add(client)
    db.flush()
    portfolio = PayflowPortfolioModel(client_id=client.id, code="sample-portfolio", name="Sample Portfolio")
    db.add(portfolio)
    db.flush()
    accounts = [
        PayflowAccountModel(
            client_id=client.id,
            portfolio_id=portfolio.id,
            customer_name=f"Sample Customer {n:02d}",
            customer_first_name="Sample",
            account_reference=f"SAMPLE-{n:04d}",
            case_reference=f"CASE-SAMPLE-{n:04d}",
            email=f"sample{n:02d}@example.com",
            email_consent=True,
            original_balance=500.0,
            outstanding_balance=425.0,
            recovered_balance=75.0,
            collection_status="Active",
            currency_code="CAD",
        )
        for n in range(1, ACCOUNTS + 1)
    ]
    db.add_all(accounts)

    steps = [
        _step("s1", "First reminder email", "Payment Reminder", SUBJECTS[0][0], STANDARD, "s2", "Human Created"),
        # The weak one: the AI check proposes to replace it.
        _step("s2", "Second reminder email", "Payment Reminder", SUBJECTS[1][0], STANDARD, "s3", "Human Created"),
        _step("s3", "Final reminder email", "Firm Reminder", SUBJECTS[3][0], STANDARD, None, "AI Generated"),
    ]
    strategy = PayflowStrategyModel(
        code="sample-email-performance-wf",
        name="Sample Reminder Workflow",
        client_id=client.id,
        portfolio_id=portfolio.id,
        status="Active",
        origin="Human Created",
        source="Human Created",
        version=2,
        summary="Sample workflow for the Email Subject Performance feature.",
        segment={},
        steps=steps,
        entry_node_id="s1",
        ai_context=[],
        created_by="Sample data script",
    )
    db.add(strategy)
    db.flush()

    def version(subject: str, body: str, stage: str, purpose: str, step_id: str | None, origin: str, days_ago: int):
        row = PayflowMessageVersionModel(
            client_id=client.id,
            content_hash=content_hash("Email", subject, body),
            channel="Email",
            language="EN",
            subject_template=subject,
            subject_key=subject_key(subject),
            body_template=body,
            purpose=purpose,
            reminder_stage=stage,
            origin=origin,
            strategy_id=strategy.id,
            step_id=step_id,
            strategy_version=1 if body == SUPPORT else 2,
            first_used_at=now - timedelta(days=days_ago),
            last_used_at=now,
        )
        db.add(row)
        db.flush()
        return row

    counts = {"emails": 0, "payments": 0}
    serial = 0

    def email(spec, account, sent_at, score, body_version, *, paid_twice=False):
        nonlocal serial
        subject, step_id, stage, purpose = spec[:4]
        opened_pct, clicked_pct, initiated_pct, paid_pct = spec[5:9] if len(spec) > 4 else (95, 85, 78, 70)
        serial += 1
        delivered = score < 96
        opened = delivered and score < opened_pct
        clicked = opened and score < clicked_pct
        initiated = clicked and score < initiated_pct
        paid = initiated and score < paid_pct
        comm = PayflowCommunicationModel(
            code=f"COM-SMP{serial:07d}",
            client_id=client.id,
            account_id=account.id,
            channel="Email",
            purpose=purpose,
            status="Payment Link Clicked" if clicked else "Opened / Read" if opened else "Delivered" if delivered else "Sent",
            workflow_name=strategy.name,
            strategy_id=strategy.id,
            subject=subject.replace("{{account_reference}}", account.account_reference),
            body_lines=["Sample email (see the stored text version)."],
            payment_link=True,
            why_message="Sample data",
            events=[],
            balance=425.0,
            provider="sendgrid",
            recipient=account.email,
            sent_at=sent_at,
            delivered_at=sent_at + timedelta(minutes=1) if delivered else None,
            opens_count=2 if opened else 0,
            first_opened_at=sent_at + timedelta(hours=1) if opened else None,
            last_opened_at=sent_at + timedelta(hours=2) if opened else None,
            clicks_count=2 if clicked else 0,
            first_clicked_at=sent_at + timedelta(hours=2) if clicked else None,
            last_clicked_at=sent_at + timedelta(hours=3) if clicked else None,
            engagement_tracking="available",
            payment_initiated=initiated,
            payment_completed=paid,
            eligibility_status="Eligible",
            subject_template=subject,
            subject_key=subject_key(subject),
            message_version_id=body_version.id,
            step_id=step_id,
            strategy_version=body_version.strategy_version,
            portfolio_id=portfolio.id,
            reminder_stage=stage,
            language="EN",
            is_manual=False,
            date_bucket="Last 30 Days",
            date_label=sent_at.strftime("%d %b %Y"),
            time_label=sent_at.strftime("%H:%M"),
        )
        db.add(comm)
        db.flush()
        counts["emails"] += 1
        if clicked:
            db.add(
                PayflowCommunicationEventModel(
                    communication_id=comm.id, event_type="click", occurred_at=comm.last_clicked_at, detail="Sample"
                )
            )
        if paid:
            for index in range(2 if paid_twice else 1):  # a plan: two instalments from one email
                counts["payments"] += 1
                db.add(
                    PayflowPaymentTransactionModel(
                        public_id=f"txn_sample_{comm.id}_{index}",
                        client_id=client.id,
                        account_id=account.id,
                        direction="payment",
                        status="Succeeded",
                        option_type="partial",
                        amount=75.0,
                        currency_code="CAD",
                        provider="mock",
                        provider_ref=f"sample_{comm.id}_{index}",
                        method_label="Sample data",
                        notes="Sample data (Email Subject Performance)",
                        attributed_communication_id=comm.id,
                        attribution_method="plan" if index else "last_click",
                        attributed_at=sent_at + timedelta(hours=4),
                        completed_at=min(now, sent_at + timedelta(hours=4 + index * 24)),
                    )
                )

    for subject_index, spec in enumerate(SUBJECTS):
        subject, step_id, stage, purpose, per_day = spec[:5]
        origin = "AI Generated" if subject_index in (2, 3) else "Human Created"
        bodies = [
            version(subject, STANDARD, stage, purpose, step_id, origin, DAYS),
            version(subject, SUPPORT, stage, purpose, step_id, "Human Created", DAYS),
        ]
        for day in range(DAYS):
            volume = per_day + ((day + subject_index * 3) % 4)
            for n in range(volume):
                score = (n * 37 + day * 11 + subject_index * 7) % 100
                account = accounts[(serial + n) % ACCOUNTS]
                sent_at = now - timedelta(days=day, hours=(n * 3) % 10)
                # The support-led body does a little better: comparable variations in the popup.
                body = bodies[1] if n % 2 else bodies[0]
                email(spec, account, sent_at, max(0, score - (4 if n % 2 else 0)), body,
                      paid_twice=purpose == "Payment Plan Reminder" and n % 3 == 0)

    thin_body = version(THIN[0], SUPPORT, THIN[2], THIN[3], THIN[1], "AI Generated", 1)
    for n in range(8):
        email(THIN, accounts[n], now - timedelta(hours=n + 1), (n * 13) % 100, thin_body)

    # Payments that no message can be credited with ("not attributed" in the measurement notes).
    for n in range(3):
        counts["payments"] += 1
        db.add(
            PayflowPaymentTransactionModel(
                public_id=f"txn_sample_unattributed_{n}",
                client_id=client.id,
                account_id=accounts[n].id,
                direction="payment",
                status="Succeeded",
                option_type="partial",
                amount=75.0,
                currency_code="CAD",
                provider="mock",
                provider_ref=f"sample_unattributed_{n}",
                method_label="Sample data",
                notes="Sample data (Email Subject Performance)",
                attribution_method="none",
                attributed_at=now,
                completed_at=now - timedelta(days=n + 1),
            )
        )
    db.commit()
    return counts


def main() -> int:
    if (settings.APP_ENV or "").strip().lower() in {"production", "prod"}:
        print("Refusing to add sample data: APP_ENV is production.")
        return 1
    sync_schema(engine)  # the new columns / tables, when the API has not been started since the update
    db = SessionLocal()
    try:
        removed = remove(db)
        if "--remove" in sys.argv[1:]:
            print("Sample client removed." if removed else "There was no sample client.")
            return 0
        counts = seed(db)
        print(
            f'Sample client "{CLIENT_NAME}" {"refreshed" if removed else "added"}: '
            f'{counts["emails"]} emails, {counts["payments"]} payments, 1 workflow.'
        )
        print("Open the Dashboard > Email Subject Performance and filter by this client.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
