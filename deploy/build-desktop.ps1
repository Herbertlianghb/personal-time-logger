[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$BuildRoot,
    [string]$PackagePath
)
$ErrorActionPreference = 'Stop'
$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$VersionMatch = [regex]::Match((Get-Content -LiteralPath (Join-Path $SourceRoot 'time_logger\desktop_app.py') -Raw), '(?m)^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"')
if (-not $VersionMatch.Success) { throw 'Desktop version is missing.' }
$Version = $VersionMatch.Groups[1].Value
if (-not $PythonPath) {
    $PythonPath = Join-Path $SourceRoot 'work\desktop-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) { $PythonPath = Join-Path $SourceRoot '.venv-desktop\Scripts\python.exe' }
}
if (-not $BuildRoot) { $BuildRoot = Join-Path $SourceRoot 'work\desktop-build' }
if (-not $PackagePath) { $PackagePath = Join-Path $SourceRoot ('dist\PersonalTimeLogger-' + $Version) }
$Python = (Resolve-Path -LiteralPath $PythonPath).Path
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$PackagePath = [IO.Path]::GetFullPath($PackagePath)
if (Test-Path -LiteralPath $PackagePath) { throw 'Package output already exists; left unchanged.' }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$BuildDist = Join-Path $BuildRoot ('dist-' + [guid]::NewGuid().ToString('N'))
$PreviousPath = $env:PATH
try {
    # Qt uses Windows' ICU exports. An unrelated Poppler ICU on the inherited
    # PATH has incompatible suffixed exports and must not enter this package.
    $env:PATH = ([IO.Path]::GetDirectoryName($Python), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join [IO.Path]::PathSeparator
    & $Python (Join-Path $PSScriptRoot 'generate-desktop-icon.py')
    if ($LASTEXITCODE -ne 0) { throw "Application icon generation failed with exit code $LASTEXITCODE" }
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name PersonalTimeLogger `
        --icon (Join-Path $PSScriptRoot 'personal-time-logger.ico') `
        --version-file (Join-Path $PSScriptRoot 'desktop-version.txt') `
        --workpath (Join-Path $BuildRoot 'analysis') --specpath $BuildRoot --distpath $BuildDist `
        --paths $SourceRoot --exclude-module tkinter --exclude-module unittest --exclude-module lark_oapi `
        (Join-Path $SourceRoot 'desktop_entry.py')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    $env:PATH = $PreviousPath
}
& $Python (Join-Path $PSScriptRoot 'package-desktop.py') --built (Join-Path $BuildDist 'PersonalTimeLogger') --output $PackagePath
if ($LASTEXITCODE -ne 0) { throw "Package assembly failed with exit code $LASTEXITCODE" }
