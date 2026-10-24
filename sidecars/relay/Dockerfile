FROM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39
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
