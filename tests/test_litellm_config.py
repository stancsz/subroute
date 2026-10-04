from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "litellm.yaml"
EXPERTS_CONFIG_PATH = ROOT / "config" / "litellm.experts.yaml"


def config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def experts_config() -> dict:
    return yaml.safe_load(EXPERTS_CONFIG_PATH.read_text(encoding="utf-8"))


def test_target_and_advisor_routes_declare_separate_context_policies():
    for settings, experts in ((config(), False), (experts_config(), True)):
        assert settings["router_settings"]["enable_pre_call_checks"] is True
        for deployment in settings["model_list"]:
            info = deployment["model_info"]
            is_advisor = experts or deployment["model_name"].endswith("-advisor")
            context = 32000 if is_advisor else 256000
            assert info["max_input_tokens"] == context, deployment["model_name"]
            assert info["context_window"] == context
            assert info["auto_compact_token_limit"] == context * 0.8
            if info.get("advisor_selectable"):
                if info["max_input_tokens"] == 256000:
                    assert info["advisor_max_input_tokens"] == 32000
            assert info["auto_compact_token_limit"] == info["context_window"] * 0.8
            assert info["compaction_scope"] == "total"


def test_compose_isolates_mutable_state_and_pins_gateway_image():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    prod, staging = services["gateway"], services["gateway-staging"]
    prod_env = dict(item.split("=", 1) for item in prod["environment"] if "=" in item)
    stage_env = dict(item.split("=", 1) for item in staging["environment"] if "=" in item)
    assert prod_env["HEALTH_CHECK_TIMEOUT_SECONDS"] == "10"
    assert stage_env["HEALTH_CHECK_TIMEOUT_SECONDS"] == "10"
    assert "OPENAI_API_KEY" not in prod_env and "OPENAI_API_KEY" not in stage_env
    assert "GEMINI_API_KEY" not in prod_env and "GEMINI_API_KEY" not in stage_env
    assert prod_env["ACTIVE_MODEL_STATE_PATH"] != stage_env["ACTIVE_MODEL_STATE_PATH"]
    assert "./config:/app/config:ro" in staging["volumes"]
    assert "gateway-staging-state:/app/state" in staging["volumes"]
    assert "@postgres-staging:5432/litellm_staging" in stage_env["DATABASE_URL"]
    assert "@postgres:5432/litellm" in prod_env["DATABASE_URL"]
    assert set(prod["depends_on"]) == {"postgres", "antigravity"}
    assert set(staging["depends_on"]) == {"postgres-staging", "antigravity"}
    assert prod["depends_on"]["antigravity"]["condition"] == "service_healthy"
    assert staging["depends_on"]["antigravity"]["condition"] == "service_healthy"
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
    assert set(services["postgres"]["volumes"]).isdisjoint(services["postgres-staging"]["volumes"])
    assert prod["image"] == staging["image"]
    assert "@sha256:" in prod["image"]


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
    assert (package / "plugins" / "advisor_plugin.py").is_file()


def test_standard_channels_use_native_litellm_provider_configuration():
    deployments = config()["model_list"]
    by_name = {deployment["model_name"]: deployment for deployment in deployments}

    assert "openai" not in by_name
    assert by_name["current"]["model_info"]["selectable"] is False
    assert by_name["current"]["model_info"]["disable_background_health_check"] is True
    assert "gemini-api" not in by_name
    assert not any(
        "gpt-5.2-codex" in str(deployment.get("litellm_params", {}).get("model", ""))
        for deployment in deployments
    )
    assert by_name["openrouter"]["litellm_params"] == {
        "model": "openrouter/minimax/minimax-m3",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "reasoning": {"exclude": True},
    }
    assert not any(name.endswith("-guided") for name in by_name)
    assert by_name["minimax"]["litellm_params"]["model"].startswith("minimax/")
    assert by_name["minimax-tts"]["litellm_params"] == {
        "model": "minimax/speech-2.6-hd",
        "api_key": "os.environ/MINIMAX_API_KEY",
    }
    assert "speech-synthesis" in by_name["minimax-tts"]["model_info"]["capabilities"]
    assert by_name["mimo-v2.5-asr"]["litellm_params"]["model"] == "openai/mimo-v2.5-asr"
    assert "speech-recognition" in by_name["mimo-v2.5-asr"]["model_info"]["capabilities"]
    assert by_name["freetoken"]["litellm_params"]["model"].startswith("openai/")
    assert by_name["desktop"]["litellm_params"] == {
        "model": "ollama/qwen2.5-coder",
        "api_base": "os.environ/OLLAMA_API_BASE",
    }
    assert config()["litellm_settings"]["callbacks"] == [
        "subroute.plugins.dynamic_router.dynamic_routing_plugin",
        "subroute.plugins.advisor_plugin.advisor_plugin_instance",
        "subroute.plugins.codex_credentials.codex_credential_refresher",
    ]


def test_subscription_channels_stay_behind_litellm():
    deployments = config()["model_list"]
    by_name = {deployment["model_name"]: deployment for deployment in deployments}

    assert by_name["gemini-subscription"]["litellm_params"]["model"] == (
        "antigravity/gemini-3.8-flash"
    )
    for name in (
        "gemini-subscription",
        "gemini-subscription-3.7-flash",
        "gemini-subscription-3.6-flash",
        "gemini-subscription-pro",
    ):
        capabilities = set(by_name[name]["model_info"]["capabilities"])
        assert "buffered-sse" in capabilities
        assert "streaming" not in capabilities
    assert config()["litellm_settings"]["custom_provider_map"] == [
        {
            "provider": "codex-subscription",
            "custom_handler": "subroute.handlers.codex_subscription.codex_subscription_handler",
        },
        {
            "provider": "antigravity",
            "custom_handler": "subroute.handlers.antigravity.antigravity_handler",
        },
        {
            "provider": "codex-advisor",
            "custom_handler": "subroute.handlers.codex_advisor.codex_advisor_handler",
        },
    ]
    codex = by_name["codex-subscription"]["litellm_params"]
    assert codex["model"] == "codex-subscription/gpt-6-sol"
    assert codex["store"] is False
    assert codex["allowed_openai_params"] == ["reasoning_effort"]
    assert codex["extra_headers"]["ChatGPT-Account-ID"] == "refreshed-at-dispatch"
    for alias in ("codex-astra", "codex-terra", "codex-luna", "codex-reserve"):
        params = by_name[alias]["litellm_params"]
        assert params["allowed_openai_params"] == ["reasoning_effort"]
        assert params["store"] is False
    assert by_name["codex-reserve"]["litellm_params"]["model"] == (
        "codex-subscription/gpt-5.6-luna"
    )
    assert by_name["codex-reserve"]["model_info"]["display_name"] == (
        "GPT-5.6 Luna (reserve-capable)"
    )
    assert by_name["codex-gpt-6.1-sol-advisor"]["litellm_params"]["model"] == "codex-advisor/gpt-6.1-sol"
    assert by_name["codex-sol-advisor"]["litellm_params"]["model"] == (
        "codex-advisor/gpt-6-sol"
    )
    assert by_name["codex-astra-advisor"]["litellm_params"]["model"] == (
        "codex-advisor/gpt-6-astra"
    )
    luna_advisor = by_name["codex-luna-advisor"]
    assert luna_advisor["model_info"]["advisor_selectable"] is True
    assert luna_advisor["model_info"]["selectable"] is False
    assert luna_advisor["litellm_params"]["model"] == "codex-advisor/gpt-6-luna"
    assert by_name["gemini-subscription"]["litellm_params"]["model"] == (
        "antigravity/gemini-3.8-flash"
    )


def test_only_explicit_auto_route_has_fallbacks_without_retries():
    settings = config()
    routing = settings["router_settings"]

    assert routing["num_retries"] == 0
    assert routing["fallbacks"] == [
        {"auto": ["auto-gemini-subscription"]},
        {"auto-gemini-subscription": ["auto-codex-luna"]},
    ]
    assert settings["litellm_settings"]["drop_params"] is True
    assert settings["general_settings"]["master_key"] == (
        "os.environ/GATEWAY_MASTER_KEY"
    )
    assert settings["general_settings"]["database_url"] == (
        "os.environ/DATABASE_URL"
    )
    assert settings["general_settings"]["health_check_concurrency"] == 2
    assert settings["general_settings"]["health_check_skip_disabled_background_models"] is True
    assert "disable_error_logs" not in settings["general_settings"]


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


def test_gateway_defaults_to_loopback_no_auth_but_supports_an_explicit_master_key():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'GATEWAY_MASTER_KEY=${GATEWAY_MASTER_KEY:-sk-gateway-local-dev}' not in compose
    assert not any(
        line.strip().startswith("- GATEWAY_MASTER_KEY")
        for line in compose.splitlines()
    )
    assert config()["general_settings"]["master_key"] == "os.environ/GATEWAY_MASTER_KEY"


def test_experts_port_exposes_only_bounded_advisor_aliases():
    from subroute.plugins.advisor_plugin import ADVISOR_MODEL_NAMES, TOOL_HISTORY_ADVISORS

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    experts = services["experts"]
    expert_models = experts_config()["model_list"]

    assert experts["ports"] == ["127.0.0.1:4040:4040"]
    assert experts["command"] == [
        "--config", "/app/config/litellm.experts.yaml", "--port", "4040",
        "--host", "0.0.0.0", "--telemetry", "False",
    ]
    assert "depends_on" not in experts
    assert {model["model_name"] for model in expert_models} == {
        "codex-sol-advisor", "codex-astra-advisor", "codex-luna-advisor",
        "codex-gpt-6.1-sol-advisor",
    }
    assert all(model["model_info"]["selectable"] is False for model in expert_models)
    assert {
        model["litellm_params"]["model"] for model in expert_models
    } == {
        "codex-advisor/gpt-6-sol", "codex-advisor/gpt-6-astra",
        "codex-advisor/gpt-6-luna", "codex-advisor/gpt-6.1-sol",
    }
    assert experts_config()["router_settings"] == {
        "num_retries": 0, "fallbacks": [], "enable_pre_call_checks": True,
    }
    assert "callbacks" not in experts_config()["litellm_settings"]
    assert experts_config()["general_settings"]["master_key"] == "os.environ/EXPERTS_API_KEY"
    gateway_advisors = {
        item["model_name"]: item["litellm_params"]["model"]
        for item in config()["model_list"]
        if item["litellm_params"]["model"].startswith("codex-advisor/")
    }
    assert gateway_advisors == {
        item["model_name"]: item["litellm_params"]["model"] for item in expert_models
    }
    assert gateway_advisors == {
        alias: f"codex-advisor/{model}" for alias, model in ADVISOR_MODEL_NAMES.items()
    }
    assert set(gateway_advisors) == TOOL_HISTORY_ADVISORS
