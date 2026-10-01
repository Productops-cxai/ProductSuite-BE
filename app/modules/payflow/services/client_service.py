"""PayFlow client onboarding service."""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowClientFieldMappingModel,
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowUserClientAssignmentModel,
    PayflowUserClientPermissionModel,
    PayflowUserMembershipModel,
    UserModel,
)
from app.modules.payflow.crm_catalog import (
    BULK_UPLOAD_HEADERS,
    CRM_INBOUND_FIELDS,
    CRM_OUTBOUND_FIELDS,
    GOVERNANCE_RULE_LIBRARY,
    PAYFLOW_TARGET_FIELDS,
)
from app.shared.enums import (
    PayflowAiMode,
    PayflowBusinessDomain,
    PayflowClientStatus,
    PayflowClientType,
    PayflowConnectionStatus,
    PayflowDataSourceType,
    PayflowMappingStatus,
    PayflowOnboardingStepStatus,
    PayflowPortfolioStatus,
    PayflowRoleCode,
)


_CLIENT_TYPE_LABELS = {
    PayflowClientType.FIRST_PARTY.value: "First Party",
    PayflowClientType.THIRD_PARTY.value: "Third Party",
}
_CLIENT_TYPE_FROM_LABEL = {v.lower(): k for k, v in _CLIENT_TYPE_LABELS.items()}
_CLIENT_TYPE_FROM_LABEL.update({k: k for k in _CLIENT_TYPE_LABELS})

_AI_MODE_LABELS = {
    PayflowAiMode.AUTOPILOT.value: "Autopilot",
    PayflowAiMode.SUPERVISED_AI.value: "Supervised AI",
}
_AI_MODE_FROM_LABEL = {v.lower(): k for k, v in _AI_MODE_LABELS.items()}
_AI_MODE_FROM_LABEL.update({k: k for k in _AI_MODE_LABELS})

_DOMAIN_LABELS = {
    PayflowBusinessDomain.COLLECTIONS.value: "Collections",
}
_DOMAIN_FROM_LABEL = {v.lower(): k for k, v in _DOMAIN_LABELS.items()}
_DOMAIN_FROM_LABEL.update({k: k for k in _DOMAIN_LABELS})

_CONNECTION_LABELS = {
    PayflowConnectionStatus.NOT_CONNECTED.value: "Not Connected",
    PayflowConnectionStatus.CONNECTING.value: "Connecting",
    PayflowConnectionStatus.CONNECTED.value: "Connected",
    PayflowConnectionStatus.CONNECTION_FAILED.value: "Connection Failed",
}

_MAPPING_LABELS = {
    PayflowMappingStatus.MAPPED.value: "Mapped",
    PayflowMappingStatus.NEEDS_ATTENTION.value: "Needs Attention",
    PayflowMappingStatus.UNMAPPED.value: "Unmapped",
    PayflowMappingStatus.VALIDATED.value: "Validated",
}
_MAPPING_FROM_LABEL = {v.lower(): k for k, v in _MAPPING_LABELS.items()}
_MAPPING_FROM_LABEL.update({k: k for k in _MAPPING_LABELS})

_STATUS_LABELS = {
    PayflowClientStatus.DRAFT.value: "Draft",
    PayflowClientStatus.ACTIVE.value: "Active",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "").strip())


def _parse_client_type(value: str | None, default: str = PayflowClientType.THIRD_PARTY.value) -> str:
    if not value or not str(value).strip():
        return default
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    # first party / first_party / First Party
    key2 = str(value).strip().lower()
    if key2 in _CLIENT_TYPE_FROM_LABEL:
        return _CLIENT_TYPE_FROM_LABEL[key2]
    if key in _CLIENT_TYPE_FROM_LABEL:
        return _CLIENT_TYPE_FROM_LABEL[key]
    if key in ("first_party", "firstparty"):
        return PayflowClientType.FIRST_PARTY.value
    if key in ("third_party", "thirdparty"):
        return PayflowClientType.THIRD_PARTY.value
    raise ValidationAppError("Client type must be First Party or Third Party")


def _parse_ai_mode(value: str | None, default: str = PayflowAiMode.SUPERVISED_AI.value) -> str:
    if not value or not str(value).strip():
        return default
    key = str(value).strip().lower()
    if key in _AI_MODE_FROM_LABEL:
        return _AI_MODE_FROM_LABEL[key]
    key2 = key.replace(" ", "_").replace("-", "_")
    if key2 in _AI_MODE_FROM_LABEL:
        return _AI_MODE_FROM_LABEL[key2]
    raise ValidationAppError("AI mode must be Autopilot or Supervised AI")


def _parse_domain(value: str | None) -> str:
    if not value or not str(value).strip():
        return PayflowBusinessDomain.COLLECTIONS.value
    key = str(value).strip().lower()
    if key in _DOMAIN_FROM_LABEL:
        return _DOMAIN_FROM_LABEL[key]
    raise ValidationAppError("Business domain must be Collections for Phase 1")


class PayflowClientService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def mapping_catalog(self) -> dict:
        return {
            "fields": CRM_INBOUND_FIELDS,
            "outbound_fields": CRM_OUTBOUND_FIELDS,
            "payflow_fields": PAYFLOW_TARGET_FIELDS,
            "governance_rules": GOVERNANCE_RULE_LIBRARY,
            "catalog_version": "0.3",
        }

    # ------------------------------------------------------------------
    # List / get
    # ------------------------------------------------------------------

    def list_clients(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        client_type: str | None = None,
        business_domain: str | None = None,
        ai_mode: str | None = None,
        supervisor_user_id: str | None = None,
        client_ids: list[int] | None = None,
    ) -> dict:
        q = self.db.query(PayflowClientModel)
        if client_ids is not None:
            if not client_ids:
                return {"clients": []}
            q = q.filter(PayflowClientModel.id.in_(client_ids))
        if search:
            term = f"%{search.strip()}%"
            q = q.filter(
                (PayflowClientModel.name.ilike(term))
                | (PayflowClientModel.code.ilike(term))
                | (PayflowClientModel.category.ilike(term))
            )
        if status:
            q = q.filter(PayflowClientModel.status == status.strip().lower())
        if client_type:
            q = q.filter(
                PayflowClientModel.client_type == _parse_client_type(client_type)
            )
        if business_domain:
            q = q.filter(
                PayflowClientModel.business_domain == _parse_domain(business_domain)
            )
        if ai_mode:
            q = q.filter(PayflowClientModel.ai_mode == _parse_ai_mode(ai_mode))
        if supervisor_user_id:
            try:
                uid = UUID(supervisor_user_id)
            except ValueError as exc:
                raise ValidationAppError("Invalid supervisor filter") from exc
            q = (
                q.join(PayflowUserClientAssignmentModel)
                .join(PayflowUserMembershipModel)
                .filter(PayflowUserMembershipModel.user_id == uid)
            )

        rows = q.order_by(PayflowClientModel.name).all()
        return {"clients": [self._list_item(c) for c in rows]}

    def get_client(self, client_id: int) -> dict:
        client = self._get_or_404(client_id)
        if self._sync_catalog_mappings(client):
            self.db.commit()
            client = self._get_or_404(client_id)
        return self._detail(client)

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    def create_client(self, payload: dict[str, Any]) -> dict:
        name = (payload.get("name") or "").strip()
        code = _normalize_code(payload.get("code") or "")
        if not name:
            raise ValidationAppError("Client name is required")
        if not code:
            raise ValidationAppError("Client code / reference is required")
        self._assert_unique_code(code)

        client = PayflowClientModel(
            name=name,
            code=code,
            category=(payload.get("industry") or payload.get("category") or "").strip() or None,
            status=PayflowClientStatus.DRAFT.value,
            client_type=_parse_client_type(payload.get("client_type")),
            business_domain=_parse_domain(payload.get("business_domain")),
            ai_mode=_parse_ai_mode(payload.get("ai_mode")),
            data_source_type=None,
            connection_status=PayflowConnectionStatus.NOT_CONNECTED.value,
            environment="Sandbox",
            sync_frequency="Every 15 minutes",
            brand_name="",
            sender_name="",
            email_from="collections@payflow.io",
            sms_sender_id="PAYFLOW",
            channel_email=True,
            channel_sms=True,
            channel_whatsapp=False,
            governance_rules=[],
            updated_at=_utcnow(),
        )
        self.db.add(client)
        self.db.flush()
        self._seed_default_mappings(client)
        self.db.commit()
        self.db.refresh(client)
        return self._detail(client)

    def update_client(self, client_id: int, payload: dict[str, Any]) -> dict:
        client = self._get_or_404(client_id)
        if client.status == PayflowClientStatus.ACTIVE.value:
            # Allow config updates after activation for non-identity fields;
            # identity uniqueness still enforced.
            pass

        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise ValidationAppError("Client name is required")
            client.name = name

        if "code" in payload and payload["code"] is not None:
            code = _normalize_code(str(payload["code"]))
            if not code:
                raise ValidationAppError("Client code / reference is required")
            self._assert_unique_code(code, exclude_id=client.id)
            client.code = code

        if "industry" in payload or "category" in payload:
            industry = payload.get("industry", payload.get("category"))
            client.category = (str(industry).strip() if industry is not None else None) or None

        if "client_type" in payload and payload["client_type"] is not None:
            client.client_type = _parse_client_type(payload["client_type"])

        if "business_domain" in payload and payload["business_domain"] is not None:
            client.business_domain = _parse_domain(payload["business_domain"])

        if "ai_mode" in payload and payload["ai_mode"] is not None:
            client.ai_mode = _parse_ai_mode(payload["ai_mode"])

        # Data source (CRM only)
        if "data_source_type" in payload:
            ds = payload["data_source_type"]
            if ds is None or ds == "":
                client.data_source_type = None
                client.connection_status = PayflowConnectionStatus.NOT_CONNECTED.value
            else:
                ds_norm = str(ds).strip().lower()
                if ds_norm not in ("crm", PayflowDataSourceType.CRM.value):
                    raise ValidationAppError("Only CRM data source is supported")
                if client.data_source_type != PayflowDataSourceType.CRM.value:
                    client.connection_status = PayflowConnectionStatus.NOT_CONNECTED.value
                client.data_source_type = PayflowDataSourceType.CRM.value

        if "connection_status" in payload and payload["connection_status"] is not None:
            cs = str(payload["connection_status"]).strip().lower().replace(" ", "_")
            label_map = {v.lower().replace(" ", "_"): k for k, v in _CONNECTION_LABELS.items()}
            label_map.update({k: k for k in _CONNECTION_LABELS})
            if cs not in label_map:
                raise ValidationAppError("Invalid connection status")
            client.connection_status = label_map[cs]

        for field in (
            "crm_system_name",
            "integration_ref",
            "environment",
            "sync_frequency",
            "brand_name",
            "sender_name",
            "email_from",
            "sms_sender_id",
        ):
            if field in payload and payload[field] is not None:
                setattr(client, field, str(payload[field]).strip())

        if "channels" in payload and isinstance(payload["channels"], dict):
            ch = payload["channels"]
            if "email" in ch:
                client.channel_email = bool(ch["email"])
            if "sms" in ch:
                client.channel_sms = bool(ch["sms"])
            if "whatsapp" in ch:
                client.channel_whatsapp = bool(ch["whatsapp"])

        if "governance_rules" in payload and payload["governance_rules"] is not None:
            rules = payload["governance_rules"]
            if not isinstance(rules, list):
                raise ValidationAppError("governance_rules must be a list")
            unknown = [r for r in rules if r not in GOVERNANCE_RULE_LIBRARY]
            if unknown:
                raise ValidationAppError(f"Unknown governance rule(s): {', '.join(map(str, unknown))}")
            client.governance_rules = list(rules)

        if "mappings" in payload and payload["mappings"] is not None:
            self._replace_mappings(client, payload["mappings"])

        if "supervisor_user_ids" in payload and payload["supervisor_user_ids"] is not None:
            self._sync_supervisors(client, payload["supervisor_user_ids"])

        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(client)
        return self._detail(client)

    def activate_client(self, client_id: int) -> dict:
        client = self._get_or_404(client_id)
        blockers = self._activation_blockers(client)
        if blockers:
            raise ValidationAppError("; ".join(blockers))
        client.status = PayflowClientStatus.ACTIVE.value
        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(client)
        return self._detail(client)

    # ------------------------------------------------------------------
    # Portfolios
    # ------------------------------------------------------------------

    def list_portfolios(self, client_id: int) -> dict:
        self._get_or_404(client_id)
        rows = (
            self.db.query(PayflowPortfolioModel)
            .filter(PayflowPortfolioModel.client_id == client_id)
            .order_by(PayflowPortfolioModel.name)
            .all()
        )
        return {"portfolios": [self._portfolio_item(p) for p in rows]}

    def create_portfolio(self, client_id: int, payload: dict[str, Any]) -> dict:
        client = self._get_or_404(client_id)
        name = (payload.get("name") or "").strip()
        code = _normalize_code(payload.get("code") or "")
        if not name:
            raise ValidationAppError("Portfolio name is required")
        if not code:
            raise ValidationAppError("Portfolio code / reference is required")
        exists = (
            self.db.query(PayflowPortfolioModel)
            .filter(
                PayflowPortfolioModel.client_id == client.id,
                PayflowPortfolioModel.code == code,
            )
            .first()
        )
        if exists:
            raise ConflictError("Portfolio code already exists for this client")

        status = (payload.get("status") or PayflowPortfolioStatus.ONBOARDING.value).strip().lower()
        if status not in {s.value for s in PayflowPortfolioStatus}:
            raise ValidationAppError("Invalid portfolio status")

        row = PayflowPortfolioModel(
            client_id=client.id,
            name=name,
            code=code,
            status=status,
            description=(payload.get("description") or "").strip() or None,
        )
        self.db.add(row)
        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(row)
        return self._portfolio_item(row)

    def update_portfolio(self, client_id: int, portfolio_id: int, payload: dict[str, Any]) -> dict:
        client = self._get_or_404(client_id)
        row = (
            self.db.query(PayflowPortfolioModel)
            .filter(
                PayflowPortfolioModel.id == portfolio_id,
                PayflowPortfolioModel.client_id == client_id,
            )
            .first()
        )
        if not row:
            raise NotFoundError("Portfolio not found")

        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise ValidationAppError("Portfolio name is required")
            row.name = name
        if "code" in payload and payload["code"] is not None:
            code = _normalize_code(str(payload["code"]))
            if not code:
                raise ValidationAppError("Portfolio code / reference is required")
            clash = (
                self.db.query(PayflowPortfolioModel)
                .filter(
                    PayflowPortfolioModel.client_id == client_id,
                    PayflowPortfolioModel.code == code,
                    PayflowPortfolioModel.id != portfolio_id,
                )
                .first()
            )
            if clash:
                raise ConflictError("Portfolio code already exists for this client")
            row.code = code
        if "status" in payload and payload["status"] is not None:
            status = str(payload["status"]).strip().lower()
            if status not in {s.value for s in PayflowPortfolioStatus}:
                raise ValidationAppError("Invalid portfolio status")
            row.status = status
        if "description" in payload:
            row.description = (
                str(payload["description"]).strip() if payload["description"] else None
            )

        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(row)
        return self._portfolio_item(row)

    # ------------------------------------------------------------------
    # Bulk upload
    # ------------------------------------------------------------------

    def bulk_template_bytes(self) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "Clients"
        ws.append(BULK_UPLOAD_HEADERS)
        ws.append(
            [
                "Example Client",
                "EX-CLT-001",
                "Third Party",
                "Collections",
                "Payments",
                "Supervised AI",
            ]
        )
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def bulk_upload(self, file_bytes: bytes) -> dict:
        try:
            wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        except Exception as exc:
            raise ValidationAppError("Invalid Excel file") from exc

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValidationAppError("Excel file is empty")

        headers = [str(h or "").strip().lower() for h in rows[0]]
        missing = [h for h in ("client_name", "client_code") if h not in headers]
        if missing:
            raise ValidationAppError(
                f"Missing required column(s): {', '.join(missing)}"
            )

        idx = {h: i for i, h in enumerate(headers)}
        created: list[dict] = []
        errors: list[dict] = []
        seen_codes: set[str] = set()

        for row_num, row in enumerate(rows[1:], start=2):
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue

            def cell(key: str) -> str:
                i = idx.get(key)
                if i is None or i >= len(row) or row[i] is None:
                    return ""
                return str(row[i]).strip()

            name = cell("client_name")
            code = _normalize_code(cell("client_code"))
            try:
                if not name:
                    raise ValidationAppError("Client name is required")
                if not code:
                    raise ValidationAppError("Client code / reference is required")
                if code.lower() in seen_codes:
                    raise ConflictError(f"Duplicate client code in file: {code}")
                seen_codes.add(code.lower())
                self._assert_unique_code(code)

                payload = {
                    "name": name,
                    "code": code,
                    "client_type": cell("client_type") or "Third Party",
                    "business_domain": cell("business_domain") or "Collections",
                    "industry": cell("industry") or None,
                    "ai_mode": cell("ai_mode") or "Supervised AI",
                }
                detail = self.create_client(payload)
                created.append(
                    {
                        "row": row_num,
                        "id": detail["id"],
                        "code": detail["code"],
                        "name": detail["name"],
                    }
                )
            except (ValidationAppError, ConflictError) as exc:
                errors.append({"row": row_num, "code": code or None, "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                errors.append({"row": row_num, "code": code or None, "message": str(exc)})

        return {
            "created_count": len(created),
            "error_count": len(errors),
            "created": created,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_404(self, client_id: int) -> PayflowClientModel:
        client = (
            self.db.query(PayflowClientModel)
            .options(
                joinedload(PayflowClientModel.field_mappings),
                joinedload(PayflowClientModel.portfolios),
                joinedload(PayflowClientModel.assignments)
                .joinedload(PayflowUserClientAssignmentModel.membership)
                .joinedload(PayflowUserMembershipModel.user),
                joinedload(PayflowClientModel.assignments)
                .joinedload(PayflowUserClientAssignmentModel.membership)
                .joinedload(PayflowUserMembershipModel.role),
                joinedload(PayflowClientModel.assignments)
                .joinedload(PayflowUserClientAssignmentModel.permissions)
                .joinedload(PayflowUserClientPermissionModel.permission),
            )
            .filter(PayflowClientModel.id == client_id)
            .first()
        )
        if not client:
            raise NotFoundError("Client not found")
        return client

    def _assert_unique_code(self, code: str, exclude_id: int | None = None) -> None:
        q = self.db.query(PayflowClientModel).filter(PayflowClientModel.code == code)
        if exclude_id is not None:
            q = q.filter(PayflowClientModel.id != exclude_id)
        if q.first():
            raise ConflictError("Client code / reference already exists")

    def _seed_default_mappings(self, client: PayflowClientModel) -> None:
        for i, field in enumerate(CRM_INBOUND_FIELDS):
            status, payflow_field = self._default_mapping_state(field)
            self.db.add(
                PayflowClientFieldMappingModel(
                    client_id=client.id,
                    source_field=field["source_field"],
                    payflow_field=payflow_field,
                    sample_value=field["sample_value"] or None,
                    status=status,
                    is_required=field["required"],
                    sort_order=i,
                )
            )
        self.db.flush()

    @staticmethod
    def _default_mapping_state(field: dict) -> tuple[str, str]:
        """Return (status, payflow_field) for a catalog field."""
        available = (field.get("available") or "").strip()
        payflow = field["payflow_field"]
        if available == "PayFlow-built":
            return PayflowMappingStatus.UNMAPPED.value, "— Not mapped —"
        # Open decisions / missing / partial columns → Needs Attention
        if available in ("Ambiguous", "No", "Partial"):
            if field["required"] or available == "Ambiguous":
                return PayflowMappingStatus.NEEDS_ATTENTION.value, payflow
            return PayflowMappingStatus.UNMAPPED.value, "— Not mapped —"
        if field["required"] or available in ("Yes", "Derived"):
            return PayflowMappingStatus.MAPPED.value, payflow
        return PayflowMappingStatus.UNMAPPED.value, "— Not mapped —"

    def _sync_catalog_mappings(self, client: PayflowClientModel) -> bool:
        """Align stored mappings with CRM catalog v0.3. Returns True if changed."""
        existing = {
            m.source_field: m
            for m in self.db.query(PayflowClientFieldMappingModel)
            .filter(PayflowClientFieldMappingModel.client_id == client.id)
            .all()
        }
        catalog_sources = {f["source_field"] for f in CRM_INBOUND_FIELDS}
        changed = False

        # Drop obsolete v0.1 rows not in v0.3 catalog.
        for source, row in list(existing.items()):
            if source not in catalog_sources:
                self.db.delete(row)
                existing.pop(source, None)
                changed = True

        for i, field in enumerate(CRM_INBOUND_FIELDS):
            source = field["source_field"]
            status, payflow_field = self._default_mapping_state(field)
            row = existing.get(source)
            if row is None:
                self.db.add(
                    PayflowClientFieldMappingModel(
                        client_id=client.id,
                        source_field=source,
                        payflow_field=payflow_field,
                        sample_value=field["sample_value"] or None,
                        status=status,
                        is_required=field["required"],
                        sort_order=i,
                    )
                )
                changed = True
                continue
            # Refresh catalog metadata; preserve admin Mapped/Validated choices when still valid.
            if row.is_required != field["required"]:
                row.is_required = field["required"]
                changed = True
            if row.sort_order != i:
                row.sort_order = i
                changed = True
            sample = field["sample_value"] or None
            if row.sample_value != sample:
                row.sample_value = sample
                changed = True
            # If still pointing at a removed PayFlow label, reset to catalog default.
            pf = (row.payflow_field or "").strip()
            valid_targets = set(PAYFLOW_TARGET_FIELDS)
            if pf and pf not in valid_targets:
                row.payflow_field = payflow_field
                row.status = status
                changed = True
            elif row.status == PayflowMappingStatus.UNMAPPED.value and field["required"]:
                row.payflow_field = payflow_field
                row.status = status
                changed = True

        if changed:
            self.db.flush()
        return changed

    def _replace_mappings(self, client: PayflowClientModel, mappings: list) -> None:
        if not isinstance(mappings, list):
            raise ValidationAppError("mappings must be a list")
        existing = {
            m.source_field: m
            for m in self.db.query(PayflowClientFieldMappingModel)
            .filter(PayflowClientFieldMappingModel.client_id == client.id)
            .all()
        }
        for item in mappings:
            source = str(item.get("source_field") or "").strip()
            if not source or source not in existing:
                raise ValidationAppError(f"Unknown source field: {source or '(empty)'}")
            row = existing[source]
            if "payflow_field" in item and item["payflow_field"] is not None:
                row.payflow_field = str(item["payflow_field"]).strip()
            if "sample_value" in item and item["sample_value"] is not None:
                row.sample_value = str(item["sample_value"]).strip() or None
            if "status" in item and item["status"] is not None:
                st = str(item["status"]).strip().lower()
                if st not in _MAPPING_FROM_LABEL:
                    raise ValidationAppError(f"Invalid mapping status: {item['status']}")
                row.status = _MAPPING_FROM_LABEL[st]
            else:
                # Derive status
                pf = (row.payflow_field or "").strip()
                if not pf or pf == "— Not mapped —":
                    row.status = PayflowMappingStatus.UNMAPPED.value
                else:
                    row.status = PayflowMappingStatus.MAPPED.value

    def _sync_supervisors(self, client: PayflowClientModel, user_ids: list) -> None:
        try:
            wanted = {UUID(str(u)) for u in user_ids}
        except Exception as exc:
            raise ValidationAppError("Invalid supervisor_user_ids") from exc

        supervisor_role = (
            self.db.query(PayflowRoleModel)
            .filter(PayflowRoleModel.code == PayflowRoleCode.SUPERVISOR.value)
            .first()
        )
        if not supervisor_role and wanted:
            raise ValidationAppError("Supervisor role is not configured")

        # Current assignments for this client
        current = (
            self.db.query(PayflowUserClientAssignmentModel)
            .join(PayflowUserMembershipModel)
            .filter(PayflowUserClientAssignmentModel.client_id == client.id)
            .all()
        )
        current_by_user: dict[UUID, PayflowUserClientAssignmentModel] = {}
        for a in current:
            if a.membership and a.membership.user_id:
                current_by_user[a.membership.user_id] = a

        # Remove supervisors no longer wanted (only remove supervisor-role assignments)
        for uid, assignment in list(current_by_user.items()):
            role_code = assignment.membership.role.code if assignment.membership.role else None
            if uid not in wanted and role_code == PayflowRoleCode.SUPERVISOR.value:
                self.db.delete(assignment)

        # Add missing
        for uid in wanted:
            if uid in current_by_user:
                continue
            membership = (
                self.db.query(PayflowUserMembershipModel)
                .options(joinedload(PayflowUserMembershipModel.role))
                .filter(PayflowUserMembershipModel.user_id == uid)
                .first()
            )
            if not membership:
                raise ValidationAppError(f"User {uid} has no PayFlow membership")
            if membership.role.code != PayflowRoleCode.SUPERVISOR.value:
                raise ValidationAppError(
                    "Only users with the Supervisor role can be assigned as client supervisors"
                )
            user = self.db.query(UserModel).filter(UserModel.id == uid).first()
            if not user or user.status == "disabled":
                raise ValidationAppError("Supervisor user is not available")
            assignment = PayflowUserClientAssignmentModel(
                membership_id=membership.id,
                client_id=client.id,
            )
            self.db.add(assignment)
            self.db.flush()
            # Apply role default permissions (not editable from client UI).
            role_perm_links = (
                self.db.query(PayflowRolePermissionModel)
                .filter(PayflowRolePermissionModel.payflow_role_id == membership.payflow_role_id)
                .all()
            )
            for link in role_perm_links:
                self.db.add(
                    PayflowUserClientPermissionModel(
                        assignment_id=assignment.id,
                        permission_id=link.permission_id,
                    )
                )
        self.db.flush()

    def _supervisors(self, client: PayflowClientModel) -> list[dict]:
        result = []
        for a in client.assignments or []:
            m = a.membership
            if not m or not m.user:
                continue
            role_code = m.role.code if m.role else None
            if role_code and role_code != PayflowRoleCode.SUPERVISOR.value:
                # Still show if assigned; prefer supervisors
                if role_code == PayflowRoleCode.OPERATIONS_ADMIN.value:
                    continue
            # Read-only permission view: role standard labels from assignment or role.
            perm_names: list[str] = []
            if a.permissions:
                for link in a.permissions:
                    if link.permission and link.permission.name:
                        perm_names.append(link.permission.name)
            elif m.role:
                role_links = (
                    self.db.query(PayflowRolePermissionModel)
                    .options(joinedload(PayflowRolePermissionModel.permission))
                    .filter(PayflowRolePermissionModel.payflow_role_id == m.role.id)
                    .all()
                )
                for link in role_links:
                    if link.permission and link.permission.name:
                        perm_names.append(link.permission.name)
            result.append(
                {
                    "user_id": str(m.user_id),
                    "full_name": m.user.full_name,
                    "email": m.user.email,
                    "status": m.user.status,
                    "short_name": (m.user.full_name or "").split()[0] if m.user.full_name else "",
                    "role_name": m.role.name if m.role else None,
                    "permission_names": sorted(set(perm_names)),
                }
            )
        return result

    def _mapping_summary(self, client: PayflowClientModel) -> dict:
        mappings = list(client.field_mappings or [])
        mapped = sum(
            1
            for m in mappings
            if m.status
            in (PayflowMappingStatus.MAPPED.value, PayflowMappingStatus.VALIDATED.value)
        )
        attention = sum(
            1 for m in mappings if m.status == PayflowMappingStatus.NEEDS_ATTENTION.value
        )
        unmapped = sum(
            1 for m in mappings if m.status == PayflowMappingStatus.UNMAPPED.value
        )
        required_missing = [
            m.source_field
            for m in mappings
            if m.is_required
            and m.status
            not in (PayflowMappingStatus.MAPPED.value, PayflowMappingStatus.VALIDATED.value)
        ]
        return {
            "mapped": mapped,
            "attention": attention,
            "unmapped": unmapped,
            "total": len(mappings),
            "required_missing": required_missing,
        }

    def _onboarding_progress(self, client: PayflowClientModel) -> dict:
        summary = self._mapping_summary(client)
        supervisors = self._supervisors(client)
        portfolio_count = len(client.portfolios or [])

        profile_ok = bool(client.name and client.code and client.client_type and client.business_domain)
        source_ok = (
            client.data_source_type == PayflowDataSourceType.CRM.value
            and client.connection_status == PayflowConnectionStatus.CONNECTED.value
        )
        mapping_ok = len(summary["required_missing"]) == 0 and summary["attention"] == 0
        branding_ok = bool(
            (client.channel_email or client.channel_sms)
            and (client.brand_name or client.client_type == PayflowClientType.THIRD_PARTY.value)
        )
        # Third party can use PayFlow defaults; require at least one channel
        if not (client.channel_email or client.channel_sms):
            branding_ok = False
        ai_ok = bool(client.ai_mode)
        supervisors_ok = len(supervisors) > 0
        activation_ok = client.status == PayflowClientStatus.ACTIVE.value

        def step(ok: bool, started: bool = True) -> str:
            if ok:
                return PayflowOnboardingStepStatus.COMPLETE.value
            if not started:
                return PayflowOnboardingStepStatus.PENDING.value
            return PayflowOnboardingStepStatus.INCOMPLETE.value

        steps = [
            {
                "key": "profile",
                "label": "Client Profile",
                "status": step(profile_ok, True),
            },
            {
                "key": "portfolios",
                "label": "Sub-Clients / Portfolios",
                "status": (
                    PayflowOnboardingStepStatus.COMPLETE.value
                    if portfolio_count > 0
                    else PayflowOnboardingStepStatus.PENDING.value
                ),
                "informational": True,
            },
            {
                "key": "data_source",
                "label": "Data Source",
                "status": step(source_ok, client.data_source_type is not None),
            },
            {
                "key": "data_mapping",
                "label": "Data Mapping",
                "status": step(mapping_ok, summary["total"] > 0),
            },
            {
                "key": "branding",
                "label": "Branding & Channels",
                "status": step(branding_ok, True),
            },
            {
                "key": "ai_governance",
                "label": "AI & Governance",
                "status": step(ai_ok, True),
            },
            {
                "key": "supervisors",
                "label": "Supervisors",
                "status": step(supervisors_ok, True),
            },
            {
                "key": "activation",
                "label": "Review & Activation",
                "status": (
                    PayflowOnboardingStepStatus.COMPLETE.value
                    if activation_ok
                    else (
                        PayflowOnboardingStepStatus.BLOCKED.value
                        if self._activation_blockers(client)
                        else PayflowOnboardingStepStatus.PENDING.value
                    )
                ),
            },
        ]
        required_complete = sum(
            1
            for s in steps
            if not s.get("informational")
            and s["status"] == PayflowOnboardingStepStatus.COMPLETE.value
        )
        required_total = sum(1 for s in steps if not s.get("informational"))
        return {
            "steps": steps,
            "completed_required": required_complete,
            "total_required": required_total,
            "percent": int(round((required_complete / required_total) * 100)) if required_total else 0,
            "eligible_for_activation": len(self._activation_blockers(client)) == 0,
        }

    def _activation_blockers(self, client: PayflowClientModel) -> list[str]:
        blockers: list[str] = []
        if not (client.name or "").strip():
            blockers.append("Client name is required")
        if not (client.code or "").strip():
            blockers.append("Client code is required")
        if client.data_source_type != PayflowDataSourceType.CRM.value:
            blockers.append("A primary data source has not been selected")
        elif client.connection_status != PayflowConnectionStatus.CONNECTED.value:
            blockers.append("CRM connection is not established")
        summary = self._mapping_summary(client)
        if summary["required_missing"]:
            blockers.append(
                f"{len(summary['required_missing'])} required field mapping(s) are incomplete"
            )
        if summary["attention"] > 0:
            blockers.append(f"{summary['attention']} field mapping(s) need attention")
        if not client.channel_email and not client.channel_sms:
            blockers.append("At least one communication channel must be enabled")
        if not self._supervisors(client):
            blockers.append("Assign at least one supervisor")
        return blockers

    def _list_item(self, client: PayflowClientModel) -> dict:
        # Light load supervisors without full joinedload on list — query assignments
        supervisors = []
        assignments = (
            self.db.query(PayflowUserClientAssignmentModel)
            .options(
                joinedload(PayflowUserClientAssignmentModel.membership)
                .joinedload(PayflowUserMembershipModel.user),
                joinedload(PayflowUserClientAssignmentModel.membership)
                .joinedload(PayflowUserMembershipModel.role),
            )
            .filter(PayflowUserClientAssignmentModel.client_id == client.id)
            .all()
        )
        for a in assignments:
            m = a.membership
            if not m or not m.user:
                continue
            if m.role and m.role.code == PayflowRoleCode.OPERATIONS_ADMIN.value:
                continue
            supervisors.append(
                {
                    "user_id": str(m.user_id),
                    "full_name": m.user.full_name,
                    "short_name": (m.user.full_name or "").split()[0] if m.user.full_name else "",
                }
            )

        return {
            "id": client.id,
            "code": client.code,
            "name": client.name,
            "category": client.category,
            "industry": client.category,
            "status": client.status,
            "status_label": _STATUS_LABELS.get(client.status, client.status),
            "client_type": client.client_type,
            "client_type_label": _CLIENT_TYPE_LABELS.get(client.client_type or "", client.client_type),
            "business_domain": client.business_domain,
            "business_domain_label": _DOMAIN_LABELS.get(
                client.business_domain or "", client.business_domain
            ),
            "ai_mode": client.ai_mode,
            "ai_mode_label": _AI_MODE_LABELS.get(client.ai_mode or "", client.ai_mode),
            "data_source_type": client.data_source_type,
            "connection_status": client.connection_status,
            "connection_status_label": _CONNECTION_LABELS.get(
                client.connection_status or "", client.connection_status
            ),
            "supervisors": supervisors,
            "created_at": client.created_at,
            "updated_at": client.updated_at or client.created_at,
        }

    def _detail(self, client: PayflowClientModel) -> dict:
        # Ensure relationships loaded
        client = self._get_or_404(client.id)
        summary = self._mapping_summary(client)
        supervisors = self._supervisors(client)
        progress = self._onboarding_progress(client)
        blockers = self._activation_blockers(client)

        mappings = sorted(client.field_mappings or [], key=lambda m: m.sort_order)
        portfolios = sorted(client.portfolios or [], key=lambda p: p.name.lower())

        return {
            **self._list_item(client),
            "crm_system_name": client.crm_system_name,
            "integration_ref": client.integration_ref,
            "environment": client.environment,
            "sync_frequency": client.sync_frequency,
            "brand_name": client.brand_name or "",
            "sender_name": client.sender_name or "",
            "email_from": client.email_from or "",
            "sms_sender_id": client.sms_sender_id or "",
            "channels": {
                "email": bool(client.channel_email),
                "sms": bool(client.channel_sms),
                "whatsapp": bool(client.channel_whatsapp),
            },
            "governance_rules": list(client.governance_rules or []),
            "mappings": [
                {
                    "id": m.id,
                    "source_field": m.source_field,
                    "payflow_field": m.payflow_field,
                    "sample_value": m.sample_value,
                    "status": m.status,
                    "status_label": _MAPPING_LABELS.get(m.status, m.status),
                    "is_required": m.is_required,
                    "sort_order": m.sort_order,
                }
                for m in mappings
            ],
            "mapping_summary": summary,
            "portfolios": [self._portfolio_item(p) for p in portfolios],
            "portfolio_count": len(portfolios),
            "supervisor_user_ids": [s["user_id"] for s in supervisors],
            "supervisors": supervisors,
            "onboarding": progress,
            "activation_blockers": blockers,
        }

    def _portfolio_item(self, p: PayflowPortfolioModel) -> dict:
        return {
            "id": p.id,
            "client_id": p.client_id,
            "name": p.name,
            "code": p.code,
            "status": p.status,
            "description": p.description,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
