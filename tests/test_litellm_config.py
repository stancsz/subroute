from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "litellm.yaml"


def config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_all_public_protocols_are_owned_by_litellm_proxy():
    package = ROOT / "src" / "unified_llm_gateway"

    assert not (package / "kernel.py").exists()
    assert not any((package / "protocols").glob("*.py"))
    assert not any((package / "adapters").glob("*.py"))
    assert (package / "handlers" / "antigravity.py").is_file()
    assert (package / "plugins" / "advisor_plugin.py").is_file()


def test_standard_channels_use_native_litellm_provider_configuration():
    deployments = config()["model_list"]
    by_name = {deployment["model_name"]: deployment for deployment in deployments}

    assert by_name["openai"]["litellm_params"]["model"].startswith("openai/")
    assert by_name["openai-guided"]["litellm_params"] == by_name["openai"][
        "litellm_params"
    ]
    assert by_name["gemini-api"]["litellm_params"]["model"].startswith("gemini/")
    assert by_name["minimax"]["litellm_params"]["model"].startswith("minimax/")
    assert by_name["minimax-guided"]["litellm_params"] == by_name["minimax"][
        "litellm_params"
    ]
    assert by_name["freetoken"]["litellm_params"]["model"].startswith("openai/")
    assert by_name["desktop"]["litellm_params"] == {
        "model": "ollama/qwen2.5-coder",
        "api_base": "os.environ/OLLAMA_API_BASE",
    }
    assert config()["litellm_settings"]["callbacks"] == [
        "unified_llm_gateway.plugins.advisor_plugin.advisor_plugin_instance",
        "unified_llm_gateway.plugins.codex_credentials.codex_credential_refresher",
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
            "custom_handler": "unified_llm_gateway.handlers.antigravity.antigravity_handler",
        }
    ]
    codex = by_name["codex-subscription"]["litellm_params"]
    assert codex["model"].startswith("openai/responses/")
    assert codex["store"] is False
    assert codex["extra_headers"]["ChatGPT-Account-ID"] == "refreshed-at-dispatch"


def test_routing_fails_closed_without_retry_or_fallback():
    settings = config()
    routing = settings["router_settings"]

    assert routing["num_retries"] == 0
    assert routing["fallbacks"] == []
    assert settings["general_settings"]["master_key"] == (
        "os.environ/GATEWAY_MASTER_KEY"
    )
    assert "disable_error_logs" not in settings["general_settings"]


def test_windows_launchers_force_utf8_and_loopback_defaults():
    gateway = (ROOT / "scripts" / "start-gateway.ps1").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in gateway
    assert '[int]$Port = 4005' in gateway
    assert '[string]$HostAddress = "127.0.0.1"' in gateway
    assert not (ROOT / "scripts" / "start-antigravity.ps1").exists()
    assert "4010" not in gateway
