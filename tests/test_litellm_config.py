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
            speech = deployment["model_name"].startswith(("mimo-v2.5-asr", "mimo-v2.5-tts"))
            local_qwen = deployment["model_name"] == "qwen3.8-27b"
            context = 32000 if is_advisor else 8000 if speech else 40960 if local_qwen else 256000
            assert info["max_input_tokens"] == (32768 if local_qwen else context), deployment["model_name"]
            assert info["context_window"] == context
            trigger = 32768 if local_qwen else 1_000_000 if context == 256000 else context * 0.8
            assert info["auto_compact_token_limit"] == trigger
            if info.get("advisor_selectable"):
                if info["max_input_tokens"] == 256000:
                    assert info["advisor_max_input_tokens"] == 32000
            if not local_qwen and context != 256000:
                assert info["auto_compact_token_limit"] == info["context_window"] * 0.8
            assert info["compaction_scope"] == "total"

    supplemental = next(
        deployment for deployment in config()["model_list"]
        if deployment["model_name"] == "claude-opus-5-5-low-advisor"
    )
    assert supplemental["litellm_params"]["model"] == "antigravity/claude-opus-5-5-low"
    assert supplemental["model_info"]["max_input_tokens"] == 32000
    assert supplemental["model_info"]["auto_compact_token_limit"] == 25600


def test_advisor_compaction_summarizers_are_configured_for_text_and_images():
    by_name = {deployment["model_name"]: deployment for deployment in config()["model_list"]}
    assert by_name["minimax"]["model_info"]["source_id"] == "minimax"
    assert by_name["mimo-v2.6-flash"]["model_info"]["capabilities"] == ["text", "streaming"]
    assert "vision" in by_name["mimo-v2.5-pro"]["model_info"]["capabilities"]


def test_local_qwen_responses_uses_litellm_chat_bridge():
    import inspect
    from litellm.responses.main import _will_bridge_to_chat_completions

    deployment = next(item for item in config()["model_list"] if item["model_name"] == "qwen3.8-27b")
    params = deployment["litellm_params"]
    # Dev and pinned-image builds both label themselves 1.103.0 but expose
    # different private-helper signatures. Exercise the native bridge in each.
    extra = {"api_base": params.get("api_base")} if "api_base" in inspect.signature(_will_bridge_to_chat_completions).parameters else {}
    assert _will_bridge_to_chat_completions(
        "qwen3.8-27b", "openai", params.get("use_chat_completions_api", False), deployment["model_info"],
        **extra,
    )


def test_default_target_client_alias_is_catalogued_and_uses_luna_when_unresolved():
    models = config()["model_list"]
    default = next(item for item in models if item["model_name"] == "default")
    luna = next(item for item in models if item["model_name"] == "codex-luna")
    assert default["model_info"]["selectable"] is False
    assert default["litellm_params"]["model"] == luna["litellm_params"]["model"]
    assert default["litellm_params"]["api_base"] == luna["litellm_params"]["api_base"]


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
    assert prod["build"] == staging["build"]
    recipe = (ROOT / prod["build"]["dockerfile"]).read_text()
    assert "FROM ghcr.io/berriai/litellm-database@sha256:bd07ceb1fc7c4505f116c4eb2767956a8accba3119548dd8ae55e5356a381d56" in recipe
    assert "onnxruntime==1.31.0 flatbuffers==25.12.19" in recipe
    assert ":latest" not in prod["image"] and ":latest" not in recipe


def test_compose_publishes_gateway_ports_on_loopback():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    assert services["gateway"]["ports"] == ["127.0.0.1:4000:4000"]
    assert services["gateway-staging"]["ports"] == ["127.0.0.1:4005:4005"]


def test_local_qwen_two_gpu_slots_preserve_input_output_and_context_headroom():
    from subroute.sift_plugin import sift_qwen_hook

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    service = services["qwen-local"]
    command = service["command"]
    info = next(item["model_info"] for item in config()["model_list"]
                if item["model_name"] == "qwen3.8-27b")

    for flag, expected in {
        "--ctx-size": "81920",
        "--parallel": "2",
        "--kv-unified-per-slot": "40960",
        "--n-predict": "4096",
        "--cache-ram": "0",
        "--n-gpu-layers": "all",
        "--fit": "off",
        "--ctx-checkpoints": "0",
        "--override-tensor": "^token_embd[.]weight$=CUDA0",
        "--cache-type-k": "q4_0",
        "--cache-type-v": "q4_0",
    }.items():
        assert command[command.index(flag) + 1] == expected
    assert int(command[command.index("--ctx-size") + 1]) == 2 * info["context_window"]
    assert info["max_input_tokens"] + info["max_output_tokens"] + 4096 == info["context_window"]
    assert sift_qwen_hook.model_targets == {"qwen3.8-27b": info["auto_compact_token_limit"]}
    assert service["cpus"] == 6.0
    assert service["mem_limit"] == service["memswap_limit"] == "12g"
    # Fixed placement avoids the contradictory CUDA/NVML free-memory views
    # observed under WSL with a game running. GPU residency needs live checks.
    assert "--fit-target" not in command
    assert "--kv-offload" in command


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
    assert by_name["default"]["model_info"]["selectable"] is False
    assert by_name["default"]["litellm_params"]["model"] == by_name["codex-luna"]["litellm_params"]["model"]
    assert by_name["auto"]["model_info"]["selectable"] is False
    assert by_name["auto"]["model_info"]["disable_background_health_check"] is True
    assert "gemini-api" not in by_name
    assert not any(
        "gpt-5.2-codex" in str(deployment.get("litellm_params", {}).get("model", ""))
        for deployment in deployments
    )
    assert by_name["openrouter"]["litellm_params"] == {
        "model": "openrouter/minimax/minimax-m3.1-flash",
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
        "subroute.usage_accounting.usage_accounting",
        "subroute.plugins.dynamic_router.dynamic_routing_plugin",
        "subroute.sift_plugin.sift_auto_hook",
        "subroute.sift_plugin.sift_qwen_hook",
        "subroute.plugins.advisor_plugin.advisor_plugin_instance",
        "subroute.sift_plugin.sift_target_hook",
        "subroute.plugins.codex_credentials.codex_credential_refresher",
        "websearch_interception",
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
    assert argon["model_info"]["advisor_selectable"] is True
    assert argon["model_info"]["advisor_max_input_tokens"] == 32000
    for name in (
        "gemini-subscription",
        "gemini-subscription-argon",
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
    for alias in ("codex-gpt-6.1-sol", "codex-astra", "codex-luna"):
        params = by_name[alias]["litellm_params"]
        assert params["allowed_openai_params"] == ["reasoning_effort"]
        assert params["store"] is False
    assert by_name["codex-gpt-6.1-sol"]["litellm_params"]["model"] == (
        "codex-subscription/gpt-6.1-sol"
    )
    assert by_name["codex-gpt-6.1-sol"]["model_info"]["display_name"] == (
        "GPT-6.1 Sol"
    )
    assert "codex-terra" not in by_name
    assert "codex-reserve" not in by_name
    assert not any("gpt-5" in deployment["litellm_params"]["model"] for deployment in by_name.values())
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


def test_minimax_m31_flash_routes_are_selectable_and_fail_over_once_per_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from subroute.plugins.dynamic_router import RoutingControlPlane

    settings = config()
    by_name = {item["model_name"]: item for item in settings["model_list"]}
    control = RoutingControlPlane(CONFIG_PATH, tmp_path / "routing-state.json")
    choices = {choice.model_id: choice for choice in control.choices}

    assert {"minimax-flash", "openrouter-flash"} <= choices.keys()
    assert choices["minimax-flash"].source_id == "openrouter"
    assert choices["openrouter-flash"].source_id == "openrouter"
    assert by_name["minimax-flash"]["litellm_params"]["model"] == (
        "openrouter/minimax/minimax-m3.1-flash"
    )
    assert by_name["openrouter-flash"]["litellm_params"]["model"] == (
        "openrouter/minimax/minimax-m3.1-flash"
    )
    assert by_name["minimax-flash"]["litellm_params"] == by_name["openrouter-flash"][
        "litellm_params"
    ]

    fallbacks = {
        model: targets
        for group in settings["router_settings"]["fallbacks"]
        for model, targets in group.items()
    }
    default_chain = settings["router_settings"]["default_fallbacks"]
    assert default_chain == ["openrouter", "mimo-v2.6-pro", "codex-luna"]
    assert fallbacks["minimax-flash"] == ["mimo-v2.6-pro", "codex-luna"]
    assert fallbacks["openrouter-flash"] == ["mimo-v2.6-pro", "codex-luna"]
    for model in ("minimax-flash", "openrouter-flash"):
        chain = [model, *fallbacks.get(model, default_chain)]
        assert chain == [model, "mimo-v2.6-pro", "codex-luna"], (model, chain)
        providers = [by_name[item]["model_info"]["source_id"] for item in chain]
        assert len(providers) == len(set(providers)), (model, providers)
    for model in ("mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5"):
        chain = [model, *fallbacks[model]]
        providers = [by_name[item]["model_info"]["source_id"] for item in chain]
        assert len(providers) == len(set(providers)), (model, providers)


def test_declared_target_and_advisor_fallbacks_are_bounded_without_generic_retries():
    settings = config()
    routing = settings["router_settings"]

    assert routing["num_retries"] == 0
    assert routing["default_fallbacks"] == ["openrouter", "mimo-v2.6-pro", "codex-luna"]
    assert routing["fallbacks"] == [
        {"auto": ["mimo-v2.6-pro", "codex-luna"]},
        {"openrouter": ["mimo-v2.6-pro", "codex-luna"]},
        {"openrouter-flash": ["mimo-v2.6-pro", "codex-luna"]},
        {"minimax-flash": ["mimo-v2.6-pro", "codex-luna"]},
        {"mimo-v2.6-pro": ["openrouter", "codex-luna"]},
        {"mimo-v2.6-flash": ["openrouter", "codex-luna"]},
        {"mimo-v2.5-pro": ["openrouter", "codex-luna"]},
        {"mimo-v2.5": ["openrouter", "codex-luna"]},
        {"codex-luna": ["openrouter", "mimo-v2.6-pro"]},
        {"gemini-3.8-flash-advisor": ["codex-gpt-6.1-sol-advisor"]},
        {"codex-gpt-6.1-sol-advisor": ["gemini-3.8-flash-advisor"]},
        {"codex-sol-advisor": []},
        {"codex-astra-advisor": []},
        {"codex-luna-advisor": []},
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
    assert settings["general_settings"]["disable_spend_logs"] is False
    assert settings["litellm_settings"]["turn_off_message_logging"] is True
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


def test_gateway_master_key_is_optional_and_passed_to_both_gateways():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'GATEWAY_MASTER_KEY=${GATEWAY_MASTER_KEY:-sk-gateway-local-dev}' not in compose
    services = yaml.safe_load(compose)["services"]
    for service in ("gateway", "gateway-staging"):
        assert "GATEWAY_MASTER_KEY" in services[service]["environment"]
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
        "codex-gpt-6.1-sol-advisor", "gemini-4-argon-advisor",
    }
    assert all(model["model_info"]["selectable"] is False for model in expert_models)
    assert {
        model["litellm_params"]["model"] for model in expert_models
    } == {
        "codex-advisor/gpt-6-sol", "codex-advisor/gpt-6-astra",
        "codex-advisor/gpt-6-luna", "codex-advisor/gpt-6.1-sol",
        "antigravity/gemini-4-argon-low",
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
        if item["litellm_params"]["model"].startswith("codex-advisor/")
    }
    assert gateway_advisors == {
        alias: f"codex-advisor/{model}" for alias, model in ADVISOR_MODEL_NAMES.items()
    }
    assert set(gateway_advisors) == TOOL_HISTORY_ADVISORS
