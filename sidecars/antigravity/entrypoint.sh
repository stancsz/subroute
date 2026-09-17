#!/bin/sh
set -eu

mkdir -p \
    /root/.config \
    /root/.gemini/antigravity-cli/cache

if [ ! -s /root/.gemini/antigravity-cli/settings.json ]; then
    printf '%s\n' '{"onboardingComplete":true,"trustedWorkspaces":["/app"]}' \
        > /root/.gemini/antigravity-cli/settings.json
fi
if [ ! -s /root/.gemini/antigravity-cli/cache/onboarding.json ]; then
    printf '%s\n' '{"consumerOnboardingComplete":true,"enterpriseOnboardingComplete":true,"onboardingComplete":true}' \
        > /root/.gemini/antigravity-cli/cache/onboarding.json
fi

host_auth=/root/.config/agy-host-auth
if [ -s "$host_auth" ]; then
    umask 077
    mv "$host_auth" /root/.gemini/antigravity-cli/antigravity-oauth-token
fi

exec python /app/bridge.py
