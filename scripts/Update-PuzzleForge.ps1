[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$Revision,
    [string]$RepoRoot = (Get-Location).Path,
    [switch]$Tune
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'src\puzzleforge\cli.py'))) {
    throw 'Open PowerShell in the PuzzleForge folder (the one containing Start-PuzzleForge.cmd).'
}

# Do not update files underneath an existing Python/GPU worker.
$Running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(cuBitCrack|clBitCrack)\.exe$' -or
    ($_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match '-m\s+puzzleforge\s+local-(app|run|retune)')
})
if ($Running.Count) {
    throw 'Stop PuzzleForge with Ctrl+C, then run this update again. No files have changed.'
}

$UpdateStage = Join-Path ([System.IO.Path]::GetTempPath()) ('puzzleforge-update-' + [guid]::NewGuid().ToString('N'))
$Backup = Join-Path $RepoRoot ('.puzzleforge\updates\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$Changed = [System.Collections.Generic.List[object]]::new()
$UpdateSucceeded = $false
$CampaignLock = $null
New-Item -ItemType Directory -Path $UpdateStage | Out-Null
try {
    $ProfilePath = Join-Path $RepoRoot '.puzzleforge\local\profile.json'
    if (Test-Path -LiteralPath $ProfilePath) {
        $ProfileData = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
        $LockPath = [System.IO.Path]::ChangeExtension([string]$ProfileData.database, '.gpu.lock')
        $CampaignLock = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $Archive = Join-Path $UpdateStage 'source.zip'
    Write-Host 'Downloading the pinned PuzzleForge update...'
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/verknode/puzzleforge1/archive/$Revision.zip" -OutFile $Archive
    $Unpacked = Join-Path $UpdateStage 'source'
    Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked
    $Source = Join-Path $Unpacked "puzzleforge1-$Revision"
    if (-not (Test-Path -LiteralPath (Join-Path $Source 'src\puzzleforge\runtime.py'))) {
        throw 'The downloaded revision is not the expected performance update.'
    }
    # Only tracked application code is replaced. State, binaries and the venv are excluded.
    $AllowedDirectories = @('src', 'scripts', 'tests', 'docs', 'tools')
    $AllowedRootFiles = @('pyproject.toml', 'README.md', 'Start-PuzzleForge.cmd', 'Start-PuzzleForge-Cold.cmd', 'Tune-PuzzleForge.cmd')
    $Files = @(Get-ChildItem -LiteralPath $Source -Recurse -File | Where-Object {
        $Relative = $_.FullName.Substring($Source.Length + 1)
        $Top = ($Relative -split '[\\/]')[0]
        $AllowedDirectories -contains $Top -or $AllowedRootFiles -contains $Relative
    })
    if ($Files.Count -lt 20) { throw 'The downloaded source is incomplete.' }
    New-Item -ItemType Directory -Path $Backup | Out-Null
    foreach ($File in $Files) {
        $Relative = $File.FullName.Substring($Source.Length + 1)
        $Destination = Join-Path $RepoRoot $Relative
        $Saved = Join-Path $Backup $Relative
        $Existed = Test-Path -LiteralPath $Destination
        if ($Existed) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Saved) | Out-Null
            Copy-Item -LiteralPath $Destination -Destination $Saved
        }
        $Changed.Add([pscustomobject]@{ Destination = $Destination; Saved = $Saved; Existed = $Existed })
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Destination -Force
    }
    Set-Content -LiteralPath (Join-Path $Backup 'installed-revision.txt') -Value $Revision -Encoding ASCII
    $UpdateSucceeded = $true
    Write-Host "Updated. Previous code is saved in: $Backup" -ForegroundColor Green
} catch {
    $UpdateError = $_
    if (-not $UpdateSucceeded) {
        foreach ($Entry in $Changed) {
            if ($Entry.Existed) {
                Copy-Item -LiteralPath $Entry.Saved -Destination $Entry.Destination -Force
            } elseif (Test-Path -LiteralPath $Entry.Destination) {
                Remove-Item -LiteralPath $Entry.Destination -Force
            }
        }
    }
    throw $UpdateError
} finally {
    if ($null -ne $CampaignLock) { $CampaignLock.Dispose() }
    Remove-Item -LiteralPath $UpdateStage -Recurse -Force
}

Set-Location -LiteralPath $RepoRoot
$Launcher = Join-Path $RepoRoot 'scripts\puzzleforge-local.ps1'
if ($Tune) { & $Launcher -Tune } else { & $Launcher }
