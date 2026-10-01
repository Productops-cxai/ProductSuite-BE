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
    available: str  # Yes | Derived | Partial | Ambiguous | No | PayFlow-built


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
        "source_field": "(none — missing from placement)",
        "payflow_field": "loan_identifier",
        "meaning": "BLOCKER. Remit file needs this UUID. Ask where it comes from.",
        "required": True,
        "sample_value": "",
        "group": "Account and Loan",
        "available": "No",
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
    # ----- Collection history (PayFlow-built; not in placement feed) -----
    {
        "source_field": "LAST_PAYMENT_* / PAYMENT_ACTIVITY_HISTORY",
        "payflow_field": "prior_payments",
        "meaning": "Columns exist but 0% populated. PayFlow builds history over time.",
        "required": False,
        "sample_value": "",
        "group": "Collection history",
        "available": "PayFlow-built",
    },
    {
        "source_field": "(none — PayFlow-built: promise_to_pay_history)",
        "payflow_field": "promise_to_pay_history",
        "meaning": "Built from PayFlow activity and status files.",
        "required": False,
        "sample_value": "",
        "group": "Collection history",
        "available": "PayFlow-built",
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
]


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
        "meaning": "Credit account id (loan)",
        "required": True,
        "format": "UUID",
        "sample_value": "bd52a426-...",
        "notes": "NOT in placement file — open blocker",
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

BULK_UPLOAD_HEADERS: list[str] = [
    "client_name",
    "client_code",
    "client_type",
    "business_domain",
    "industry",
    "ai_mode",
]
