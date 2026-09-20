"""Injectable clock for tests that must not depend on the wall clock.

Several production modules call ``datetime.now(timezone.utc)`` through a
module-level ``datetime`` name (orchestration.plugins.common.tasks,
api.routers.forecasts, ...). Patching that name with ``fixed_datetime(now)``
freezes "now" for that module only, so results cannot flip near an hour
boundary while a test runs.
"""

from datetime import datetime


def fixed_datetime(now: datetime):
    """A ``datetime`` subclass whose ``now()`` always returns ``now``."""
    if now.tzinfo is None:
        raise ValueError("fixed clock must be timezone-aware")

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    return _FixedDatetime
