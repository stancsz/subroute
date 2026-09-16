[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    & py -V:Astral/CPython3.11.14 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python 3.11 virtual environment." }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Unable to upgrade pip." }
& $python -m pip install -e "$root[test]"
if ($LASTEXITCODE -ne 0) { throw "Unable to install gateway dependencies." }

$prismaSchema = & $python -X utf8 -c "import pathlib, litellm_proxy_extras; print(pathlib.Path(litellm_proxy_extras.__file__).parent / 'schema.prisma')"
if ($LASTEXITCODE -ne 0 -or -not $prismaSchema) {
    throw "Unable to locate LiteLLM's bundled Prisma schema."
}
& $python -m prisma generate --schema $prismaSchema
if ($LASTEXITCODE -ne 0) { throw "Prisma client generation failed." }

Write-Host "Gateway environment ready: $venv"
