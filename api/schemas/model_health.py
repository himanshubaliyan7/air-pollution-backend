from datetime import datetime

from pydantic import BaseModel


class ModelHealthOut(BaseModel):
    station_id: str
    pollutant: str
    horizon_hours: int
    precision: float | None
    recall: float | None
    f1: float | None
    mae: float | None
    rmse: float | None
    evaluation_window_end: datetime
