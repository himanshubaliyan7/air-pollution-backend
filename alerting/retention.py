"""Keeping subscriber data only as long as it is needed.

Email addresses of school staff are personal data (India's DPDP Act 2023:
purpose limitation, erasure on request, no retention past the purpose). So:

* "delete my data" erases a subscriber and every log row about them;
* sign-ups never confirmed are deleted 7 days after they were made (the
  confirm link is long dead by then);
* unsubscribed subscriptions are deleted 90 days after unsubscribing (long
  enough to switch alerts back on from a manage link).

Stdlib + SQLAlchemy only: the API image imports this module.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import AlertLog, AlertSubscription, DigestLog

logger = logging.getLogger(__name__)

UNCONFIRMED_RETENTION = timedelta(days=7)
UNSUBSCRIBED_RETENTION = timedelta(days=90)


def erase_subscriber(session: Session, subscriber_id) -> None:
    """Delete one subscriber and their send history. Caller commits."""
    session.execute(delete(DigestLog).where(DigestLog.subscriber_id == subscriber_id))
    session.execute(delete(AlertLog).where(AlertLog.subscriber_id == subscriber_id))
    session.execute(delete(AlertSubscription).where(AlertSubscription.subscriber_id == subscriber_id))


def purge_subscriptions(session: Session, now: datetime) -> dict[str, int]:
    unconfirmed = session.execute(
        select(AlertSubscription.subscriber_id).where(
            AlertSubscription.is_confirmed.is_(False),
            AlertSubscription.created_at < now - UNCONFIRMED_RETENTION,
        )
    ).scalars().all()
    unsubscribed = session.execute(
        select(AlertSubscription.subscriber_id).where(
            AlertSubscription.is_active.is_(False),
            AlertSubscription.unsubscribed_at < now - UNSUBSCRIBED_RETENTION,
        )
    ).scalars().all()
    for subscriber_id in {*unconfirmed, *unsubscribed}:
        erase_subscriber(session, subscriber_id)
    session.commit()
    result = {"unconfirmed_deleted": len(unconfirmed), "unsubscribed_deleted": len(unsubscribed)}
    logger.info("Subscription retention: %s", result)
    return result
