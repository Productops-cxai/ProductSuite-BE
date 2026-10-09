"""CRM field catalog — PayFlow_CRM_Integration_Fields_v0.3 (30 Sep 2026).

Inbound mapped to real placement files:
  PLACE_NCR_CA_V2 (third party) / PLACE_FP_NCR_CA_V2 (first party).
"""

from __future__ import annotations

from typing import TypedDict


class CrmFieldDef(TypedDict):
    source_field: str  # CRM placement column / derivation (UI left column)
    payflow_field: str  # Canonical PayFlow field name (UI mapping target)
    meaning: str
    required: bool
    sample_value: str
    group: str
    available: str  # Yes | Derived | Optional | PayFlow-built (Partial | Ambiguous | No = open)


# Phase-1 INBOUND — CRM → PayFlow. required=True blocks activation if unmapped
# (contact Y*: email required; phone Ambiguous / optional for mapping).
CRM_INBOUND_FIELDS: list[CrmFieldDef] = [
    # ----- Client and Portfolio -----
    {
        "source_field": "AGENCY_NAME + filename",
        "payflow_field": "client_code",
        "meaning": "Two streams: NCR third party, NCR-FP first party. Confirm P4 code mapping.",
        "required": True,
        "sample_value": "NCR / NCR-FP",
        "group": "Client and Portfolio",
        "available": "Derived",
    },
    {
        "source_field": "filename (FP or not)",
        "payflow_field": "portfolio",
        "meaning": "Separate files per stream (First party / Third party).",
        "required": True,
        "sample_value": "First party / Third party",
        "group": "Client and Portfolio",
        "available": "Derived",
    },
    {
        "source_field": "filename",
        "payflow_field": "product_code",
        "meaning": "Confirm P4 mapping with the product team.",
        "required": True,
        "sample_value": "P4",
        "group": "Client and Portfolio",
        "available": "Partial",
    },
    {
        "source_field": "ADDRESS_COUNTRY",
        "payflow_field": "country_code",
        "meaning": "ISO 3166 alpha-2. Placement feed is 100% CA.",
        "required": True,
        "sample_value": "CA",
        "group": "Client and Portfolio",
        "available": "Yes",
    },
    # ----- Account and Loan -----
    {
        "source_field": "CUSTOMER_ACCOUNT_ID",
        "payflow_field": "tenant_account_id",
        "meaning": "19-digit numeric. Primary account anchor; 100% populated.",
        "required": True,
        "sample_value": "1819480931672618378",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "id",
        "payflow_field": "loan_identifier",
        "meaning": "CRM case UUID (`id` on Debtor Summary / FetchDebtorCaseSummary). PayFlow stores it as loan_identifier.",
        "required": True,
        "sample_value": "bd52a426-8c1e-4f2a-9b3d-1a2b3c4d5e6f",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "ACCOUNT_STATUS_ID",
        "payflow_field": "account_status",
        "meaning": "OPEN / CLOSED. Exclude non-OPEN at intake.",
        "required": True,
        "sample_value": "OPEN",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "AGENCY_DATE_SENT",
        "payflow_field": "placement_date",
        "meaning": "Also AGENCY_DAYS_PLACED, PLACEABLE_DAYS available in feed.",
        "required": True,
        "sample_value": "2018-09-25",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "NEGATIVE_BALANCE_REASON",
        "payflow_field": "negative_balance_reason",
        "meaning": "Failed ACH NSF, Other, Non-receipt, Un-authorized, SNAD. Good AI context.",
        "required": False,
        "sample_value": "Failed ACH NSF",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "ACCOUNT_CATEGORY",
        "payflow_field": "account_category",
        "meaning": "PERSONAL / BUSINESS / PREMIER.",
        "required": False,
        "sample_value": "PERSONAL",
        "group": "Account and Loan",
        "available": "Yes",
    },
    # ----- Balances and Dates -----
    {
        "source_field": "CURRENCY_CODE",
        "payflow_field": "currency_code",
        "meaning": "ISO 4217. Placement feed is 100% CAD.",
        "required": True,
        "sample_value": "CAD",
        "group": "Balances and Dates",
        "available": "Yes",
    },
    {
        "source_field": "CUSTOMER_BALANCE or CONFIRMED_BALANCE",
        "payflow_field": "current_balance",
        "meaning": "Ambiguous: both present and differ. Confirm which to collect against. Use absolute value of negative decimals.",
        "required": True,
        "sample_value": "-150.00",
        "group": "Balances and Dates",
        "available": "Ambiguous",
    },
    {
        "source_field": "FEE_AMOUNT",
        "payflow_field": "fee_amount",
        "meaning": "Column present but empty in current feed.",
        "required": False,
        "sample_value": "",
        "group": "Balances and Dates",
        "available": "No",
    },
    # ----- Customer and Contact -----
    {
        "source_field": "CUSTOMER_FIRST_NAME",
        "payflow_field": "customer_first_name",
        "meaning": "Customer first name (100% populated).",
        "required": True,
        "sample_value": "Jordan",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "CUSTOMER_LAST_NAME",
        "payflow_field": "customer_last_name",
        "meaning": "Customer last name (100% populated).",
        "required": True,
        "sample_value": "Alvarez",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "CUSTOMER_EMAIL",
        "payflow_field": "email",
        "meaning": "Y* one contact channel required. Email is 100% populated and channel-ready.",
        "required": True,
        "sample_value": "jordan.alvarez@example.com",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "CUSTOMER_PHONE_HOME",
        "payflow_field": "phone_mobile",
        "meaning": "Y* labelled HOME (99.5%). Confirm SMS-capable, or use ALT_PHN.",
        "required": False,
        "sample_value": "4165551234",
        "group": "Customer and Contact",
        "available": "Ambiguous",
    },
    {
        "source_field": "CUSTOMER_PHONE_WORK",
        "payflow_field": "phone_work",
        "meaning": "Mostly empty (~0.9% populated).",
        "required": False,
        "sample_value": "",
        "group": "Customer and Contact",
        "available": "No",
    },
    {
        "source_field": "CUSTOMER_LANGUAGE",
        "payflow_field": "language",
        "meaning": "EN / FR. Drives message language.",
        "required": False,
        "sample_value": "EN",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "ADDRESS_LINE_1",
        "payflow_field": "address_line1",
        "meaning": "Street address (100% populated).",
        "required": False,
        "sample_value": "12 King St W",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "ADDRESS_CITY",
        "payflow_field": "city",
        "meaning": "City (100% populated).",
        "required": False,
        "sample_value": "Toronto",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "ADDRESS_STATE",
        "payflow_field": "province_state",
        "meaning": "Province / state code. Use to derive timezone.",
        "required": False,
        "sample_value": "ON",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "ADDRESS_POSTAL_CODE",
        "payflow_field": "postal_code",
        "meaning": "Postal code (100% populated).",
        "required": False,
        "sample_value": "M5H 1A1",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "TIMEZONE",
        "payflow_field": "timezone",
        "meaning": "Empty in feed. Derive from province for send scheduling.",
        "required": False,
        "sample_value": "",
        "group": "Customer and Contact",
        "available": "No",
    },
    {
        "source_field": "CUSTOMER_DOB",
        "payflow_field": "date_of_birth",
        "meaning": "Date of birth YYYY-MM-DD. Used with age_group for segmentation.",
        "required": False,
        "sample_value": "1988-04-12",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "AGE_GROUP",
        "payflow_field": "age_group",
        "meaning": "Age band: 18-24, 25-34, 35-44, 45-54, 55-64, 65+.",
        "required": False,
        "sample_value": "35-44",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "REGION",
        "payflow_field": "region",
        "meaning": "Collection region. If blank, copy province_state.",
        "required": False,
        "sample_value": "ON",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "EMPLOYMENT_STATUS",
        "payflow_field": "employment_status",
        "meaning": "Employment status/type from CRM payload (e.g. Employed, Self-employed, Unemployed, Retired).",
        "required": False,
        "sample_value": "Employed",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "INCOME_BAND",
        "payflow_field": "income_band",
        "meaning": "Customer income band from CRM payload for segmentation.",
        "required": False,
        "sample_value": "$40K–$75K",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "EDUCATION_LEVEL",
        "payflow_field": "education_level",
        "meaning": "Highest education level from CRM payload.",
        "required": False,
        "sample_value": "Bachelor",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "CUSTOMER_SEGMENT",
        "payflow_field": "customer_segment",
        "meaning": "CRM customer segment / persona used for targeting and workflows.",
        "required": False,
        "sample_value": "Digital preferred",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "CLIENT_CODE",
        "payflow_field": "client_code",
        "meaning": "Must match an existing PayFlow client code. Daily file does not create clients.",
        "required": True,
        "sample_value": "paypal",
        "group": "Client and Portfolio",
        "available": "Yes",
    },
    {
        "source_field": "SUB_CLIENT_CODE",
        "payflow_field": "sub_client_code",
        "meaning": "Must match an existing portfolio code under the client.",
        "required": True,
        "sample_value": "paypal-loans",
        "group": "Client and Portfolio",
        "available": "Yes",
    },
    {
        "source_field": "DUE_DATE",
        "payflow_field": "due_date",
        "meaning": "Next payment / delinquency due date YYYY-MM-DD.",
        "required": False,
        "sample_value": "2026-09-15",
        "group": "Balances and Dates",
        "available": "Yes",
    },
    {
        "source_field": "DAYS_PAST_DUE",
        "payflow_field": "days_past_due",
        "meaning": "Integer days past due.",
        "required": False,
        "sample_value": "22",
        "group": "Balances and Dates",
        "available": "Yes",
    },
    {
        "source_field": "ORIGINAL_BALANCE",
        "payflow_field": "original_balance",
        "meaning": "Placement original. Applied only when creating an account; later files do not overwrite.",
        "required": False,
        "sample_value": "5400.00",
        "group": "Balances and Dates",
        "available": "Yes",
    },
    {
        "source_field": "COLLECTION_STATUS",
        "payflow_field": "collection_status",
        "meaning": "PayFlow collection status including Promise to Pay.",
        "required": False,
        "sample_value": "Active",
        "group": "Account and Loan",
        "available": "Yes",
    },
    # ----- Collection history (daily file + PayFlow activity) -----
    {
        "source_field": "LAST_PAYMENT_AMOUNT",
        "payflow_field": "last_payment_amount",
        "meaning": "Most recent payment amount from CRM.",
        "required": False,
        "sample_value": "150.00",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "LAST_PAYMENT_DATE",
        "payflow_field": "last_payment_date",
        "meaning": "Most recent payment date YYYY-MM-DD.",
        "required": False,
        "sample_value": "2026-09-28",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "LAST_PAYMENT_IS_PTP",
        "payflow_field": "last_payment_is_ptp",
        "meaning": "Y if the last payment fulfilled a promise-to-pay.",
        "required": False,
        "sample_value": "N",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "PTP_CODE",
        "payflow_field": "ptp_code",
        "meaning": "Promise-to-pay code from CRM.",
        "required": False,
        "sample_value": "PTP-OK",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "PTP_AMOUNT",
        "payflow_field": "ptp_amount",
        "meaning": "Promised amount.",
        "required": False,
        "sample_value": "200.00",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "PTP_DUE_DATE",
        "payflow_field": "ptp_due_date",
        "meaning": "Promise due date YYYY-MM-DD.",
        "required": False,
        "sample_value": "2026-10-20",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "(none — PayFlow-built: prior_collection_activity)",
        "payflow_field": "prior_collection_activity",
        "meaning": "Built from PayFlow's own activity.",
        "required": False,
        "sample_value": "",
        "group": "Collection history",
        "available": "PayFlow-built",
    },
    # ----- Identifiers / placement / hold / consent (Debtor Summary + daily file) -----
    {
        "source_field": "id / CASE_ID",
        "payflow_field": "crm_case_id",
        "meaning": "CRM case UUID / Case ID from FetchDebtorCaseSummary.",
        "required": False,
        "sample_value": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "debtor_id",
        "payflow_field": "debtor_id",
        "meaning": "Customer / Debtor ID from CRM.",
        "required": False,
        "sample_value": "DBT-10482",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "client_reference_number",
        "payflow_field": "client_reference_number",
        "meaning": "Client reference number on the CRM case.",
        "required": False,
        "sample_value": "CRN-PP-10482",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "date_listed / AGENCY_DATE_SENT",
        "payflow_field": "date_listed",
        "meaning": "Date the account was listed / placed with the agency.",
        "required": False,
        "sample_value": "2026-08-12",
        "group": "Account and Loan",
        "available": "Yes",
    },
    {
        "source_field": "LAST_EMAIL_SENT_DATE",
        "payflow_field": "last_email_sent_date",
        "meaning": "Most recent email contact date from CRM.",
        "required": False,
        "sample_value": "2026-09-28",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "LAST_SMS_SENT_DATE",
        "payflow_field": "last_sms_sent_date",
        "meaning": "Most recent SMS contact date from CRM.",
        "required": False,
        "sample_value": "2026-09-30",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "LAST_CONTACT_DATE",
        "payflow_field": "last_contact_date",
        "meaning": "Most recent contact of any channel from CRM.",
        "required": False,
        "sample_value": "2026-09-30",
        "group": "Collection history",
        "available": "Yes",
    },
    {
        "source_field": "dn_client_demographic_is_provincial_hold / COMMUNICATION_HOLD",
        "payflow_field": "provincial_hold",
        "meaning": "Communication / provincial hold flag. Y/N.",
        "required": False,
        "sample_value": "N",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "HOLD_DAYS",
        "payflow_field": "hold_days",
        "meaning": "Days remaining (or duration) on the communication / provincial hold.",
        "required": False,
        "sample_value": "0",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "email_list[].consent / EMAIL_CONSENT",
        "payflow_field": "email_consent",
        "meaning": "Email consent / contact permission. Y/N.",
        "required": False,
        "sample_value": "Y",
        "group": "Customer and Contact",
        "available": "Yes",
    },
    {
        "source_field": "updated_at / SOURCE_UPDATED_AT",
        "payflow_field": "source_updated_at",
        "meaning": "CRM source row last-updated timestamp.",
        "required": False,
        "sample_value": "2026-10-05T14:30:00Z",
        "group": "Collection history",
        "available": "Yes",
    },
]


# Align the catalog with what the real daily files actually carry
# (placement feed + debtor dump / PayFlow daily template).
#   Yes / Derived / PayFlow-built  -> available from the feed or built by PayFlow
#   Optional                       -> NOT in the current CRM feed; optional daily-file
#                                     column. Never blocks mapping or activation.
_OPTIONAL_NOTE = (
    " Optional: not in the current CRM feed. Add this column to the daily file only if "
    "the source has it; otherwise leave it blank."
)
_CATALOG_OVERRIDES: dict[str, dict[str, str]] = {
    "CUSTOMER_BALANCE or CONFIRMED_BALANCE": {
        "available": "Yes",
        "meaning": (
            "Daily file column outstanding_balance. Absolute value of negative "
            "decimals; collection is run against this balance."
        ),
    },
    "CUSTOMER_PHONE_HOME": {
        "available": "Yes",
        "meaning": (
            "Daily file column phone_mobile (placement feed labels it HOME). Optional; "
            "email is the primary contact channel, SMS needs a mobile-capable number."
        ),
    },
    "filename": {
        "available": "Derived",
        "meaning": "Daily file product column; defaults to P4 for CRM debtor exports.",
    },
}
_OPTIONAL_NOT_IN_FEED: frozenset[str] = frozenset(
    {
        "FEE_AMOUNT",
        "CUSTOMER_PHONE_WORK",
        "TIMEZONE",
        "AGE_GROUP",
        "EMPLOYMENT_STATUS",
        "INCOME_BAND",
        "EDUCATION_LEVEL",
        "CUSTOMER_SEGMENT",
        "DUE_DATE",
        "DAYS_PAST_DUE",
        "LAST_PAYMENT_IS_PTP",
        "PTP_CODE",
        "PTP_AMOUNT",
        "PTP_DUE_DATE",
        "email_list[].consent / EMAIL_CONSENT",
    }
)

for _field in CRM_INBOUND_FIELDS:
    _src = _field["source_field"]
    if _src in _CATALOG_OVERRIDES:
        _field.update(_CATALOG_OVERRIDES[_src])  # type: ignore[typeddict-item]
    elif _src in _OPTIONAL_NOT_IN_FEED:
        _field["available"] = "Optional"
        _field["required"] = False
        _field["meaning"] = _field["meaning"].rstrip() + _OPTIONAL_NOTE


class CrmOutboundFieldDef(TypedDict):
    file: str
    field: str
    meaning: str
    required: bool
    format: str
    sample_value: str
    notes: str


# OUTBOUND — PayFlow → CRM (+ status files PayFlow consumes)
CRM_OUTBOUND_FIELDS: list[CrmOutboundFieldDef] = [
    {
        "file": "REMIT",
        "field": "TENANT_ACCOUNT_ID",
        "meaning": "PayPal account number",
        "required": True,
        "format": "Numeric",
        "sample_value": "1819480931672618378",
        "notes": "",
    },
    {
        "file": "REMIT",
        "field": "LOAN_IDENTIFIER",
        "meaning": "Loan / credit account id (same inbound payload id)",
        "required": True,
        "format": "UUID / string",
        "sample_value": "bd52a426-...",
        "notes": "Carried from inbound payload; used to key remit rows",
    },
    {
        "file": "REMIT",
        "field": "REPAYMENT_AMOUNT",
        "meaning": "Amount paid",
        "required": True,
        "format": "Decimal 2dp",
        "sample_value": "100.05",
        "notes": "Positive amount collected",
    },
    {
        "file": "REMIT",
        "field": "CURRENCY_CODE",
        "meaning": "Currency",
        "required": True,
        "format": "ISO 4217",
        "sample_value": "CAD",
        "notes": "",
    },
    {
        "file": "REMIT",
        "field": "SCHEDULE_DATE",
        "meaning": "Date paid",
        "required": True,
        "format": "dd/mm/yyyy",
        "sample_value": "12/07/2021",
        "notes": "Product country timezone, not future",
    },
    {
        "file": "REMIT",
        "field": "ACCOUNTING_REFERENCE_DATA",
        "meaning": "Cash Application Identifier",
        "required": True,
        "format": "OCA_NCR_GPL_CA_P4_DDMMYYYY_WK",
        "sample_value": "OCA_NCR_GPL_CA_P4_20042021_01",
        "notes": "",
    },
    {
        "file": "REMIT",
        "field": "OPTIONAL1 (request_id)",
        "meaning": "Idempotency id",
        "required": True,
        "format": "UUID",
        "sample_value": "8fc52f49-...",
        "notes": "Retry-safe, no double-post",
    },
    {
        "file": "CLOSURE",
        "field": "TENANT_ACCOUNT_ID / DATE_OF_CLOSURE / CLOSURE_CODE",
        "meaning": "Close an account",
        "required": True,
        "format": "num / date / code",
        "sample_value": "... / 25/09/2026 / SIF",
        "notes": "Closure code list on maintenance sheet",
    },
    {
        "file": "REMIT_REVERSAL",
        "field": "PAYMENT_REFERENCE_ID + amount + REJECT_CATEGORY",
        "meaning": "Reverse a payment",
        "required": True,
        "format": "ref / decimal / enum",
        "sample_value": "... / 100.05 / BANK_RETURN",
        "notes": "",
    },
    {
        "file": "FRAUD",
        "field": "PAYPAL ACCT# + FRAUD_TYPE",
        "meaning": "Flag fraud",
        "required": True,
        "format": "num / enum",
        "sample_value": "... / SPOOF",
        "notes": "SPOOF / IDENTITY_THEFT / OTHER",
    },
    {
        "file": "REMIT_STATUS",
        "field": "PAYMENT_REFERENCE_ID + STATUS_CODE + STATUS_DESCRIPTION",
        "meaning": "Remit outcome (consumed from CRM)",
        "required": True,
        "format": "ref / enum / text",
        "sample_value": "... / ACCEPTED / ok",
        "notes": "ACCEPTED / INVALID_DATA / PROCESSING_FAILURE / FORMAT_ERROR",
    },
    {
        "file": "REMIT_REVERSAL_STATUS",
        "field": "(same shape as REMIT_STATUS)",
        "meaning": "Reversal outcome (consumed from CRM)",
        "required": True,
        "format": "enum / text",
        "sample_value": "ACCEPTED",
        "notes": "",
    },
]

PAYFLOW_TARGET_FIELDS: list[str] = sorted(
    {f["payflow_field"] for f in CRM_INBOUND_FIELDS} | {"— Not mapped —"}
)

GOVERNANCE_RULE_LIBRARY: list[str] = [
    "High Balance Human Review",
    "Repeated Attempts Escalation",
    "Low Confidence Review",
    "Dispute Detected Review",
    "Settlement Offer Approval",
]

# Minimal template (non-CRM / file-only onboarding). Hierarchy CRM export uses
# clients (2).csv columns: is_master_client, master_client__client_number, etc.
BULK_UPLOAD_HEADERS: list[str] = [
    "client_number",
    "client_code",
    "company_name",
    "short_name",
    "is_master_client",
    "master_client__client_number",
    "product",
    "email_address",
    "phone_number",
    "clientdemographiccontactinformation__full_name",
    "clientdemographiccontactinformation__email_address",
    "clientdemographiccontactinformation__phone_number",
    "address_line_1",
    "city__name",
    "province__name",
    "country__name",
    "zip_code",
    "client_industry__name",
    "client_type__name",
    "correspondence_language__name",
    "currency__name",
    "client_status__name",
]

CLIENT_IMPORT_MASTER_REQUIRED: tuple[str, ...] = (
    "client_number",
    "client_code",
    "is_master_client",
)

# Daily CRM account file — one sheet "Accounts". Clients/portfolios must already exist.
ACCOUNT_IMPORT_HEADERS: list[str] = [
    "client_code",
    "sub_client_code",
    "account_id",
    "crm_case_id",
    "debtor_id",
    "client_reference_number",
    "product_code",
    "customer_first_name",
    "customer_last_name",
    "date_of_birth",
    "age_group",
    "employment_status",
    "income_band",
    "education_level",
    "customer_segment",
    "address_line1",
    "city",
    "province_state",
    "postal_code",
    "country_code",
    "region",
    "currency_code",
    "outstanding_balance",
    "original_balance",
    "fee_amount",
    "email",
    "phone_mobile",
    "phone_work",
    "language",
    "date_listed",
    "last_email_sent_date",
    "last_sms_sent_date",
    "last_contact_date",
    "provincial_hold",
    "hold_days",
    "email_consent",
    "source_updated_at",
    "last_payment_amount",
    "last_payment_date",
    "last_payment_is_ptp",
    "ptp_code",
    "ptp_amount",
    "ptp_due_date",
    "due_date",
    "days_past_due",
    "account_status",
    "collection_status",
    "negative_balance_reason",
    "account_category",
]

ACCOUNT_IMPORT_REQUIRED: tuple[str, ...] = (
    "client_code",
    "sub_client_code",
    "account_id",
    "product_code",
    "customer_first_name",
    "customer_last_name",
    "country_code",
    "currency_code",
    "outstanding_balance",
    "email",
)

# CRM Debtor Summary / UAT dump column → PayFlow account import header.
# Applied when loading CSV/XLSX so native CRM exports can be uploaded directly.
ACCOUNT_HEADER_ALIASES: dict[str, str] = {
    "account_number": "account_id",
    "first_name": "customer_first_name",
    "last_name": "customer_last_name",
    "email_address": "email",
    "principle_amount": "original_balance",
    "id": "crm_case_id",
    "date_last_email_sent": "last_email_sent_date",
    "date_of_last_sms_sent": "last_sms_sent_date",
    "date_of_last_contact": "last_contact_date",
    "dn_client_demographic_is_provincial_hold": "provincial_hold",
    "dn_debtorcaseaddress_province_hold_days": "hold_days",
    "dn_currency_name": "currency_code",
    "dn_collection_status_name": "collection_status",
    "dn_debtorcaseaddress_address_line_1": "address_line1",
    "dn_debtorcaseaddress_city_name": "city",
    "dn_debtorcaseaddress_province_name": "province_state",
    "dn_debtorcaseaddress_postal_code": "postal_code",
    "dn_debtorcaseaddress_country_name": "country_code",
    "dn_debtorcasephonenumber_phone_number": "phone_mobile",
    "dn_client_demographic_product": "product_code",
    "account_product": "product_code",
    "updated_at": "source_updated_at",
    "case_id": "crm_case_id",
}

# When CRM provides client_number, these PayFlow columns may be resolved/defaulted per row.
ACCOUNT_CRM_RESOLVABLE_REQUIRED: tuple[str, ...] = (
    "client_code",
    "sub_client_code",
    "product_code",
    "country_code",
    "currency_code",
)

AGE_GROUPS: tuple[str, ...] = ("18-24", "25-34", "35-44", "45-54", "55-64", "65+")
ACCOUNT_IMPORT_LANGUAGES: tuple[str, ...] = ("EN", "FR")
ACCOUNT_IMPORT_CURRENCIES: tuple[str, ...] = ("CAD", "USD")
ACCOUNT_CRM_STATUSES: tuple[str, ...] = ("OPEN", "CLOSED")
ACCOUNT_CRM_DEFAULT_COUNTRY = "CA"
ACCOUNT_CRM_DEFAULT_CURRENCY = "CAD"
ACCOUNT_CRM_DEFAULT_PRODUCT = "P4"
