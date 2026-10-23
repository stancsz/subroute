#!/bin/sh
set -eu

revision=4ca720788d1e01f1bff70c033e0d0028fd02e502
digest=e792d8fb3142fe6d9171876d6da0f71f05a71028718debc72dbec93ff645e67d
model="/root/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/$revision/Qwen3.8-27B-UD-IQ2_XXS.gguf"
url="https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$revision/Qwen3.8-27B-UD-IQ2_XXS.gguf"

if [ ! -f "$model" ]; then
    mkdir -p "$(dirname "$model")"
    curl --fail --location --retry 3 --continue-at - --output "$model.partial" "$url"
    printf '%s  %s\n' "$digest" "$model.partial" | sha256sum -c -
    mv "$model.partial" "$model"
fi

printf '%s  %s\n' "$digest" "$model" | sha256sum -c -

# GPU placement is explicit in Compose. WSL CUDA and Windows free-memory
# reporting disagreed during the gaming trial, making autofitting unreliable.
exec /app/llama-server --model "$model" "$@"
