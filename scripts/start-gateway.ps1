[CmdletBinding()]
param(
    [int]$Port = 4000,
    [string]$HostAddress = "127.0.0.1",
    [string]$Config = (Join-Path $PSScriptRoot "..\config\litellm.yaml")
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$litellmExecutable = Join-Path $root ".venv\Scripts\litellm.exe"

if (-not (Test-Path -LiteralPath $litellmExecutable)) {
    throw "Gateway virtual environment is missing. Run .\scripts\setup.ps1 first."
}

if (-not $env:FREETOKEN_BASE_URL) { $env:FREETOKEN_BASE_URL = "http://127.0.0.1:1919/v1" }
if (-not $env:FREETOKEN_API_KEY) { $env:FREETOKEN_API_KEY = "local" }
if (-not $env:OLLAMA_API_BASE) { $env:OLLAMA_API_BASE = "http://127.0.0.1:11434" }
# Leave GATEWAY_MASTER_KEY unset for the default loopback-only, single-user
# gateway. Supplying it explicitly opts back into LiteLLM bearer-token auth.
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgresql://litellm:litellm-local-db@127.0.0.1:5433/litellm" }
if (-not $env:UI_USERNAME) { $env:UI_USERNAME = "admin" }

& $litellmExecutable --config (Resolve-Path $Config) --host $HostAddress --port $Port --telemetry False
exit $LASTEXITCODE
