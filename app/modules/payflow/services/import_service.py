"""Daily CRM account file ingest — validate, create/refresh, persist history."""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowImportErrorModel,
    PayflowImportRunModel,
    PayflowPortfolioModel,
    UserModel,
)
from app.modules.payflow.crm_catalog import (
    ACCOUNT_CRM_STATUSES,
    ACCOUNT_IMPORT_CURRENCIES,
    ACCOUNT_IMPORT_HEADERS,
    ACCOUNT_IMPORT_LANGUAGES,
    ACCOUNT_IMPORT_REQUIRED,
    AGE_GROUPS,
)
from app.shared.enums import PayflowCollectionStatus, PayflowPortfolioStatus

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BE_ROOT = Path(__file__).resolve().parents[4]
_UPLOADS = _BE_ROOT / "uploads" / "imports"
_SAMPLE_PATH = _BE_ROOT / "docs" / "samples" / "payflow_daily_accounts_sample.xlsx"

_COLLECTION_BY_LOWER = {s.value.lower(): s.value for s in PayflowCollectionStatus}

_COMPARE_FLOATS = (
    "outstanding_balance",
    "fee_amount",
    "last_payment_amount",
    "ptp_amount",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%%d %%b %%Y, %%H:%%M")


def _cell(row: tuple, idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    if i is None or i >= len(row) or row[i] is None:
        return ""
    val = row[i]
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc).isoformat()
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    return str(val).strip()


def _parse_date(raw: str, field: str) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValidationAppError(f"{field} must be a date (YYYY-MM-DD)")


def _parse_datetime(raw: str, field: str) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19] if "T" in text or " " in text else text[:10], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValidationAppError(f"{field} must be a datetime (ISO-8601)")


def _parse_float(raw: str, field: str, *, required: bool) -> float | None:
    if raw == "" or raw is None:
        if required:
            raise ValidationAppError(f"{field} is required")
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise ValidationAppError(f"{field} must be a number") from exc


def _parse_int(raw: str, field: str) -> int | None:
    if not raw:
        return None
    try:
        return int(float(str(raw).replace(",", "")))
    except ValueError as exc:
        raise ValidationAppError(f"{field} must be a whole number") from exc


def _parse_yn(raw: str, field: str = "value") -> bool | None:
    if not raw:
        return None
    v = raw.strip().upper()
    if v in ("Y", "YES", "TRUE", "1"):
        return True
    if v in ("N", "NO", "FALSE", "0"):
        return False
    raise ValidationAppError(f"{field} must be Y or N")


def _round2(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _fmt_dt(value: datetime | None) -> str:
    if not value:
        return "—"
    local = value
    if local.tzinfo is None:
        local = local.replace(tzinfo=timezone.utc)
    return local.strftime("%d %b %Y, %H:%M")


def _fmt_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


SAMPLE_ACCOUNT_ROWS: list[dict[str, Any]] = [
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-10482",
        "customer_first_name": "John",
        "customer_last_name": "Smith",
        "date_of_birth": "1986-03-14",
        "age_group": "35-44",
        "address_line1": "88 Queen St W",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5H 2N2",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 3980,
        "original_balance": 5400,
        "fee_amount": 0,
        "email": "john.smith@example.com",
        "phone_mobile": "4165550101",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 270,
        "last_payment_date": "2026-09-20",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-01",
        "days_past_due": 35,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-11903",
        "customer_first_name": "Amina",
        "customer_last_name": "Hassan",
        "date_of_birth": "1994-11-02",
        "age_group": "25-34",
        "address_line1": "12 King St W",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5H 1A1",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 2350,
        "original_balance": 2350,
        "fee_amount": 25,
        "email": "amina.hassan@example.com",
        "phone_mobile": "6475550199",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-10-12",
        "days_past_due": 0,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-finance",
        "account_id": "PP-10655",
        "customer_first_name": "Marc",
        "customer_last_name": "Tremblay",
        "date_of_birth": "1979-07-22",
        "age_group": "45-54",
        "address_line1": "440 Boul. René-Lévesque",
        "city": "Montreal",
        "province_state": "QC",
        "postal_code": "H2Z 1V7",
        "country_code": "CA",
        "region": "QC",
        "currency_code": "CAD",
        "outstanding_balance": 2870,
        "original_balance": 3120,
        "fee_amount": 0,
        "email": "marc.tremblay@example.com",
        "phone_mobile": "5145550188",
        "phone_work": "5145552200",
        "language": "FR",
        "last_payment_amount": 250,
        "last_payment_date": "2026-09-30",
        "last_payment_is_ptp": "Y",
        "ptp_code": "PTP-OK",
        "ptp_amount": 250,
        "ptp_due_date": "2026-09-30",
        "due_date": "2026-10-15",
        "days_past_due": 0,
        "account_status": "OPEN",
        "collection_status": "Promise to Pay",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "canadian-tire",
        "sub_client_code": "ct-triangle",
        "account_id": "CT-20394",
        "customer_first_name": "Priya",
        "customer_last_name": "Nair",
        "date_of_birth": "1991-01-09",
        "age_group": "35-44",
        "address_line1": "55 Albert St",
        "city": "Ottawa",
        "province_state": "ON",
        "postal_code": "K1P 6A4",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 1180,
        "original_balance": 1275,
        "fee_amount": 0,
        "email": "priya.nair@example.com",
        "phone_mobile": "6135550144",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 95,
        "last_payment_date": "2026-10-01",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-18",
        "days_past_due": 18,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "canadian-tire",
        "sub_client_code": "ct-triangle",
        "account_id": "CT-58841",
        "customer_first_name": "David",
        "customer_last_name": "Chen",
        "date_of_birth": "2000-05-30",
        "age_group": "18-24",
        "address_line1": "900 Burrard St",
        "city": "Vancouver",
        "province_state": "BC",
        "postal_code": "V6Z 2S9",
        "country_code": "CA",
        "region": "BC",
        "currency_code": "CAD",
        "outstanding_balance": 860,
        "original_balance": 860,
        "fee_amount": 15,
        "email": "david.chen@example.com",
        "phone_mobile": "6045550177",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-10-08",
        "days_past_due": 0,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "northstar-utilities",
        "sub_client_code": "ns-residential",
        "account_id": "NS-30112",
        "customer_first_name": "Helen",
        "customer_last_name": "Park",
        "date_of_birth": "1968-12-11",
        "age_group": "55-64",
        "address_line1": "210 7 Ave SW",
        "city": "Calgary",
        "province_state": "AB",
        "postal_code": "T2P 0W6",
        "country_code": "CA",
        "region": "AB",
        "currency_code": "CAD",
        "outstanding_balance": 515,
        "original_balance": 515,
        "fee_amount": 0,
        "email": "helen.park@example.com",
        "phone_mobile": "4035550122",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 80,
        "last_payment_date": "2026-08-15",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-10",
        "days_past_due": 26,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "northstar-utilities",
        "sub_client_code": "ns-residential",
        "account_id": "NS-30244",
        "customer_first_name": "Robert",
        "customer_last_name": "Singh",
        "date_of_birth": "1954-02-03",
        "age_group": "65+",
        "address_line1": "100 King St E",
        "city": "Hamilton",
        "province_state": "ON",
        "postal_code": "L8N 1A4",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 0,
        "original_balance": 788,
        "fee_amount": 0,
        "email": "robert.singh@example.com",
        "phone_mobile": "9055550133",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 788,
        "last_payment_date": "2026-10-02",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-10-02",
        "days_past_due": 0,
        "account_status": "OPEN",
        "collection_status": "Resolved",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-11021",
        "customer_first_name": "Sofia",
        "customer_last_name": "Martinez",
        "date_of_birth": "1998-08-19",
        "age_group": "25-34",
        "address_line1": "1 Adelaide St E",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5C 2V9",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 1620,
        "original_balance": 2100,
        "fee_amount": 0,
        "email": "sofia.martinez@example.com",
        "phone_mobile": "4165550166",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 200,
        "last_payment_date": "2026-09-25",
        "last_payment_is_ptp": "Y",
        "ptp_code": "PTP-PARTIAL",
        "ptp_amount": 200,
        "ptp_due_date": "2026-10-25",
        "due_date": "2026-10-25",
        "days_past_due": 0,
        "account_status": "OPEN",
        "collection_status": "Promise to Pay",
        "negative_balance_reason": "Failed ACH NSF",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "unknown-client",
        "sub_client_code": "sc-x",
        "account_id": "ZX-00419",
        "customer_first_name": "Alex",
        "customer_last_name": "Kim",
        "date_of_birth": "1990-01-01",
        "age_group": "35-44",
        "address_line1": "10 Main St",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5V 1A1",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 410,
        "original_balance": 410,
        "fee_amount": 0,
        "email": "alex.kim@example.com",
        "phone_mobile": "4165550000",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-10-01",
        "days_past_due": 5,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-NOEMAIL",
        "customer_first_name": "Jordan",
        "customer_last_name": "Lee",
        "date_of_birth": "1992-06-06",
        "age_group": "25-34",
        "address_line1": "200 Front St W",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5V 3K2",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 640,
        "original_balance": 640,
        "fee_amount": 0,
        "email": "",
        "phone_mobile": "4165550110",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-20",
        "days_past_due": 16,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-third-party",
        "account_id": "PP-10702",
        "customer_first_name": "Nina",
        "customer_last_name": "Cole",
        "date_of_birth": "1983-09-09",
        "age_group": "35-44",
        "address_line1": "77 Bloor St W",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5S 1M2",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 640,
        "original_balance": 640,
        "fee_amount": 0,
        "email": "nina.cole@example.com",
        "phone_mobile": "4165550211",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-12",
        "days_past_due": 24,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-NEG01",
        "customer_first_name": "Sam",
        "customer_last_name": "Wright",
        "date_of_birth": "1975-04-04",
        "age_group": "45-54",
        "address_line1": "5 York St",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5J 0B1",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": -40,
        "original_balance": 400,
        "fee_amount": 0,
        "email": "sam.wright@example.com",
        "phone_mobile": "4165550440",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": "",
        "last_payment_date": "",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-01",
        "days_past_due": 35,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
    {
        "client_code": "paypal",
        "sub_client_code": "paypal-loans",
        "account_id": "PP-10482",
        "customer_first_name": "John",
        "customer_last_name": "Smith",
        "date_of_birth": "1986-03-14",
        "age_group": "35-44",
        "address_line1": "88 Queen St W",
        "city": "Toronto",
        "province_state": "ON",
        "postal_code": "M5H 2N2",
        "country_code": "CA",
        "region": "ON",
        "currency_code": "CAD",
        "outstanding_balance": 3980,
        "original_balance": 5400,
        "fee_amount": 0,
        "email": "john.smith@example.com",
        "phone_mobile": "4165550101",
        "phone_work": "",
        "language": "EN",
        "last_payment_amount": 270,
        "last_payment_date": "2026-09-20",
        "last_payment_is_ptp": "N",
        "ptp_code": "",
        "ptp_amount": "",
        "ptp_due_date": "",
        "due_date": "2026-09-01",
        "days_past_due": 35,
        "account_status": "OPEN",
        "collection_status": "Active",
        "negative_balance_reason": "",
        "account_category": "PERSONAL",
    },
]


_SEGMENT_CYCLE = [
    ("Employed", "$40K–$75K", "Bachelor", "Digital preferred"),
    ("Self-employed", "$75K–$120K", "College", "High balance"),
    ("Employed", "$120K+", "Graduate", "Early arrears"),
    ("Unemployed", "Under $40K", "High school", "Hardship"),
    ("Retired", "$40K–$75K", "College", "Promise to pay"),
    ("Employed", "$75K–$120K", "Bachelor", "SMS preferred"),
]


def _enrich_sample_row(index: int, row: dict[str, Any]) -> dict[str, Any]:
    employment, income, education, segment = _SEGMENT_CYCLE[index % len(_SEGMENT_CYCLE)]
    enriched = dict(row)
    enriched.setdefault("employment_status", employment)
    enriched.setdefault("income_band", income)
    enriched.setdefault("education_level", education)
    enriched.setdefault("customer_segment", segment)
    account_id = str(enriched.get("account_id") or f"ACC-{index + 1}")
    enriched.setdefault("product_code", "P4")
    enriched.setdefault("crm_case_id", f"CASE-{account_id}")
    enriched.setdefault("debtor_id", f"DBT-{account_id}")
    enriched.setdefault("client_reference_number", f"CRN-{account_id}")
    enriched.setdefault("date_listed", "2026-08-12")
    enriched.setdefault("last_email_sent_date", "2026-09-20")
    enriched.setdefault("last_sms_sent_date", "2026-09-22")
    enriched.setdefault("last_contact_date", "2026-09-22")
    enriched.setdefault("provincial_hold", "N")
    enriched.setdefault("hold_days", 0)
    enriched.setdefault("email_consent", "Y")
    enriched.setdefault("source_updated_at", "2026-10-05T14:30:00Z")
    return enriched


for _i, _row in enumerate(SAMPLE_ACCOUNT_ROWS):
    SAMPLE_ACCOUNT_ROWS[_i] = _enrich_sample_row(_i, _row)


class PayflowImportService:
    def __init__(self, db: Session):
        self.db = db

    def template_bytes(self, *, include_samples: bool = True) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "Accounts"
        ws.append(list(ACCOUNT_IMPORT_HEADERS))
        if include_samples:
            for row in SAMPLE_ACCOUNT_ROWS:
                ws.append([row.get(h, "") for h in ACCOUNT_IMPORT_HEADERS])
        readme = wb.create_sheet("README")
        readme.append(["PayFlow daily CRM account file"])
        readme.append(["Sheet Accounts, header row 1. Clients and sub-clients must already exist."])
        readme.append(["Match key: client_code + account_id. Missing accounts in this file are not deleted."])
        readme.append(["original_balance is applied only when creating an account."])
        readme.append(["account_status CLOSED rows are rejected. Dates YYYY-MM-DD. Y/N flags: last_payment_is_ptp, provincial_hold, email_consent."])
        readme.append(["Optional segmentation: employment_status, income_band, education_level, customer_segment."])
        readme.append(["Also: crm_case_id, debtor_id, client_reference_number, product_code*, date_listed, last_*_sent/contact, hold_days, source_updated_at."])
        readme.append(["Required: " + ", ".join(ACCOUNT_IMPORT_REQUIRED)])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def write_sample_workbook(self) -> Path:
        _SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SAMPLE_PATH.write_bytes(self.template_bytes(include_samples=True))
        return _SAMPLE_PATH

    def validate_account_file(self, file_bytes: bytes, filename: str) -> dict[str, Any]:
        """Dry-run validation — no DB writes."""
        safe_name = Path(filename or "upload.xlsx").name
        try:
            idx, data_rows = self._load_account_sheet(file_bytes)
        except ValidationAppError as exc:
            return self._preview_file_fail(safe_name, self._guess_field(str(exc.message)), str(exc.message))
        except Exception:
            return self._preview_file_fail(
                safe_name,
                "File structure",
                "The file is not a valid Excel workbook.",
            )

        clients = {c.code.lower(): c for c in self.db.query(PayflowClientModel).all()}
        portfolios = self.db.query(PayflowPortfolioModel).all()
        portfolios_by_key = {(p.client_id, p.code.lower()): p for p in portfolios}

        seen_keys: set[tuple[str, str]] = set()
        created = updated = unchanged = failed = 0
        preview: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        preview_limit = 50

        for row in data_rows:
            record_id = _cell(row, idx, "account_id") or "Row"
            client_code = _cell(row, idx, "client_code")
            sub_code = _cell(row, idx, "sub_client_code")
            try:
                parsed = self._parse_row(row, idx)
                key = (parsed["client_code"].lower(), parsed["account_id"].lower())
                if key in seen_keys:
                    raise ValidationAppError("Duplicate record — this account appears twice in the file.")
                seen_keys.add(key)

                client = clients.get(parsed["client_code"].lower())
                if not client:
                    raise ValidationAppError("Unknown Client — this client has not been set up in PayFlow.")
                portfolio = portfolios_by_key.get((client.id, parsed["sub_client_code"].lower()))
                if not portfolio:
                    raise ValidationAppError(
                        "Unknown Sub-Client — this portfolio does not exist for the Client."
                    )

                existing = (
                    self.db.query(PayflowAccountModel)
                    .filter(
                        PayflowAccountModel.client_id == client.id,
                        PayflowAccountModel.account_reference == parsed["account_id"],
                    )
                    .first()
                )
                action_key = self._classify_account_action(existing, parsed, portfolio.id)
                if action_key == "created":
                    created += 1
                    action_label = "Create"
                elif action_key == "updated":
                    updated += 1
                    action_label = "Update"
                else:
                    unchanged += 1
                    action_label = "No Change"

                if len(preview) < preview_limit:
                    preview.append(
                        self._preview_account_row(
                            parsed,
                            client.name,
                            portfolio.name,
                            action_label,
                            existing,
                        )
                    )
            except ValidationAppError as exc:
                failed += 1
                err = {
                    "record_id": record_id or "—",
                    "client": client_code or "—",
                    "sub_client": sub_code or "—",
                    "field": self._guess_field(str(exc.message)),
                    "error": str(exc.message),
                    "status": "Skipped" if "Duplicate" in str(exc.message) else "Rejected",
                }
                errors.append(err)
                if len(preview) < preview_limit:
                    preview.append(
                        {
                            "id": f"err-{record_id}",
                            "record_id": record_id,
                            "client": client_code or "—",
                            "sub_client": sub_code or "—",
                            "client_name": client_code or "—",
                            "sub_client_name": sub_code or "—",
                            "action": "Error",
                            "current_balance": None,
                            "incoming_balance": None,
                            "note": record_id,
                        }
                    )

        return {
            "ok": True,
            "file_name": safe_name,
            "status": "Ready",
            "message": None,
            "summary": {
                "total": len(data_rows),
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "failed": failed,
                "new_clients": 0,
                "existing_clients": 0,
            },
            "preview": preview,
            "errors": errors,
        }

    def process_account_file(self, user: UserModel, file_bytes: bytes, filename: str) -> dict[str, Any]:
        _UPLOADS.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "upload.xlsx").name
        stored = _UPLOADS / f"{int(_now().timestamp())}-{safe_name}"
        stored.write_bytes(file_bytes)

        run = PayflowImportRunModel(
            kind="account",
            file_name=safe_name,
            stored_path=str(stored.relative_to(_BE_ROOT)).replace("\\", "/"),
            uploaded_by_user_id=user.id,
            uploaded_by_name=user.full_name or user.email,
            status="Validating",
        )
        self.db.add(run)
        self.db.flush()

        try:
            idx, data_rows = self._load_account_sheet(file_bytes)
        except ValidationAppError as exc:
            return self._fail_run(run, self._guess_field(str(exc.message)), str(exc.message))
        except Exception as exc:
            return self._fail_run(run, "File structure", "The file is not a valid Excel workbook.", exc)

        run.status = "Processing"
        run.total_count = len(data_rows)
        self.db.flush()

        clients = {c.code.lower(): c for c in self.db.query(PayflowClientModel).all()}
        portfolios = (
            self.db.query(PayflowPortfolioModel).all()
        )
        portfolios_by_key = {(p.client_id, p.code.lower()): p for p in portfolios}

        seen_keys: set[tuple[str, str]] = set()
        created = updated = unchanged = failed = 0

        for row in data_rows:
            record_id = _cell(row, idx, "account_id") or "Row"
            client_code = _cell(row, idx, "client_code")
            sub_code = _cell(row, idx, "sub_client_code")
            try:
                parsed = self._parse_row(row, idx)
                key = (parsed["client_code"].lower(), parsed["account_id"].lower())
                if key in seen_keys:
                    raise ValidationAppError("Duplicate record — this account appears twice in the file.")
                seen_keys.add(key)

                client = clients.get(parsed["client_code"].lower())
                if not client:
                    raise ValidationAppError("Unknown Client — this client has not been set up in PayFlow.")
                portfolio = portfolios_by_key.get((client.id, parsed["sub_client_code"].lower()))
                if not portfolio:
                    raise ValidationAppError(
                        "Unknown Sub-Client — this portfolio does not exist for the Client."
                    )

                existing = (
                    self.db.query(PayflowAccountModel)
                    .filter(
                        PayflowAccountModel.client_id == client.id,
                        PayflowAccountModel.account_reference == parsed["account_id"],
                    )
                    .first()
                )
                action = self._upsert(existing, client, portfolio, parsed)
                if action == "created":
                    created += 1
                elif action == "updated":
                    updated += 1
                else:
                    unchanged += 1
            except ValidationAppError as exc:
                failed += 1
                self.db.add(
                    PayflowImportErrorModel(
                        run_id=run.id,
                        record_id=record_id or "—",
                        client=client_code or "—",
                        sub_client=sub_code or "—",
                        field=self._guess_field(str(exc.message)),
                        message=str(exc.message),
                        status="Skipped" if "Duplicate" in str(exc.message) else "Rejected",
                    )
                )

        if created or updated or unchanged:
            run.status = "Completed with Errors" if failed else "Completed"
        else:
            run.status = "Failed"
        run.created_count = created
        run.updated_count = updated
        run.unchanged_count = unchanged
        run.failed_count = failed
        run.completed_at = _now()
        self.db.commit()
        self.db.refresh(run)
        return self._serialize_run(run)

    def list_imports(self, kind: str | None = None) -> dict[str, Any]:
        q = self.db.query(PayflowImportRunModel).options(joinedload(PayflowImportRunModel.errors))
        if kind:
            q = q.filter(PayflowImportRunModel.kind == kind)
        rows = q.order_by(PayflowImportRunModel.created_at.desc()).all()
        return {"imports": [self._serialize_run(r) for r in rows]}

    def get_import(self, import_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowImportRunModel)
            .options(joinedload(PayflowImportRunModel.errors))
            .filter(PayflowImportRunModel.id == import_id)
            .first()
        )
        if not row:
            raise NotFoundError("Import not found")
        return self._serialize_run(row)

    def latest_account_intake(self) -> dict[str, Any]:
        row = (
            self.db.query(PayflowImportRunModel)
            .filter(
                PayflowImportRunModel.kind == "account",
                PayflowImportRunModel.status.in_(("Completed", "Completed with Errors")),
            )
            .order_by(PayflowImportRunModel.created_at.desc())
            .first()
        )
        if not row:
            return {
                "files": 0,
                "latest_received_at": "—",
                "latest_assigned_at": "—",
                "accounts_in_files": 0,
            }
        return {
            "files": 1,
            "latest_received_at": _fmt_dt(row.created_at),
            "latest_assigned_at": _fmt_dt(row.completed_at or row.created_at),
            "accounts_in_files": row.created_count + row.updated_count + row.unchanged_count,
        }

    def _load_account_sheet(self, file_bytes: bytes) -> tuple[dict[str, int], list[tuple[Any, ...]]]:
        try:
            wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        except Exception as exc:
            raise ValidationAppError("The file is not a valid Excel workbook.") from exc

        ws = wb["Accounts"] if "Accounts" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValidationAppError("The file is empty.")

        headers = [str(h or "").strip().lower() for h in rows[0]]
        missing = [h for h in ACCOUNT_IMPORT_REQUIRED if h not in headers]
        if missing:
            raise ValidationAppError(f"Missing required column(s): {', '.join(missing)}")

        idx = {h: i for i, h in enumerate(headers)}
        data_rows = [
            row
            for row in rows[1:]
            if row and any(c is not None and str(c).strip() != "" for c in row)
        ]
        if not data_rows:
            raise ValidationAppError("No account rows were found.")
        return idx, data_rows

    def _classify_account_action(
        self,
        existing: PayflowAccountModel | None,
        parsed: dict[str, Any],
        portfolio_id: int,
    ) -> str:
        if existing is None:
            return "created"
        if self._is_unchanged(existing, parsed, portfolio_id):
            return "unchanged"
        return "updated"

    def _preview_account_row(
        self,
        parsed: dict[str, Any],
        client_name: str,
        portfolio_name: str,
        action_label: str,
        existing: PayflowAccountModel | None,
    ) -> dict[str, Any]:
        account_id = parsed["account_id"]
        current = float(existing.outstanding_balance) if existing else None
        incoming = float(parsed["outstanding_balance"])
        return {
            "id": account_id,
            "record_id": account_id,
            "client": parsed["client_code"],
            "sub_client": parsed["sub_client_code"],
            "client_name": client_name,
            "sub_client_name": portfolio_name,
            "action": action_label,
            "current_balance": current,
            "incoming_balance": incoming,
            "note": account_id,
        }

    def _preview_file_fail(self, file_name: str, field: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "file_name": file_name,
            "status": "Failed",
            "message": message,
            "summary": {
                "total": 0,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "failed": 0,
                "new_clients": 0,
                "existing_clients": 0,
            },
            "preview": [],
            "errors": [
                {
                    "record_id": "File",
                    "client": "—",
                    "sub_client": "—",
                    "field": field,
                    "error": message,
                    "status": "Rejected",
                }
            ],
        }

    def _fail_run(self, run: PayflowImportRunModel, field: str, message: str, exc: Exception | None = None) -> dict:
        _ = exc
        run.status = "Failed"
        run.completed_at = _now()
        self.db.add(
            PayflowImportErrorModel(
                run_id=run.id,
                record_id="File",
                client="—",
                sub_client="—",
                field=field,
                message=message,
                status="Rejected",
            )
        )
        self.db.commit()
        self.db.refresh(run)
        return self._serialize_run(run)

    def _parse_row(self, row: tuple, idx: dict[str, int]) -> dict[str, Any]:
        def c(key: str) -> str:
            return _cell(row, idx, key)

        for key in ACCOUNT_IMPORT_REQUIRED:
            if not c(key):
                raise ValidationAppError(f"{key} is required")

        email = c("email")
        if not _EMAIL_RE.match(email):
            raise ValidationAppError("email is not a valid address")

        outstanding = _parse_float(c("outstanding_balance"), "outstanding_balance", required=True)
        if outstanding is None or outstanding < 0:
            raise ValidationAppError("outstanding_balance cannot be negative")

        country = c("country_code").upper()
        if len(country) != 2:
            raise ValidationAppError("country_code must be ISO-2 (e.g. CA)")

        currency = c("currency_code").upper()
        if currency not in ACCOUNT_IMPORT_CURRENCIES:
            raise ValidationAppError("currency_code must be CAD or USD")

        language = (c("language") or "EN").upper()
        if language not in ACCOUNT_IMPORT_LANGUAGES:
            raise ValidationAppError("language must be EN or FR")

        age_group = c("age_group")
        if age_group and age_group not in AGE_GROUPS:
            raise ValidationAppError("age_group is not a recognised band")

        account_status = (c("account_status") or "OPEN").upper()
        if account_status not in ACCOUNT_CRM_STATUSES:
            raise ValidationAppError("account_status must be OPEN or CLOSED")
        if account_status == "CLOSED":
            raise ValidationAppError("CLOSED accounts are excluded at intake")

        collection_raw = c("collection_status")
        if collection_raw:
            collection_status = _COLLECTION_BY_LOWER.get(collection_raw.lower())
            if not collection_status:
                raise ValidationAppError("collection_status is not a recognised PayFlow status")
        else:
            collection_status = PayflowCollectionStatus.ACTIVE.value

        province = c("province_state")
        region = c("region") or province
        first = c("customer_first_name")
        last = c("customer_last_name")
        return {
            "client_code": c("client_code"),
            "sub_client_code": c("sub_client_code"),
            "account_id": c("account_id"),
            "crm_case_id": c("crm_case_id") or c("case_id") or None,
            "debtor_id": c("debtor_id") or c("customer_debtor_id") or None,
            "client_reference_number": c("client_reference_number") or None,
            "product_code": c("product_code"),
            "customer_first_name": first,
            "customer_last_name": last,
            "customer_name": f"{first} {last}".strip(),
            "date_of_birth": _parse_date(c("date_of_birth"), "date_of_birth"),
            "age_group": age_group or None,
            "employment_status": c("employment_status") or c("employment_type") or None,
            "income_band": c("income_band") or None,
            "education_level": c("education_level") or None,
            "customer_segment": c("customer_segment") or c("persona") or None,
            "address_line1": c("address_line1") or None,
            "city": c("city") or None,
            "province_state": province or None,
            "postal_code": c("postal_code") or None,
            "country_code": country,
            "region": region or None,
            "currency_code": currency,
            "outstanding_balance": outstanding,
            "original_balance": _parse_float(c("original_balance"), "original_balance", required=False),
            "fee_amount": _parse_float(c("fee_amount"), "fee_amount", required=False),
            "email": email,
            "phone_mobile": c("phone_mobile") or None,
            "phone_work": c("phone_work") or None,
            "language": language,
            "date_listed": _parse_date(c("date_listed") or c("placement_date"), "date_listed"),
            "last_email_sent_date": _parse_date(c("last_email_sent_date"), "last_email_sent_date"),
            "last_sms_sent_date": _parse_date(c("last_sms_sent_date"), "last_sms_sent_date"),
            "last_contact_date": _parse_date(c("last_contact_date"), "last_contact_date"),
            "provincial_hold": _parse_yn(
                c("provincial_hold") or c("communication_hold"), "provincial_hold"
            ),
            "hold_days": _parse_int(c("hold_days"), "hold_days"),
            "email_consent": _parse_yn(
                c("email_consent") or c("contact_permission"), "email_consent"
            ),
            "source_updated_at": _parse_datetime(c("source_updated_at"), "source_updated_at"),
            "last_payment_amount": _parse_float(c("last_payment_amount"), "last_payment_amount", required=False),
            "last_payment_date": _parse_date(c("last_payment_date"), "last_payment_date"),
            "last_payment_is_ptp": _parse_yn(c("last_payment_is_ptp"), "last_payment_is_ptp"),
            "ptp_code": c("ptp_code") or None,
            "ptp_amount": _parse_float(c("ptp_amount"), "ptp_amount", required=False),
            "ptp_due_date": _parse_date(c("ptp_due_date"), "ptp_due_date"),
            "due_date": _parse_date(c("due_date"), "due_date"),
            "days_past_due": _parse_int(c("days_past_due"), "days_past_due"),
            "account_status": account_status,
            "collection_status": collection_status,
            "negative_balance_reason": c("negative_balance_reason") or None,
            "account_category": c("account_category") or None,
        }

    def _upsert(
        self,
        existing: PayflowAccountModel | None,
        client: PayflowClientModel,
        portfolio: PayflowPortfolioModel,
        parsed: dict[str, Any],
    ) -> str:
        now = _now()
        stamp = now.strftime("%d %b %Y, %H:%M")
        incoming_original = parsed["original_balance"]
        if existing is None:
            original = incoming_original if incoming_original is not None else parsed["outstanding_balance"]
            recovered = max(0.0, float(original) - float(parsed["outstanding_balance"]))
            row = PayflowAccountModel(
                client_id=client.id,
                portfolio_id=portfolio.id,
                customer_name=parsed["customer_name"],
                account_reference=parsed["account_id"],
                case_reference=f"CASE-{parsed['account_id']}-01",
                original_balance=float(original),
                outstanding_balance=float(parsed["outstanding_balance"]),
                recovered_balance=recovered,
                collection_status=parsed["collection_status"],
                last_action="Created from CRM daily file",
                next_action="Assign workflow",
                human_review=parsed["collection_status"] == PayflowCollectionStatus.HUMAN_REVIEW.value,
                timeline=[
                    {
                        "label": "CRM import",
                        "detail": f"Account created from {parsed['account_id']}",
                        "at": stamp,
                    }
                ],
            )
            self._apply_crm_fields(row, parsed, original_locked=False)
            row.last_crm_refresh_at = now
            self.db.add(row)
            self.db.flush()
            return "created"

        if self._is_unchanged(existing, parsed, portfolio.id):
            existing.last_crm_refresh_at = now
            return "unchanged"

        original = float(existing.original_balance or 0)
        existing.portfolio_id = portfolio.id
        existing.customer_name = parsed["customer_name"]
        existing.outstanding_balance = float(parsed["outstanding_balance"])
        existing.recovered_balance = max(0.0, original - existing.outstanding_balance)
        existing.collection_status = parsed["collection_status"]
        existing.last_action = "Refreshed from CRM daily file"
        existing.human_review = parsed["collection_status"] == PayflowCollectionStatus.HUMAN_REVIEW.value
        events = list(existing.timeline or [])
        events.append(
            {
                "label": "CRM refresh",
                "detail": f"Outstanding {parsed['outstanding_balance']}",
                "at": stamp,
            }
        )
        existing.timeline = events[-40:]
        self._apply_crm_fields(existing, parsed, original_locked=True)
        existing.last_crm_refresh_at = now
        return "updated"

    def _apply_crm_fields(self, row: PayflowAccountModel, parsed: dict[str, Any], *, original_locked: bool) -> None:
        row.customer_first_name = parsed["customer_first_name"]
        row.customer_last_name = parsed["customer_last_name"]
        row.date_of_birth = parsed["date_of_birth"]
        row.age_group = parsed["age_group"]
        row.employment_status = parsed["employment_status"]
        row.income_band = parsed["income_band"]
        row.education_level = parsed["education_level"]
        row.customer_segment = parsed["customer_segment"]
        row.address_line1 = parsed["address_line1"]
        row.city = parsed["city"]
        row.province_state = parsed["province_state"]
        row.postal_code = parsed["postal_code"]
        row.country_code = parsed["country_code"]
        row.region = parsed["region"]
        row.email = parsed["email"]
        row.phone_mobile = parsed["phone_mobile"]
        row.phone_work = parsed["phone_work"]
        row.language = parsed["language"]
        row.currency_code = parsed["currency_code"]
        if not original_locked and parsed["original_balance"] is not None:
            row.original_balance = float(parsed["original_balance"])
        row.fee_amount = parsed["fee_amount"]
        row.due_date = parsed["due_date"]
        row.days_past_due = parsed["days_past_due"]
        row.last_payment_amount = parsed["last_payment_amount"]
        row.last_payment_date = parsed["last_payment_date"]
        row.last_payment_is_ptp = parsed["last_payment_is_ptp"]
        row.ptp_code = parsed["ptp_code"]
        row.ptp_amount = parsed["ptp_amount"]
        row.ptp_due_date = parsed["ptp_due_date"]
        row.account_status = parsed["account_status"]
        row.account_category = parsed["account_category"]
        row.negative_balance_reason = parsed["negative_balance_reason"]
        row.crm_case_id = parsed["crm_case_id"]
        row.debtor_id = parsed["debtor_id"]
        row.client_reference_number = parsed["client_reference_number"]
        row.product_code = parsed["product_code"]
        row.date_listed = parsed["date_listed"]
        row.last_email_sent_date = parsed["last_email_sent_date"]
        row.last_sms_sent_date = parsed["last_sms_sent_date"]
        row.last_contact_date = parsed["last_contact_date"]
        row.provincial_hold = parsed["provincial_hold"]
        row.hold_days = parsed["hold_days"]
        row.email_consent = parsed["email_consent"]
        row.source_updated_at = parsed["source_updated_at"]

    def _is_unchanged(self, row: PayflowAccountModel, parsed: dict[str, Any], portfolio_id: int) -> bool:
        checks: list[tuple[Any, Any]] = [
            (row.portfolio_id, portfolio_id),
            (row.customer_name, parsed["customer_name"]),
            (_round2(row.outstanding_balance), _round2(parsed["outstanding_balance"])),
            (row.collection_status, parsed["collection_status"]),
            (row.email, parsed["email"]),
            (row.phone_mobile, parsed["phone_mobile"]),
            (row.language, parsed["language"]),
            (row.age_group, parsed["age_group"]),
            (row.employment_status, parsed["employment_status"]),
            (row.income_band, parsed["income_band"]),
            (row.education_level, parsed["education_level"]),
            (row.customer_segment, parsed["customer_segment"]),
            (row.region, parsed["region"]),
            (row.address_line1, parsed["address_line1"]),
            (row.city, parsed["city"]),
            (row.province_state, parsed["province_state"]),
            (row.postal_code, parsed["postal_code"]),
            (row.country_code, parsed["country_code"]),
            (row.currency_code, parsed["currency_code"]),
            (_round2(row.fee_amount), _round2(parsed["fee_amount"])),
            (row.due_date, parsed["due_date"]),
            (row.days_past_due, parsed["days_past_due"]),
            (_round2(row.last_payment_amount), _round2(parsed["last_payment_amount"])),
            (row.last_payment_date, parsed["last_payment_date"]),
            (row.last_payment_is_ptp, parsed["last_payment_is_ptp"]),
            (row.ptp_code, parsed["ptp_code"]),
            (_round2(row.ptp_amount), _round2(parsed["ptp_amount"])),
            (row.ptp_due_date, parsed["ptp_due_date"]),
            (row.account_status, parsed["account_status"]),
            (row.account_category, parsed["account_category"]),
            (row.date_of_birth, parsed["date_of_birth"]),
            (row.crm_case_id, parsed["crm_case_id"]),
            (row.debtor_id, parsed["debtor_id"]),
            (row.client_reference_number, parsed["client_reference_number"]),
            (row.product_code, parsed["product_code"]),
            (row.date_listed, parsed["date_listed"]),
            (row.last_email_sent_date, parsed["last_email_sent_date"]),
            (row.last_sms_sent_date, parsed["last_sms_sent_date"]),
            (row.last_contact_date, parsed["last_contact_date"]),
            (row.provincial_hold, parsed["provincial_hold"]),
            (row.hold_days, parsed["hold_days"]),
            (row.email_consent, parsed["email_consent"]),
            (row.source_updated_at, parsed["source_updated_at"]),
        ]
        return all(a == b for a, b in checks)

    def _guess_field(self, message: str) -> str:
        for h in ACCOUNT_IMPORT_HEADERS:
            if message.lower().startswith(h) or h in message:
                return h
        if "Unknown Client" in message:
            return "client_code"
        if "Unknown Sub-Client" in message or "portfolio" in message.lower():
            return "sub_client_code"
        if "Duplicate" in message:
            return "account_id"
        return "record"

    def _serialize_run(self, run: PayflowImportRunModel) -> dict[str, Any]:
        return {
            "id": run.id,
            "kind": run.kind,
            "file_name": run.file_name,
            "date_time": _fmt_dt(run.created_at),
            "uploaded_by": run.uploaded_by_name,
            "status": run.status,
            "counts": {
                "total": run.total_count,
                "created": run.created_count,
                "updated": run.updated_count,
                "unchanged": run.unchanged_count,
                "failed": run.failed_count,
            },
            "errors": [
                {
                    "record_id": e.record_id,
                    "client": e.client,
                    "sub_client": e.sub_client,
                    "field": e.field,
                    "error": e.message,
                    "status": e.status,
                }
                for e in (run.errors or [])
            ],
        }


def seed_demo_portfolios(db: Session) -> None:
    specs = [
        ("paypal", "paypal-loans", "PayPal Loans"),
        ("paypal", "paypal-finance", "PayPal Finance"),
        ("canadian-tire", "ct-triangle", "Canadian Tire Triangle Cards"),
        ("northstar-utilities", "ns-residential", "Northstar Residential"),
    ]
    clients = {c.code: c for c in db.query(PayflowClientModel).all()}
    portfolios = db.query(PayflowPortfolioModel).all()
    by_key = {(p.client_id, p.code.lower()): p for p in portfolios}
    for client_code, code, name in specs:
        client = clients.get(client_code)
        if not client:
            continue
        if (client.id, code.lower()) in by_key:
            continue
        db.add(
            PayflowPortfolioModel(
                client_id=client.id,
                code=code,
                name=name,
                status=PayflowPortfolioStatus.ACTIVE.value,
                description="Seeded for daily CRM account import.",
            )
        )
    db.flush()
    first_by_client: dict[int, PayflowPortfolioModel] = {}
    for p in db.query(PayflowPortfolioModel).order_by(PayflowPortfolioModel.id).all():
        first_by_client.setdefault(p.client_id, p)
    for account in db.query(PayflowAccountModel).filter(PayflowAccountModel.portfolio_id.is_(None)).all():
        first = first_by_client.get(account.client_id)
        if first:
            account.portfolio_id = first.id
