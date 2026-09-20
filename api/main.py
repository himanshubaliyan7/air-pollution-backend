from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers import exceedance, forecasts, stations, subscriptions
from common.logging_conf import configure_logging

configure_logging()

app = FastAPI(title="Air Pollution Prediction API", version="0.1.0")

app.include_router(stations.router, prefix="/api/v1")
app.include_router(forecasts.router, prefix="/api/v1")
app.include_router(exceedance.router, prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
