"""Alert subscriptions with owner-verified double opt-in.

Nobody can create, change, read back or cancel a subscription without
controlling the mailbox: every credential (confirm token, manage token,
unsubscribe token) is delivered only by email and never appears in any API
response. POST /subscriptions answers identically whether the address is new,
pending, confirmed or rate-limited, so it is not an enumeration oracle.
"""

import logging
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from alerting.mailer import send_confirmation_email, send_manage_email
from alerting.tokens import (
    expiry_from,
    hash_token,
    is_unsubscribe_token,
    new_token,
    parse_unsubscribe_token,
)
from alerting.retention import erase_subscriber
from api.db import get_db
from api.schemas.subscriptions import (
    ManageIn,
    SubscriptionAvailabilityOut,
    SubscriptionIn,
    SubscriptionRequestOut,
    SubscriptionStatusOut,
    TokenIn,
)
from common.config import get_settings, utc_now
from db.models import AlertSubscription, Station

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

_INVALID_TOKEN = "invalid or expired token"
_RATE_WINDOW = timedelta(hours=1)


def _require_known_stations(db: Session, station_ids: list[str]) -> None:
    known = set(db.execute(select(Station.station_id).where(Station.station_id.in_(station_ids))).scalars().all())
    unknown = [sid for sid in station_ids if sid not in known]
    if unknown:
        # A typo'd id would otherwise "subscribe" the user to alerts that can never arrive.
        raise HTTPException(status_code=422, detail=f"unknown station_ids: {unknown}")


def _send_slot_values(now: datetime):
    """SET-clause for taking one slot of the per-email hourly send budget.
    Evaluated by the database inside the same UPDATE that guards on the
    budget, so concurrent requests cannot exceed the limit."""
    window_over = or_(
        AlertSubscription.email_window_started_at.is_(None),
        AlertSubscription.email_window_started_at <= now - _RATE_WINDOW,
    )
    return window_over, {
        "email_window_started_at": case((window_over, now), else_=AlertSubscription.email_window_started_at),
        "email_send_count": case((window_over, 1), else_=AlertSubscription.email_send_count + 1),
    }


def _register(db: Session, payload: SubscriptionIn, now: datetime) -> tuple[str, str] | None:
    """Apply a subscription request to the database.

    Returns ("confirm" | "manage", plaintext token) when an email must be sent,
    or None when the per-email send limit swallowed the request. Only the
    token's hash is stored.
    """
    settings = get_settings()
    stations, pollutants = payload.station_ids, [p.value for p in payload.pollutants]
    token = new_token()
    token_hash, expires = hash_token(token), expiry_from(now, settings)

    # New address: a pending row (inactive, unconfirmed) that has used 1 email.
    inserted = db.execute(
        insert(AlertSubscription)
        .values(
            subscriber_id=uuid.uuid4(),
            email=payload.email,
            station_ids=stations,
            pollutants=pollutants,
            is_active=False,
            is_confirmed=False,
            created_at=now,
            confirm_token_hash=token_hash,
            confirm_token_expires_at=expires,
            email_window_started_at=now,
            email_send_count=1,
        )
        .on_conflict_do_nothing(index_elements=["email"])
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if inserted is not None:
        return "confirm", token

    window_over, slot = _send_slot_values(now)
    within_budget = or_(window_over, AlertSubscription.email_send_count < settings.subscription_emails_per_hour)

    # Existing PENDING address: nobody has proven ownership, so the latest
    # request simply replaces the pending selection and gets a fresh link (the
    # previous link stops working). It stays inactive until confirmed.
    pending = db.execute(
        update(AlertSubscription)
        .where(
            AlertSubscription.email == payload.email,
            AlertSubscription.is_confirmed.is_(False),
            within_budget,
        )
        .values(
            station_ids=stations,
            pollutants=pollutants,
            is_active=False,
            confirm_token_hash=token_hash,
            confirm_token_expires_at=expires,
            **slot,
        )
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if pending is not None:
        return "confirm", token

    # Existing CONFIRMED address: never touch stations/pollutants/is_active.
    # Only mail its owner a link to manage the subscription.
    confirmed = db.execute(
        update(AlertSubscription)
        .where(
            AlertSubscription.email == payload.email,
            AlertSubscription.is_confirmed.is_(True),
            within_budget,
        )
        .values(manage_token_hash=token_hash, manage_token_expires_at=expires, **slot)
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if confirmed is not None:
        return "manage", token

    return None  # rate limited


def _send_in_background(kind: str, email: str, token: str) -> None:
    # After the response, so latency and SMTP failures cannot reveal whether
    # the address already existed. Never logs the token.
    try:
        (send_confirmation_email if kind == "confirm" else send_manage_email)(email, token)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send %s email to %s", kind, email)


@router.get("/availability", response_model=SubscriptionAvailabilityOut)
def subscription_availability():
    """Lets a client say up front that sign-ups are invite-only (demo mode)."""
    if get_settings().subscription_allowlist() is None:
        return SubscriptionAvailabilityOut(open=True, message="Anyone can subscribe.")
    return SubscriptionAvailabilityOut(
        open=False,
        message="This is a demo: daily emails are limited to invited addresses. "
        "Other addresses will not receive an email.",
    )


@router.post("", status_code=202, response_model=SubscriptionRequestOut)
def request_subscription(payload: SubscriptionIn, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Ask to subscribe. Always answers the same 202 body (no subscriber id, no
    hint whether the address is already known). Emails a confirmation link for
    a new/pending address, or a manage link for a confirmed one; nothing is
    activated or modified until the mailbox owner uses a link.

    In demo mode an address that is not invited gets the same 202, but nothing
    is stored or sent: the reply must not reveal who is invited, and we keep no
    data about people who cannot use the service."""
    _require_known_stations(db, payload.station_ids)
    if not get_settings().may_subscribe(payload.email):
        logger.info("Demo mode: ignored a sign-up from an address that is not invited")
        return SubscriptionRequestOut()
    outcome = _register(db, payload, utc_now())
    db.commit()
    if outcome is not None:
        kind, token = outcome
        background.add_task(_send_in_background, kind, payload.email, token)
    return SubscriptionRequestOut()


@router.post("/confirm", response_model=SubscriptionStatusOut)
def confirm_subscription(payload: TokenIn, db: Session = Depends(get_db)):
    """Single-use: the token is erased from the row the moment it works."""
    now = utc_now()
    row = db.execute(
        update(AlertSubscription)
        .where(
            AlertSubscription.confirm_token_hash == hash_token(payload.token),
            AlertSubscription.confirm_token_expires_at > now,
            AlertSubscription.is_confirmed.is_(False),
        )
        .values(
            is_confirmed=True,
            is_active=True,
            confirmed_at=now,
            confirm_token_hash=None,
            confirm_token_expires_at=None,
        )
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)
    db.commit()
    return SubscriptionStatusOut(status="confirmed")


@router.post("/manage", response_model=SubscriptionStatusOut)
def manage_subscription(payload: ManageIn, db: Session = Depends(get_db)):
    """Replace the stations/pollutants of a confirmed subscription (and switch
    it on, which is how someone who unsubscribed re-subscribes). Authorised
    only by the emailed manage token."""
    _require_known_stations(db, payload.station_ids)
    now = utc_now()
    row = db.execute(
        update(AlertSubscription)
        .where(
            AlertSubscription.manage_token_hash == hash_token(payload.token),
            AlertSubscription.manage_token_expires_at > now,
            AlertSubscription.is_confirmed.is_(True),
        )
        .values(
            station_ids=payload.station_ids,
            pollutants=[p.value for p in payload.pollutants],
            is_active=True,
            unsubscribed_at=None,
        )
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)
    db.commit()
    return SubscriptionStatusOut(status="updated")


def _owner_condition(token: str, now: datetime):
    """SQL condition matching the confirmed subscription `token` authorises:
    the stateless unsubscribe token from an alert email, or an emailed manage
    token. None when the token cannot be valid."""
    if is_unsubscribe_token(token):
        subscriber_id = parse_unsubscribe_token(token)
        if subscriber_id is None:
            return None
        condition = AlertSubscription.subscriber_id == subscriber_id
    else:
        condition = (
            AlertSubscription.manage_token_hash == hash_token(token)
        ) & (AlertSubscription.manage_token_expires_at > now)
    return condition & AlertSubscription.is_confirmed.is_(True)


def _unsubscribe(db: Session, token: str, now: datetime) -> bool:
    """Idempotent; can only ever switch a subscription off."""
    condition = _owner_condition(token, now)
    if condition is None:
        return False
    row = db.execute(
        update(AlertSubscription)
        .where(condition)
        .values(
            is_active=False,
            # Keep the first unsubscribe time on repeats: it starts the retention clock.
            unsubscribed_at=func.coalesce(AlertSubscription.unsubscribed_at, now),
        )
        .returning(AlertSubscription.subscriber_id)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        return False
    db.commit()
    return True


@router.post("/unsubscribe", response_model=SubscriptionStatusOut)
def unsubscribe(payload: TokenIn, db: Session = Depends(get_db)):
    if not _unsubscribe(db, payload.token, utc_now()):
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)
    return SubscriptionStatusOut(status="unsubscribed")


@router.post("/delete", response_model=SubscriptionStatusOut)
def delete_subscription(payload: TokenIn, db: Session = Depends(get_db)):
    """Erase the subscription and its send history ("delete my data"). Same
    tokens as unsubscribing. Irreversible; subscribing again starts over."""
    condition = _owner_condition(payload.token, utc_now())
    subscriber_id = (
        db.execute(select(AlertSubscription.subscriber_id).where(condition)).scalar_one_or_none()
        if condition is not None else None
    )
    if subscriber_id is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)
    erase_subscriber(db, subscriber_id)
    db.commit()
    return SubscriptionStatusOut(status="deleted")


@router.post("/unsubscribe/one-click", response_model=SubscriptionStatusOut)
async def unsubscribe_one_click(token: str, request: Request, db: Session = Depends(get_db)):
    """RFC 8058 target of the List-Unsubscribe header: the mail provider POSTs
    `List-Unsubscribe=One-Click` (form-encoded) with no user interaction."""
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    if form.get("List-Unsubscribe") != ["One-Click"] or len(token) > 256:
        raise HTTPException(status_code=400, detail="expected List-Unsubscribe=One-Click")
    if not await run_in_threadpool(_unsubscribe, db, token, utc_now()):
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)
    return SubscriptionStatusOut(status="unsubscribed")
