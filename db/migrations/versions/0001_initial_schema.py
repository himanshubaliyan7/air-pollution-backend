"""initial schema + timescaledb hypertables

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Imported lazily so `alembic downgrade`/tooling that doesn't need the
    # repo's full dependency set can still load this module.
    from db.models import Base, HYPERTABLE_SPECS

    bind = op.get_bind()

    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))

    Base.metadata.create_all(bind=bind)

    for table_name, time_column, chunk_interval in HYPERTABLE_SPECS:
        bind.execute(
            sa.text(
                f"""
                SELECT create_hypertable(
                    '{table_name}', '{time_column}',
                    chunk_time_interval => INTERVAL '{chunk_interval}',
                    if_not_exists => TRUE,
                    migrate_data => TRUE
                )
                """
            )
        )


def downgrade() -> None:
    from db.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
