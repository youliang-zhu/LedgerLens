param([string]$PythonPath, [string]$RuntimePath)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.local/viewer-venv/Scripts/pythonw.exe' }
if (-not $RuntimePath) { $RuntimePath = Join-Path $projectRoot '.local/runtime/ezbookkeeping' }
$pythonResolved = (Resolve-Path -LiteralPath $PythonPath).Path
$runtimeResolved = (Resolve-Path -LiteralPath $RuntimePath).Path
if (-not (Test-Path -LiteralPath (Join-Path $runtimeResolved 'ezbookkeeping.exe'))) { throw 'ezBookkeeping runtime missing' }
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'LedgerLens.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'LedgerLens.lnk already exists; not overwritten' }
$shellObject = New-Object -ComObject WScript.Shell
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonResolved
$shortcut.Arguments = '-m ledgerlens --runtime "' + $runtimeResolved + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'LedgerLens - Local bill review and ezBookkeeping dashboard'
$shortcut.Save()
Write-Output $shortcutPath
