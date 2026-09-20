import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.subscriptions import SubscriptionIn, SubscriptionOut
from db.models import AlertSubscription, Station

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut)
def create_subscription(payload: SubscriptionIn, db: Session = Depends(get_db)):
    """Creates a subscription for a NEW email only.

    Deliberately never modifies, reactivates or reveals an existing row: with
    no proof of email ownership, an upsert would let anyone overwrite a
    victim's stations and read back their subscriber_id (the only credential
    for DELETE). A 409 is returned instead. Changing or re-enabling a
    subscription needs an owner-verified flow (emailed confirmation token),
    which is not built yet.
    """
    known = set(
        db.execute(select(Station.station_id).where(Station.station_id.in_(payload.station_ids))).scalars().all()
    )
    unknown = [sid for sid in payload.station_ids if sid not in known]
    if unknown:
        # A typo'd id would otherwise "subscribe" the user to alerts that can never arrive.
        raise HTTPException(status_code=422, detail=f"unknown station_ids: {unknown}")

    stmt = (
        insert(AlertSubscription)
        .values(
            subscriber_id=uuid.uuid4(),
            email=payload.email,
            station_ids=payload.station_ids,
            pollutants=[p.value for p in payload.pollutants],
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["email"])
        .returning(AlertSubscription.subscriber_id)
    )
    subscriber_id = db.execute(stmt).scalar_one_or_none()
    if subscriber_id is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="this email already has a subscription")
    db.commit()
    return SubscriptionOut(subscriber_id=subscriber_id, status="subscribed")


@router.delete("/{subscriber_id}", response_model=SubscriptionOut)
def unsubscribe(subscriber_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(AlertSubscription, subscriber_id)
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    row.is_active = False
    db.commit()
    return SubscriptionOut(subscriber_id=subscriber_id, status="unsubscribed")
