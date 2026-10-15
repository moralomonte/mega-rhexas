# Coordinator only. The astronomy stack stays on the workers.
FROM python:3.12-slim

RUN useradd --system --uid 10001 --home /var/lib/rhexas --shell /usr/sbin/nologin rhexas \
    && mkdir -p /var/lib/rhexas \
    && chown rhexas:rhexas /var/lib/rhexas

WORKDIR /opt/rhexas
COPY src/rhexas/__init__.py /opt/rhexas/src/rhexas/__init__.py
COPY src/rhexas/config.py /opt/rhexas/src/rhexas/config.py
COPY src/rhexas/tics.py /opt/rhexas/src/rhexas/tics.py
COPY src/rhexas/coord /opt/rhexas/src/rhexas/coord
COPY deploy/entrypoint.py /opt/rhexas/entrypoint.py

ENV PYTHONPATH=/opt/rhexas/src \
    PYTHONUNBUFFERED=1 \
    RHEXAS_DATA=/var/lib/rhexas

EXPOSE 8787
VOLUME ["/var/lib/rhexas"]
ENTRYPOINT ["python", "/opt/rhexas/entrypoint.py"]
CMD ["serve", "--config", "/var/lib/rhexas/coord.toml"]
