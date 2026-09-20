FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir sqlalchemy>=2.0 alembic>=1.13 psycopg2-binary>=2.9 \
    pydantic>=2.6 pydantic-settings>=2.2 pyyaml>=6.0

COPY common ./common
COPY db ./db

WORKDIR /app/db

CMD ["alembic", "upgrade", "head"]
