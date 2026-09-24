# syntax=docker/dockerfile:1
FROM python:3.11.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY docker/requirements/db.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY common ./common
COPY db ./db

WORKDIR /app/db

USER app

CMD ["alembic", "upgrade", "head"]
