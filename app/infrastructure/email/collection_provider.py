"""Collection email provider abstraction — SendGrid preferred, SMTP fallback.

Credentials stay in env/settings (never frontend). Empty SendGrid + empty SMTP → log-only.
"""

from __future__ import annotations

import html
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import EmailLogModel

logger = logging.getLogger("app.email.collection")

# http(s) URLs in plain body → used for scrubbing + optional linkify fallback.
_URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)
# Lines that are only a URL (optional trailing punctuation).
_BARE_URL_LINE_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)


def scrub_action_urls_from_body(body: str, action_link: str | None = None) -> str:
    """Remove raw payment / CTA URLs from visible copy (button carries the href)."""
    text = (body or "").replace("\r\n", "\n")
    if not text.strip():
        return ""

    targets: list[str] = []
    if action_link:
        targets.append(action_link.strip())
    for match in _URL_RE.finditer(text):
        url = match.group(1).rstrip(".,);]:'\"")
        lower = url.lower()
        # Portal + identity action URLs belong behind the CTA button.
        if (
            "/pay/" in lower
            or "/activate" in lower
            or "/reset-password" in lower
        ) and url not in targets:
            targets.append(url)

    for url in targets:
        if not url:
            continue
        text = text.replace(url, "")

    cleaned_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if _BARE_URL_LINE_RE.match(stripped):
            continue
        # Drop orphan labels left after URL removal ("Pay securely:", "Pay here:", …).
        if re.match(
            r"(?i)^(pay(?:\s+securely|\s+here)?|pay now|set up your plan|"
            r"visit your secure payment page(?:[^.]*))\s*[:\-–]?\s*$",
            stripped,
        ):
            continue
        # Collapse "…. : " leftovers on the same line.
        stripped = re.sub(r"[:\-–]\s*$", "", stripped).rstrip()
        if stripped:
            cleaned_lines.append(stripped)

    # Collapse 3+ blank lines → one blank separator.
    out: list[str] = []
    blank = 0
    for line in cleaned_lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def _cta_label_for_type(email_type: str) -> str:
    et = (email_type or "").strip().lower()
    if et in {"activation"}:
        return "Activate Account"
    if et in {"password_reset"}:
        return "Reset Password"
    if "collection" in et or "payment" in et or et in {"collection_workflow"}:
        return "Pay Now"
    return "Continue"


def _format_balance(balance: float | int | None) -> str | None:
    if balance is None:
        return None
    try:
        amount = float(balance)
    except (TypeError, ValueError):
        return None
    return f"${amount:,.2f}"


def _paragraphs_html(body: str) -> str:
    """Escape body and wrap non-empty paragraphs; preserve blank-line breaks."""
    blocks = re.split(r"\n\s*\n", (body or "").strip())
    parts: list[str] = []
    for block in blocks:
        lines = [html.escape(ln.strip(), quote=False) for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        inner = "<br>\n".join(lines)
        parts.append(
            f'<p style="margin:0 0 14px; font-size:14px; line-height:1.55; color:#1f2937;">{inner}</p>'
        )
    return "".join(parts)


def build_email_html(
    body: str,
    *,
    subject: str | None = None,
    from_name: str | None = None,
    from_email: str | None = None,
    action_link: str | None = None,
    balance: float | int | None = None,
    email_type: str = "collection_workflow",
    unsubscribe_url: str | None = None,
) -> str:
    """Branded HTML for collection (and other) emails — CTA button, no raw URL in body."""
    brand = (from_name or settings.SENDGRID_FROM_NAME or "PayFlow").strip()
    cta = _cta_label_for_type(email_type)
    visible = scrub_action_urls_from_body(body, action_link)
    # Fallback: if scrubbing wiped everything, keep a short generic line.
    if not visible.strip():
        visible = body.strip() or "You have a message from PayFlow."
        if action_link:
            visible = scrub_action_urls_from_body(visible, action_link) or (
                "Please use the button below to continue."
            )

    balance_label = _format_balance(balance)
    primary = "#2563eb"  # Matches PayFlow primary used in the app preview
    paragraphs = _paragraphs_html(visible)

    header_sub = html.escape((subject or "").strip(), quote=False)
    header_brand = html.escape(brand, quote=False)

    balance_block = ""
    if balance_label is not None and "collection" in (email_type or "").lower():
        balance_block = (
            '<div style="margin:4px 0 18px; padding:12px 14px; border:1px solid #e5e7eb; '
            'border-radius:8px; background:#f8fafc;">'
            '<div style="font-size:11px; color:#6b7280; margin-bottom:2px;">Outstanding Balance</div>'
            f'<div style="font-size:18px; font-weight:700; color:#111827;">{html.escape(balance_label)}</div>'
            "</div>"
        )

    button_block = ""
    if action_link:
        safe_href = html.escape(action_link, quote=True)
        safe_cta = html.escape(cta, quote=False)
        button_block = (
            '<div style="margin:8px 0 20px;">'
            f'<a href="{safe_href}" '
            f'style="display:inline-block; background:{primary}; color:#ffffff; '
            "font-size:14px; font-weight:700; text-decoration:none; "
            'padding:12px 22px; border-radius:8px;">'
            f"{safe_cta}</a>"
            "</div>"
        )

    footer_bits = [html.escape(brand, quote=False)]
    if from_email:
        footer_bits.append(html.escape(from_email.strip(), quote=False))
    footer = " · ".join(footer_bits)

    unsub_block = ""
    if unsubscribe_url:
        safe_unsub = html.escape(unsubscribe_url, quote=True)
        unsub_block = (
            '<p style="margin:14px 0 0; font-size:11px; line-height:1.45; color:#9ca3af;">'
            "If you no longer wish to receive these emails, "
            f'<a href="{safe_unsub}" style="color:#6b7280; text-decoration:underline;">'
            "unsubscribe</a>."
            "</p>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{header_sub or header_brand}</title></head>"
        '<body style="margin:0; padding:0; background:#f3f4f6;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f3f4f6; padding:24px 12px;">'
        "<tr><td align=\"center\">"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:560px; background:#ffffff; border-radius:10px; overflow:hidden; '
        'border:1px solid #e5e7eb;">'
        "<tr><td "
        f'style="background:{primary}; color:#ffffff; padding:16px 20px;">'
        f'<div style="font-size:15px; font-weight:700;">{header_brand}</div>'
        + (
            f'<div style="font-size:12px; opacity:0.92; margin-top:4px;">{header_sub}</div>'
            if header_sub
            else ""
        )
        + "</td></tr>"
        '<tr><td style="padding:20px 20px 8px; font-family:Arial, Helvetica, sans-serif;">'
        f"{paragraphs}"
        f"{balance_block}"
        f"{button_block}"
        f'<p style="margin:16px 0 0; font-size:12px; color:#6b7280;">{footer}</p>'
        f"{unsub_block}"
        "</td></tr></table>"
        "</td></tr></table>"
        "</body></html>"
    )


def _plain_body_for_send(
    body: str,
    *,
    action_link: str | None,
    cta_label: str,
    unsubscribe_url: str | None = None,
) -> str:
    """Plain-text alternative: scrub raw URLs from copy; append CTA line once."""
    visible = scrub_action_urls_from_body(body, action_link)
    if not visible.strip():
        visible = (body or "").strip() or "You have a message from PayFlow."
        if action_link:
            visible = scrub_action_urls_from_body(visible, action_link) or (
                "Please use the link below to continue."
            )
    parts = [visible.rstrip()]
    if action_link and action_link not in visible:
        parts.append(f"{cta_label}: {action_link}")
    if unsubscribe_url:
        parts.append(f"Unsubscribe: {unsubscribe_url}")
    return "\n\n".join(parts)


def sendgrid_configured() -> bool:
    return bool(
        (settings.SENDGRID_API_KEY or "").strip() and (settings.SENDGRID_FROM_EMAIL or "").strip()
    )


def smtp_configured() -> bool:
    return bool((settings.SMTP_HOST or "").strip())


def active_collection_provider() -> str:
    """Which provider will be used for the next collection send."""
    if sendgrid_configured():
        return "sendgrid"
    if smtp_configured():
        return "smtp"
    return "log"


def _resolve_from(
    *,
    from_email: str | None,
    from_name: str | None,
) -> tuple[str, str]:
    email = (from_email or "").strip() or (settings.SENDGRID_FROM_EMAIL or "").strip() or (
        settings.SMTP_FROM or settings.SMTP_USER or "noreply@payflow.local"
    ).strip()
    name = (from_name or "").strip() or (settings.SENDGRID_FROM_NAME or "PayFlow").strip()
    return email, name


def _send_via_sendgrid(
    *,
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    from_email: str,
    from_name: str,
    custom_args: dict[str, str] | None,
    list_unsubscribe_url: str | None = None,
) -> tuple[bool, str | None, str | None]:
    personalization: dict[str, Any] = {"to": [{"email": to_email}]}
    if custom_args:
        personalization["custom_args"] = {str(k): str(v) for k, v in custom_args.items()}
    # plain first, then html — SendGrid multipart; open pixel only works with HTML part.
    payload: dict[str, Any] = {
        "personalizations": [personalization],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html", "value": html_body},
        ],
        "tracking_settings": {
            "click_tracking": {"enable": True, "enable_text": True},
            "open_tracking": {"enable": True},
        },
    }
    if list_unsubscribe_url:
        # RFC 8058 one-click — Gmail/Yahoo show Unsubscribe in the header chrome.
        payload["headers"] = {
            "List-Unsubscribe": f"<{list_unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {settings.SENDGRID_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code in (200, 202):
            return True, None, resp.headers.get("X-Message-Id")
        return False, f"sendgrid_http_{resp.status_code}: {resp.text[:400]}", None
    except Exception as exc:  # noqa: BLE001
        logger.exception("SendGrid collection send failed to=%s", to_email)
        return False, str(exc)[:500], None


def _send_via_smtp(
    *,
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    from_email: str,
    from_name: str,
    list_unsubscribe_url: str | None = None,
) -> tuple[bool, str | None, str | None]:
    host = settings.SMTP_HOST.strip()
    port = int(settings.SMTP_PORT or 587)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    if list_unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{list_unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            user = (settings.SMTP_USER or "").strip()
            password = settings.SMTP_PASSWORD or ""
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        # SMTP has no stable message id — synthesize a log-scoped id later.
        return True, None, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP collection send failed to=%s", to_email)
        return False, str(exc)[:500], None


def send_collection_email(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body: str,
    email_type: str = "collection_workflow",
    action_link: Optional[str] = None,
    related_user_id: Optional[UUID] = None,
    custom_args: dict[str, str] | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    balance: float | int | None = None,
    unsubscribe_url: str | None = None,
    list_unsubscribe_url: str | None = None,
    commit: bool = True,
) -> EmailLogModel:
    """Send collection email via SendGrid (if configured) else SMTP else log-only.

    Returns EmailLogModel with dynamic attrs:
    - provider_message_id
    - provider_name (sendgrid | smtp | log)
    - error (optional)

    ``unsubscribe_url`` is the customer page (footer link).
    ``list_unsubscribe_url`` is the one-click HTTPS endpoint for List-Unsubscribe.
    Activation / password-reset callers leave both unset.
    """
    resolved_from, resolved_name = _resolve_from(from_email=from_email, from_name=from_name)
    provider = active_collection_provider()
    status = "logged"
    err: str | None = None
    provider_id: str | None = None
    # SendGrid rejects content.value of length 0.
    if not (body or "").strip():
        body = "Payment reminder"

    cta_label = _cta_label_for_type(email_type)
    plain_body = _plain_body_for_send(
        body,
        action_link=action_link,
        cta_label=cta_label,
        unsubscribe_url=unsubscribe_url,
    )
    html_body = build_email_html(
        body,
        subject=subject,
        from_name=resolved_name,
        from_email=resolved_from,
        action_link=action_link,
        balance=balance,
        email_type=email_type,
        unsubscribe_url=unsubscribe_url,
    )

    if provider == "sendgrid":
        ok, err, provider_id = _send_via_sendgrid(
            to_email=to_email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            from_email=resolved_from,
            from_name=resolved_name,
            custom_args=custom_args,
            list_unsubscribe_url=list_unsubscribe_url,
        )
        status = "sent" if ok else "failed"
    elif provider == "smtp":
        ok, err, provider_id = _send_via_smtp(
            to_email=to_email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            from_email=resolved_from,
            from_name=resolved_name,
            list_unsubscribe_url=list_unsubscribe_url,
        )
        status = "sent" if ok else "failed"
        if ok and not provider_id:
            provider_id = f"smtp-{to_email.lower()}-{subject[:24]}"

    # Log stores the customer-facing plain copy (URLs scrubbed; CTA line retained).
    stored_body = plain_body
    if err:
        stored_body = f"{plain_body}\n\n[{provider}: {err}]"
    if provider_id:
        stored_body = f"{stored_body}\n\n[provider_id={provider_id}]"
    stored_body = f"{stored_body}\n\n[from={resolved_name} <{resolved_from}>]"

    row = EmailLogModel(
        to_email=to_email.lower(),
        subject=subject,
        body=stored_body,
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        status=status,
    )
    setattr(row, "provider_message_id", provider_id)
    setattr(row, "provider_name", provider)
    setattr(row, "error", err)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
        setattr(row, "provider_message_id", provider_id)
        setattr(row, "provider_name", provider)
        setattr(row, "error", err)
    else:
        db.flush()
    logger.info(
        "COLLECTION_EMAIL %s provider=%s id=%s to=%s type=%s from=%s",
        status,
        provider,
        row.id,
        to_email,
        email_type,
        resolved_from,
    )
    return row
