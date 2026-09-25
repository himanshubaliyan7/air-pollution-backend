"""owner-verified double opt-in for alert_subscriptions

Adds confirmation state, hashed single-use confirm / multi-use manage tokens
and a per-email send counter (DB-enforced rate limit) to alert_subscriptions.
Touches no other table.

Existing rows: nobody ever proved they own those mailboxes (the old flow had
no confirmation), so none are grandfathered as confirmed - any existing row is
deactivated and must re-subscribe through the new flow. Production had 0 rows
when this shipped (2026-09-25).

Revision ID: 0004_subscription_confirmation
Revises: 0003_station_aqi_snapshots
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_subscription_confirmation"
down_revision: Union[str, None] = "0003_station_aqi_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "alert_subscriptions"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column(_TABLE, sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("confirm_token_hash", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column("confirm_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("manage_token_hash", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column("manage_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("email_window_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("email_send_count", sa.Integer(), nullable=False, server_default=sa.text("0")))

    # Same names Postgres/SQLAlchemy would generate for unique=True columns.
    op.create_unique_constraint("alert_subscriptions_confirm_token_hash_key", _TABLE, ["confirm_token_hash"])
    op.create_unique_constraint("alert_subscriptions_manage_token_hash_key", _TABLE, ["manage_token_hash"])

    op.execute(f"UPDATE {_TABLE} SET is_active = false")


def downgrade() -> None:
    op.drop_constraint("alert_subscriptions_manage_token_hash_key", _TABLE, type_="unique")
    op.drop_constraint("alert_subscriptions_confirm_token_hash_key", _TABLE, type_="unique")
    # Pending (unconfirmed) rows are inactive, so they simply become inactive
    # rows in the old schema; nothing can start receiving alerts by downgrading.
    for col in (
        "email_send_count",
        "email_window_started_at",
        "manage_token_expires_at",
        "manage_token_hash",
        "confirm_token_expires_at",
        "confirm_token_hash",
        "confirmed_at",
        "is_confirmed",
    ):
        op.drop_column(_TABLE, col)
