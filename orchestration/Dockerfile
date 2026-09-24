FROM apache/airflow:2.9.3-python3.11

USER root
# netCDF4/xarray need libhdf5/libnetcdf at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libhdf5-dev libnetcdf-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

USER airflow

WORKDIR /app

COPY orchestration/requirements-airflow.txt ./
RUN pip install --no-cache-dir -r requirements-airflow.txt

COPY common ./common
COPY db ./db
COPY ingestion ./ingestion
COPY features ./features
COPY models ./models
COPY alerting ./alerting
COPY orchestration/dags /opt/airflow/dags
COPY orchestration/plugins /app/orchestration/plugins
COPY orchestration/include /opt/airflow/include

# model_artifacts named volume (docker-compose.yml) mounts over this path;
# Docker seeds a fresh named volume's initial content/ownership from
# whatever is here at image build time, so this needs to be owned by the
# airflow user for the airflow user to actually write artifacts into it.
# Done as root + explicit chown rather than relying on the preceding COPYs
# (run under USER airflow) to have left /app owned by that user - that
# implicit ownership isn't guaranteed across Docker/BuildKit versions (seen
# failing with "Permission denied" on a newer engine even though the same
# Dockerfile content built fine on an older one).
USER root
# No group chown: the official image puts `airflow` in group `root` (gid 0),
# not a dedicated `airflow` group (confirmed - `chown airflow:airflow` fails
# with "invalid group"), so only the user ownership needs setting here.
RUN mkdir -p /app/models/artifacts && chown -R airflow /app/models/artifacts
USER airflow

ENV PYTHONPATH=/app
ENV AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags

USER root
COPY orchestration/entrypoint.sh /custom-entrypoint.sh
RUN chmod +x /custom-entrypoint.sh
USER airflow

ENTRYPOINT ["/custom-entrypoint.sh"]
