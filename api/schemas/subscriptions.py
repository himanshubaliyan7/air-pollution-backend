import uuid
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from common.constants import Pollutant

MAX_EMAIL_LENGTH = 254  # RFC 5321 path limit
MAX_STATIONS_PER_SUBSCRIPTION = 10


def _dedupe(items: list) -> list:
    return list(dict.fromkeys(items))


class SubscriptionIn(BaseModel):
    email: EmailStr
    station_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=MAX_STATIONS_PER_SUBSCRIPTION
    )
    pollutants: list[Pollutant] = Field(min_length=1, max_length=len(Pollutant))

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        # EmailStr only lowercases the domain; Foo@x.com and foo@x.com must not
        # become two subscriptions (each would be sent every alert).
        v = v.strip().lower()
        if len(v) > MAX_EMAIL_LENGTH:
            raise ValueError(f"email longer than {MAX_EMAIL_LENGTH} characters")
        return v

    @field_validator("station_ids", "pollutants")
    @classmethod
    def _no_duplicates(cls, v: list) -> list:
        return _dedupe(v)


class SubscriptionOut(BaseModel):
    subscriber_id: uuid.UUID
    status: str
