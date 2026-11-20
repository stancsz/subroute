from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "litellm.yaml"


def config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_context_metadata_describes_each_configured_route():
    settings = config()
    assert settings["router_settings"]["enable_pre_call_checks"] is True
    for deployment in settings["model_list"]:
        name = deployment["model_name"]
        info = deployment["model_info"]
        assert not name.endswith("-advisor") and not name.startswith("experts/")
        if name.startswith("catalog-"):
            continue
        speech = name.startswith(("mimo-v2.5-asr", "mimo-v2.5-tts"))
        context = 8000 if speech else 256000
        assert info["max_input_tokens"] == context
        assert info["context_window"] == context


def test_compose_isolates_mutable_state_and_pins_gateway_image():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    prod, staging = services["gateway"], services["gateway-staging"]
    prod_env = dict(item.split("=", 1) for item in prod["environment"] if "=" in item)
    stage_env = dict(item.split("=", 1) for item in staging["environment"] if "=" in item)
    assert prod_env["HEALTH_CHECK_TIMEOUT_SECONDS"] == "10"
    assert stage_env["HEALTH_CHECK_TIMEOUT_SECONDS"] == "10"
    assert "OPENAI_API_KEY" not in prod_env and "OPENAI_API_KEY" not in stage_env
    assert "GEMINI_API_KEY" not in prod_env and "GEMINI_API_KEY" not in stage_env
    assert prod_env["ACTIVE_MODEL_STATE_PATH"] == "/app/legacy-config/active_model.json"
    assert stage_env["ACTIVE_MODEL_STATE_PATH"] == "/app/state/active_model.json"
    assert any(mount.endswith(":/app/legacy-config") for mount in prod["volumes"])
    assert "./src:/app/src:ro" not in prod["volumes"]
    assert "./config:/app/config:ro" not in staging["volumes"]
    assert "./src:/app/src:ro" not in staging["volumes"]
    assert "gateway-staging-state:/app/state" in staging["volumes"]
    assert "@postgres:5432/litellm_staging" in stage_env["DATABASE_URL"]
    assert "@postgres:5432/litellm" in prod_env["DATABASE_URL"]
    assert set(prod["depends_on"]) == set(staging["depends_on"]) == {"postgres"}
    assert set(services["postgres"]["networks"]) == {"production", "staging"}
    assert staging["profiles"] == ["staging"]
    assert services["antigravity"]["profiles"] == ["antigravity"]
    assert not {"postgres-staging", "experts", "experts-staging", "antigravity-staging", "searxng", "searxng-staging"} & services.keys()
    antigravity = services["antigravity"]
    assert "ports" not in antigravity
    assert antigravity["volumes"] == [
        "antigravity-config:/root/.config",
        "antigravity-data:/root/.local/share",
        "antigravity-gemini:/root/.gemini",
    ]
    antigravity_env = dict(
        item.split("=", 1) for item in antigravity["environment"] if "=" in item
    )
    assert antigravity_env["GEMINI_FORCE_FILE_STORAGE"] == "true"
    assert prod["image"] == "subroute-gateway:local"
    assert staging["image"] == "subroute-gateway:staging"
    assert set(prod["networks"]) == {"production"}
    assert set(staging["networks"]) == {"staging"}
    assert set(antigravity["networks"]) == {"production", "staging"}
    assert prod_env["ANTIGRAVITY_BRIDGE_URL"] == stage_env["ANTIGRAVITY_BRIDGE_URL"] == "http://antigravity:4015"
    recipe = (ROOT / prod["build"]["dockerfile"]).read_text()
    assert "FROM python:3.13-slim@sha256:" in recipe
    assert "uv sync --locked --no-dev" in recipe
    assert "--all-extras" not in recipe
    assert "AS uv-tools" in recipe
    assert "onnxruntime" not in recipe
    assert "advisor-ranker" not in recipe
    assert ":latest" not in prod["image"] and ":latest" not in recipe


def test_compose_publishes_gateway_ports_on_loopback():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    assert services["gateway"]["ports"] == ["127.0.0.1:4000:4000"]
    assert services["gateway-staging"]["ports"] == ["127.0.0.1:4005:4005"]


def test_antigravity_image_pins_build_dependencies_and_disables_self_update():
    dockerfile = (ROOT / "sidecars" / "antigravity" / "Dockerfile").read_text()

    assert "FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:" in dockerfile
    assert "ARG TARGETARCH" in dockerfile
    assert 'case "${TARGETARCH}" in' in dockerfile
    assert 'if [ "${TARGETARCH}" = "$(dpkg --print-architecture)" ]' in dockerfile
    runtime_stage = dockerfile.split("\nFROM python:3.13-slim@sha256:", 1)[1]
    assert "apt-get" not in runtime_stage
    assert "curl" not in runtime_stage
    assert "python:3.13-slim@sha256:" in dockerfile
    assert "ARG DEBIAN_SNAPSHOT=20260925T000000Z" in dockerfile
    assert "https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in dockerfile
    assert (
        "https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}"
        in dockerfile
    )
    assert dockerfile.count("apt-get update") == 1
    assert (
        dockerfile.index("> /etc/apt/sources.list")
        < dockerfile.index("apt-get update")
    )
    assert dockerfile.index("apt-get update") < dockerfile.index("apt-get install")
    assert "ARG AGY_VERSION=1.2.11" in dockerfile
    assert (
        "AGY_SHA256_AMD64=c91c62c5e6fa954f5a7e1d7b9ad417d749db4aa60a4ba0b3d604dec1b645d190"
        in dockerfile
    )
    assert (
        "AGY_SHA256_ARM64=01513bc61f9592353045ba801ebb407fbccb8984fcbfde591bc6b681b24e92bc"
        in dockerfile
    )
    assert "AGY_CLI_DISABLE_AUTO_UPDATE=true" in dockerfile
    assert "https://antigravity.google/cli/install.sh" not in dockerfile


def test_all_public_protocols_are_owned_by_litellm_proxy():
    package = ROOT / "src" / "subroute"

    assert not (package / "kernel.py").exists()
    assert not any((package / "protocols").glob("*.py"))
    assert not any((package / "adapters").glob("*.py"))
    assert (package / "handlers" / "antigravity.py").is_file()


def test_standard_channels_use_native_litellm_provider_configuration():
    deployments = config()["model_list"]
    by_name = {deployment["model_name"]: deployment for deployment in deployments}

    assert "openai" not in by_name
    assert by_name["auto"]["model_info"]["selectable"] is False
    assert by_name["auto"]["model_info"]["disable_background_health_check"] is True
    assert "gemini-api" not in by_name
    assert not any(
        "gpt-5.2-codex" in str(deployment.get("litellm_params", {}).get("model", ""))
        for deployment in deployments
    )
    assert by_name["minimax/minimax-m3.1-flash"]["litellm_params"] == {
        "model": "openrouter/minimax/minimax-m3.1-flash",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "allowed_openai_params": ["reasoning"],
    }
    assert not any(name.endswith("-guided") for name in by_name)
    assert by_name["minimax"]["litellm_params"]["model"].startswith("minimax/")
    assert by_name["minimax-tts"]["litellm_params"] == {
        "model": "minimax/speech-2.6-hd",
        "api_key": "os.environ/MINIMAX_API_KEY",
    }
    assert by_name["mimo-v2.5-asr"]["litellm_params"]["model"] == "openai/mimo-v2.5-asr"
    assert by_name["freetoken"]["litellm_params"]["model"].startswith("openai/")
    assert by_name["desktop"]["litellm_params"] == {
        "model": "ollama/qwen2.5-coder",
        "api_base": "os.environ/OLLAMA_API_BASE",
    }
    assert config()["litellm_settings"]["callbacks"] == [
        "subroute.plugins.dynamic_router.dynamic_routing_plugin",
        "subroute.plugins.codex_credentials.codex_credential_refresher",
    ]


def test_subscription_channels_stay_behind_litellm():
    deployments = config()["model_list"]
    by_name = {deployment["model_name"]: deployment for deployment in deployments}

    assert by_name["gemini-subscription"]["litellm_params"]["model"] == (
        "antigravity/gemini-3.8-flash"
    )
    argon = by_name["gemini-subscription-argon"]
    assert argon["litellm_params"]["model"] == "antigravity/gemini-4-argon"
    assert argon["model_info"]["reasoning_efforts"] == ["low", "medium", "high"]
    assert config()["litellm_settings"]["custom_provider_map"] == [
        {
            "provider": "codex-subscription",
            "custom_handler": "subroute.handlers.codex_subscription.codex_subscription_handler",
        },
        {
            "provider": "antigravity",
            "custom_handler": "subroute.handlers.antigravity.antigravity_handler",
        },
    ]
    codex = by_name["codex-subscription"]["litellm_params"]
    assert codex["model"] == "codex-subscription/gpt-6-sol"
    assert codex["store"] is False
    assert codex["allowed_openai_params"] == ["reasoning_effort"]
    assert codex["extra_headers"]["ChatGPT-Account-ID"] == "refreshed-at-dispatch"
    for alias in ("codex-gpt-6.1-sol", "codex-astra", "codex-luna"):
        params = by_name[alias]["litellm_params"]
        assert params["allowed_openai_params"] == ["reasoning_effort"]
        assert params["store"] is False
    assert by_name["codex-gpt-6.1-sol"]["litellm_params"]["model"] == (
        "codex-subscription/gpt-6.1-sol"
    )
    assert by_name["codex-gpt-6.1-sol"]["model_info"]["display_name"] == "OpenAI: GPT-6.1 Sol"
    assert "codex-terra" not in by_name
    assert "codex-reserve" not in by_name
    assert not any("gpt-5" in item["litellm_params"]["model"] for item in deployments)


def test_windows_launchers_force_utf8_and_loopback_defaults():
    gateway = (ROOT / "scripts" / "start-gateway.ps1").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in gateway
    assert '[int]$Port = 4000' in gateway
    assert '[string]$HostAddress = "127.0.0.1"' in gateway
    assert '127.0.0.1:5433/litellm' in gateway
    assert '$env:UI_USERNAME = "admin"' in gateway
    assert 'sk-gateway-local-dev' not in gateway
    assert '.venv\\Scripts\\litellm.exe' in gateway
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    assert "Python3.11.14" in setup
    assert "prisma generate --schema" in setup
    assert not (ROOT / "scripts" / "start-antigravity.ps1").exists()
    assert "4010" not in gateway


def test_gateway_clients_are_keyless_with_litellm_native_local_mode():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'GATEWAY_MASTER_KEY=${GATEWAY_MASTER_KEY:-sk-gateway-local-dev}' not in compose
    services = yaml.safe_load(compose)["services"]
    for service in ("gateway", "gateway-staging"):
        environment = services[service]["environment"]
        assert "GATEWAY_MASTER_KEY" not in environment
        assert "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true" in environment
    assert "master_key" not in config()["general_settings"]

def test_default_alias_and_opt_in_fallbacks_are_native_litellm_settings():
    settings = config()["router_settings"]
    assert settings["model_group_alias"]["default"] == "codex-luna"
    assert settings["model_group_alias"]["openrouter-flash"] == "minimax/minimax-m3.1-flash"
    assert settings["fallbacks"] == [{"auto": ["mimo-v2.6-pro", "codex-luna"]}]
    assert "default_fallbacks" not in settings
    assert settings["num_retries"] == 0
