"""Create (if missing) and migrate the dedicated integration-test database.

    TEST_DATABASE_URL=postgresql+psycopg2://USER:PASS@localhost:5433/airpollution_test \
        python -m scripts.setup_test_db

The database name must end in "_test": the integration tests TRUNCATE every
application table (see tests/integration/conftest.py), so they must never
share a database with real data.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"


def main() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        sys.exit("TEST_DATABASE_URL is not set")
    parsed = make_url(url)
    if not (parsed.database or "").endswith("_test"):
        sys.exit(f"Refusing: database {parsed.database!r} does not end in '_test'")

    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
            print(f"created database {parsed.database}")
    admin.dispose()

    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        check=True, env=env, cwd=ALEMBIC_INI.parent,
    )
    print("test database migrated to head")


if __name__ == "__main__":
    main()
