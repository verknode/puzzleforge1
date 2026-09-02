[CmdletBinding()]
param(
    # Empty means: create new campaigns as hypothesis, and leave an existing
    # campaign in whatever mode it is already running.
    [ValidateSet("", "hypothesis", "cold")]
    [string]$Mode = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$Profile = Join-Path $RepoRoot ".puzzleforge\local\profile.json"

Set-Location $RepoRoot

if (-not (Test-Path $VenvPython)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & py -3 -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    } else {
        $Python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $Python) {
            throw "Python 3.11+ was not found. Install Python and run this file again."
        }
        & python -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    }
}

& $VenvPython -m pip install --disable-pip-version-check --quiet -e $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Could not install PuzzleForge." }

if (-not (Test-Path $Profile)) {
    $Candidates = @(
        (Join-Path $RepoRoot "cuBitCrack.exe"),
        (Join-Path $RepoRoot ".puzzleforge\bin\cuBitCrack.exe"),
        (Join-Path $RepoRoot "clBitCrack.exe"),
        (Join-Path $RepoRoot ".puzzleforge\bin\clBitCrack.exe")
    )
    $Engine = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Engine) {
        throw "Place cuBitCrack.exe in the PuzzleForge folder and run Start-PuzzleForge.cmd again."
    }
    $SetupMode = if ($Mode) { $Mode } else { "hypothesis" }
    & $VenvPython -m puzzleforge local-setup --binary $Engine --puzzle 71 --mode $SetupMode
    if ($LASTEXITCODE -ne 0) { throw "PuzzleForge local setup failed." }
} elseif ($Mode) {
    $Current = (Get-Content $Profile -Raw | ConvertFrom-Json).planner_mode
    if ($Current -ne $Mode) {
        & $VenvPython -m puzzleforge "$Mode-enable"
        if ($LASTEXITCODE -ne 0) { throw "Could not switch to $Mode mode." }
    }
}

& $VenvPython -m puzzleforge generator-enable --cpu-percent 10
if ($LASTEXITCODE -ne 0) { throw "Could not enable Generator Lab." }

& $VenvPython -m puzzleforge local-app
