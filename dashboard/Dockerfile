FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    pydantic>=2.6 pydantic-settings>=2.2 pyyaml>=6.0 python-dateutil>=2.9 tzdata>=2024.1 \
    streamlit>=1.32 plotly>=5.20 requests>=2.31 pandas>=2.2 numpy>=1.26

COPY common ./common
COPY models/exceedance.py ./models/exceedance.py
COPY models/__init__.py ./models/__init__.py
COPY dashboard ./dashboard

ENV PYTHONPATH=/app

EXPOSE 8501

CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
