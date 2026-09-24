FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && /root/.local/bin/agy --help >/dev/null

ENV PATH=/root/.local/bin:${PATH}
WORKDIR /app
COPY bridge.py /app/bridge.py
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod 755 /app/entrypoint.sh
EXPOSE 4015
CMD ["/app/entrypoint.sh"]
