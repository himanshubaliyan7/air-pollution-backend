# syntax=docker/dockerfile:1
FROM python:3.11.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged runtime user (fixed uid so volume ownership is predictable).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Locked dependency set (docker/requirements/api.txt). NOTE: the previous
# unquoted `pip install pkg>=x` lines were parsed by the shell as output
# redirections, so the version floors were never applied.
COPY docker/requirements/api.txt ./requirements.txt
RUN pip install -r requirements.txt

# Application code stays root-owned and read-only to the runtime user.
COPY common ./common
COPY db ./db
COPY ingestion ./ingestion
COPY features ./features
COPY models ./models
COPY api ./api

USER app

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
