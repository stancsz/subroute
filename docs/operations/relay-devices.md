# Relay devices: coordinator and container worker

This guide sets up the optional Relay coordinator on device A and its Dockerized outbound worker on device B. The worker polls the coordinator; it does not open or publish an inbound port. Keep the existing gateway profile and saved routing desk unchanged. The worker's `model` remains `default`, following the gateway's saved target. Verify the selected host's client route before queuing work; one workstation's successful smoke does not establish another host's connectivity.

## What each device needs

| Device A: coordinator/operator | Device B: worker |
| --- | --- |
| Docker Engine/Desktop with Compose; Subroute checkout; coordinator image and private `.env.relay`; LAN-reachable port 8765 if B is separate; interactive Claude Code MCP client or optional supervisor. | Docker Engine/Desktop using Linux containers; Subroute checkout and worker image; private workspace, runner config and credentials. |

With the container image, B does not need host Python or a host Claude Code installation. Claude Code runs inside the image. The operator key stays on A and is never placed on B. Each worker has its own node key, matching one entry in A's `RELAY_NODE_KEYS`, plus a gateway key accepted by LiteLLM on port 4000. These credentials are distinct.

The existing `docker-compose.relay.yml` is the optional coordinator on A. The new `docker-compose.relay-worker.yml` is a separate Compose project for B. Both may be tested on one PC, using different project names, a distinct node identity, separate local workspaces/config and private home volumes.

## Prepare device A

From the Subroute checkout on A, make the coordinator's private environment using the existing guide and template. Generate distinct random operator and node credentials; put the node credential in `RELAY_NODE_KEYS` under the worker's exact node name. Never reuse the operator key as a node key.

```powershell
Copy-Item .\sidecars\relay\.env.example .\.env.relay
py -3 -c "import secrets; print(secrets.token_urlsafe(36))"
```

Edit `.env.relay` locally. For a separate B or same-PC container trial, set `RELAY_PUBLISH_IP` to A's reachable LAN IPv4 and add A's host/IP to `RELAY_ALLOWED_HOSTS`; restrict the host firewall to the intended LAN. The hub URL B uses is `http://<A-LAN-IP>:8765`, never `localhost` or the Docker-only name `relay`. The coordinator's default publish address is loopback-only, which a separate worker container cannot reach. The two Compose projects have separate networks, so use A's LAN address rather than `relay` or `localhost` for the provided setup. Confirm the firewall permits the intended connection.

Build and start only the optional coordinator when ready:

```powershell
docker compose --env-file .\.env.relay -f .\docker-compose.relay.yml up -d --build
docker compose --env-file .\.env.relay -f .\docker-compose.relay.yml ps
```

The coordinator uses HTTP on the trusted LAN unless an existing HTTPS reverse proxy is configured. Do not port-forward or expose this service to the public internet.

## Prepare device B

Check out the same Subroute revision on B. Install Docker Desktop with Linux containers or Docker Engine and Compose. Create a dedicated workspace and a private worker environment file. Copy the runner config example, then set `url` to A's reachable hub address and `gateway_url` to the existing LiteLLM host's reachable address. If LiteLLM runs on A, these are `http://<A-LAN-IP>:8765` and `http://<A-LAN-IP>:4000`. Verify that the gateway's existing listener permits that LAN connection. If LiteLLM runs on B instead, use `http://host.docker.internal:4000`. Never use `localhost` for a host service from inside the container, and never use `relay` across this separate Compose project.

```powershell
Copy-Item .\sidecars\relay\.env.worker.example .\sidecars\relay\.env.worker
Copy-Item .\sidecars\relay\runner.container.example.json .\runner.relay.json
```

Edit `runner.relay.json`: change `node` to the node name used in A's `RELAY_NODE_KEYS` and replace both example IP addresses. The example sets `gateway_key_env` to `GATEWAY_MASTER_KEY`, `model` to `default`, the `project` workspace alias to `/workspace`, and private state under `/home/relay/.subroute-relay/node`. Keep the config local and untracked because it contains machine-specific details, even though keys are supplied through the environment.

In `.env.worker`, replace both credential placeholders. `RELAY_NODE_KEY` must exactly match that node's value on A; `GATEWAY_MASTER_KEY` must be a key accepted by the port-4000 gateway. Set `RELAY_WORKSPACE` to B's intended project directory. Set `RELAY_RUNNER_CONFIG` if the config is elsewhere. Keep `.env.worker` and `runner.relay.json` private and out of source control.

The orchestrator owns Northstar planning, review and continuation. The default worker receives a short bounded task contract and reads applicable workspace `AGENTS.md` and `GOAL.md`; no skill bundle is required on B. For selected supplementary guidance, explicitly set `northstar_path` to a local instruction file visible inside the container, using a read-only bind in your own Compose override if needed. A missing configured file stops the runner. This opt-in does not enable a plugin loader or nested delegation.

On Linux, the dedicated workspace must be writable by UID/GID `10001:10001`; for a newly created disposable workspace, `mkdir -p ./relay-workspace` followed by `sudo chown 10001:10001 ./relay-workspace` sets that ownership. For an existing project, use an appropriate local ACL or dedicated writable checkout. Do not point two clients at the same shared writable directory: local locks do not coordinate a shared NAS across machines. Use separate checkouts or worktrees.

Build the worker image and inspect the resolved Compose configuration without starting it:

```powershell
docker build -f .\sidecars\relay\Dockerfile -t subroute-relay:0.1.0 .
docker compose --env-file .\sidecars\relay\.env.worker -f .\docker-compose.relay-worker.yml build
docker compose --env-file .\sidecars\relay\.env.worker -f .\docker-compose.relay-worker.yml config --quiet
```

Start the worker and inspect its logs and coordinator fleet status before queuing a task. Registration and idle polling do not call the model. Queuing a real task launches Claude and consumes inference. Start with one small goal with explicit task, turn and time caps. The separate `scripts/verify_relay.py` default mode is a provider-free fixture harness; it substitutes a fixture CLI and is not a runtime mode of this worker.

```powershell
docker compose --env-file .\sidecars\relay\.env.worker -f .\docker-compose.relay-worker.yml up -d
docker compose --env-file .\sidecars\relay\.env.worker -f .\docker-compose.relay-worker.yml logs --tail 100 worker
```

To prepare disconnected machines, build the coordinator and worker images on a connected machine, then transfer both image archives and the source/configuration through an approved channel. `docker save`/`docker load` can transfer images; do not bundle environment files, runner configs containing secrets, or credentials in the archive. On A and B, load the matching image tag before starting the corresponding Compose project.

## Boundaries and current evidence

The worker container has a read-only root, a private named home volume for state/locks/Claude configuration, a read-only config mount, and only the explicitly mounted workspace as a writable bind. It has no published port, Docker socket, database, or anonymous volumes. Docker is the filesystem mount boundary, not a hostile-code sandbox: approved Bash commands can access the network and use credentials present in the worker environment. Tool approvals and workspace configuration remain policy controls, not protection from malicious code with those permissions.

The one-host Docker trial and its exact result are recorded in [the evidence record](../evals/relay-2026-10-23.md). Two physical devices and sustained operation remain unverified. Follow [the Relay design and operations guide](relay.md) for the host-native Python runner alternative, MCP operator setup, task acceptance, recovery and additional limitations.
