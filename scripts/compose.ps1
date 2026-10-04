param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArgs
)

$ErrorActionPreference = 'Stop'
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '..'))

$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExe = $dockerCommand.Source
} else {
    $dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
}

if (-not (Test-Path -LiteralPath $dockerExe -PathType Leaf)) {
    throw 'Docker Desktop CLI was not found. Start Docker Desktop and verify its installation.'
}

& $dockerExe compose @ComposeArgs
exit $LASTEXITCODE
