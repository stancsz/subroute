FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY sidecars/relay/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 relay && useradd --uid 10001 --gid relay --no-create-home relay \
    && mkdir /data && chown relay:relay /data
COPY src/subroute/relay /app/src/subroute/relay
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "subroute.relay.server"]
