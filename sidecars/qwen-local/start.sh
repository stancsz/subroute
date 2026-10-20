#!/bin/sh
set -eu

revision=4ca720788d1e01f1bff70c033e0d0028fd02e502
digest=d847e2c1e4aa276e4b7b8e9ad7628050e61e165d49ab995407bc36677a6f3864
model="/root/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/$revision/Qwen3.8-27B-UD-IQ3_S.gguf"
url="https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$revision/Qwen3.8-27B-UD-IQ3_S.gguf"

if [ ! -f "$model" ]; then
    mkdir -p "$(dirname "$model")"
    curl --fail --location --retry 3 --continue-at - --output "$model.partial" "$url"
    printf '%s  %s\n' "$digest" "$model.partial" | sha256sum -c -
    mv "$model.partial" "$model"
fi

printf '%s  %s\n' "$digest" "$model" | sha256sum -c -
exec /app/llama-server --model "$model" "$@"
