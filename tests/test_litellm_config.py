from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "litellm.yaml"
EXPERTS_CONFIG_PATH = ROOT / "config" / "litellm.experts.yaml"


def config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def experts_config() -> dict:
    return yaml.safe_load(EXPERTS_CONFIG_PATH.read_text(encoding="utf-8"))


def test_compose_isolates_mutable_state_and_pins_gateway_image():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    prod, staging = services["gateway"], services["gateway-staging"]
    prod_env = dict(item.split("=", 1) for item in prod["environment"] if "=" in item)
    stage_env = dict(item.split("=", 1) for item in staging["environment"] if "=" in item)
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

    assert by_name["openai"]["litellm_params"]["model"].startswith("openai/")
    assert by_name["current"]["model_info"]["selectable"] is False
    assert by_name["gemini-api"]["litellm_params"]["model"].startswith("gemini/")
    assert by_name["openrouter"]["litellm_params"] == {
        "model": "openrouter/minimax/minimax-m3",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "reasoning": {"exclude": True},
    }
    assert not any(name.endswith("-guided") for name in by_name)
    assert by_name["minimax"]["litellm_params"]["model"].startswith("minimax/")
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
    assert config()["litellm_settings"]["custom_provider_map"] == [
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
    assert codex["model"] == "openai/responses/gpt-6-sol"
    assert codex["store"] is False
    assert codex["extra_headers"]["ChatGPT-Account-ID"] == "refreshed-at-dispatch"
    assert by_name["codex-terra-advisor"]["model_info"]["advisor_selectable"] is True
    assert by_name["codex-sol-advisor"]["litellm_params"]["model"] == (
        "codex-advisor/gpt-6-sol"
    )
    assert by_name["codex-astra-advisor"]["litellm_params"]["model"] == (
        "codex-advisor/gpt-6-astra"
    )
    assert by_name["gemini-subscription"]["litellm_params"]["model"] == (
        "antigravity/gemini-3.8-flash"
    )


def test_routing_fails_closed_without_retry_or_fallback():
    settings = config()
    routing = settings["router_settings"]

    assert routing["num_retries"] == 0
    assert routing["fallbacks"] == []
    assert settings["litellm_settings"]["drop_params"] is True
    assert settings["general_settings"]["master_key"] == (
        "os.environ/GATEWAY_MASTER_KEY"
    )
    assert settings["general_settings"]["database_url"] == (
        "os.environ/DATABASE_URL"
    )
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
        "codex-sol-advisor", "codex-astra-advisor",
    }
    assert all(model["model_info"]["selectable"] is False for model in expert_models)
    assert {
        model["litellm_params"]["model"] for model in expert_models
    } == {"codex-advisor/gpt-6-sol", "codex-advisor/gpt-6-astra"}
    assert experts_config()["router_settings"] == {"num_retries": 0, "fallbacks": []}
    assert "callbacks" not in experts_config()["litellm_settings"]
    assert experts_config()["general_settings"]["master_key"] == "os.environ/EXPERTS_API_KEY"
