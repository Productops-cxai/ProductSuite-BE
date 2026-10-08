"""PayFlow client onboarding service."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientFieldMappingModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowImportErrorModel,
    PayflowImportRunModel,
    PayflowNotificationModel,
    PayflowPortfolioModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowRuleModel,
    PayflowStrategyModel,
    PayflowUserClientAssignmentModel,
    PayflowUserClientPermissionModel,
    PayflowUserMembershipModel,
    UserModel,
)
from app.shared.deletion import record_deletion, snapshot_model
from app.modules.payflow.crm_catalog import (
    BULK_UPLOAD_HEADERS,
    CLIENT_IMPORT_MASTER_REQUIRED,
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
    PayflowRoleScope,
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


def _parse_client_type_soft(value: str | None) -> str:
    """Map CRM labels (Lender, Non-Commercial, …) without failing import."""
    try:
        return _parse_client_type(value)
    except ValidationAppError:
        raw = (value or "").strip().lower()
        if "first" in raw:
            return PayflowClientType.FIRST_PARTY.value
        return PayflowClientType.THIRD_PARTY.value


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "y")


_PRIMARY_CONTACT_FIELDS = (
    "crm_client_number",
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_phone",
    "address_line1",
    "address_line2",
    "city",
    "province_state",
    "country",
    "postal_code",
    "correspondence_language",
    "currency_code",
    "crm_status",
)

_BE_ROOT = Path(__file__).resolve().parents[4]
_CLIENT_UPLOADS = _BE_ROOT / "uploads" / "client-imports"


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
        client, _action = self.upsert_client(payload, commit=True)
        return self._detail(client)

    def upsert_client(
        self,
        payload: dict[str, Any],
        *,
        commit: bool = True,
        soft_client_type: bool = False,
    ) -> tuple[PayflowClientModel, str]:
        """Create or update a client. Match by crm_client_number, then code."""
        name = (payload.get("name") or "").strip()
        code = _normalize_code(payload.get("code") or "")
        crm_number = (payload.get("crm_client_number") or "").strip() or None
        if not name:
            raise ValidationAppError("Client name is required")
        if not code:
            raise ValidationAppError("Client code / reference is required")

        client = self._find_client(crm_number=crm_number, code=code)
        type_parser = _parse_client_type_soft if soft_client_type else _parse_client_type
        ds = self._normalize_data_source(payload.get("data_source_type"), allow_none=True)

        if client is None:
            self._assert_unique_code(code)
            if crm_number:
                self._assert_unique_crm_number(crm_number)
            client = PayflowClientModel(
                name=name,
                code=code,
                category=(payload.get("industry") or payload.get("category") or "").strip()
                or None,
                status=PayflowClientStatus.DRAFT.value,
                client_type=type_parser(payload.get("client_type")),
                business_domain=_parse_domain(payload.get("business_domain")),
                ai_mode=_parse_ai_mode(payload.get("ai_mode")),
                data_source_type=ds or PayflowDataSourceType.FILE.value,
                connection_status=(
                    PayflowConnectionStatus.CONNECTED.value
                    if (ds or PayflowDataSourceType.FILE.value)
                    == PayflowDataSourceType.FILE.value
                    else PayflowConnectionStatus.NOT_CONNECTED.value
                ),
                environment="Sandbox",
                sync_frequency="Every 15 minutes",
                brand_name=(payload.get("brand_name") or "").strip()
                or (payload.get("short_name") or "").strip()
                or "",
                sender_name="",
                email_from="collections@payflow.io",
                sms_sender_id="PAYFLOW",
                channel_email=True,
                channel_sms=True,
                channel_whatsapp=False,
                governance_rules=[],
                updated_at=_utcnow(),
            )
            self._apply_primary_fields(client, payload, create=True)
            self.db.add(client)
            self.db.flush()
            if ds == PayflowDataSourceType.CRM.value or ds is None:
                self._seed_default_mappings(client)
            action = "created"
        else:
            if code.lower() != (client.code or "").lower():
                self._assert_unique_code(code, exclude_id=client.id)
                client.code = code
            if crm_number and crm_number != (client.crm_client_number or ""):
                self._assert_unique_crm_number(crm_number, exclude_id=client.id)
            client.name = name
            if "industry" in payload or "category" in payload:
                industry = payload.get("industry", payload.get("category"))
                client.category = (str(industry).strip() if industry is not None else None) or None
            if payload.get("client_type") is not None:
                client.client_type = type_parser(payload.get("client_type"))
            if payload.get("business_domain") is not None:
                client.business_domain = _parse_domain(payload.get("business_domain"))
            if payload.get("ai_mode") is not None:
                client.ai_mode = _parse_ai_mode(payload.get("ai_mode"))
            if "data_source_type" in payload:
                self._set_data_source(client, ds)
            if payload.get("brand_name") is not None or payload.get("short_name"):
                client.brand_name = (
                    (payload.get("brand_name") or "").strip()
                    or (payload.get("short_name") or "").strip()
                    or client.brand_name
                )
            self._apply_primary_fields(client, payload, create=False)
            if (
                client.data_source_type == PayflowDataSourceType.CRM.value
                and not (client.field_mappings or [])
            ):
                self._seed_default_mappings(client)
            client.updated_at = _utcnow()
            action = "updated"

        if commit:
            self.db.commit()
            self.db.refresh(client)
        else:
            self.db.flush()
        return client, action

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

        if "data_source_type" in payload:
            ds = self._normalize_data_source(payload["data_source_type"], allow_none=True)
            self._set_data_source(client, ds)

        if "connection_status" in payload and payload["connection_status"] is not None:
            if client.data_source_type == PayflowDataSourceType.FILE.value:
                client.connection_status = PayflowConnectionStatus.CONNECTED.value
            else:
                cs = str(payload["connection_status"]).strip().lower().replace(" ", "_")
                label_map = {v.lower().replace(" ", "_"): k for k, v in _CONNECTION_LABELS.items()}
                label_map.update({k: k for k in _CONNECTION_LABELS})
                if cs not in label_map:
                    raise ValidationAppError("Invalid connection status")
                client.connection_status = label_map[cs]

        branding_keys = ("brand_name", "sender_name", "email_from", "sms_sender_id", "channels")
        if any(k in payload for k in branding_keys):
            self._require_draft_for_branding(client)

        for field in (
            "crm_system_name",
            "integration_ref",
            "environment",
            "sync_frequency",
            "brand_name",
            "sender_name",
            "email_from",
            "sms_sender_id",
            *_PRIMARY_CONTACT_FIELDS,
        ):
            if field in payload and payload[field] is not None:
                setattr(client, field, str(payload[field]).strip() or None)

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

        newly_assigned = []
        if "supervisor_user_ids" in payload and payload["supervisor_user_ids"] is not None:
            newly_assigned = self._sync_supervisors(client, payload["supervisor_user_ids"])

        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(client)
        if newly_assigned:
            try:
                from app.modules.payflow.services.notification_service import (
                    PayflowNotificationService,
                )

                svc = PayflowNotificationService(self.db)
                for user in newly_assigned:
                    if user:
                        svc.notify_supervisor_assignment(
                            user=user,
                            client_id=client.id,
                            client_name=client.name,
                            send_mail=True,
                        )
            except Exception:
                pass
        return self._detail(client)

    def update_logo(
        self,
        client_id: int,
        *,
        file_bytes: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> dict:
        from pathlib import Path
        import time

        client = self._get_or_404(client_id)
        self._require_draft_for_branding(client)
        if client.client_type == PayflowClientType.THIRD_PARTY.value:
            raise ValidationAppError(
                "Third Party clients use PayFlow operator branding; client logo upload is not used"
            )
        allowed = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        ext = None
        if content_type and content_type.lower() in allowed:
            ext = allowed[content_type.lower()]
        elif filename:
            lower = filename.lower()
            if lower.endswith(".jpg") or lower.endswith(".jpeg"):
                ext = ".jpg"
            elif lower.endswith(".png"):
                ext = ".png"
            elif lower.endswith(".webp"):
                ext = ".webp"
            elif lower.endswith(".gif"):
                ext = ".gif"
        if not ext:
            raise ValidationAppError("Upload a JPG, PNG, WEBP or GIF image.")
        if len(file_bytes) > 2 * 1024 * 1024:
            raise ValidationAppError("Logo must be 2 MB or smaller.")

        project_root = Path(__file__).resolve().parents[4]
        upload_root = project_root / "uploads" / "client-logos"
        upload_root.mkdir(parents=True, exist_ok=True)

        for old in upload_root.glob(f"{client.id}*"):
            try:
                old.unlink()
            except OSError:
                pass

        dest_name = f"{client.id}-{int(time.time())}{ext}"
        dest = upload_root / dest_name
        dest.write_bytes(file_bytes)
        client.logo_url = f"/uploads/client-logos/{dest_name}"
        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(client)
        return self._detail(client)

    def remove_logo(self, client_id: int) -> dict:
        from pathlib import Path

        client = self._get_or_404(client_id)
        self._require_draft_for_branding(client)
        project_root = Path(__file__).resolve().parents[4]
        upload_root = project_root / "uploads" / "client-logos"
        for old in upload_root.glob(f"{client.id}*"):
            try:
                old.unlink()
            except OSError:
                pass
        client.logo_url = None
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
            .options(
                joinedload(PayflowPortfolioModel.accounts),
                joinedload(PayflowPortfolioModel.strategies),
            )
            .filter(PayflowPortfolioModel.client_id == client_id)
            .order_by(PayflowPortfolioModel.name)
            .all()
        )
        return {"portfolios": [self._portfolio_item(p) for p in rows]}

    def create_portfolio(self, client_id: int, payload: dict[str, Any]) -> dict:
        row, _action = self.upsert_portfolio(client_id, payload, commit=True)
        return self._portfolio_item(row)

    def upsert_portfolio(
        self,
        client_id: int,
        payload: dict[str, Any],
        *,
        commit: bool = True,
    ) -> tuple[PayflowPortfolioModel, str]:
        client = self._get_or_404(client_id)
        name = (payload.get("name") or "").strip()
        code = _normalize_code(payload.get("code") or "")
        crm_number = (payload.get("crm_client_number") or "").strip() or None
        if not name:
            raise ValidationAppError("Portfolio name is required")
        if not code:
            raise ValidationAppError("Portfolio code / reference is required")

        row = self._find_portfolio(client.id, crm_number=crm_number, code=code)
        status = (payload.get("status") or PayflowPortfolioStatus.ONBOARDING.value).strip().lower()
        if status not in {s.value for s in PayflowPortfolioStatus}:
            raise ValidationAppError("Invalid portfolio status")

        if row is None:
            clash = (
                self.db.query(PayflowPortfolioModel)
                .filter(
                    PayflowPortfolioModel.client_id == client.id,
                    PayflowPortfolioModel.code == code,
                )
                .first()
            )
            if clash:
                raise ConflictError("Portfolio code already exists for this client")
            row = PayflowPortfolioModel(
                client_id=client.id,
                name=name,
                code=code,
                status=status,
                description=(payload.get("description") or "").strip() or None,
                crm_client_number=crm_number,
            )
            self.db.add(row)
            action = "created"
        else:
            if code.lower() != (row.code or "").lower():
                clash = (
                    self.db.query(PayflowPortfolioModel)
                    .filter(
                        PayflowPortfolioModel.client_id == client.id,
                        PayflowPortfolioModel.code == code,
                        PayflowPortfolioModel.id != row.id,
                    )
                    .first()
                )
                if clash:
                    raise ConflictError("Portfolio code already exists for this client")
                row.code = code
            row.name = name
            if "status" in payload and payload["status"] is not None:
                row.status = status
            if "description" in payload:
                row.description = (
                    str(payload["description"]).strip() if payload.get("description") else None
                )
            if crm_number is not None:
                row.crm_client_number = crm_number
            action = "updated"

        client.updated_at = _utcnow()
        if commit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row, action

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
        if "crm_client_number" in payload:
            row.crm_client_number = (
                str(payload["crm_client_number"]).strip() if payload.get("crm_client_number") else None
            )

        client.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(row)
        return self._portfolio_item(row)

    def _purge_account(self, account: PayflowAccountModel) -> list[dict[str, str]]:
        related: list[dict[str, str]] = []
        comms = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.account_id == account.id)
            .all()
        )
        for comm in comms:
            related.append(
                {"entity_type": "communication", "id": str(comm.id), "label": comm.code}
            )
            self.db.delete(comm)
        reviews = (
            self.db.query(PayflowHumanReviewModel)
            .filter(PayflowHumanReviewModel.account_id == account.id)
            .all()
        )
        for review in reviews:
            related.append({"entity_type": "review", "id": str(review.id), "label": review.code})
            self.db.delete(review)
        related.append(
            {
                "entity_type": "account",
                "id": str(account.id),
                "label": f"{account.customer_name} ({account.account_reference})",
            }
        )
        self.db.delete(account)
        return related

    def delete_portfolio(
        self,
        client_id: int,
        portfolio_id: int,
        *,
        actor: UserModel,
        source: str | None = None,
    ) -> dict:
        self._get_or_404(client_id)
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
        snapshot = snapshot_model(row)
        related: list[dict[str, str]] = []
        accounts = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.portfolio_id == portfolio_id)
            .all()
        )
        for account in accounts:
            related.extend(self._purge_account(account))
        strategies = (
            self.db.query(PayflowStrategyModel)
            .filter(PayflowStrategyModel.portfolio_id == portfolio_id)
            .all()
        )
        for strategy in strategies:
            related.append(
                {"entity_type": "workflow", "id": str(strategy.id), "label": strategy.name}
            )
            self.db.delete(strategy)
        record_deletion(
            self.db,
            actor=actor,
            module="payflow",
            entity_type="portfolio",
            entity_id=row.id,
            entity_label=f"{row.name} ({row.code})",
            source=source,
            record_snapshot=snapshot,
            related_deleted=related,
        )
        self.db.delete(row)
        self.db.commit()
        return {"message": "Portfolio deleted"}

    def delete_client(
        self, client_id: int, *, actor: UserModel, source: str | None = None
    ) -> dict:
        client = self._get_or_404(client_id)
        snapshot = snapshot_model(client)
        related: list[dict[str, str]] = []

        notifications = (
            self.db.query(PayflowNotificationModel)
            .filter(PayflowNotificationModel.client_id == client_id)
            .all()
        )
        for note in notifications:
            related.append({"entity_type": "notification", "id": str(note.id), "label": note.title})
            self.db.delete(note)

        assignments = (
            self.db.query(PayflowUserClientAssignmentModel)
            .filter(PayflowUserClientAssignmentModel.client_id == client_id)
            .all()
        )
        for assignment in assignments:
            perms = (
                self.db.query(PayflowUserClientPermissionModel)
                .filter(PayflowUserClientPermissionModel.assignment_id == assignment.id)
                .all()
            )
            for perm in perms:
                self.db.delete(perm)
            related.append(
                {
                    "entity_type": "supervisor_assignment",
                    "id": str(assignment.id),
                    "label": str(assignment.membership_id),
                }
            )
            self.db.delete(assignment)

        comms = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.client_id == client_id)
            .all()
        )
        for comm in comms:
            related.append(
                {"entity_type": "communication", "id": str(comm.id), "label": comm.code}
            )
            self.db.delete(comm)

        reviews = (
            self.db.query(PayflowHumanReviewModel)
            .filter(PayflowHumanReviewModel.client_id == client_id)
            .all()
        )
        for review in reviews:
            related.append({"entity_type": "review", "id": str(review.id), "label": review.code})
            self.db.delete(review)

        accounts = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.client_id == client_id)
            .all()
        )
        for account in accounts:
            related.append(
                {
                    "entity_type": "account",
                    "id": str(account.id),
                    "label": f"{account.customer_name} ({account.account_reference})",
                }
            )
            self.db.delete(account)

        strategies = (
            self.db.query(PayflowStrategyModel)
            .filter(PayflowStrategyModel.client_id == client_id)
            .all()
        )
        for strategy in strategies:
            related.append(
                {"entity_type": "workflow", "id": str(strategy.id), "label": strategy.name}
            )
            self.db.delete(strategy)

        portfolios = (
            self.db.query(PayflowPortfolioModel)
            .filter(PayflowPortfolioModel.client_id == client_id)
            .all()
        )
        for portfolio in portfolios:
            related.append(
                {"entity_type": "portfolio", "id": str(portfolio.id), "label": portfolio.name}
            )
            self.db.delete(portfolio)

        mappings = (
            self.db.query(PayflowClientFieldMappingModel)
            .filter(PayflowClientFieldMappingModel.client_id == client_id)
            .all()
        )
        for mapping in mappings:
            related.append(
                {
                    "entity_type": "field_mapping",
                    "id": str(mapping.id),
                    "label": mapping.source_field,
                }
            )
            self.db.delete(mapping)

        rules = (
            self.db.query(PayflowRuleModel)
            .filter(PayflowRuleModel.client_id == client_id)
            .all()
        )
        for rule in rules:
            related.append({"entity_type": "rule", "id": str(rule.id), "label": rule.name})
            self.db.delete(rule)

        record_deletion(
            self.db,
            actor=actor,
            module="payflow",
            entity_type="client",
            entity_id=client.id,
            entity_label=f"{client.name} ({client.code})",
            source=source,
            record_snapshot=snapshot,
            related_deleted=related,
        )
        self.db.delete(client)
        self.db.commit()
        return {"message": "Client deleted"}

    # ------------------------------------------------------------------
    # Bulk upload (CRM clients hierarchy + simple template)
    # ------------------------------------------------------------------

    def bulk_template_bytes(self) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "Clients"
        ws.append(BULK_UPLOAD_HEADERS)
        ws.append(
            [
                "2026-0001-001",
                "EX-MASTER",
                "Example Master Client",
                "EXMASTER",
                "True",
                "",
                "Consumer",
                "ops@example.com",
                "15155550100",
                "Jane Contact",
                "jane@example.com",
                "15155550100",
                "100 Main St",
                "Montreal",
                "Quebec",
                "Canada",
                "H3B1A1",
                "Financial Institutions",
                "Third Party",
                "English",
                "Canadian Dollars",
                "Enable",
            ]
        )
        ws.append(
            [
                "2026-0001-002",
                "EX-SUB-01",
                "Example Master Client",
                "EX Sub Book",
                "False",
                "2026-0001-001",
                "Loans",
                "ops@example.com",
                "15155550100",
                "",
                "",
                "",
                "100 Main St",
                "Montreal",
                "Quebec",
                "Canada",
                "H3B1A1",
                "Financial Institutions",
                "Third Party",
                "English",
                "Canadian Dollars",
                "Enable",
            ]
        )
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def bulk_validate(self, file_bytes: bytes, filename: str) -> dict:
        safe_name = (filename or "upload.xlsx").split("/")[-1]
        try:
            headers, data_rows = self._load_client_rows(file_bytes, filename)
        except ValidationAppError as exc:
            return self._bulk_preview_fail(safe_name, "File structure", exc.message)

        return self._process_client_hierarchy(
            headers, data_rows, filename=safe_name, dry_run=True, user=None
        )

    def bulk_upload(self, file_bytes: bytes, *, user: UserModel | None = None, filename: str = "upload.xlsx") -> dict:
        safe_name = (filename or "upload.xlsx").split("/")[-1]
        headers, data_rows = self._load_client_rows(file_bytes, filename)
        result = self._process_client_hierarchy(
            headers, data_rows, filename=safe_name, dry_run=False, user=user, file_bytes=file_bytes
        )
        # Back-compat shape for older FE callers
        result["created_count"] = result.get("summary", {}).get("created", 0)
        result["error_count"] = result.get("summary", {}).get("failed", 0)
        result["created"] = [
            {
                "row": p.get("record_id"),
                "id": p.get("entity_id"),
                "code": p.get("sub_client") if p.get("is_sub") else p.get("client_code"),
                "name": p.get("sub_client_name") if p.get("is_sub") else p.get("client_name"),
            }
            for p in result.get("preview", [])
            if p.get("action") in ("Create", "Update") and p.get("entity_id")
        ]
        result["errors"] = [
            {
                "row": e.get("record_id"),
                "code": e.get("sub_client") if e.get("sub_client") not in (None, "—") else e.get("client"),
                "message": e.get("error"),
            }
            for e in result.get("errors", [])
        ]
        return result

    def _load_client_rows(
        self, file_bytes: bytes, filename: str
    ) -> tuple[list[str], list[dict[str, str]]]:
        lower = (filename or "").lower()
        if lower.endswith(".csv"):
            try:
                text = file_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = file_bytes.decode("latin-1")
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames:
                raise ValidationAppError("CSV file is empty or missing headers")
            headers = [str(h or "").strip().lower() for h in reader.fieldnames]
            rows: list[dict[str, str]] = []
            for raw in reader:
                row = {
                    str(k or "").strip().lower(): ("" if v is None else str(v).strip())
                    for k, v in raw.items()
                }
                if any(row.values()):
                    rows.append(row)
            return headers, rows

        try:
            wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        except Exception as exc:
            raise ValidationAppError("Invalid Excel or CSV file") from exc
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
        if not raw_rows:
            raise ValidationAppError("Excel file is empty")
        headers = [str(h or "").strip().lower() for h in raw_rows[0]]
        rows = []
        for raw in raw_rows[1:]:
            if not raw or all(c is None or str(c).strip() == "" for c in raw):
                continue
            row = {}
            for i, h in enumerate(headers):
                if not h:
                    continue
                val = raw[i] if i < len(raw) else None
                row[h] = "" if val is None else str(val).strip()
            if any(row.values()):
                rows.append(row)
        return headers, rows

    def _is_hierarchy_format(self, headers: list[str]) -> bool:
        return "is_master_client" in headers or (
            "client_number" in headers and "client_code" in headers
        )

    def _process_client_hierarchy(
        self,
        headers: list[str],
        data_rows: list[dict[str, str]],
        *,
        filename: str,
        dry_run: bool,
        user: UserModel | None,
        file_bytes: bytes | None = None,
    ) -> dict:
        if self._is_hierarchy_format(headers):
            missing = [h for h in CLIENT_IMPORT_MASTER_REQUIRED if h not in headers]
            if missing:
                return self._bulk_preview_fail(
                    filename,
                    "File structure",
                    f"Missing required column(s): {', '.join(missing)}",
                )
            return self._process_crm_hierarchy(
                data_rows, filename=filename, dry_run=dry_run, user=user, file_bytes=file_bytes
            )

        # Legacy simple template: client_name + client_code
        if "client_name" not in headers or "client_code" not in headers:
            return self._bulk_preview_fail(
                filename,
                "File structure",
                "Expected CRM hierarchy columns (is_master_client, client_number, "
                "client_code) or legacy client_name/client_code columns.",
            )
        return self._process_simple_clients(
            data_rows, filename=filename, dry_run=dry_run, user=user, file_bytes=file_bytes
        )

    def _process_crm_hierarchy(
        self,
        data_rows: list[dict[str, str]],
        *,
        filename: str,
        dry_run: bool,
        user: UserModel | None,
        file_bytes: bytes | None,
    ) -> dict:
        preview: list[dict] = []
        errors: list[dict] = []
        created = updated = failed = 0
        preview_limit = 80
        masters_in_file: dict[str, dict[str, str]] = {}
        seen_codes: set[str] = set()
        seen_numbers: set[str] = set()

        # Pass 1 — masters
        for row_num, row in enumerate(data_rows, start=2):
            if not _truthy(row.get("is_master_client")):
                continue
            record_id = row.get("client_number") or f"Row {row_num}"
            code = _normalize_code(row.get("client_code") or "")
            number = (row.get("client_number") or "").strip()
            name = (row.get("company_name") or row.get("short_name") or "").strip()
            try:
                if not number:
                    raise ValidationAppError("client_number is required for master clients")
                if not code:
                    raise ValidationAppError("client_code is required")
                if not name:
                    raise ValidationAppError("company_name or short_name is required")
                if number.lower() in seen_numbers:
                    raise ConflictError(f"Duplicate client_number in file: {number}")
                if code.lower() in seen_codes:
                    raise ConflictError(f"Duplicate client_code in file: {code}")
                seen_numbers.add(number.lower())
                seen_codes.add(code.lower())
                masters_in_file[number.lower()] = row

                payload = self._crm_row_to_client_payload(row, data_source="crm")
                existing = self._find_client(crm_number=number, code=code)
                action = "Update" if existing else "Create"
                entity_id = existing.id if existing else None
                if not dry_run:
                    client, act = self.upsert_client(
                        payload, commit=False, soft_client_type=True
                    )
                    # CRM export ingest counts as an established file-based CRM link
                    client.connection_status = PayflowConnectionStatus.CONNECTED.value
                    entity_id = client.id
                    action = "Create" if act == "created" else "Update"
                if action == "Create":
                    created += 1
                else:
                    updated += 1
                if len(preview) < preview_limit:
                    preview.append(
                        {
                            "id": f"master-{row_num}",
                            "record_id": record_id,
                            "client": name,
                            "client_name": name,
                            "client_code": code,
                            "sub_client": "—",
                            "sub_client_name": "—",
                            "action": action,
                            "note": number,
                            "is_sub": False,
                            "entity_id": entity_id,
                        }
                    )
            except (ValidationAppError, ConflictError) as exc:
                failed += 1
                errors.append(
                    {
                        "record_id": record_id,
                        "client": name or "—",
                        "sub_client": "—",
                        "field": "client_number",
                        "error": exc.message,
                        "status": "Skipped" if "Duplicate" in exc.message else "Rejected",
                    }
                )

        # Resolve masters already in DB for children whose parent is not in this file
        db_masters_by_number = {
            (c.crm_client_number or "").strip().lower(): c
            for c in self.db.query(PayflowClientModel).all()
            if (c.crm_client_number or "").strip()
        }

        # Pass 2 — sub-clients
        for row_num, row in enumerate(data_rows, start=2):
            if _truthy(row.get("is_master_client")):
                continue
            record_id = row.get("client_number") or f"Row {row_num}"
            code = _normalize_code(row.get("client_code") or "")
            number = (row.get("client_number") or "").strip()
            master_number = (row.get("master_client__client_number") or "").strip()
            name = (
                row.get("short_name")
                or row.get("product")
                or row.get("company_name")
                or ""
            ).strip()
            try:
                if not master_number:
                    raise ValidationAppError(
                        "master_client__client_number is required for sub-clients"
                    )
                if not code:
                    raise ValidationAppError("client_code is required")
                if not name:
                    raise ValidationAppError("short_name, product, or company_name is required")
                if number and number.lower() in seen_numbers:
                    raise ConflictError(f"Duplicate client_number in file: {number}")
                if code.lower() in seen_codes:
                    raise ConflictError(f"Duplicate client_code in file: {code}")
                if number:
                    seen_numbers.add(number.lower())
                seen_codes.add(code.lower())

                parent = self._find_client(crm_number=master_number, code=None)
                if parent is None:
                    parent = db_masters_by_number.get(master_number.lower())
                master_row = masters_in_file.get(master_number.lower())
                if parent is None and master_row is None:
                    raise ValidationAppError(
                        f"Unknown master client_number: {master_number}"
                    )

                parent_name = (
                    parent.name
                    if parent
                    else (
                        master_row.get("company_name")
                        or master_row.get("short_name")
                        or master_number
                    )
                )
                parent_code = (
                    parent.code
                    if parent
                    else _normalize_code(master_row.get("client_code") or "")
                )

                existing = (
                    self._find_portfolio(parent.id, crm_number=number or None, code=code)
                    if parent
                    else None
                )
                action = "Update" if existing else "Create"
                entity_id = existing.id if existing else None
                if not dry_run:
                    if parent is None:
                        # Master should have been upserted in pass 1
                        parent = self._find_client(crm_number=master_number, code=None)
                    if parent is None:
                        raise ValidationAppError(
                            f"Unknown master client_number: {master_number}"
                        )
                    port, act = self.upsert_portfolio(
                        parent.id,
                        {
                            "name": name,
                            "code": code,
                            "crm_client_number": number or None,
                            "description": (row.get("product") or "").strip() or None,
                            "status": PayflowPortfolioStatus.ONBOARDING.value,
                        },
                        commit=False,
                    )
                    entity_id = port.id
                    action = "Create" if act == "created" else "Update"
                    db_masters_by_number[master_number.lower()] = parent
                    parent_name = parent.name
                    parent_code = parent.code

                if action == "Create":
                    created += 1
                else:
                    updated += 1
                if len(preview) < preview_limit:
                    preview.append(
                        {
                            "id": f"sub-{row_num}",
                            "record_id": record_id,
                            "client": parent_name,
                            "client_name": parent_name,
                            "client_code": parent_code or "—",
                            "sub_client": code,
                            "sub_client_name": name,
                            "action": action,
                            "note": number or code,
                            "is_sub": True,
                            "entity_id": entity_id,
                        }
                    )
            except (ValidationAppError, ConflictError) as exc:
                failed += 1
                errors.append(
                    {
                        "record_id": record_id,
                        "client": master_number or "—",
                        "sub_client": code or name or "—",
                        "field": "master_client__client_number"
                        if not master_number
                        else "client_code",
                        "error": exc.message,
                        "status": "Skipped" if "Duplicate" in exc.message else "Rejected",
                    }
                )

        if not dry_run:
            run = self._persist_client_import_run(
                user=user,
                filename=filename,
                file_bytes=file_bytes,
                created=created,
                updated=updated,
                failed=failed,
                errors=errors,
            )
            self.db.commit()
            return {
                "ok": True,
                "file_name": filename,
                "status": run.status if run else ("Completed with Errors" if failed else "Completed"),
                "import_id": run.id if run else None,
                "message": None,
                "summary": {
                    "total": created + updated + failed,
                    "created": created,
                    "updated": updated,
                    "unchanged": 0,
                    "failed": failed,
                    "new_clients": created,
                    "existing_clients": updated,
                },
                "preview": preview,
                "errors": errors,
            }

        return {
            "ok": True,
            "file_name": filename,
            "status": "Ready",
            "message": None,
            "summary": {
                "total": created + updated + failed,
                "created": created,
                "updated": updated,
                "unchanged": 0,
                "failed": failed,
                "new_clients": created,
                "existing_clients": updated,
            },
            "preview": preview,
            "errors": errors,
        }

    def _process_simple_clients(
        self,
        data_rows: list[dict[str, str]],
        *,
        filename: str,
        dry_run: bool,
        user: UserModel | None,
        file_bytes: bytes | None,
    ) -> dict:
        preview: list[dict] = []
        errors: list[dict] = []
        created = updated = failed = 0
        seen_codes: set[str] = set()
        for row_num, row in enumerate(data_rows, start=2):
            name = (row.get("client_name") or row.get("company_name") or "").strip()
            code = _normalize_code(row.get("client_code") or "")
            record_id = f"Row {row_num}"
            try:
                if not name:
                    raise ValidationAppError("Client name is required")
                if not code:
                    raise ValidationAppError("Client code / reference is required")
                if code.lower() in seen_codes:
                    raise ConflictError(f"Duplicate client code in file: {code}")
                seen_codes.add(code.lower())
                existing = self._find_client(crm_number=None, code=code)
                action = "Update" if existing else "Create"
                entity_id = existing.id if existing else None
                if not dry_run:
                    client, act = self.upsert_client(
                        {
                            "name": name,
                            "code": code,
                            "client_type": row.get("client_type") or "Third Party",
                            "business_domain": row.get("business_domain") or "Collections",
                            "industry": row.get("industry") or None,
                            "ai_mode": row.get("ai_mode") or "Supervised AI",
                            "data_source_type": "file",
                        },
                        commit=False,
                        soft_client_type=True,
                    )
                    entity_id = client.id
                    action = "Create" if act == "created" else "Update"
                if action == "Create":
                    created += 1
                else:
                    updated += 1
                if len(preview) < 50:
                    preview.append(
                        {
                            "id": f"row-{row_num}",
                            "record_id": record_id,
                            "client": name,
                            "client_name": name,
                            "client_code": code,
                            "sub_client": "—",
                            "sub_client_name": "—",
                            "action": action,
                            "note": code,
                            "is_sub": False,
                            "entity_id": entity_id,
                        }
                    )
            except (ValidationAppError, ConflictError) as exc:
                failed += 1
                errors.append(
                    {
                        "record_id": record_id,
                        "client": name or "—",
                        "sub_client": "—",
                        "field": "client_code",
                        "error": exc.message,
                        "status": "Skipped" if "Duplicate" in exc.message else "Rejected",
                    }
                )

        if not dry_run:
            run = self._persist_client_import_run(
                user=user,
                filename=filename,
                file_bytes=file_bytes,
                created=created,
                updated=updated,
                failed=failed,
                errors=errors,
            )
            self.db.commit()
            return {
                "ok": True,
                "file_name": filename,
                "status": run.status if run else ("Completed with Errors" if failed else "Completed"),
                "import_id": run.id if run else None,
                "message": None,
                "summary": {
                    "total": created + updated + failed,
                    "created": created,
                    "updated": updated,
                    "unchanged": 0,
                    "failed": failed,
                    "new_clients": created,
                    "existing_clients": updated,
                },
                "preview": preview,
                "errors": errors,
            }

        return {
            "ok": True,
            "file_name": filename,
            "status": "Ready",
            "message": None,
            "summary": {
                "total": created + updated + failed,
                "created": created,
                "updated": updated,
                "unchanged": 0,
                "failed": failed,
                "new_clients": created,
                "existing_clients": updated,
            },
            "preview": preview,
            "errors": errors,
        }

    def _crm_row_to_client_payload(self, row: dict[str, str], *, data_source: str) -> dict[str, Any]:
        email = (
            row.get("email_address")
            or row.get("clientdemographiccontactinformation__email_address")
            or ""
        ).strip()
        phone = (
            row.get("phone_number")
            or row.get("cell_number")
            or row.get("clientdemographiccontactinformation__phone_number")
            or ""
        ).strip()
        contact_name = (
            row.get("clientdemographiccontactinformation__full_name") or ""
        ).strip()
        contact_title = (
            row.get("clientdemographiccontactinformation__contact_title") or ""
        ).strip()
        number = (row.get("client_number") or "").strip()
        return {
            "name": (row.get("company_name") or row.get("short_name") or "").strip(),
            "code": _normalize_code(row.get("client_code") or ""),
            "short_name": (row.get("short_name") or "").strip() or None,
            "brand_name": (row.get("short_name") or "").strip() or None,
            "industry": (row.get("client_industry__name") or "").strip() or None,
            "client_type": row.get("client_type__name") or "Third Party",
            "business_domain": "Collections",
            "ai_mode": "Supervised AI",
            "data_source_type": data_source,
            "crm_client_number": number or None,
            "integration_ref": number or None,
            "crm_system_name": "CRM",
            "contact_name": contact_name or None,
            "contact_title": contact_title or None,
            "contact_email": email or None,
            "contact_phone": phone or None,
            "address_line1": (row.get("address_line_1") or "").strip() or None,
            "address_line2": (row.get("address_line_2") or "").strip() or None,
            "city": (row.get("city__name") or "").strip() or None,
            "province_state": (row.get("province__name") or "").strip() or None,
            "country": (row.get("country__name") or "").strip() or None,
            "postal_code": (row.get("zip_code") or "").strip() or None,
            "correspondence_language": (
                row.get("correspondence_language__name") or ""
            ).strip()
            or None,
            "currency_code": (row.get("currency__name") or "").strip() or None,
            "crm_status": (row.get("client_status__name") or "").strip() or None,
        }

    def _persist_client_import_run(
        self,
        *,
        user: UserModel | None,
        filename: str,
        file_bytes: bytes | None,
        created: int,
        updated: int,
        failed: int,
        errors: list[dict],
    ) -> PayflowImportRunModel | None:
        stored_path = None
        if file_bytes:
            _CLIENT_UPLOADS.mkdir(parents=True, exist_ok=True)
            safe = Path(filename).name
            dest = _CLIENT_UPLOADS / f"{int(_utcnow().timestamp())}-{safe}"
            dest.write_bytes(file_bytes)
            try:
                stored_path = str(dest.relative_to(_BE_ROOT)).replace("\\", "/")
            except ValueError:
                stored_path = str(dest)

        if created or updated:
            status = "Completed with Errors" if failed else "Completed"
        else:
            status = "Failed"

        run = PayflowImportRunModel(
            kind="client",
            file_name=filename,
            stored_path=stored_path,
            uploaded_by_user_id=user.id if user else None,
            uploaded_by_name=(user.full_name if user else None) or (user.email if user else "System"),
            status=status,
            total_count=created + updated + failed,
            created_count=created,
            updated_count=updated,
            unchanged_count=0,
            failed_count=failed,
            completed_at=_utcnow(),
        )
        self.db.add(run)
        self.db.flush()
        for err in errors:
            self.db.add(
                PayflowImportErrorModel(
                    run_id=run.id,
                    record_id=str(err.get("record_id") or "—"),
                    client=str(err.get("client") or "—"),
                    sub_client=str(err.get("sub_client") or "—"),
                    field=str(err.get("field") or "—"),
                    message=str(err.get("error") or "Error"),
                    status=str(err.get("status") or "Rejected"),
                )
            )
        return run

    @staticmethod
    def _bulk_preview_fail(file_name: str, field: str, message: str) -> dict:
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_404(self, client_id: int) -> PayflowClientModel:
        client = (
            self.db.query(PayflowClientModel)
            .options(
                joinedload(PayflowClientModel.field_mappings),
                joinedload(PayflowClientModel.portfolios).joinedload(PayflowPortfolioModel.accounts),
                joinedload(PayflowClientModel.portfolios).joinedload(PayflowPortfolioModel.strategies),
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

    def _assert_unique_crm_number(self, crm_number: str, exclude_id: int | None = None) -> None:
        q = self.db.query(PayflowClientModel).filter(
            PayflowClientModel.crm_client_number == crm_number
        )
        if exclude_id is not None:
            q = q.filter(PayflowClientModel.id != exclude_id)
        if q.first():
            raise ConflictError("CRM client number already linked to another client")

    def _find_client(
        self, *, crm_number: str | None, code: str | None
    ) -> PayflowClientModel | None:
        if crm_number:
            row = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.crm_client_number == crm_number)
                .first()
            )
            if row:
                return row
        if code:
            return (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.code == code)
                .first()
            )
        return None

    def _find_portfolio(
        self,
        client_id: int,
        *,
        crm_number: str | None,
        code: str | None,
    ) -> PayflowPortfolioModel | None:
        if crm_number:
            row = (
                self.db.query(PayflowPortfolioModel)
                .filter(
                    PayflowPortfolioModel.client_id == client_id,
                    PayflowPortfolioModel.crm_client_number == crm_number,
                )
                .first()
            )
            if row:
                return row
        if code:
            return (
                self.db.query(PayflowPortfolioModel)
                .filter(
                    PayflowPortfolioModel.client_id == client_id,
                    PayflowPortfolioModel.code == code,
                )
                .first()
            )
        return None

    @staticmethod
    def _normalize_data_source(value: Any, *, allow_none: bool = False) -> str | None:
        if value is None or value == "":
            if allow_none:
                return None
            raise ValidationAppError("Data source is required")
        ds = str(value).strip().lower()
        if ds in ("crm", PayflowDataSourceType.CRM.value):
            return PayflowDataSourceType.CRM.value
        if ds in ("file", "daily_file", "file_only", PayflowDataSourceType.FILE.value):
            return PayflowDataSourceType.FILE.value
        raise ValidationAppError("Data source must be CRM or File")

    def _set_data_source(self, client: PayflowClientModel, ds: str | None) -> None:
        prev = client.data_source_type
        client.data_source_type = ds
        if ds is None:
            client.connection_status = PayflowConnectionStatus.NOT_CONNECTED.value
        elif ds == PayflowDataSourceType.FILE.value:
            client.connection_status = PayflowConnectionStatus.CONNECTED.value
        elif ds == PayflowDataSourceType.CRM.value and prev != PayflowDataSourceType.CRM.value:
            client.connection_status = PayflowConnectionStatus.NOT_CONNECTED.value

    def _apply_primary_fields(
        self, client: PayflowClientModel, payload: dict[str, Any], *, create: bool
    ) -> None:
        for field in _PRIMARY_CONTACT_FIELDS:
            if field not in payload:
                continue
            val = payload.get(field)
            if val is None and not create:
                continue
            setattr(client, field, (str(val).strip() if val is not None else None) or None)
        if payload.get("integration_ref") is not None:
            client.integration_ref = str(payload["integration_ref"]).strip() or None
        elif create and payload.get("crm_client_number"):
            client.integration_ref = str(payload["crm_client_number"]).strip()
        if payload.get("crm_system_name") is not None:
            client.crm_system_name = str(payload["crm_system_name"]).strip() or None

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

    def _sync_supervisors(self, client: PayflowClientModel, user_ids: list) -> list:
        try:
            wanted = {UUID(str(u)) for u in user_ids}
        except Exception as exc:
            raise ValidationAppError("Invalid supervisor_user_ids") from exc

        newly_assigned: list = []

        # Current assignments for this client
        current = (
            self.db.query(PayflowUserClientAssignmentModel)
            .join(PayflowUserMembershipModel)
            .options(joinedload(PayflowUserClientAssignmentModel.membership).joinedload(
                PayflowUserMembershipModel.role
            ))
            .filter(PayflowUserClientAssignmentModel.client_id == client.id)
            .all()
        )
        current_by_user: dict[UUID, PayflowUserClientAssignmentModel] = {}
        for a in current:
            if a.membership and a.membership.user_id:
                current_by_user[a.membership.user_id] = a

        # Remove client-scoped users no longer wanted (never touch platform-wide admins).
        for uid, assignment in list(current_by_user.items()):
            role = assignment.membership.role if assignment.membership else None
            scope = role.scope if role else None
            if uid not in wanted and scope == PayflowRoleScope.CLIENT_SCOPED.value:
                self.db.delete(assignment)

        # Add missing — any client-scoped role (Supervisor or custom).
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
            if not membership.role or membership.role.scope != PayflowRoleScope.CLIENT_SCOPED.value:
                raise ValidationAppError(
                    "Only client-scoped users can be assigned to a client"
                )
            user = self.db.query(UserModel).filter(UserModel.id == uid).first()
            if not user or user.status == "disabled":
                raise ValidationAppError("User is not available for client assignment")
            assignment = PayflowUserClientAssignmentModel(
                membership_id=membership.id,
                client_id=client.id,
            )
            self.db.add(assignment)
            self.db.flush()
            # Permissions come live from the role — no per-assignment snapshot.
            newly_assigned.append(user)
        self.db.flush()
        return newly_assigned

    def _supervisors(self, client: PayflowClientModel) -> list[dict]:
        result = []
        for a in client.assignments or []:
            m = a.membership
            if not m or not m.user:
                continue
            if m.role and m.role.scope == PayflowRoleScope.PLATFORM_WIDE.value:
                continue
            # Always show current role permissions (Edit access updates apply immediately).
            perm_names: list[str] = []
            if m.role:
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
        supervisors = self._supervisors(client)
        portfolio_count = len(client.portfolios or [])

        profile_ok = bool(client.name and client.code and client.client_type and client.business_domain)
        # Clients use daily file intake; CRM field mapping is global (System Mapping).
        is_file = client.data_source_type == PayflowDataSourceType.FILE.value
        is_crm = client.data_source_type == PayflowDataSourceType.CRM.value
        source_ok = is_file or (
            is_crm and client.connection_status == PayflowConnectionStatus.CONNECTED.value
        )
        branding_ok = len(self._branding_issues(client)) == 0
        ai_ok = bool(client.ai_mode)
        supervisors_ok = len(supervisors) > 0
        activation_ok = client.status == PayflowClientStatus.ACTIVE.value
        mapping = self._mapping_summary(client)
        mapped = int(mapping.get("mapped") or 0)
        unmapped = int(mapping.get("unmapped") or 0)
        attention = int(mapping.get("attention") or 0)
        total_fields = int(mapping.get("total") or 0)
        # Empty catalog → treat as complete; otherwise all required must be clean.
        mapping_ok = total_fields == 0 or (
            unmapped == 0 and attention == 0 and mapped > 0
        )

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
                "status": step(mapping_ok, total_fields > 0 or is_crm),
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
                "label": "Supervisor Assignment",
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

    def _branding_issues(self, client: PayflowClientModel) -> list[str]:
        """Required branding/channel fields by client type (AC5)."""
        issues: list[str] = []
        if not client.channel_email and not client.channel_sms:
            issues.append("At least one communication channel must be enabled")
        is_first = client.client_type == PayflowClientType.FIRST_PARTY.value
        brand = (client.brand_name or "").strip()
        if is_first:
            if not brand:
                issues.append("Display / brand name is required for First Party clients")
            if not (client.sender_name or "").strip():
                issues.append("Sender name is required for First Party clients")
            if client.channel_email and not (client.email_from or "").strip():
                issues.append("Email from address is required when Email is enabled")
            if client.channel_sms and not (client.sms_sender_id or "").strip():
                issues.append("SMS sender ID is required when SMS is enabled")
        elif not brand and not (client.name or "").strip():
            issues.append(
                "Client display / reference name is required for Third Party clients"
            )
        return issues

    def _require_draft_for_branding(self, client: PayflowClientModel) -> None:
        if client.status != PayflowClientStatus.DRAFT.value:
            raise ValidationAppError(
                "Branding & channels can only be updated while the client is in Draft status"
            )

    def _activation_blockers(self, client: PayflowClientModel) -> list[str]:
        blockers: list[str] = []
        if not (client.name or "").strip():
            blockers.append("Client name is required")
        if not (client.code or "").strip():
            blockers.append("Client code is required")
        # Phase-1 client intake is daily file; CRM→PayFlow mapping is system-wide.
        if client.data_source_type not in (
            PayflowDataSourceType.FILE.value,
            PayflowDataSourceType.CRM.value,
        ):
            blockers.append("A primary data source has not been selected")
        blockers.extend(self._branding_issues(client))
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

        progress = self._onboarding_progress(client)
        # Match Lovable / FE banner chips (exclude AI & activation / portfolios).
        _setup_chip_keys = {
            "profile",
            "data_source",
            "data_mapping",
            "branding",
            "supervisors",
        }
        incomplete = [
            s["label"]
            for s in progress["steps"]
            if s.get("key") in _setup_chip_keys
            and s.get("status") != PayflowOnboardingStepStatus.COMPLETE.value
        ]
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
            "setup_incomplete": incomplete,
            "setup_steps_remaining": len(incomplete),
            "onboarding": progress,
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
            "crm_client_number": client.crm_client_number,
            "contact_name": client.contact_name,
            "contact_title": client.contact_title,
            "contact_email": client.contact_email,
            "contact_phone": client.contact_phone,
            "address_line1": client.address_line1,
            "address_line2": client.address_line2,
            "city": client.city,
            "province_state": client.province_state,
            "country": client.country,
            "postal_code": client.postal_code,
            "correspondence_language": client.correspondence_language,
            "currency_code": client.currency_code,
            "crm_status": client.crm_status,
            "environment": client.environment,
            "sync_frequency": client.sync_frequency,
            "brand_name": client.brand_name or "",
            "logo_url": client.logo_url,
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
        accounts = list(p.accounts or [])
        account_count = len(accounts)
        case_count = account_count  # one collection case per account in Phase 1
        outstanding = float(sum(float(a.outstanding_balance or 0) for a in accounts))
        active_strategy = None
        for s in p.strategies or []:
            if (s.status or "").lower() in ("active", "approved"):
                active_strategy = s
                break
        if active_strategy is None and (p.strategies or []):
            # Prefer most recently updated strategy for display if none active
            active_strategy = max(
                p.strategies,
                key=lambda s: s.updated_at or s.created_at,
            )
            if (active_strategy.status or "").lower() not in ("active", "approved"):
                active_strategy = None

        return {
            "id": p.id,
            "client_id": p.client_id,
            "name": p.name,
            "code": p.code,
            "status": p.status,
            "status_label": {
                PayflowPortfolioStatus.ONBOARDING.value: "Onboarding",
                PayflowPortfolioStatus.ACTIVE.value: "Active",
                PayflowPortfolioStatus.PAUSED.value: "Paused",
            }.get(p.status, p.status),
            "description": p.description,
            "crm_client_number": p.crm_client_number,
            "account_count": account_count,
            "case_count": case_count,
            "outstanding": outstanding,
            "active_strategy_id": active_strategy.id if active_strategy else None,
            "active_strategy_name": active_strategy.name if active_strategy else None,
            "last_file_received": "No file received yet",
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }

    def get_portfolio(self, client_id: int, portfolio_id: int) -> dict:
        client = self._get_or_404(client_id)
        portfolio = (
            self.db.query(PayflowPortfolioModel)
            .options(
                joinedload(PayflowPortfolioModel.accounts),
                joinedload(PayflowPortfolioModel.strategies),
            )
            .filter(
                PayflowPortfolioModel.id == portfolio_id,
                PayflowPortfolioModel.client_id == client_id,
            )
            .first()
        )
        if not portfolio:
            raise NotFoundError("Portfolio not found")
        strategies = sorted(
            portfolio.strategies or [],
            key=lambda s: s.updated_at or s.created_at,
            reverse=True,
        )
        return {
            **self._portfolio_item(portfolio),
            "client_name": client.name,
            "client_code": client.code,
            "strategies": [
                {
                    "id": s.id,
                    "name": s.name,
                    "code": s.code,
                    "status": s.status,
                    "origin": s.origin,
                    "version": s.version,
                    "updated_at": s.updated_at or s.created_at,
                }
                for s in strategies
            ],
        }
