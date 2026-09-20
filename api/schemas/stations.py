from datetime import datetime

from pydantic import BaseModel


class StationOut(BaseModel):
    station_id: str
    name: str
    lat: float
    lon: float
    city: str
    is_active: bool

    model_config = {"from_attributes": True}


class StationDetailOut(StationOut):
    latest_observed_at: datetime | None = None
