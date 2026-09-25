"""owner-verified double opt-in for alert_subscriptions, plus the daily digest

Adds confirmation state, hashed single-use confirm / multi-use manage tokens,
a per-email send counter (DB-enforced rate limit) and unsubscribed_at (for
retention) to alert_subscriptions, and a digest_log table (one row per
subscriber per digest day).

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


_NEW_COLUMNS = (
    sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("confirm_token_hash", sa.String(), nullable=True),
    sa.Column("confirm_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("manage_token_hash", sa.String(), nullable=True),
    sa.Column("manage_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("email_window_started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("email_send_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
)
# Same names Postgres/SQLAlchemy would generate for unique=True columns.
_UNIQUE = {
    "alert_subscriptions_confirm_token_hash_key": "confirm_token_hash",
    "alert_subscriptions_manage_token_hash_key": "manage_token_hash",
}


def upgrade() -> None:
    # 0001 builds the schema with metadata.create_all from the CURRENT models,
    # so a database created from scratch already has all of this; an existing
    # one does not. Only what is missing is added (same approach as 0003).
    from db.models import DigestLog

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    upgrading_old_table = "is_confirmed" not in existing
    for column in _NEW_COLUMNS:
        if column.name not in existing:
            op.add_column(_TABLE, column)

    constraints = {c["name"] for c in inspector.get_unique_constraints(_TABLE)}
    for name, column in _UNIQUE.items():
        if name not in constraints:
            op.create_unique_constraint(name, _TABLE, [column])

    if upgrading_old_table:
        # Nobody ever proved ownership of pre-existing rows.
        op.execute(f"UPDATE {_TABLE} SET is_active = false")

    DigestLog.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    from db.models import DigestLog

    DigestLog.__table__.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_constraint("alert_subscriptions_manage_token_hash_key", _TABLE, type_="unique")
    op.drop_constraint("alert_subscriptions_confirm_token_hash_key", _TABLE, type_="unique")
    # Pending (unconfirmed) rows are inactive, so they simply become inactive
    # rows in the old schema; nothing can start receiving alerts by downgrading.
    for col in (
        "unsubscribed_at",
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
