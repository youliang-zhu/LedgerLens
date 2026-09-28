param([string]$PythonPath, [string]$RuntimePath, [switch]$UpdateIcon)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.local/viewer-venv/Scripts/pythonw.exe' }
if (-not $RuntimePath) { $RuntimePath = Join-Path $projectRoot '.local/runtime/ezbookkeeping' }
$pythonResolved = (Resolve-Path -LiteralPath $PythonPath).Path
$runtimeResolved = (Resolve-Path -LiteralPath $RuntimePath).Path
if (-not (Test-Path -LiteralPath (Join-Path $runtimeResolved 'ezbookkeeping.exe'))) { throw 'ezBookkeeping runtime missing' }
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'LedgerLens.lnk'
$iconPath = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'ledgerlens/static/icon.ico')).Path
$shellObject = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    if (-not $UpdateIcon) { throw 'LedgerLens.lnk already exists; use -UpdateIcon to change only its icon' }
    $existing = $shellObject.CreateShortcut($shortcutPath)
    if ($existing.TargetPath -ne $pythonResolved -or $existing.WorkingDirectory -ne $projectRoot) {
        throw 'Existing shortcut belongs to another installation; unchanged'
    }
    $backupFolder = Join-Path $projectRoot '.local/shortcut-backups'
    New-Item -ItemType Directory -Path $backupFolder -Force | Out-Null
    Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $backupFolder ('LedgerLens-' + [guid]::NewGuid().ToString('N') + '.lnk'))
    $existing.IconLocation = $iconPath + ',0'
    $existing.Save()
    Write-Output $shortcutPath
    return
}
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonResolved
$shortcut.Arguments = '-m ledgerlens --runtime "' + $runtimeResolved + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'LedgerLens - Local bill review and ezBookkeeping dashboard'
$shortcut.IconLocation = $iconPath + ',0'
$shortcut.Save()
Write-Output $shortcutPath
