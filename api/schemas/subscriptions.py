import uuid

from pydantic import BaseModel, EmailStr


class SubscriptionIn(BaseModel):
    email: EmailStr
    station_ids: list[str]
    pollutants: list[str]


class SubscriptionOut(BaseModel):
    subscriber_id: uuid.UUID
    status: str
