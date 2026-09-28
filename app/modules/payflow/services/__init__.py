from __future__ import annotations

from app.infrastructure.database.models import PayflowUserClientPermissionModel

# Ensure relationship string target resolves for joinedload("permission")
_ = PayflowUserClientPermissionModel
