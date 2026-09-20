from datetime import datetime

from pydantic import BaseModel


class StationOut(BaseModel):
    station_id: str
    name: str
    lat: float
    lon: float
    city: str
    is_active: bool
    # Newest sensor reading we hold, and whether a forecast current enough to
    # act on exists - the upstream feed lags badly, so most stations have none.
    latest_observed_at: datetime | None = None
    has_current_forecast: bool = False

    model_config = {"from_attributes": True}


class StationDetailOut(StationOut):
    pass
