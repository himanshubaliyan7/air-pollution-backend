FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    sqlalchemy>=2.0 psycopg2-binary>=2.9 pydantic>=2.6 pydantic-settings>=2.2 pyyaml>=6.0 \
    pandas>=2.2 numpy>=1.26 python-dateutil>=2.9 tzdata>=2024.1 \
    fastapi>=0.110 "uvicorn[standard]>=0.29" email-validator>=2.1 \
    lightgbm>=4.3 scikit-learn>=1.4

COPY common ./common
COPY db ./db
COPY ingestion ./ingestion
COPY features ./features
COPY models ./models
COPY api ./api

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
