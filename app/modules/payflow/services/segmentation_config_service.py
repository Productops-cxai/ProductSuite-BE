"""Client demographic segmentation configuration for communication variants.

Defaults match the hard-coded age audiences that already ship with workflow suggestion.
Country / region dimensions are off by default so existing behaviour is unchanged until
an authorized user enables them. Config changes never rewrite approved workflows — each
workflow stores its own `segmentation_snapshot` at generation time.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowSegmentationConfigModel,
    PayflowSegmentationConfigVersionModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService

DIMENSIONS = ("age", "country", "region")
DEFAULT_PRIORITY = ["region", "country", "age"]
DEFAULT_MIN_AUDIENCE_SIZE = 12
DEFAULT_MAX_VERSIONS = 6

# Same six bands as workflow_suggestion/audiences.py (open [min, max) years).
DEFAULT_AGE_GROUPS: list[dict[str, Any]] = [
    {"key": "age_18_24", "label": "18 – 24", "min_age": 18, "max_age": 24},
    {"key": "age_25_34", "label": "25 – 34", "min_age": 25, "max_age": 34},
    {"key": "age_35_44", "label": "35 – 44", "min_age": 35, "max_age": 44},
    {"key": "age_45_54", "label": "45 – 54", "min_age": 45, "max_age": 54},
    {"key": "age_55_64", "label": "55 – 64", "min_age": 55, "max_age": 64},
    {"key": "age_65_plus", "label": "65 and over", "min_age": 65, "max_age": None},
]


def default_segmentation_config() -> dict[str, Any]:
    """The built-in config: age on, country/region off — identical to pre-config behaviour."""
    return {
        "min_audience_size": DEFAULT_MIN_AUDIENCE_SIZE,
        "max_versions": DEFAULT_MAX_VERSIONS,
        "priority": list(DEFAULT_PRIORITY),
        "dimensions": {
            "age": {"enabled": True, "groups": copy.deepcopy(DEFAULT_AGE_GROUPS)},
            "country": {"enabled": False, "values": []},
            "region": {"enabled": False, "values": []},
        },
    }


def normalize_segmentation_config(raw: Any) -> dict[str, Any]:
    """Validate and normalize a config payload. Raises ValidationAppError on bad input."""
    if raw is None:
        return default_segmentation_config()
    if not isinstance(raw, dict):
        raise ValidationAppError("Segmentation config must be an object")

    base = default_segmentation_config()
    min_size = raw.get("min_audience_size", base["min_audience_size"])
    max_versions = raw.get("max_versions", base["max_versions"])
    try:
        min_size = int(min_size)
        max_versions = int(max_versions)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("min_audience_size and max_versions must be integers") from exc
    if min_size < 1:
        raise ValidationAppError("min_audience_size must be at least 1")
    if max_versions < 1 or max_versions > 20:
        raise ValidationAppError("max_versions must be between 1 and 20")

    priority = raw.get("priority", base["priority"])
    if not isinstance(priority, list) or not priority:
        raise ValidationAppError("priority must be a non-empty list")
    cleaned_priority: list[str] = []
    for item in priority:
        name = str(item or "").strip().lower()
        if name not in DIMENSIONS:
            raise ValidationAppError(f"priority entries must be one of: {', '.join(DIMENSIONS)}")
        if name not in cleaned_priority:
            cleaned_priority.append(name)
    for name in DEFAULT_PRIORITY:
        if name not in cleaned_priority:
            cleaned_priority.append(name)

    dims_raw = raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else {}
    dimensions: dict[str, Any] = {}

    age_raw = dims_raw.get("age") if isinstance(dims_raw.get("age"), dict) else {}
    age_enabled = bool(age_raw.get("enabled", True))
    groups = age_raw.get("groups", DEFAULT_AGE_GROUPS)
    dimensions["age"] = {
        "enabled": age_enabled,
        "groups": _normalize_age_groups(groups) if age_enabled or groups else copy.deepcopy(DEFAULT_AGE_GROUPS),
    }
    if age_enabled and not dimensions["age"]["groups"]:
        raise ValidationAppError("At least one age group is required when age segmentation is enabled")

    for dim in ("country", "region"):
        dim_raw = dims_raw.get(dim) if isinstance(dims_raw.get(dim), dict) else {}
        enabled = bool(dim_raw.get("enabled", False))
        values_raw = dim_raw.get("values") or []
        if values_raw and not isinstance(values_raw, list):
            raise ValidationAppError(f"{dim}.values must be a list")
        values: list[str] = []
        seen: set[str] = set()
        for value in values_raw:
            label = str(value or "").strip()
            if not label:
                continue
            key = label.casefold()
            if key in seen:
                raise ValidationAppError(f"Duplicate {dim} value: {label}")
            seen.add(key)
            values.append(label)
        dimensions[dim] = {"enabled": enabled, "values": values}

    if not any(dimensions[d]["enabled"] for d in DIMENSIONS):
        raise ValidationAppError("Enable at least one segmentation dimension (age, country, or region)")

    return {
        "min_audience_size": min_size,
        "max_versions": max_versions,
        "priority": cleaned_priority,
        "dimensions": dimensions,
    }


def _normalize_age_groups(groups: Any) -> list[dict[str, Any]]:
    if not isinstance(groups, list) or not groups:
        raise ValidationAppError("age.groups must be a non-empty list")
    cleaned: list[dict[str, Any]] = []
    keys: set[str] = set()
    ranges: list[tuple[int, int | None, str]] = []
    for item in groups:
        if not isinstance(item, dict):
            raise ValidationAppError("Each age group must be an object")
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        if not key or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key):
            raise ValidationAppError(
                f"Invalid age group key '{key or '(empty)'}' — use lowercase letters, digits, underscore"
            )
        if key in keys:
            raise ValidationAppError(f"Duplicate age group key: {key}")
        keys.add(key)
        if not label:
            raise ValidationAppError(f"Age group '{key}' needs a label")
        try:
            min_age = int(item.get("min_age"))
        except (TypeError, ValueError) as exc:
            raise ValidationAppError(f"Age group '{key}' needs an integer min_age") from exc
        max_raw = item.get("max_age", None)
        if max_raw is None or max_raw == "":
            max_age: int | None = None
        else:
            try:
                max_age = int(max_raw)
            except (TypeError, ValueError) as exc:
                raise ValidationAppError(f"Age group '{key}' max_age must be an integer or null") from exc
        if min_age < 18:
            raise ValidationAppError(f"Age group '{key}' min_age must be at least 18")
        if max_age is not None and max_age < min_age:
            raise ValidationAppError(f"Age group '{key}' max_age must be >= min_age")
        ranges.append((min_age, max_age, key))
        cleaned.append({"key": key, "label": label, "min_age": min_age, "max_age": max_age})

    # Overlap check on half-open [min, max+1) / [min, ∞).
    ordered = sorted(ranges, key=lambda r: (r[0], r[1] is None, r[1] or 0))
    for i, (lo_a, hi_a, key_a) in enumerate(ordered):
        end_a = hi_a + 1 if hi_a is not None else None  # inclusive max → exclusive end
        for lo_b, hi_b, key_b in ordered[i + 1 :]:
            end_b = hi_b + 1 if hi_b is not None else None
            # overlap if ranges intersect
            if end_a is None or lo_b < end_a:
                if end_b is None or lo_a < end_b:
                    raise ValidationAppError(
                        f"Age groups '{key_a}' and '{key_b}' overlap — ranges must be exclusive"
                    )
    return cleaned


def age_bands_from_config(config: dict[str, Any] | None) -> dict[str, tuple[str, tuple[int, int | None]]]:
    """Audience key → (label, [low, high) years) for find_audiences / matching."""
    cfg = config or default_segmentation_config()
    groups = ((cfg.get("dimensions") or {}).get("age") or {}).get("groups") or DEFAULT_AGE_GROUPS
    bands: dict[str, tuple[str, tuple[int, int | None]]] = {}
    for group in groups:
        key = group["key"]
        label = group["label"]
        low = int(group["min_age"])
        high = None if group.get("max_age") is None else int(group["max_age"]) + 1
        bands[key] = (label, (low, high))
    return bands


def slug_key(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_") or "unknown"
    return f"{prefix}_{slug}"


class SegmentationConfigService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can(user, "view_workflows", client_id):
            raise ForbiddenError("View Workflows permission required")

    def _assert_manage(self, user: UserModel, client_id: int) -> None:
        self._assert_view(user, client_id)
        if self.access.is_operations_admin(user):
            return
        if not self.access.can(user, "create_edit_workflows", client_id):
            raise ForbiddenError("Create / Edit Workflows permission required")

    def get_config(self, user: UserModel, client_id: int) -> dict[str, Any]:
        self._assert_view(user, client_id)
        row = (
            self.db.query(PayflowSegmentationConfigModel)
            .filter(PayflowSegmentationConfigModel.client_id == client_id)
            .first()
        )
        if not row:
            return {
                "client_id": client_id,
                "version": 0,
                "is_default": True,
                "config": default_segmentation_config(),
                "updated_by": None,
                "updated_at": None,
                "history": [],
            }
        return self._serialize(row)

    def resolve_for_client(self, client_id: int) -> dict[str, Any]:
        """Config used for AI generation / matching — default when the client has none saved."""
        row = (
            self.db.query(PayflowSegmentationConfigModel)
            .filter(PayflowSegmentationConfigModel.client_id == client_id)
            .first()
        )
        if not row or not isinstance(row.config, dict):
            return default_segmentation_config()
        try:
            return normalize_segmentation_config(row.config)
        except ValidationAppError:
            return default_segmentation_config()

    def update_config(
        self,
        user: UserModel,
        client_id: int,
        payload: dict[str, Any],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._assert_manage(user, client_id)
        cleaned = normalize_segmentation_config(payload.get("config") if "config" in payload else payload)
        row = (
            self.db.query(PayflowSegmentationConfigModel)
            .filter(PayflowSegmentationConfigModel.client_id == client_id)
            .first()
        )
        if not row:
            row = PayflowSegmentationConfigModel(
                client_id=client_id,
                config=cleaned,
                version=1,
                updated_by=user.full_name,
            )
            self.db.add(row)
            self.db.flush()
        else:
            row.config = cleaned
            row.version = int(row.version or 0) + 1
            row.updated_by = user.full_name
            self.db.flush()

        history = PayflowSegmentationConfigVersionModel(
            config_id=row.id,
            client_id=client_id,
            version=int(row.version or 1),
            config=copy.deepcopy(cleaned),
            note=(note or "").strip() or f"Updated by {user.full_name}",
            changed_by=user.full_name,
        )
        self.db.add(history)
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row)

    def history(self, user: UserModel, client_id: int) -> dict[str, Any]:
        self._assert_view(user, client_id)
        row = (
            self.db.query(PayflowSegmentationConfigModel)
            .filter(PayflowSegmentationConfigModel.client_id == client_id)
            .first()
        )
        if not row:
            return {"client_id": client_id, "items": [], "total": 0}
        items = (
            self.db.query(PayflowSegmentationConfigVersionModel)
            .filter(PayflowSegmentationConfigVersionModel.config_id == row.id)
            .order_by(
                PayflowSegmentationConfigVersionModel.version.desc(),
                PayflowSegmentationConfigVersionModel.id.desc(),
            )
            .all()
        )
        return {
            "client_id": client_id,
            "items": [
                {
                    "version": item.version,
                    "config": item.config or {},
                    "note": item.note,
                    "changed_by": item.changed_by,
                    "created_at": item.created_at,
                }
                for item in items
            ],
            "total": len(items),
        }

    def _serialize(self, row: PayflowSegmentationConfigModel) -> dict[str, Any]:
        try:
            config = normalize_segmentation_config(row.config)
        except ValidationAppError:
            config = default_segmentation_config()
        history_rows = (
            self.db.query(PayflowSegmentationConfigVersionModel)
            .filter(PayflowSegmentationConfigVersionModel.config_id == row.id)
            .order_by(PayflowSegmentationConfigVersionModel.version.desc())
            .limit(20)
            .all()
        )
        return {
            "client_id": row.client_id,
            "version": int(row.version or 1),
            "is_default": False,
            "config": config,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at,
            "history": [
                {
                    "version": h.version,
                    "note": h.note,
                    "changed_by": h.changed_by,
                    "created_at": h.created_at,
                }
                for h in history_rows
            ],
        }


def get_client_segmentation(db: Session, client_id: int) -> dict[str, Any]:
    """Convenience for AI / runtime callers (no user ACL — caller already authorized)."""
    return SegmentationConfigService(db).resolve_for_client(client_id)
