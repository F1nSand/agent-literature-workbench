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

$token = [guid]::NewGuid().ToString('N').Substring(0, 10)
$prefix = "workbench-m5-restore-$token"
$network = "$prefix-net"
$sourceDb = "$prefix-source-db"
$restoreDb = "$prefix-restore-db"
$sourceDbVolume = "$prefix-source-postgres"
$restoreDbVolume = "$prefix-restore-postgres"
$sourceFilesVolume = "$prefix-source-files"
$restoreFilesVolume = "$prefix-restore-files"
$backupVolume = "$prefix-backup"
$image = 'pgvector/pgvector:0.8.6-pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f'
$appImage = 'literature-workbench-api:latest'
$database = 'restore_drill'
$databaseUser = 'workbench'
$databasePassword = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
$createdVolumes = @()
$createdContainers = @()
$networkCreated = $false

function Invoke-Docker {
    param([string[]]$Arguments)
    & $dockerExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-DockerText {
    param([string[]]$Arguments)
    $output = & $dockerExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE."
    }
    return ($output -join [Environment]::NewLine)
}

function New-DrillVolume {
    param([string]$Name)
    $existing = & $dockerExe volume inspect $Name 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        throw "Refusing to reuse existing test volume $Name."
    }
    [void](Invoke-DockerText @('volume', 'create', $Name))
    $script:createdVolumes += $Name
}

function Wait-Postgres {
    param([string]$ContainerName)
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        & $dockerExe exec $ContainerName pg_isready -U $databaseUser -d $database *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw "Isolated PostgreSQL container $ContainerName did not become ready."
}

try {
    foreach ($volume in @($sourceDbVolume, $restoreDbVolume, $sourceFilesVolume, $restoreFilesVolume, $backupVolume)) {
        New-DrillVolume $volume
    }
    [void](Invoke-DockerText @('network', 'create', $network))
    $networkCreated = $true

    Invoke-Docker @(
        'run', '-d', '--rm', '--name', $sourceDb, '--network', $network, '--network-alias', $sourceDb,
        '--mount', "type=volume,source=$sourceDbVolume,target=/var/lib/postgresql/data",
        '-e', "POSTGRES_DB=$database", '-e', "POSTGRES_USER=$databaseUser", '-e', "POSTGRES_PASSWORD=$databasePassword", $image
    )
    $createdContainers += $sourceDb
    Wait-Postgres $sourceDb

    $sourceUrl = "postgresql+psycopg://${databaseUser}:${databasePassword}@${sourceDb}:5432/${database}"
    Invoke-Docker @(
        'run', '--rm', '--network', $network,
        '-e', "DATABASE_URL=$sourceUrl", '-e', 'DATA_DIR=/data', '-e', 'SILICONFLOW_API_KEY=',
        $appImage, 'alembic', 'upgrade', 'head'
    )
    $seedOutput = Invoke-DockerText @(
        'run', '--rm', '--network', $network,
        '--mount', "type=volume,source=$sourceFilesVolume,target=/data",
        '-e', "DATABASE_URL=$sourceUrl", '-e', 'DATA_DIR=/data', '-e', 'SILICONFLOW_API_KEY=',
        $appImage, 'python', 'tests/m5_backup_restore_fixture.py', 'seed'
    )
    $seed = $seedOutput | ConvertFrom-Json

    Invoke-Docker @(
        'run', '--rm', '--network', $network,
        '--mount', "type=volume,source=$backupVolume,target=/backup",
        '-e', "PGPASSWORD=$databasePassword", $image,
        'pg_dump', '-h', $sourceDb, '-U', $databaseUser, '-d', $database,
        '--format=custom', '--no-owner', '--no-acl', '--file=/backup/database.dump'
    )
    Invoke-Docker @(
        'run', '--rm',
        '--mount', "type=volume,source=$sourceFilesVolume,target=/data,readonly",
        '--mount', "type=volume,source=$backupVolume,target=/backup",
        $image, 'sh', '-c', 'tar -czf /backup/files.tar.gz -C /data .'
    )
    $backupHashes = Invoke-DockerText @(
        'run', '--rm', '--mount', "type=volume,source=$backupVolume,target=/backup",
        $image, 'sha256sum', '/backup/database.dump', '/backup/files.tar.gz'
    )
    $hashesByPath = @{}
    foreach ($line in ($backupHashes -split "`r?`n")) {
        $parts = $line.Trim() -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[0] -match '^[0-9a-f]{64}$') {
            $hashesByPath[$parts[1].TrimStart('*')] = $parts[0]
        }
    }
    $dumpHash = $hashesByPath['/backup/database.dump']
    $filesArchiveHash = $hashesByPath['/backup/files.tar.gz']
    if (-not $dumpHash -or -not $filesArchiveHash) { throw 'Could not calculate backup archive checksums.' }

    Invoke-Docker @('stop', $sourceDb)
    $createdContainers = @($createdContainers | Where-Object { $_ -ne $sourceDb })

    Invoke-Docker @(
        'run', '-d', '--rm', '--name', $restoreDb, '--network', $network, '--network-alias', $restoreDb,
        '--mount', "type=volume,source=$restoreDbVolume,target=/var/lib/postgresql/data",
        '-e', "POSTGRES_DB=$database", '-e', "POSTGRES_USER=$databaseUser", '-e', "POSTGRES_PASSWORD=$databasePassword", $image
    )
    $createdContainers += $restoreDb
    Wait-Postgres $restoreDb
    Invoke-Docker @(
        'run', '--rm', '--network', $network,
        '--mount', "type=volume,source=$backupVolume,target=/backup",
        '-e', "PGPASSWORD=$databasePassword", $image,
        'pg_restore', '-h', $restoreDb, '-U', $databaseUser, '-d', $database,
        '--no-owner', '--no-acl', '/backup/database.dump'
    )
    Invoke-Docker @(
        'run', '--rm',
        '--mount', "type=volume,source=$backupVolume,target=/backup,readonly",
        '--mount', "type=volume,source=$restoreFilesVolume,target=/data",
        $image, 'sh', '-c', 'tar -xzf /backup/files.tar.gz -C /data'
    )

    $restoreUrl = "postgresql+psycopg://${databaseUser}:${databasePassword}@${restoreDb}:5432/${database}"
    $verifyOutput = Invoke-DockerText @(
        'run', '--rm', '--network', $network,
        '--mount', "type=volume,source=$restoreFilesVolume,target=/data,readonly",
        '-e', "DATABASE_URL=$restoreUrl", '-e', 'DATA_DIR=/data', '-e', 'SILICONFLOW_API_KEY=',
        $appImage, 'python', 'tests/m5_backup_restore_fixture.py', 'verify'
    )
    $verification = $verifyOutput | ConvertFrom-Json
    if ($verification.pdf_sha256 -ne $seed.pdf_sha256) {
        throw 'Restored PDF checksum differs from the pre-backup source.'
    }

    if (-not $ReportPath) {
        $ReportPath = Join-Path $projectRoot ("experiments\reports\m5-backup-restore-drill-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    } elseif (-not [System.IO.Path]::IsPathRooted($ReportPath)) {
        $ReportPath = Join-Path $projectRoot $ReportPath
    }
    $report = [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString('o')
        classification = 'isolated synthetic database and file-volume restore drill; no user volumes touched'
        isolated_project = $prefix
        database_dump_sha256 = $dumpHash
        file_archive_sha256 = $filesArchiveHash
        source_manifest = $seed
        restored_manifest = $verification
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
    Write-Output (Get-Content -LiteralPath $ReportPath -Raw)
} finally {
    foreach ($container in $createdContainers) {
        & $dockerExe stop $container *> $null
    }
    if ($networkCreated) {
        & $dockerExe network rm $network *> $null
    }
    if ($createdVolumes.Count -gt 0) {
        & $dockerExe volume rm @createdVolumes
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Some isolated restore-drill volumes could not be removed: $($createdVolumes -join ', ')"
        }
    }
}
