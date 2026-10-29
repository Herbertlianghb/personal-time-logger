[CmdletBinding()]
param(
    [string]$PackagePath = $PSScriptRoot,
    [switch]$Launch,
    [switch]$StartWithWindows,
    [switch]$RemoveShortcuts
)
$ErrorActionPreference = 'Stop'
$PackageRoot = (Resolve-Path -LiteralPath $PackagePath).Path
$LocalRoot = [Environment]::GetFolderPath('LocalApplicationData')
$InstallParent = [IO.Path]::GetFullPath((Join-Path $LocalRoot 'Programs\PersonalTimeLogger'))
$ManifestName = 'package-manifest.json'
$ManifestPath = Join-Path $PackageRoot $ManifestName
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw 'Package manifest is missing.' }
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Manifest.name -ne 'PersonalTimeLogger' -or [string]$Manifest.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Unexpected package identity.' }
$Version = [string]$Manifest.version
$InstallRoot = [IO.Path]::GetFullPath((Join-Path $InstallParent $Version))
if (-not $InstallRoot.StartsWith($InstallParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Install target is outside the PersonalTimeLogger directory.'
}
$Executable = Join-Path $InstallRoot 'PersonalTimeLogger.exe'

function Confirm-Package([string]$Root) {
    foreach ($Entry in $Manifest.files) {
        $Target = [IO.Path]::GetFullPath((Join-Path $Root ([string]$Entry.path)))
        if (-not $Target.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid package member path.' }
        if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) { throw "Package file missing: $($Entry.path)" }
        if ((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash -ne $Entry.sha256) { throw "Package file differs: $($Entry.path)" }
    }
}
Confirm-Package $PackageRoot
$Shell = New-Object -ComObject WScript.Shell
$Links = @(
    @{Path=(Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '个人时间记录.lnk'); Args=''},
    @{Path=(Join-Path ([Environment]::GetFolderPath('Programs')) '个人时间记录\个人时间记录.lnk'); Args=''}
)
$StartupPath = Join-Path ([Environment]::GetFolderPath('Startup')) '个人时间记录.lnk'
if ($StartWithWindows -or $RemoveShortcuts -or (Test-Path -LiteralPath $StartupPath)) {
    $Links += @{Path=$StartupPath; Args='--background'}
}
foreach ($Link in $Links) {
    if (Test-Path -LiteralPath $Link.Path) {
        $Existing = $Shell.CreateShortcut($Link.Path)
        $ManagedPrevious = $false
        if ($Existing.TargetPath) {
            $OldExecutable = [IO.Path]::GetFullPath($Existing.TargetPath)
            $OldRoot = [IO.Path]::GetDirectoryName($OldExecutable)
            $OldVersion = [IO.Path]::GetFileName($OldRoot)
            if ($OldRoot.StartsWith($InstallParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
                $OldVersion -match '^[0-9]+\.[0-9]+\.[0-9]+$' -and
                [IO.Path]::GetFileName($OldExecutable) -eq 'PersonalTimeLogger.exe' -and
                (Test-Path -LiteralPath (Join-Path $OldRoot $ManifestName) -PathType Leaf)) {
                $OldManifest = Get-Content -LiteralPath (Join-Path $OldRoot $ManifestName) -Raw -Encoding UTF8 | ConvertFrom-Json
                $ManagedPrevious = $OldManifest.name -eq 'PersonalTimeLogger' -and $OldManifest.version -eq $OldVersion
            }
        }
        if (($Existing.TargetPath -ne $Executable -and -not $ManagedPrevious) -or $Existing.Arguments -ne $Link.Args) { throw "An unrelated shortcut already exists: $($Link.Path)" }
    }
}
if ($RemoveShortcuts) {
    foreach ($Link in $Links) {
        if (Test-Path -LiteralPath $Link.Path -PathType Leaf) { Remove-Item -LiteralPath $Link.Path }
    }
    [pscustomobject]@{version=$Version; action='shortcuts_removed'; data_preserved=$true} | ConvertTo-Json
    return
}
if (Test-Path -LiteralPath $InstallRoot) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $ManifestName) -PathType Leaf)) { throw 'Install target exists without this package manifest; left unchanged.' }
    $ExistingManifest = Get-FileHash -LiteralPath (Join-Path $InstallRoot $ManifestName) -Algorithm SHA256
    $SourceManifest = Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256
    if ($ExistingManifest.Hash -ne $SourceManifest.Hash) { throw "A different build already occupies version $Version; left unchanged." }
    Confirm-Package $InstallRoot
} else {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    foreach ($Item in Get-ChildItem -LiteralPath $PackageRoot -Force) {
        Copy-Item -LiteralPath $Item.FullName -Destination $InstallRoot -Recurse
    }
    Confirm-Package $InstallRoot
}
foreach ($Link in $Links) {
    if (-not (Test-Path -LiteralPath $Link.Path) -or $Shell.CreateShortcut($Link.Path).TargetPath -ne $Executable) {
        $LinkFolder = [IO.Path]::GetDirectoryName($Link.Path)
        New-Item -ItemType Directory -Path $LinkFolder -Force | Out-Null
        $Shortcut = $Shell.CreateShortcut($Link.Path)
        $Shortcut.TargetPath = $Executable
        $Shortcut.Arguments = $Link.Args
        $Shortcut.WorkingDirectory = $InstallRoot
        $Shortcut.IconLocation = $Executable + ',0'
        $Shortcut.Description = '个人时间记录：自然记录与正式历史时间线'
        $Shortcut.Save()
    }
}
if ($Launch) { Start-Process -FilePath $Executable -WindowStyle Normal }
[pscustomobject]@{version=$Version; executable=$Executable; shortcuts=$Links.Path; state_directory=(Join-Path $env:USERPROFILE '.personal-time-logger\state'); data_preserved=$true} | ConvertTo-Json -Depth 4
