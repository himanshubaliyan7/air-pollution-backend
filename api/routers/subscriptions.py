import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.subscriptions import SubscriptionIn, SubscriptionOut
from db.models import AlertSubscription

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut)
def create_or_update_subscription(payload: SubscriptionIn, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    stmt = insert(AlertSubscription).values(
        subscriber_id=uuid.uuid4(),
        email=payload.email,
        station_ids=payload.station_ids,
        pollutants=payload.pollutants,
        is_active=True,
        created_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["email"],
        set_={
            "station_ids": stmt.excluded.station_ids,
            "pollutants": stmt.excluded.pollutants,
            "is_active": True,
        },
    ).returning(AlertSubscription.subscriber_id)
    subscriber_id = db.execute(stmt).scalar_one()
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
