FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    OCI_AUTH=config \
    OCI_PROFILE=DEFAULT \
    OCI_CONFIG_FILE=/home/appuser/.oci/config \
    PORT=8765

WORKDIR ${APP_HOME}

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir oci \
    && useradd --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser index.html maintenance_center.html build_fleet_data.py portal_server.py favicon.svg oci_config.example README.md SECURITY.md fleet_data_sample_json/fleet_data.sample.json ./
RUN chown -R appuser:appuser ${APP_HOME}

USER appuser

EXPOSE 8765

CMD ["sh", "-c", "python portal_server.py --host 0.0.0.0 --port ${PORT} --auth ${OCI_AUTH} --profile ${OCI_PROFILE} --config-file ${OCI_CONFIG_FILE}"]