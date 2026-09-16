[CmdletBinding()]
param(
    [int]$Port = 4005,
    [string]$HostAddress = "127.0.0.1",
    [string]$Config = (Join-Path $PSScriptRoot "..\config\litellm.yaml")
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

if (-not $env:FREETOKEN_BASE_URL) { $env:FREETOKEN_BASE_URL = "http://127.0.0.1:1919/v1" }
if (-not $env:FREETOKEN_API_KEY) { $env:FREETOKEN_API_KEY = "local" }
if (-not $env:OLLAMA_API_BASE) { $env:OLLAMA_API_BASE = "http://127.0.0.1:11434" }
if (-not $env:GATEWAY_MASTER_KEY) { $env:GATEWAY_MASTER_KEY = "sk-gateway-local-dev" }

& litellm --config (Resolve-Path $Config) --host $HostAddress --port $Port --telemetry False
exit $LASTEXITCODE
