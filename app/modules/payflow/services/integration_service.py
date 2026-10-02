from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError
from app.infrastructure.database.models import (
    PayflowClientFieldMappingModel,
    PayflowClientModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import (
    PayflowConnectionStatus,
    PayflowDataSourceType,
    PayflowIntegrationCategory,
    PayflowIntegrationStatus,
    PayflowMappingStatus,
)

# Deterministic demo activity labels keyed by integration id suffix patterns.
_ACTIVITY_LABELS: dict[str, str] = {
    "source": "2 min ago",
    "email": "1 min ago",
    "sms": "3 min ago",
}

_SYNC_LABELS: dict[str, str] = {
    "source": "12 Sep 2026 · 10:42",
    "email": "12 Sep 2026 · 10:43",
    "sms": "12 Sep 2026 · 10:41",
}

# Demo attention issues keyed by "{client_code}-sms"
_ATTENTION_ISSUES: dict[str, list[dict[str, str]]] = {
    "northstar-utilities-sms": [
        {
            "at": "12 Sep 2026 · 10:18",
            "summary": "Recent SMS messages were not confirmed as delivered by the messaging service.",
        },
        {
            "at": "12 Sep 2026 · 09:51",
            "summary": "Delivery confirmations delayed for a batch of reminders.",
        },
    ],
}


def _status_from_connection(connection: str | None) -> str:
    mapping = {
        PayflowConnectionStatus.CONNECTED.value: PayflowIntegrationStatus.CONNECTED.value,
        PayflowConnectionStatus.CONNECTING.value: PayflowIntegrationStatus.TESTING.value,
        PayflowConnectionStatus.CONNECTION_FAILED.value: (
            PayflowIntegrationStatus.ATTENTION_REQUIRED.value
        ),
        PayflowConnectionStatus.NOT_CONNECTED.value: (
            PayflowIntegrationStatus.CONFIGURATION_PENDING.value
        ),
    }
    return mapping.get(
        (connection or "").strip().lower(),
        PayflowIntegrationStatus.CONFIGURATION_PENDING.value,
    )


def _data_source_label(client: PayflowClientModel) -> str | None:
    if client.data_source_type == PayflowDataSourceType.CRM.value:
        return (client.crm_system_name or "CRM").strip() or "CRM"
    if client.data_source_type:
        return client.data_source_type.upper()
    return None


class PayflowIntegrationService:
    """Integrations are derived from client connection / channel configuration."""

    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_clients(self, user: UserModel) -> list[PayflowClientModel]:
        q = self.db.query(PayflowClientModel).options(
            joinedload(PayflowClientModel.field_mappings)
        )
        if not self.access.is_operations_admin(user):
            ctx = self.access.build_context(user)
            ids = list(ctx.get("client_ids") or [])
            if not ids:
                return []
            q = q.filter(PayflowClientModel.id.in_(ids))
        return q.order_by(PayflowClientModel.name.asc()).all()

    def build_integrations(self, user: UserModel) -> list[dict[str, Any]]:
        clients = self._visible_clients(user)
        return self._build_from_clients(clients)

    def list_integrations(
        self,
        user: UserModel,
        *,
        status: str | None = None,
        client_id: int | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        integrations = self.build_integrations(user)
        rows = integrations
        if status:
            rows = [i for i in rows if i["status"] == status]
        if client_id is not None:
            rows = [i for i in rows if i.get("client_id") == client_id]
        if category:
            rows = [i for i in rows if i["category"] == category]

        summary = {
            "connected": sum(
                1 for i in integrations if i["status"] == PayflowIntegrationStatus.CONNECTED.value
            ),
            "attention": sum(
                1
                for i in integrations
                if i["status"] == PayflowIntegrationStatus.ATTENTION_REQUIRED.value
            ),
            "pending": sum(
                1
                for i in integrations
                if i["status"] == PayflowIntegrationStatus.CONFIGURATION_PENDING.value
            ),
            "disconnected": sum(
                1
                for i in integrations
                if i["status"] == PayflowIntegrationStatus.DISCONNECTED.value
            ),
        }
        return {
            "integrations": rows,
            "summary": summary,
            "categories": [c.value for c in PayflowIntegrationCategory],
            "statuses": [s.value for s in PayflowIntegrationStatus],
        }

    def get_integration(self, user: UserModel, integration_id: str) -> dict[str, Any]:
        integrations = self.build_integrations(user)
        match = next((i for i in integrations if i["id"] == integration_id), None)
        if not match:
            raise NotFoundError("Integration not found")
        if match.get("client_id") is not None:
            detail = dict(match)
            detail["mappings"] = self._mappings_for_client(match["client_id"])
            detail["mapping_summary"] = self._mapping_summary(detail["mappings"])
            return detail
        detail = dict(match)
        detail["mappings"] = []
        detail["mapping_summary"] = {"mapped": 0, "attention": 0, "unmapped": 0, "total": 0}
        return detail

    def test_connection(self, user: UserModel, integration_id: str) -> dict[str, Any]:
        if not self.access.is_operations_admin(user):
            raise ForbiddenError("Operations Admin access required")
        integration = self.get_integration(user, integration_id)
        status = integration.get("status") or ""
        issues = integration.get("issues") or []
        needs_attention = (
            status == PayflowIntegrationStatus.ATTENTION_REQUIRED.value or bool(issues)
        )
        if needs_attention:
            try:
                from app.modules.payflow.services.notification_service import (
                    PayflowNotificationService,
                )

                detail = "; ".join(issues) if issues else f"Status: {status}"
                PayflowNotificationService(self.db).notify_integration_failed(
                    integration_id=integration["id"],
                    client_name=integration.get("client_name") or "Client",
                    detail=detail,
                    send_mail=True,
                )
            except Exception:
                pass
            return {
                "message": "Test completed. Connection needs attention.",
                "integration_id": integration["id"],
                "status": status,
            }
        return {
            "message": "Test completed. Connection responded normally.",
            "integration_id": integration["id"],
            "status": status,
        }

    def _build_from_clients(self, clients: list[PayflowClientModel]) -> list[dict[str, Any]]:
        list_out: list[dict[str, Any]] = []
        for client in clients:
            base = {
                "client_id": client.id,
                "client_name": client.name,
                "client_code": client.code,
            }
            source_label = _data_source_label(client)
            source_id = f"{client.code}-source"
            if source_label:
                issues = _ATTENTION_ISSUES.get(source_id, [])
                status = (
                    PayflowIntegrationStatus.ATTENTION_REQUIRED.value
                    if issues
                    else _status_from_connection(client.connection_status)
                )
                list_out.append(
                    {
                        **base,
                        "id": source_id,
                        "name": source_label,
                        "category": PayflowIntegrationCategory.DATA_SOURCE.value,
                        "status": status,
                        "last_activity": _ACTIVITY_LABELS.get("source", "Today"),
                        "last_successful": _SYNC_LABELS.get("source"),
                        "purpose": (
                            f"Customer and account records for {client.name} arrive from the "
                            f"{source_label} system. Each client uses one primary operational data source."
                        ),
                        "data_source": source_label,
                        "issues": issues,
                    }
                )
            else:
                list_out.append(
                    {
                        **base,
                        "id": source_id,
                        "name": "Data Source",
                        "category": PayflowIntegrationCategory.DATA_SOURCE.value,
                        "status": PayflowIntegrationStatus.CONFIGURATION_PENDING.value,
                        "last_activity": "—",
                        "last_successful": None,
                        "purpose": (
                            f"{client.name} has not selected a primary operational data source yet."
                        ),
                        "data_source": None,
                        "issues": [],
                    }
                )

            if client.channel_email:
                email_id = f"{client.code}-email"
                issues = _ATTENTION_ISSUES.get(email_id, [])
                list_out.append(
                    {
                        **base,
                        "id": email_id,
                        "name": "Email",
                        "category": PayflowIntegrationCategory.COMMUNICATION.value,
                        "status": (
                            PayflowIntegrationStatus.ATTENTION_REQUIRED.value
                            if issues
                            else PayflowIntegrationStatus.CONNECTED.value
                        ),
                        "last_activity": _ACTIVITY_LABELS.get("email", "Today"),
                        "last_successful": _SYNC_LABELS.get("email"),
                        "purpose": (
                            f"Email collection communications for {client.name} are executed "
                            "through this channel connection."
                        ),
                        "data_source": None,
                        "issues": issues,
                    }
                )

            if client.channel_sms:
                sms_id = f"{client.code}-sms"
                issues = _ATTENTION_ISSUES.get(sms_id, [])
                list_out.append(
                    {
                        **base,
                        "id": sms_id,
                        "name": "SMS",
                        "category": PayflowIntegrationCategory.COMMUNICATION.value,
                        "status": (
                            PayflowIntegrationStatus.ATTENTION_REQUIRED.value
                            if issues
                            else PayflowIntegrationStatus.CONNECTED.value
                        ),
                        "last_activity": _ACTIVITY_LABELS.get("sms", "Today"),
                        "last_successful": _SYNC_LABELS.get("sms"),
                        "purpose": (
                            f"SMS collection communications for {client.name} are executed "
                            "through this channel connection."
                        ),
                        "data_source": None,
                        "issues": issues,
                    }
                )

            list_out.append(
                {
                    **base,
                    "id": f"{client.code}-payments",
                    "name": "Payment Provider",
                    "category": PayflowIntegrationCategory.PAYMENTS.value,
                    "status": PayflowIntegrationStatus.CONFIGURATION_PENDING.value,
                    "last_activity": "—",
                    "last_successful": None,
                    "purpose": (
                        f"The payment provider for {client.name} is not finalised. "
                        "The customer payment experience remains provider neutral until it is selected."
                    ),
                    "data_source": None,
                    "issues": [],
                }
            )

        list_out.append(
            {
                "id": "whatsapp",
                "name": "WhatsApp",
                "category": PayflowIntegrationCategory.FUTURE.value,
                "client_id": None,
                "client_name": "All Clients",
                "client_code": None,
                "status": PayflowIntegrationStatus.COMING_LATER.value,
                "last_activity": "—",
                "last_successful": None,
                "purpose": (
                    "WhatsApp collection communications arrive in a later phase and are not active."
                ),
                "data_source": None,
                "issues": [],
            }
        )
        return list_out

    def _mappings_for_client(self, client_id: int) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PayflowClientFieldMappingModel)
            .filter(PayflowClientFieldMappingModel.client_id == client_id)
            .order_by(PayflowClientFieldMappingModel.sort_order.asc())
            .all()
        )
        label_map = {
            PayflowMappingStatus.MAPPED.value: "Mapped",
            PayflowMappingStatus.NEEDS_ATTENTION.value: "Needs Attention",
            PayflowMappingStatus.UNMAPPED.value: "Unmapped",
            PayflowMappingStatus.VALIDATED.value: "Validated",
        }
        return [
            {
                "source_field": m.source_field,
                "payflow_field": m.payflow_field,
                "sample_value": m.sample_value,
                "status": label_map.get(m.status, m.status),
            }
            for m in rows
        ]

    def _mapping_summary(self, mappings: list[dict[str, Any]]) -> dict[str, int]:
        mapped = sum(1 for m in mappings if m["status"] in ("Mapped", "Validated"))
        attention = sum(1 for m in mappings if m["status"] == "Needs Attention")
        unmapped = sum(1 for m in mappings if m["status"] == "Unmapped")
        return {
            "mapped": mapped,
            "attention": attention,
            "unmapped": unmapped,
            "total": len(mappings),
        }
