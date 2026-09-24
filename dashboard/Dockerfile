# syntax=docker/dockerfile:1
FROM python:3.11.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/home/app

# Unprivileged runtime user. Streamlit wants a writable HOME (~/.streamlit),
# so it gets one; in the prod override HOME is a tmpfs on a read-only rootfs.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

COPY docker/requirements/dashboard.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY common ./common
COPY models/exceedance.py ./models/exceedance.py
COPY models/__init__.py ./models/__init__.py
COPY dashboard ./dashboard

USER app

EXPOSE 8501

CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
