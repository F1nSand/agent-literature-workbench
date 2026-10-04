param(
    [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExe = $dockerCommand.Source
} else {
    $dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExe -PathType Leaf)) {
    throw 'Docker Desktop CLI was not found. Start Docker Desktop and verify its installation.'
}

$token = [guid]::NewGuid().ToString('N').Substring(0, 12)
$project = "m5empty-$token"
$tempOverride = Join-Path ([System.IO.Path]::GetTempPath()) "$project.override.yaml"
$originalProjectHealthy = $false
$cleanupSucceeded = $false
$composeFiles = @('-f', (Join-Path $projectRoot 'compose.yaml'), '-f', $tempOverride)
$checks = [ordered]@{}
$ports = [ordered]@{}
$errorMessage = $null

function Invoke-Docker {
    param([string[]]$Arguments)
    $output = & $dockerExe @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($output) { $output | Write-Output }
    if ($exitCode -ne 0) { throw "Docker command failed with exit code $exitCode." }
}

function Invoke-DockerText {
    param([string[]]$Arguments)
    $output = & $dockerExe @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "Docker command failed with exit code $exitCode." }
    return ($output -join [Environment]::NewLine)
}

function Invoke-ComposeText {
    param([string[]]$Arguments)
    return Invoke-DockerText (@('compose', '--project-name', $project) + $composeFiles + $Arguments)
}

function Get-PublishedPort {
    param([string]$Service, [int]$ContainerPort)
    $address = (Invoke-ComposeText @('port', $Service, "$ContainerPort")).Trim()
    if ($address -notmatch '^127\.0\.0\.1:(\d+)$') {
        throw "Could not determine an isolated loopback port for $Service."
    }
    return [int]$Matches[1]
}

function Wait-Healthy {
    param([int]$TimeoutSeconds = 180)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $rows = Invoke-ComposeText @('ps', '--all', '--format', 'json')
        $containers = @()
        foreach ($line in ($rows -split "`r?`n" | Where-Object { $_.Trim() })) {
            $containers += ($line | ConvertFrom-Json)
        }
        $byService = @{}
        foreach ($container in $containers) { $byService[$container.Service] = $container }
        $ready = $true
        foreach ($service in @('db', 'api', 'worker', 'frontend')) {
            if (-not $byService.ContainsKey($service) -or $byService[$service].State -ne 'running') {
                $ready = $false
                break
            }
            if ($byService[$service].Health -ne 'healthy') {
                $ready = $false
                break
            }
        }
        if ($ready) { return $containers }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The isolated Compose services did not all become healthy before timeout.'
}

try {
    # Never inherit credentials into this synthetic startup test.
    $env:SILICONFLOW_API_KEY = ''
    $env:POSTGRES_PASSWORD = [guid]::NewGuid().ToString('N')

    $existingContainers = Invoke-DockerText @('ps', '-a', '--filter', "label=com.docker.compose.project=$project", '--format', '{{.ID}}')
    $existingVolumes = Invoke-DockerText @('volume', 'ls', '--filter', "label=com.docker.compose.project=$project", '--format', '{{.Name}}')
    if ($existingContainers.Trim() -or $existingVolumes.Trim()) {
        throw "Refusing to reuse existing Compose resources for project $project."
    }

    @'
services:
  db:
    ports: !override
      - "127.0.0.1::5432"
  api:
    ports: !override
      - "127.0.0.1::8000"
  frontend:
    ports: !override
      - "127.0.0.1::5173"
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:5173/').then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))"]
      interval: 3s
      timeout: 3s
      retries: 20
      start_period: 10s
  worker:
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0 if b'app.jobs.worker' in open('/proc/1/cmdline','rb').read() else 1)"]
      interval: 3s
      timeout: 2s
      retries: 20
      start_period: 5s
'@ | Set-Content -LiteralPath $tempOverride -Encoding utf8

    Invoke-ComposeText @('up', '--detach', '--build')
    $containerRows = Wait-Healthy
    $checks['compose_services_healthy'] = $true
    $checks['service_health'] = [ordered]@{}
    foreach ($container in $containerRows) {
        if ($container.Service -in @('db', 'api', 'worker', 'frontend')) {
            $checks['service_health'][$container.Service] = $container.Health
        }
    }

    $ports['db'] = Get-PublishedPort 'db' 5432
    $ports['api'] = Get-PublishedPort 'api' 8000
    $ports['frontend'] = Get-PublishedPort 'frontend' 5173

    $apiUrl = "http://127.0.0.1:$($ports['api'])/api/v1/health/ready"
    $readyResponse = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 10
    $checks['api_readiness'] = ($readyResponse.status -eq 'ok' -and $readyResponse.database -eq 'ok')
    $checks['api_readiness_response'] = [ordered]@{ status = $readyResponse.status; database = $readyResponse.database }
    if (-not $checks['api_readiness']) { throw 'API readiness did not report status=ok and database=ok.' }

    $frontendResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$($ports['frontend'])/" -TimeoutSec 30 -UseBasicParsing
    $checks['frontend_http_200'] = ($frontendResponse.StatusCode -eq 200)
    $checks['frontend_status_code'] = [int]$frontendResponse.StatusCode
    if (-not $checks['frontend_http_200']) { throw 'Frontend did not return HTTP 200.' }

    $migrationOutput = Invoke-ComposeText @('exec', '--no-TTY', 'api', 'alembic', 'current')
    $headOutput = Invoke-ComposeText @('exec', '--no-TTY', 'api', 'alembic', 'heads')
    # Alembic revision IDs may contain underscores (for example 20260929_0005).
    $revisionPattern = '([A-Za-z0-9_-]{1,64})'
    $currentRevision = [regex]::Match($migrationOutput, "(?m)^$revisionPattern\s*(?:\(head\))?\s*$").Groups[1].Value
    $headRevision = [regex]::Match($headOutput, "(?m)^$revisionPattern\s*\(head\)\s*$").Groups[1].Value
    $checks['migration_at_head'] = ($currentRevision -and $headRevision -and $currentRevision -eq $headRevision)
    $checks['migration_revision'] = $currentRevision
    if (-not $checks['migration_at_head']) { throw 'Database migration revision did not match Alembic head.' }

    $emptyQuery = "SELECT (SELECT count(*)::text FROM documents) || ',' || (SELECT count(*)::text FROM background_tasks) || ',' || (SELECT count(*)::text FROM runs) || ',' || (SELECT count(*)::text FROM provider_calls);"
    $countsOutput = Invoke-ComposeText @('exec', '--no-TTY', 'db', 'psql', '-U', 'workbench', '-d', 'workbench', '-Atc', $emptyQuery)
    $counts = $countsOutput.Trim()
    $checks['empty_database_business_counts'] = $counts
    $checks['empty_database'] = ($counts -eq '0,0,0,0')
    if (-not $checks['empty_database']) { throw 'A new-volume database unexpectedly contains business rows.' }

    $providerCount = [int]($counts -split ',')[-1]
    $checks['provider_calls'] = $providerCount
    Invoke-ComposeText @('exec', '--no-TTY', 'api', 'python', '-c', "import os,sys; sys.exit(0 if not os.getenv('SILICONFLOW_API_KEY') else 1)") | Out-Null
    Invoke-ComposeText @('exec', '--no-TTY', 'worker', 'python', '-c', "import os,sys; sys.exit(0 if not os.getenv('SILICONFLOW_API_KEY') else 1)") | Out-Null
    $checks['provider_key_empty_in_api_and_worker'] = $true
    if (-not $checks['provider_key_empty_in_api_and_worker'] -or $providerCount -ne 0) { throw 'Provider isolation check failed.' }

    $finalRows = Invoke-ComposeText @('ps', '--all', '--format', 'json')
    $checks['compose_project'] = $project
    $checks['worker_healthcheck_scope'] = 'temporary override checks that PID 1 is running app.jobs.worker; upstream Compose has no worker healthcheck'
} catch {
    $errorMessage = $_.Exception.Message
} finally {
    & $dockerExe compose --project-name $project @composeFiles down --volumes --remove-orphans 2>&1 | Out-Null
    $cleanupSucceeded = ($LASTEXITCODE -eq 0)
    Remove-Item Env:SILICONFLOW_API_KEY, Env:POSTGRES_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tempOverride -Force -ErrorAction SilentlyContinue

    try {
        $formalRows = Invoke-DockerText @('compose', '--project-name', 'literature-workbench', 'ps', '--all', '--format', 'json')
        $formalContainers = @()
        foreach ($line in ($formalRows -split "`r?`n" | Where-Object { $_.Trim() })) { $formalContainers += ($line | ConvertFrom-Json) }
        $formalByService = @{}
        foreach ($container in $formalContainers) { $formalByService[$container.Service] = $container }
        $originalProjectHealthy = $true
        foreach ($service in @('db', 'api', 'worker', 'frontend')) {
            if (-not $formalByService.ContainsKey($service) -or $formalByService[$service].State -ne 'running') { $originalProjectHealthy = $false }
        }
        foreach ($service in @('db', 'api')) {
            if ($formalByService.ContainsKey($service) -and $formalByService[$service].Health -ne 'healthy') { $originalProjectHealthy = $false }
        }
    } catch { $originalProjectHealthy = $false }
}

if (-not $ReportPath) {
    $ReportPath = Join-Path $projectRoot ("experiments\reports\m5-compose-empty-volume-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
} elseif (-not [System.IO.Path]::IsPathRooted($ReportPath)) {
    $ReportPath = Join-Path $projectRoot $ReportPath
}
$report = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString('o')
    classification = 'isolated Compose empty-volume startup, migration, readiness, and service health acceptance; synthetic only; no Provider key or request'
    isolated_project = $project
    actual_loopback_ports = $ports
    checks = $checks
    cleanup_succeeded = $cleanupSucceeded
    formal_project_services_still_running = $originalProjectHealthy
    result = [ordered]@{ passed = (-not $errorMessage -and $cleanupSucceeded -and $originalProjectHealthy); error = $errorMessage }
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
Write-Output (Get-Content -LiteralPath $ReportPath -Raw)
if ($errorMessage) { throw "Empty-volume Compose acceptance failed: $errorMessage" }
if (-not $cleanupSucceeded) { throw 'Acceptance checks finished, but isolated project cleanup failed.' }
if (-not $originalProjectHealthy) { throw 'Isolated acceptance finished, but the formal Compose project did not remain healthy.' }
