param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'LiteratureWorkbench\Backups')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$projectName = 'literature-workbench'
$postgresImage = 'pgvector/pgvector:0.8.6-pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f'
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExe = $dockerCommand.Source
} else {
    $dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExe -PathType Leaf)) {
    throw 'Docker Desktop CLI was not found. Start Docker Desktop and verify its installation.'
}

function Invoke-Docker {
    param([string[]]$Arguments)
    $output = & $dockerExe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $diagnostic = ($output | Out-String).Trim()
        foreach ($secret in @($env:SILICONFLOW_API_KEY, $env:POSTGRES_PASSWORD, $env:PGPASSWORD)) {
            if (-not [string]::IsNullOrEmpty($secret)) { $diagnostic = $diagnostic.Replace($secret, '[REDACTED]') }
        }
        throw "Docker operation failed (exit $LASTEXITCODE): docker $($Arguments -join ' ')`n$diagnostic"
    }
    return $output
}

function Invoke-DockerText {
    param([string[]]$Arguments)
    $output = Invoke-Docker -Arguments $Arguments
    return ($output -join [Environment]::NewLine).Trim()
}

$destinationRoot = [System.IO.Path]::GetFullPath($Destination)
$rootPrefix = $projectRoot.TrimEnd('\') + '\'
$destinationWithoutTrailingSlash = $destinationRoot.TrimEnd('\')
if ([string]::Equals($destinationWithoutTrailingSlash, $projectRoot.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase) -or
    $destinationRoot.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose a backup destination outside the project directory.'
}
$null = New-Item -ItemType Directory -Path $destinationRoot -Force
$token = [guid]::NewGuid().ToString('N').Substring(0, 8)
$backupPath = Join-Path $destinationRoot ("backup-{0}-{1}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $token)
if (Test-Path -LiteralPath $backupPath) { throw 'Backup destination already exists; refusing to overwrite it.' }
$null = New-Item -ItemType Directory -Path $backupPath
$dumpPath = Join-Path $backupPath 'database.dump'
$filesPath = Join-Path $backupPath 'literature_data.tar.gz'
$temporaryDumpPath = "/tmp/literature-workbench-$token.dump"
$stoppedServices = @()
$originallyRunning = @()
$databaseContainer = $null
$backupResult = $null

try {
    $literatureVolume = Invoke-DockerText @(
        'volume', 'ls', '-q', '--filter', "label=com.docker.compose.project=$projectName",
        '--filter', 'label=com.docker.compose.volume=literature_data'
    )
    if (-not $literatureVolume) { throw 'Could not resolve the Compose literature data volume.' }

    $originallyRunning = @(Invoke-Docker @('compose', '--project-name', $projectName, 'ps', '--services', '--status', 'running') |
        ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ })
    if ($originallyRunning -notcontains 'db') { throw 'The project database must be running before creating a backup.' }
    $databaseContainer = (Invoke-DockerText @('compose', '--project-name', $projectName, 'ps', '-q', 'db')).Trim()
    if (-not $databaseContainer) { throw 'The project database container was not found.' }
    $null = Invoke-Docker @('compose', '--project-name', $projectName, 'exec', '-T', 'db', 'pg_isready', '-U', 'workbench', '-d', 'workbench')
    $null = Invoke-Docker @('volume', 'inspect', $literatureVolume)

    foreach ($service in @('worker', 'api')) {
        if ($originallyRunning -contains $service) {
            $null = Invoke-Docker @('compose', '--project-name', $projectName, 'stop', '--timeout', '90', $service)
            $stoppedServices += $service
        }
    }

    $null = Invoke-Docker @(
        'compose', '--project-name', $projectName, 'exec', '-T', 'db', 'pg_dump',
        '--username=workbench', '--dbname=workbench', '--format=custom', '--no-owner', '--no-acl', "--file=$temporaryDumpPath"
    )
    $null = Invoke-Docker @('cp', "${databaseContainer}:$temporaryDumpPath", $dumpPath)
    if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf) -or (Get-Item -LiteralPath $dumpPath).Length -lt 1) {
        throw 'Database dump is missing or empty.'
    }

    $null = Invoke-Docker @(
        'run', '--rm',
        '--mount', "type=volume,source=$literatureVolume,target=/data,readonly",
        '--mount', "type=bind,source=$backupPath,target=/backup",
        $postgresImage, 'sh', '-c', 'tar -czf /backup/literature_data.tar.gz -C /data .'
    )
    if (-not (Test-Path -LiteralPath $filesPath -PathType Leaf) -or (Get-Item -LiteralPath $filesPath).Length -lt 1) {
        throw 'Literature data archive is missing or empty.'
    }

    $snapshotSql = @'
SELECT json_build_object(
  'schema_revision', (SELECT version_num FROM alembic_version LIMIT 1),
  'documents_total', (SELECT count(*) FROM documents),
  'documents_active', (SELECT count(*) FROM documents WHERE is_deleted = false),
  'versions', (SELECT count(*) FROM document_versions),
  'pages', (SELECT count(*) FROM document_pages),
  'content_units', (SELECT count(*) FROM content_units),
  'topics', (SELECT count(*) FROM topics),
  'topic_document_links', (SELECT count(*) FROM topic_documents),
  'conversations', (SELECT count(*) FROM conversations),
  'runs', (SELECT count(*) FROM runs),
  'messages', (SELECT count(*) FROM messages),
  'evidence', (SELECT count(*) FROM evidence),
  'tool_calls', (SELECT count(*) FROM tool_calls),
  'run_events', (SELECT count(*) FROM run_events),
  'memory_items', (SELECT count(*) FROM memory_items),
  'artifacts', (SELECT count(*) FROM artifacts),
  'artifact_versions', (SELECT count(*) FROM artifact_versions),
  'notes', (SELECT count(*) FROM notes),
  'note_versions', (SELECT count(*) FROM note_versions),
  'background_tasks', (SELECT count(*) FROM background_tasks),
  'provider_calls', (SELECT count(*) FROM provider_calls)
)::text;
'@
    $inventoryJson = Invoke-DockerText @(
        'compose', '--project-name', $projectName, 'exec', '-T', 'db', 'psql',
        '-U', 'workbench', '-d', 'workbench', '-Atc', $snapshotSql
    )
    $inventory = $inventoryJson | ConvertFrom-Json
    $manifest = [ordered]@{
        schema_version = 1
        created_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        source_project = $projectName
        postgres_image = $postgresImage
        database_file = 'database.dump'
        database_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $dumpPath).Hash.ToLowerInvariant()
        literature_files_archive = 'literature_data.tar.gz'
        literature_files_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $filesPath).Hash.ToLowerInvariant()
        inventory = $inventory
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $backupPath 'manifest.json') -Encoding utf8
    $backupResult = [ordered]@{
        status = 'backup-created'
        backup_path = $backupPath
        database_sha256 = $manifest.database_sha256
        literature_files_sha256 = $manifest.literature_files_sha256
        inventory = $inventory
    }
} finally {
    if ($databaseContainer -and $originallyRunning -contains 'db') {
        & $dockerExe compose --project-name $projectName exec -T db rm -f $temporaryDumpPath *> $null
    }
    $restartErrors = @()
    foreach ($service in @('api', 'worker')) {
        if ($stoppedServices -contains $service) {
            & $dockerExe compose --project-name $projectName start $service *> $null
            if ($LASTEXITCODE -ne 0) { $restartErrors += $service }
        }
    }
    if ($restartErrors.Count -gt 0) {
        throw "Could not restart previously running services: $($restartErrors -join ', ')"
    }
}

if ($originallyRunning -contains 'api') {
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            $ready = Invoke-RestMethod -Uri 'http://127.0.0.1:8002/api/v1/health/ready' -TimeoutSec 2
            if ($ready.status -eq 'ok' -and $ready.database -eq 'ok') { $apiReady = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $apiReady) { throw 'Backup was created, but the previously running API did not become ready again.' }
}

Write-Output ($backupResult | ConvertTo-Json -Depth 5)
