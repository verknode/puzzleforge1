$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Get-ChildItem (Join-Path $RepoRoot 'scripts') -Filter '*.ps1' | ForEach-Object {
    $Tokens = $null
    $ParseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$Tokens, [ref]$ParseErrors) | Out-Null
    if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
}

# Exercise the real updater against an offline source archive and a fake launcher.
$TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('pf-update-test-' + [guid]::NewGuid().ToString('N'))
$Revision = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
New-Item -ItemType Directory -Path $TestRoot | Out-Null
try {
    $Target = Join-Path $TestRoot 'installed'
    $Fixture = Join-Path $TestRoot "puzzleforge1-$Revision"
    New-Item -ItemType Directory -Force -Path (Join-Path $Target 'src\puzzleforge'), (Join-Path $Target '.puzzleforge\local'), (Join-Path $Fixture 'src\puzzleforge'), (Join-Path $Fixture 'scripts') | Out-Null
    [IO.File]::WriteAllText((Join-Path $Target 'src\puzzleforge\cli.py'), 'old code')
    [IO.File]::WriteAllText((Join-Path $Target '.puzzleforge\local\keep.txt'), 'private state')
    [IO.File]::WriteAllText((Join-Path $Target 'cuBitCrack.exe'), 'engine bytes')
    [IO.File]::WriteAllText((Join-Path $Fixture 'src\puzzleforge\cli.py'), 'new code')
    [IO.File]::WriteAllText((Join-Path $Fixture 'src\puzzleforge\runtime.py'), 'new runtime')
    for ($i = 0; $i -lt 20; $i++) {
        [IO.File]::WriteAllText((Join-Path $Fixture "src\puzzleforge\sample$i.py"), "sample $i")
    }
    [IO.File]::WriteAllText((Join-Path $Fixture 'scripts\puzzleforge-local.ps1'), 'param([switch]$Tune) Write-Host "Test launcher"')
    $script:FixtureZip = Join-Path $TestRoot 'fixture.zip'
    Compress-Archive -LiteralPath $Fixture -DestinationPath $script:FixtureZip
    function Get-CimInstance { param($ClassName) return @() }
    function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile) Copy-Item -LiteralPath $script:FixtureZip -Destination $OutFile }
    & (Join-Path $RepoRoot 'scripts\Update-PuzzleForge.ps1') -Revision $Revision -RepoRoot $Target
    if ([IO.File]::ReadAllText((Join-Path $Target 'src\puzzleforge\cli.py')) -ne 'new code') { throw 'Code not updated' }
    if ([IO.File]::ReadAllText((Join-Path $Target '.puzzleforge\local\keep.txt')) -ne 'private state') { throw 'State changed' }
    if ([IO.File]::ReadAllText((Join-Path $Target 'cuBitCrack.exe')) -ne 'engine bytes') { throw 'Engine changed' }
    $SavedCode = @(Get-ChildItem (Join-Path $Target '.puzzleforge\updates') -Recurse -Filter 'cli.py')
    if ($SavedCode.Count -ne 1 -or [IO.File]::ReadAllText($SavedCode[0].FullName) -ne 'old code') { throw 'Backup missing' }
    function Get-CimInstance { param($ClassName) return @([pscustomobject]@{Name='python.exe'; CommandLine='python -m puzzleforge local-app'}) }
    $Rejected = $false
    try { & (Join-Path $RepoRoot 'scripts\Update-PuzzleForge.ps1') -Revision $Revision -RepoRoot $Target } catch { $Rejected = $_.ToString() -match 'Stop PuzzleForge' }
    if (-not $Rejected) { throw 'Active worker was not rejected' }
    Write-Host 'PASS: PowerShell syntax, byte-copy update, state/engine preservation, backup, active-worker guard.'
} finally {
    Set-Location -LiteralPath $RepoRoot
    Remove-Item -LiteralPath $TestRoot -Recurse -Force
}
