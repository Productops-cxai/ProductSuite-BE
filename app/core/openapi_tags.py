"""Swagger / OpenAPI tag catalog and path → section mapping.

Only affects docs UI grouping. Route paths and handlers stay unchanged.
"""

from __future__ import annotations

from typing import Any

# Display order in Swagger UI
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "Health", "description": "Process health checks"},
    {
        "name": "Auth",
        "description": "Login, tokens, profile, activation, and password reset",
    },
    {
        "name": "Platform · Overview",
        "description": "Platform admin home / overview",
    },
    {
        "name": "Platform · Products",
        "description": "Product registry and product entry",
    },
    {
        "name": "Platform · Organizations & access",
        "description": "Organizations and org-level product entitlements",
    },
    {
        "name": "Platform · People",
        "description": "People invitations and product assignments",
    },
    {
        "name": "Platform · Menus",
        "description": "Platform / product navigation menus",
    },
    {
        "name": "Platform · Logs",
        "description": "Email and deletion audit logs",
    },
    {
        "name": "Platform · Billing",
        "description": "Billing placeholders",
    },
    {
        "name": "PayFlow · Access",
        "description": "PayFlow health, menus, access context, geo helpers",
    },
    {
        "name": "PayFlow · Dashboard",
        "description": "PayFlow dashboard KPIs",
    },
    {
        "name": "PayFlow · Clients & portfolios",
        "description": "Clients, logos, activation, portfolios, client bulk import",
    },
    {
        "name": "PayFlow · Accounts",
        "description": "Debtor / case accounts",
    },
    {
        "name": "PayFlow · Imports",
        "description": "Daily account import runs and templates",
    },
    {
        "name": "PayFlow · Integrations",
        "description": "CRM / system mapping integrations",
    },
    {
        "name": "PayFlow · Reviews",
        "description": "Human review queue",
    },
    {
        "name": "PayFlow · Rules",
        "description": "Governance and collection rules",
    },
    {
        "name": "PayFlow · Workflows",
        "description": "Strategies / workflows",
    },
    {
        "name": "PayFlow · Communications",
        "description": "Outbound / inbound communications log",
    },
    {
        "name": "PayFlow · Notifications",
        "description": "In-app notifications",
    },
    {
        "name": "PayFlow · Users & roles",
        "description": "PayFlow users, roles, and permissions catalog",
    },
    {
        "name": "PayFlow · Deletion logs",
        "description": "PayFlow soft-delete audit",
    },
]


def tag_for_path(path: str) -> str | None:
    """Return the Swagger section tag for an absolute OpenAPI path."""
    if path in {"/health", "/health/"}:
        return "Health"

    if path.startswith("/auth"):
        return "Auth"

    if path.startswith("/payflow"):
        return _payflow_tag(path)

    if path.startswith("/platform/overview"):
        return "Platform · Overview"
    if path.startswith("/platform/billing"):
        return "Platform · Billing"
    if path.startswith("/products"):
        return "Platform · Products"
    if path.startswith("/me/products"):
        return "Platform · Products"
    if path.startswith("/organizations") or path.startswith("/product-access"):
        return "Platform · Organizations & access"
    if path.startswith("/people"):
        return "Platform · People"
    if path.startswith("/menus"):
        return "Platform · Menus"
    if path.startswith("/email-logs") or path.startswith("/deletion-logs"):
        return "Platform · Logs"

    return None


def _payflow_tag(path: str) -> str:
    # More specific prefixes first
    if path.startswith("/payflow/clients"):
        return "PayFlow · Clients & portfolios"
    if path.startswith("/payflow/accounts"):
        return "PayFlow · Accounts"
    if path.startswith("/payflow/imports"):
        return "PayFlow · Imports"
    if path.startswith("/payflow/integrations"):
        return "PayFlow · Integrations"
    if path.startswith("/payflow/reviews"):
        return "PayFlow · Reviews"
    if path.startswith("/payflow/rules"):
        return "PayFlow · Rules"
    if path.startswith("/payflow/workflows"):
        return "PayFlow · Workflows"
    if path.startswith("/payflow/comms"):
        return "PayFlow · Communications"
    if path.startswith("/payflow/notifications"):
        return "PayFlow · Notifications"
    if (
        path.startswith("/payflow/users")
        or path.startswith("/payflow/roles")
        or path.startswith("/payflow/permissions")
    ):
        return "PayFlow · Users & roles"
    if path.startswith("/payflow/deletion-logs"):
        return "PayFlow · Deletion logs"
    if path.startswith("/payflow/dashboard"):
        return "PayFlow · Dashboard"
    if (
        path.startswith("/payflow/health")
        or path.startswith("/payflow/geo")
        or path.startswith("/payflow/access-context")
        or path.startswith("/payflow/menus")
    ):
        return "PayFlow · Access"
    return "PayFlow · Access"


_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)


def apply_openapi_tags(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite operation tags from path prefixes; set tag metadata order."""
    schema["tags"] = list(OPENAPI_TAGS)
    paths = schema.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        tag = tag_for_path(path)
        if not tag:
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]
    return schema
