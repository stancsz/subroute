FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS build

ARG TARGETARCH
ARG DEBIAN_SNAPSHOT=20260925T000000Z
ARG AGY_VERSION=1.4.6
# Official release asset digests for AGY_VERSION; update these with the version.
ARG AGY_SHA256_AMD64=513c74ad3e70323839a65d8dccc6f9977b0903e5facd914cbb06be283c2bb640
ARG AGY_SHA256_ARM64=ef88c996178b55a3c5416f2c8b67a9c343ac0a62c900e7b7158ee2ff1135ad96

# The CLI otherwise self-updates after the build has verified the pinned asset.
ENV AGY_CLI_DISABLE_AUTO_UPDATE=true

RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
         "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ trixie main" \
         "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ trixie-updates main" \
         "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/ trixie-security main" \
         > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
         amd64) agy_asset=agy_cli_linux_x64.tar.gz; agy_sha256="$AGY_SHA256_AMD64" ;; \
         arm64) agy_asset=agy_cli_linux_arm64.tar.gz; agy_sha256="$AGY_SHA256_ARM64" ;; \
         *) echo "Unsupported Antigravity CLI architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/${agy_asset}" -o /tmp/agy.tar.gz \
    && echo "${agy_sha256}  /tmp/agy.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity \
    && install -D -m 0755 /tmp/antigravity /root/.local/bin/agy \
    && rm -f /tmp/agy.tar.gz /tmp/antigravity \
    && if [ "${TARGETARCH}" = "$(dpkg --print-architecture)" ]; then /root/.local/bin/agy --help >/dev/null; fi

COPY sidecars/antigravity/entrypoint.sh /tmp/entrypoint.sh
RUN sed -i 's/\r$//' /tmp/entrypoint.sh \
    && install -D -m 0755 /tmp/entrypoint.sh /out/entrypoint.sh

FROM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39

ENV AGY_CLI_DISABLE_AUTO_UPDATE=true
ENV PATH=/root/.local/bin:${PATH}
WORKDIR /app
COPY --from=build /root/.local/bin/agy /root/.local/bin/agy
COPY --from=build /out/entrypoint.sh /app/entrypoint.sh
COPY sidecars/antigravity/bridge.py /app/bridge.py
COPY src/subroute/audio.py /app/audio_validation.py
COPY sidecars/antigravity/audio-agent.md /app/audio-agent.md
COPY sidecars/antigravity/advisor-agent.md /app/.agents/agents/subroute-advisor/agent.md
COPY sidecars/antigravity/target-agent.md /app/.agents/agents/subroute-target/agent.md
EXPOSE 4015
CMD ["/app/entrypoint.sh"]
