$ErrorActionPreference = 'Stop'
$profilePath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.claude/subroute-minimax.settings.json'
if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
    if (Test-Path -LiteralPath $profilePath) {
        throw "MiniMax Claude profile path exists but is not a file: $profilePath"
    }

    $gatewayKey = [Environment]::GetEnvironmentVariable('GATEWAY_MASTER_KEY', 'Process')
    if ([string]::IsNullOrWhiteSpace($gatewayKey)) {
        throw 'GATEWAY_MASTER_KEY is required in the current process environment to create the MiniMax Claude profile for the first time.'
    }

    $capabilities = 'effort,xhigh_effort,max_effort,thinking,adaptive_thinking,interleaved_thinking'
    $profileEnvironment = [ordered]@{
        ANTHROPIC_BASE_URL = 'http://127.0.0.1:4000'
        ANTHROPIC_API_KEY = $gatewayKey
        ANTHROPIC_MODEL = 'minimax-flash'
        ANTHROPIC_DEFAULT_SONNET_MODEL = 'minimax-flash'
        ANTHROPIC_DEFAULT_OPUS_MODEL = 'minimax-flash'
        ANTHROPIC_DEFAULT_HAIKU_MODEL = 'minimax-flash'
        ANTHROPIC_CUSTOM_MODEL_OPTION = 'minimax-flash'
        ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES = $capabilities
        ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES = $capabilities
        ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES = $capabilities
        ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES = $capabilities
        CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT = '0'
        CLAUDE_CODE_AUTO_COMPACT_WINDOW = '200000'
    }
    $profileSettings = [ordered]@{
        model = 'minimax-flash'
        effortLevel = 'medium'
        alwaysThinkingEnabled = $true
        env = $profileEnvironment
    }
    $profileJson = $profileSettings | ConvertTo-Json -Depth 4
    $profileDirectory = Split-Path -Parent $profilePath
    New-Item -ItemType Directory -Path $profileDirectory -Force | Out-Null

    $profileStream = $null
    try {
        $profileStream = [System.IO.File]::Open($profilePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $profileWriter = [System.IO.StreamWriter]::new($profileStream, [System.Text.UTF8Encoding]::new($false))
        $profileWriter.WriteLine($profileJson)
        $profileWriter.Dispose()
        $profileStream = $null
    }
    finally {
        if ($null -ne $profileStream) { $profileStream.Dispose() }
    }
}

$claude = Get-Command claude.exe -ErrorAction SilentlyContinue
if ($claude) { $claudePath = $claude.Source }
else {
    $npmClaude = Join-Path $env:APPDATA 'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    if (Test-Path -LiteralPath $npmClaude -PathType Leaf) { $claudePath = $npmClaude }
    else { throw 'Could not find the installed native claude.exe on PATH or in the npm executable directory.' }
}

$saved = @{}
try {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'ANTHROPIC_*' -or $_.Name -in @('CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDE_CODE_SIMPLE', 'CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT') } | ForEach-Object {
        $saved[$_.Name] = $_.Value
        Remove-Item -LiteralPath "Env:$($_.Name)"
    }
    & $claudePath --setting-sources= --settings $profilePath @args
    $exitCode = $LASTEXITCODE
}
finally {
    foreach ($name in $saved.Keys) { Set-Item -LiteralPath "Env:$name" -Value $saved[$name] }
}
exit $exitCode
