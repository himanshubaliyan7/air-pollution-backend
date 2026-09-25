from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from common.constants import Pollutant

MAX_EMAIL_LENGTH = 254  # RFC 5321 path limit
MAX_STATIONS_PER_SUBSCRIPTION = 10
# token_urlsafe(32) is 43 chars; unsubscribe tokens are ~90. Bound the input.
MAX_TOKEN_LENGTH = 256

StationIds = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=64)]],
    Field(min_length=1, max_length=MAX_STATIONS_PER_SUBSCRIPTION),
]


def _dedupe(items: list) -> list:
    return list(dict.fromkeys(items))


class _Selection(BaseModel):
    station_ids: StationIds
    pollutants: list[Pollutant] = Field(min_length=1, max_length=len(Pollutant))

    @field_validator("station_ids", "pollutants")
    @classmethod
    def _no_duplicates(cls, v: list) -> list:
        return _dedupe(v)


class SubscriptionIn(_Selection):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        # EmailStr only lowercases the domain; Foo@x.com and foo@x.com must not
        # become two subscriptions (each would be sent every alert).
        v = v.strip().lower()
        if len(v) > MAX_EMAIL_LENGTH:
            raise ValueError(f"email longer than {MAX_EMAIL_LENGTH} characters")
        return v


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


class ManageIn(_Selection):
    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


class SubscriptionRequestOut(BaseModel):
    """Identical for every valid request, whatever the email's state, so the
    endpoint cannot be used to test whether an address is subscribed."""

    status: str = "check-your-email"
    message: str = (
        "If this address can be subscribed, an email with a link has been sent to it. "
        "Nothing changes until the owner of the address opens that link."
    )


class SubscriptionStatusOut(BaseModel):
    status: str
