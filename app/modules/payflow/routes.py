from fastapi import APIRouter, Depends

from app.core.deps import require_product_access
from app.infrastructure.database.models import UserModel

router = APIRouter(prefix="/payflow", tags=["PayFlow"])


@router.get("/health")
def payflow_health(
    user: UserModel = Depends(require_product_access("PAYFLOW")),
):
    return {
        "status": "ok",
        "product": "PAYFLOW",
        "message": "PayFlow entry gate passed. Operational APIs come in later phases.",
        "user_id": str(user.id),
    }
