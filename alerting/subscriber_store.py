from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AlertSubscription


def find_subscribers(session: Session, station_id: str, pollutant: str) -> list[AlertSubscription]:
    stmt = select(AlertSubscription).where(AlertSubscription.is_active.is_(True))
    rows = session.execute(stmt).scalars().all()
    return [r for r in rows if station_id in r.station_ids and pollutant in r.pollutants]
