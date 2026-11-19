"""Per-communication-variant review status helpers.

Statuses live inside each Communication step's config:
- Main / Default: config.review_status
- Segment version: config.versions[key].review_status

Legacy texts without a status are treated as already reviewed for approval so existing
workflows keep working (AC18). Newly generated or edited texts always stamp a status.
"""

from __future__ import annotations

from typing import Any

REVIEW_PENDING = "Pending Review"
REVIEW_REVIEWED = "Reviewed"
REVIEW_MODIFIED = "Modified"  # UI: "Modified / Requires Re-review"

VALID_REVIEW_STATUSES = {REVIEW_PENDING, REVIEW_REVIEWED, REVIEW_MODIFIED}


def normalize_review_status(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in {"Modified / Requires Re-review", "Requires Re-review", "Modified"}:
        return REVIEW_MODIFIED
    if text in VALID_REVIEW_STATUSES:
        return text
    return None


def active_texts(steps: list[dict] | None) -> list[tuple[dict, str, dict]]:
    """[(step, key, text_dict)] for Main + every segment version on Communication steps."""
    out: list[tuple[dict, str, dict]] = []
    for step in steps or []:
        if not isinstance(step, dict) or step.get("kind") != "Communication":
            continue
        if step.get("disabled"):
            continue
        cfg = step.get("config") if isinstance(step.get("config"), dict) else {}
        has_main = any(str(cfg.get(part) or "").strip() for part in ("subject", "body", "sms_body"))
        if has_main:
            out.append((step, "main", cfg))
        versions = cfg.get("versions") if isinstance(cfg.get("versions"), dict) else {}
        for key, version in versions.items():
            if isinstance(version, dict) and (
                str(version.get("body") or "").strip() or str(version.get("sms_body") or "").strip()
            ):
                out.append((step, str(key), version))
    return out


def review_summary(steps: list[dict] | None) -> dict[str, Any]:
    """Counts for the workflow review UI. Missing status counts as reviewed (legacy)."""
    items: list[dict[str, Any]] = []
    pending = reviewed = modified = 0
    for step, key, text in active_texts(steps):
        status = normalize_review_status(text.get("review_status")) or REVIEW_REVIEWED
        if status == REVIEW_PENDING:
            pending += 1
        elif status == REVIEW_MODIFIED:
            modified += 1
        else:
            reviewed += 1
        items.append(
            {
                "step_id": step.get("id"),
                "step_title": step.get("title"),
                "audience": key,
                "label": "Main / Default" if key == "main" else (text.get("label") or key),
                "review_status": status,
            }
        )
    blocking = [i for i in items if i["review_status"] in {REVIEW_PENDING, REVIEW_MODIFIED}]
    return {
        "total": len(items),
        "pending_review": pending,
        "reviewed": reviewed,
        "modified": modified,
        "all_reviewed": not blocking,
        "blocking": blocking,
        "items": items,
    }


def approval_blockers(steps: list[dict] | None) -> list[str]:
    """Human-readable reasons approval must wait. Empty = may approve (for review status).

    Only texts that carry an explicit review_status participate — legacy AI workflows
    without the field stay approvable (AC18).
    """
    blockers: list[str] = []
    for step, key, text in active_texts(steps):
        status = normalize_review_status(text.get("review_status"))
        if status is None:
            continue  # legacy — no per-variant review stamped
        if status == REVIEW_REVIEWED:
            continue
        label = "Main / Default" if key == "main" else (text.get("label") or key)
        title = step.get("title") or step.get("id") or "message"
        blockers.append(f"{title} / {label}: {status}")
    return blockers


def stamp_pending(text: dict[str, Any]) -> dict[str, Any]:
    return {**text, "review_status": REVIEW_PENDING}


def stamp_modified(text: dict[str, Any]) -> dict[str, Any]:
    return {**text, "review_status": REVIEW_MODIFIED}


def set_reviewed(text: dict[str, Any]) -> dict[str, Any]:
    return {**text, "review_status": REVIEW_REVIEWED}
