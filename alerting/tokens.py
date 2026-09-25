"""Subscription tokens and the links built from them.

Two kinds of secret, both delivered only by email and never returned by any API
response:

* confirm / manage tokens: secrets.token_urlsafe(32), stored ONLY as a SHA-256
  hash (a DB leak yields nothing usable), with an expiry. Confirm tokens are
  single-use.
* unsubscribe tokens (in alert emails): stateless HMAC of the subscriber id.
  An alert email must offer a working unsubscribe link for as long as the
  email exists, and a stored hash cannot be turned back into a link for the
  next alert, so this one is derived rather than stored. Format
  "u1.<subscriber-uuid hex>.<mac>". It can only ever unsubscribe, not
  re-activate or edit.

Only this module knows the token formats; callers use the helpers.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from common.config import Settings, get_settings

_UNSUB_PREFIX = "u1"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expiry_from(now: datetime, settings: Settings | None = None) -> datetime:
    return now + timedelta(hours=(settings or get_settings()).subscription_token_ttl_hours)


def _key(settings: Settings) -> bytes:
    # No fallback: a key derived from another setting (the draft used the
    # database URL) silently changes - and invalidates every unsubscribe link
    # already emailed - whenever that setting changes, and ties link security
    # to a secret with a different purpose. Unset means alerts cannot be sent.
    secret = settings.subscription_token_secret
    if not secret:
        raise RuntimeError("SUBSCRIPTION_TOKEN_SECRET is not set - cannot build or check unsubscribe links")
    return hashlib.sha256(b"alert-unsubscribe-token-v1\0" + secret.encode("utf-8")).digest()


def _mac(subscriber_id: uuid.UUID, settings: Settings) -> str:
    digest = hmac.new(_key(settings), f"unsubscribe:{subscriber_id.hex}".encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_unsubscribe_token(subscriber_id: uuid.UUID, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{_UNSUB_PREFIX}.{subscriber_id.hex}.{_mac(subscriber_id, settings)}"


def is_unsubscribe_token(token: str) -> bool:
    # token_urlsafe never contains ".", so this cleanly separates the two kinds.
    return token.startswith(_UNSUB_PREFIX + ".")


def parse_unsubscribe_token(token: str, settings: Settings | None = None) -> uuid.UUID | None:
    """The subscriber id if `token` is a genuine unsubscribe token, else None."""
    settings = settings or get_settings()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _UNSUB_PREFIX:
        return None
    try:
        subscriber_id = uuid.UUID(hex=parts[1])
    except ValueError:
        return None
    if not hmac.compare_digest(parts[2].encode("utf-8"), _mac(subscriber_id, settings).encode("utf-8")):
        return None
    return subscriber_id


# ---------------------------------------------------------------------- links

def _frontend_base(settings: Settings) -> str:
    return (settings.frontend_base_url or settings.dashboard_url).rstrip("/")


def confirm_link(token: str, settings: Settings | None = None) -> str:
    return f"{_frontend_base(settings or get_settings())}/subscriptions/confirm?token={quote(token)}"


def manage_link(token: str, settings: Settings | None = None) -> str:
    return f"{_frontend_base(settings or get_settings())}/subscriptions/manage?token={quote(token)}"


def unsubscribe_page_link(token: str, settings: Settings | None = None) -> str:
    """Human-facing page (the frontend POSTs the token to /subscriptions/unsubscribe)."""
    return f"{_frontend_base(settings or get_settings())}/subscriptions/unsubscribe?token={quote(token)}"


def one_click_unsubscribe_url(token: str, settings: Settings | None = None) -> str:
    """RFC 8058 target: the mail provider POSTs here directly, so it is the API."""
    settings = settings or get_settings()
    return f"{settings.public_api_base_url.rstrip('/')}/api/v1/subscriptions/unsubscribe/one-click?token={quote(token)}"
