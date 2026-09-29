FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build

ARG TARGETARCH
ARG DEBIAN_SNAPSHOT=20260925T000000Z
ARG AGY_VERSION=1.2.11
# Official release asset digests for AGY_VERSION; update these with the version.
ARG AGY_SHA256_AMD64=c91c62c5e6fa954f5a7e1d7b9ad417d749db4aa60a4ba0b3d604dec1b645d190
ARG AGY_SHA256_ARM64=01513bc61f9592353045ba801ebb407fbccb8984fcbfde591bc6b681b24e92bc

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

COPY entrypoint.sh /tmp/entrypoint.sh
RUN sed -i 's/\r$//' /tmp/entrypoint.sh \
    && install -D -m 0755 /tmp/entrypoint.sh /out/entrypoint.sh

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

ENV AGY_CLI_DISABLE_AUTO_UPDATE=true
ENV PATH=/root/.local/bin:${PATH}
WORKDIR /app
COPY --from=build /root/.local/bin/agy /root/.local/bin/agy
COPY --from=build /out/entrypoint.sh /app/entrypoint.sh
COPY bridge.py /app/bridge.py
COPY advisor-agent.md /app/.agents/agents/subroute-advisor/agent.md
COPY target-agent.md /app/.agents/agents/subroute-target/agent.md
EXPOSE 4015
CMD ["/app/entrypoint.sh"]
