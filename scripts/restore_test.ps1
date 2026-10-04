param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$composePath = Join-Path $projectRoot 'compose.yaml'
$overridePath = Join-Path $PSScriptRoot 'compose.restore-test.yaml'
$expectedPostgresImage = 'pgvector/pgvector:0.8.6-pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f'
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

$backupRoot = (Resolve-Path -LiteralPath $BackupPath).Path
$manifestPath = Join-Path $backupRoot 'manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Backup manifest is missing.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or $manifest.source_project -ne 'literature-workbench') {
    throw 'Backup manifest schema or source project is unsupported.'
}
if ($manifest.postgres_image -ne $expectedPostgresImage) { throw 'Backup manifest names an unsupported PostgreSQL image.' }
if ($manifest.database_file -ne 'database.dump' -or $manifest.literature_files_archive -ne 'literature_data.tar.gz') {
    throw 'Backup manifest contains unsupported artifact names.'
}
$dumpPath = Join-Path $backupRoot $manifest.database_file
$filesPath = Join-Path $backupRoot $manifest.literature_files_archive
if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf) -or -not (Test-Path -LiteralPath $filesPath -PathType Leaf)) {
    throw 'Backup archive is missing.'
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $dumpPath).Hash -ne $manifest.database_sha256.ToUpperInvariant()) {
    throw 'Database dump checksum does not match the backup manifest.'
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $filesPath).Hash -ne $manifest.literature_files_sha256.ToUpperInvariant()) {
    throw 'Literature file archive checksum does not match the backup manifest.'
}

$restoreProject = 'workbench-restore-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$projectArgs = @('--project-name', $restoreProject, '--file', $composePath, '--file', $overridePath)
$literatureVolume = "$restoreProject`_literature_data"
$fileRestoreContainer = $null
$volumeCollisions = @(
    & $dockerExe volume ls -q --filter "label=com.docker.compose.project=$restoreProject"
)
if ($LASTEXITCODE -ne 0) { throw 'Could not check the isolated restore project namespace.' }
if ($volumeCollisions.Count -gt 0) { throw 'Random restore project namespace already exists; refusing to reuse it.' }

$hadProviderKey = Test-Path Env:SILICONFLOW_API_KEY
$savedProviderKey = $env:SILICONFLOW_API_KEY
$projectCreated = $false

try {
    $projectCreated = $true
    $null = Invoke-Docker -Arguments (@('compose') + $projectArgs + @('up', '-d', 'db'))
    $databaseContainer = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $databaseContainer = Invoke-DockerText -Arguments (@('compose') + $projectArgs + @('ps', '-q', 'db'))
        if ($databaseContainer) {
            & $dockerExe compose @projectArgs exec -T db pg_isready -U workbench -d workbench *> $null
            if ($LASTEXITCODE -eq 0) { break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $databaseContainer) { throw 'Isolated restore database container did not start.' }
    & $dockerExe compose @projectArgs exec -T db pg_isready -U workbench -d workbench *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Isolated restore database did not become ready.' }

    $env:SILICONFLOW_API_KEY = ''

    $null = Invoke-Docker -Arguments @('cp', $dumpPath, "${databaseContainer}:/tmp/restore.dump")
    $null = Invoke-Docker -Arguments (@('compose') + $projectArgs + @(
        'exec', '-T', 'db', 'sh', '-c',
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore --host=db --username=workbench --dbname=workbench --clean --if-exists --no-owner --no-acl /tmp/restore.dump'
    ))
    $null = Invoke-Docker -Arguments @('exec', $databaseContainer, 'rm', '-f', '/tmp/restore.dump')

    $null = Invoke-Docker -Arguments @(
        'volume', 'create', '--label', "com.docker.compose.project=$restoreProject",
        '--label', 'com.docker.compose.volume=literature_data', $literatureVolume
    )
    $fileRestoreName = "$restoreProject-files-restore"
    $fileRestoreContainer = (Invoke-DockerText -Arguments @(
        'create', '--name', $fileRestoreName, '--mount', "type=volume,source=$literatureVolume,target=/data",
        '--entrypoint', 'sh', $expectedPostgresImage, '-c', 'sleep 600'
    )).Trim()
    if (-not $fileRestoreContainer) { throw 'Could not create the isolated file restore helper.' }
    $null = Invoke-Docker -Arguments @('cp', $filesPath, "${fileRestoreContainer}:/tmp/literature_data.tar.gz")
    $null = Invoke-Docker -Arguments @('start', $fileRestoreContainer)
    $null = Invoke-Docker -Arguments @('exec', $fileRestoreContainer, 'tar', '-xzf', '/tmp/literature_data.tar.gz', '-C', '/data')
    $null = Invoke-Docker -Arguments @('rm', '-f', $fileRestoreContainer)
    $fileRestoreContainer = $null

    $appSource = Join-Path $projectRoot 'backend\app'
    $verificationOutput = Invoke-Docker -Arguments (@('compose') + $projectArgs + @(
        'run', '--rm', '--no-deps', '--volume', "${PSScriptRoot}:/verify:ro", '--volume', "${appSource}:/app/app:ro",
        '--env', 'SILICONFLOW_API_KEY=', '--entrypoint', 'python', 'api', '/verify/verify_restored_library.py'
    ))
    $verificationLine = $verificationOutput |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_.StartsWith('{') -and $_.Contains('"status"') } |
        Select-Object -Last 1
    if (-not $verificationLine) { throw 'Restore verifier did not return a JSON report.' }
    $verificationJson = $verificationLine
    $verification = $verificationJson | ConvertFrom-Json
    if ($verification.status -ne 'verified') { throw 'Restored database and document files failed consistency verification.' }

    foreach ($field in @(
        'schema_revision', 'documents_total', 'documents_active', 'versions', 'pages', 'content_units',
        'topics', 'topic_document_links', 'conversations', 'runs', 'messages', 'evidence', 'tool_calls',
        'run_events', 'memory_items', 'artifacts', 'artifact_versions', 'notes', 'note_versions',
        'background_tasks', 'provider_calls'
    )) {
        # schema_version 1 backups created before notes omit these optional inventory fields.
        if ($field -in @('notes', 'note_versions') -and $manifest.inventory.PSObject.Properties.Name -notcontains $field) {
            continue
        }
        if ([string]$verification.$field -ne [string]$manifest.inventory.$field) {
            throw "Restored inventory does not match the backup manifest field: $field"
        }
    }
    if ($verification.PSObject.Properties.Name -contains 'verified_original_files') {
        if ($verification.verified_original_files -ne $manifest.inventory.versions) {
            throw 'Not every immutable document original has a verified file.'
        }
    } elseif ($verification.verified_pdf_files -ne $manifest.inventory.versions) {
        # Older verifier compatibility: those versions were all stored as PDFs.
        throw 'Not every document version has a verified PDF file.'
    }
    Write-Output ([ordered]@{
        status = 'restore-verified'
        isolated_project = $restoreProject
        source_backup_created_at_utc = $manifest.created_at_utc
        database_sha256 = $manifest.database_sha256
        literature_files_sha256 = $manifest.literature_files_sha256
        verification = $verification
        provider_requests_during_restore = 0
        production_volumes_touched = $false
    } | ConvertTo-Json -Depth 5)
} finally {
    if ($hadProviderKey) { $env:SILICONFLOW_API_KEY = $savedProviderKey } else { Remove-Item Env:SILICONFLOW_API_KEY -ErrorAction SilentlyContinue }
    if ($fileRestoreContainer) { & $dockerExe rm -f $fileRestoreContainer *> $null }
    if ($projectCreated) {
        & $dockerExe compose @projectArgs down --volumes --remove-orphans *> $null
        if ($LASTEXITCODE -ne 0) { throw "Could not clean isolated restore project $restoreProject." }
    }
}
