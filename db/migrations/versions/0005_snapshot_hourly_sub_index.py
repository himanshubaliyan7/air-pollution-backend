"""station_aqi_snapshots: hourly sub-index and source feed

Adds sub_index_hourly (only CPCB's direct CAAQMS feed publishes it) and source
("cpcb-caaqms" / "data-gov-in"; null for rows stored before this revision).
Both nullable, so existing rows need no backfill.

Revision ID: 0005_snapshot_hourly_sub_index
Revises: 0004_subscription_confirmation
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_snapshot_hourly_sub_index"
down_revision: Union[str, None] = "0004_subscription_confirmation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "station_aqi_snapshots"
_NEW_COLUMNS = (
    sa.Column("sub_index_hourly", sa.Float(), nullable=True),
    sa.Column("source", sa.String(), nullable=True),
)


def upgrade() -> None:
    # 0001 builds the schema from the CURRENT models, so a database created from
    # scratch already has these columns; only what is missing is added (as in 0004).
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for column in _NEW_COLUMNS:
        if column.name not in existing:
            op.add_column(_TABLE, column)


def downgrade() -> None:
    for column in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, column.name)
