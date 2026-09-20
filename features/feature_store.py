"""Read/write access to the `features` table.

feature_set_version comes from config/settings.yaml (features.feature_set_version)
and must be bumped by an operator whenever lag_features.py, time_features.py,
or weather_features.py's transform logic changes, so training never silently
mixes feature vectors computed by two different code versions.
"""

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common.config import get_settings, load_yaml_config
from common.constants import Pollutant
from db.models import Feature


def get_feature_set_version() -> str:
    settings = get_settings()
    cfg = load_yaml_config(settings.settings_config_path)
    return cfg["features"]["feature_set_version"]


def write_features(session: Session, station_id: str, pollutant: Pollutant, feature_frame: pd.DataFrame) -> int:
    if feature_frame.empty:
        return 0

    version = get_feature_set_version()
    now = datetime.now(timezone.utc)

    rows = []
    for feature_time, row in feature_frame.iterrows():
        row_dict = row.to_dict()
        # JSONB requires plain-serializable values; pandas Timestamps/NaT and
        # numpy scalar types don't serialize cleanly.
        clean = {
            k: (None if pd.isna(v) else (bool(v) if isinstance(v, (bool,)) else float(v) if not isinstance(v, str) else v))
            for k, v in row_dict.items()
        }
        rows.append(
            {
                "station_id": station_id,
                "pollutant": pollutant,
                "feature_time": feature_time.to_pydatetime(),
                "feature_set_version": version,
                "features": clean,
                "computed_at": now,
            }
        )

    stmt = insert(Feature).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["station_id", "pollutant", "feature_time", "feature_set_version"],
        set_={"features": stmt.excluded.features, "computed_at": stmt.excluded.computed_at},
    )
    session.execute(stmt)
    session.commit()
    return len(rows)


def read_features(
    session: Session, station_id: str, pollutant: Pollutant, start: datetime, end: datetime, version: str | None = None
) -> pd.DataFrame:
    version = version or get_feature_set_version()
    stmt = (
        select(Feature.feature_time, Feature.features)
        .where(
            Feature.station_id == station_id,
            Feature.pollutant == pollutant,
            Feature.feature_set_version == version,
            Feature.feature_time >= start,
            Feature.feature_time <= end,
        )
        .order_by(Feature.feature_time)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame()
    idx = pd.DatetimeIndex([r.feature_time for r in rows], tz="UTC")
    return pd.DataFrame([r.features for r in rows], index=idx)
