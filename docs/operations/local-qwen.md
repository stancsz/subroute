# Run Qwen locally with Subroute

Subroute supports two local Qwen paths. Both keep LiteLLM as the OpenAI-compatible gateway and provider adapter. The inference runtime and model are different:

| Path | Inference service | Subroute model | Current model |
| --- | --- | --- | --- |
| Compose CUDA | `qwen-local`, using llama.cpp | `qwen3.8-27b` | Unsloth Dynamic 3.0 Qwen3.8-27B `UD-IQ2_XXS` GGUF |
| Ollama | Ollama server on the host | `desktop` | `qwen2.5-coder` |

Use the Compose path for the repository's pinned Qwen3.8 setup. Use Ollama when you want Ollama to manage model downloads and GPU placement. The aliases are not interchangeable: changing the engine also changes the model and template.

**Current status (2026-10-21):** The owner approved two-bit IQ2_XXS, two concurrent requests, GPU-only model residency and a monitored 10 GiB VRAM target. The saved Compose configuration now matches that profile. All 65/65 layers, input embedding and model/KV/recurrent state were verified on GPU in the standalone trial, at about 9.25 GiB measured allocation. Gaming is explicitly deferred. The native trials remain failed overall despite a clean single 32K terminal result and 30 to 34 tokens/s during shared generation. The bounded Claude run ended with one useful integrated regression, unreliable further test generation and 28.06 active minutes across harness changes. Qwen is stopped after clean unloading. Read [the current budget and evidence](qwen-gpu-only-budget.md) before relying on performance or coexistence. The gaming override only changes restart behavior, keeping model settings owned by the base file.

## GPU prerequisites

These steps cover the NVIDIA GPU path configured by the repository. A host-installed Ollama uses its own GPU backend; the NVIDIA Container Toolkit is only needed for GPU access from Docker containers.

1. Install a current NVIDIA driver and confirm `nvidia-smi` works on the host.
2. On Linux Docker Engine, install and configure the NVIDIA Container Toolkit for Docker. On Windows, use Docker Desktop with its WSL 2 engine and NVIDIA GPU support.
3. Confirm Docker can see the GPU before starting the model:

   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
   ```

   The specific CUDA test image can be changed to one compatible with the host driver. This check validates GPU passthrough; it does not validate model capacity.

The Compose service reserves one NVIDIA GPU in `docker-compose.yml`. It does not install host GPU drivers or the NVIDIA Container Toolkit. VRAM is shared with other host and container workloads.

## Option A: repository-managed llama.cpp service

The `qwen-local` service uses a digest-pinned CUDA-enabled llama.cpp image. On first start, [`start.sh`](../../sidecars/qwen-local/start.sh) downloads the pinned Unsloth Dynamic 3.0 `UD-IQ2_XXS` GGUF revision, verifies its SHA-256, and stores it in the persistent `qwen-local-models` volume. The download is 7.27 GB (decimal). The model API listens inside Compose at `qwen-local:8080`; Compose publishes it on the host at `127.0.0.1:8088`.

From the repository root, start the gateway and Qwen service:

```powershell
docker compose build gateway
docker compose up -d gateway qwen-local
```

If the gateway is already running, the second command is sufficient. `docker compose up -d` also starts this service, but `docker compose up -d gateway` alone does not. The first model download can take a while; follow progress with:

```powershell
docker compose logs -f qwen-local
```

The service becomes ready when its `/health` endpoint reports success. Check the host endpoint and the gateway's model list:

```powershell
Invoke-RestMethod http://127.0.0.1:8088/health
Invoke-RestMethod http://127.0.0.1:4000/v1/models
```

Select `qwen3.8-27b` in the control desk at <http://127.0.0.1:4000/control>, or make a direct OpenAI-compatible request:

```powershell
curl.exe http://127.0.0.1:4000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Reply with one sentence."}]}'
```

For clients using the Anthropic Messages API, use the existing local Qwen route documented in the [Claude Code setup](../../README.md#local-qwen-runtimes). In both cases, if the saved model-routing mode is `force`, switch it to `alias` or `off` so the explicit Qwen model selection reaches its route.

The server configuration uses **two request slots**, 81,920 total tokens and a 40,960-token limit per slot. Each request keeps the 32,768-token Sift/input target and 4,096-token output cap, leaving 4,096 tokens of allowance. Do not fill all 40,960 tokens with prompt plus output: the pinned server needs a boundary margin. The profile uses all GPU layers, explicit CUDA0 input-embedding placement, GPU Q4_0 KV, fitting disabled and host prompt-cache/context checkpoints disabled. Reasoning output is disabled. Compose limits inference to six CPUs and **12 GiB RAM without swap**; batch/microbatch sizes are 128/32. The vision projector is disabled. The **10 GiB VRAM target is monitored, not a Docker-enforced quota**. GPU residency does not reserve GPU compute for games. Two-bit quantization trades weight precision for fit; the real test-writing workload and a later visible-game trial are required to judge usefulness. See [the current budget](qwen-gpu-only-budget.md) for exact pins, failed tests and measured limits.

Sift is active by default on the local Qwen route. The repository Qwen CLI runner defaults to a 256,000-token client history window, 90% client compaction, 4K output and disabled thinking. Gateway Sift stays at32K and native slots stay40K; a client override up to1,000,000is available, with full-size latency/quality unverified. See [the current productive defaults and workload evidence](qwen-gpu-only-budget.md#productive-qwen-defaults-and-corrected-workload).

Useful lifecycle commands:

```powershell
docker compose ps qwen-local
docker compose logs --tail 100 qwen-local
docker compose stop qwen-local
docker compose start qwen-local
```

The model cache persists in `qwen-local-models`. Removing the container does not remove the named volume. Do not use `docker compose down -v` unless you intend to delete all Compose volumes, including the model cache and databases.

## Option B: host Ollama server

The repository's `desktop` alias is configured as `ollama/qwen2.5-coder` in [`config/litellm.yaml`](../../config/litellm.yaml). Ollama runs separately from the gateway. On Windows, install Ollama on the host, then pull and run the configured model:

```powershell
ollama pull qwen2.5-coder
ollama run qwen2.5-coder
```

Exit the interactive prompt after confirming it responds. The Ollama server continues to run in the background. Compose already gives both gateway services this default endpoint:

```text
http://host.docker.internal:11434
```

The `desktop` alias is also the default for the host-run gateway script, which connects to `http://127.0.0.1:11434`. Select `desktop` in the Subroute control desk or request that model through `http://127.0.0.1:4000/v1`. To see what is installed, run `ollama list`; to confirm the loaded model is using the GPU, run an inference and check `ollama ps` for its processor placement while it is loaded.

On Linux, host Ollama commonly listens only on loopback, which a Compose container cannot reach through the host gateway. Configure the Ollama service to listen on a host interface reachable from the Docker bridge (commonly `OLLAMA_HOST=0.0.0.0:11434`), keep port 11434 restricted with the host firewall, and retain the Compose default `OLLAMA_API_BASE=http://host.docker.internal:11434`. If the Ollama server uses another address or port, set `OLLAMA_API_BASE` in the environment used by Compose, such as the repo-root `.env` file. For the host-run gateway, set it in the shell that launches the gateway to the Ollama URL reachable from that host process.

For a systemd-managed Linux Ollama server, set `OLLAMA_HOST` in a service override and restart the service:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Create/edit the override with `sudo systemctl edit ollama`, then run `sudo systemctl daemon-reload` and `sudo systemctl restart ollama`. Restrict access to the Docker host/bridge in the host firewall because Ollama's local API does not require authentication.

The current alias names `qwen2.5-coder`. To use a different Qwen model already installed in Ollama, update the provider model under `desktop` in `config/litellm.yaml` to the exact Ollama model name, then restart the gateway. Do not change the public alias unless you also update client selections.

### Run Ollama in Docker instead

Ollama also publishes a Docker image. For NVIDIA GPUs, its documented container invocation uses `--gpus all` and a persistent `/root/.ollama` volume. Linux Docker hosts need the NVIDIA Container Toolkit configured first. To keep Ollama private and reachable from Subroute on either Windows or Linux, attach its container directly to the Compose network instead of publishing its API port. Start the gateway once to create the network, then start Ollama on it:

```powershell
docker compose up -d gateway
docker run -d --name ollama --gpus all `
  --network subroute_default --network-alias ollama `
  -v ollama:/root/.ollama `
  ollama/ollama
docker exec ollama ollama pull qwen2.5-coder
```

Set `OLLAMA_API_BASE=http://ollama:11434` in the repo-root `.env` file, replacing any existing value rather than adding a duplicate. Then recreate the gateway with `docker compose up -d gateway` (and `gateway-staging` if you use staging). The repo's default project name is `subroute`; if you set a custom Compose project name, use its default network name in `--network`. A standalone container on a different network is not reachable by the service name.

The repository does not declare an Ollama container service. This avoids adding another inference service to the default Compose stack and lets Ollama's version, model cache, and lifetime remain host-managed. Refer to [Ollama's Docker instructions](https://docs.ollama.com/docker) for its current GPU image commands.

## Troubleshooting

- **Compose reports no compatible GPU:** verify `nvidia-smi` on the host, then verify Docker GPU passthrough with the test command above. On Linux, check NVIDIA Container Toolkit configuration and restart Docker after configuring it.
- **Qwen is slow or fails to load:** check `docker compose logs qwen-local` and free VRAM on the host. The service uses all-GPU placement with the IQ2 model and two slots. Verify the model pin and allocation logs against the current budget. Docker does not enforce a 10 GiB GPU quota; stop inference if measured allocation exceeds the target. A restart alone does not retune placement.
- **Ollama works on the host but not through Subroute:** check `OLLAMA_API_BASE` from the gateway container's point of view. On Windows Docker Desktop, the default host alias is configured. On Linux, confirm Ollama listens on an address the Compose bridge can reach and that the firewall permits that connection.
- **Model selection appears ignored:** inspect the saved policy in the control desk. In `force` mode, choose `alias` or `off` for explicit local model selection.
- **GPU use is unclear in Ollama:** keep a model loaded by sending a request, then inspect `ollama ps` and host GPU utilization. Host `nvidia-smi` alone does not show whether a specific model is fully or partly offloaded.

## References

- [Docker Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/)
- [llama.cpp Docker and CUDA](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)
- [Ollama GPU support](https://docs.ollama.com/gpu)
- [Ollama Docker instructions](https://docs.ollama.com/docker)
- [Ollama model library](https://ollama.com/library)
