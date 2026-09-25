"""SMTP delivery plus the confirm / manage emails of the subscription flow.

Deliberately stdlib-only (no jinja2): the API image imports this and does not
install the templating library that the alert emails use.
"""

import html
import logging
import smtplib
from email.message import EmailMessage

from common.config import get_settings

from alerting.tokens import confirm_link, manage_link

logger = logging.getLogger(__name__)


def send_message(msg: EmailMessage) -> bool:
    """Send through the configured SMTP server. Returns False (and sends
    nothing) when SMTP_HOST is unset, mirroring the alert notifier's dev
    behaviour. Raises on SMTP errors; callers decide whether that matters."""
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning("SMTP_HOST not configured - not sending %r to %s", msg["Subject"], msg["To"])
        return False
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_user:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    return True


def build_message(*, to: str, subject: str, text: str, html_body: str, extra_headers: dict[str, str] | None = None) -> EmailMessage:
    """multipart/alternative with the plain-text part first, then HTML.
    EmailMessage (default policy) refuses CR/LF in headers, so a hostile value
    cannot inject headers."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = get_settings().alert_from_address
    msg["To"] = to
    for name, value in (extra_headers or {}).items():
        msg[name] = value
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _link_email(*, to: str, subject: str, intro: str, link: str, outro: str, ttl_hours: int) -> EmailMessage:
    text = f"{intro}\n\n{link}\n\nThis link expires in {ttl_hours} hours.\n\n{outro}\n"
    html_body = (
        '<!DOCTYPE html><html><body style="font-family: sans-serif;">'
        f"<p>{html.escape(intro)}</p>"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(link)}</a></p>'
        f"<p>This link expires in {ttl_hours} hours.</p>"
        f"<p>{html.escape(outro)}</p>"
        "</body></html>"
    )
    return build_message(to=to, subject=subject, text=text, html_body=html_body)


def send_confirmation_email(email: str, token: str) -> bool:
    settings = get_settings()
    return send_message(
        _link_email(
            to=email,
            subject="Confirm your air quality alert subscription",
            intro="Someone (hopefully you) asked to receive Delhi NCR air quality alerts at this address. "
            "Open this link to confirm the subscription:",
            link=confirm_link(token, settings),
            outro="If you did not request this, ignore this email: nothing will be sent to you unless you confirm.",
            ttl_hours=settings.subscription_token_ttl_hours,
        )
    )


def send_manage_email(email: str, token: str) -> bool:
    settings = get_settings()
    return send_message(
        _link_email(
            to=email,
            subject="Manage your air quality alert subscription",
            intro="Someone (hopefully you) asked to sign up this address for air quality alerts, "
            "but it already has a subscription. Open this link to change the stations and pollutants, "
            "or to stop the alerts:",
            link=manage_link(token, settings),
            outro="If you did not request this, ignore this email: your subscription has not been changed.",
            ttl_hours=settings.subscription_token_ttl_hours,
        )
    )
