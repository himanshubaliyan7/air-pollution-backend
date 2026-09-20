"""add FORECAST to weather_product_type enum

Revision ID: 0002_add_forecast_weather_product_type
Revises: 0001_initial_schema
Create Date: 2026-09-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_forecast"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres 12+ allows ALTER TYPE ... ADD VALUE inside a transaction, as
    # long as the new value isn't used in the same transaction - fine here
    # since this migration only adds the label.
    op.execute("ALTER TYPE weather_product_type ADD VALUE IF NOT EXISTS 'FORECAST'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE - removing an enum label
    # requires rebuilding the type, which isn't worth doing for a downgrade
    # path on a dev/small-prod system. Left as a no-op deliberately.
    pass
