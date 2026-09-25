from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AlertSubscription


def find_subscribers(session: Session, station_id: str, pollutant: str) -> list[AlertSubscription]:
    """Only owner-confirmed AND active subscriptions ever receive alerts; a
    pending (unconfirmed) row must never be emailed. Filtered in SQL."""
    stmt = select(AlertSubscription).where(
        AlertSubscription.is_confirmed.is_(True),
        AlertSubscription.is_active.is_(True),
        AlertSubscription.station_ids.contains([station_id]),
        AlertSubscription.pollutants.contains([pollutant]),
    )
    return list(session.execute(stmt).scalars().all())
