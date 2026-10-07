[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$organizerRoot = Split-Path -Parent $PSScriptRoot
$organizerToolPath = Join-Path $organizerRoot '.tools\ncm-cli'
$organizerNpm = Get-Command npm.cmd -ErrorAction Stop
& $organizerNpm.Source install --prefix $organizerToolPath '@music163/ncm-cli@0.1.7' --save-exact --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'Official CLI installation did not finish.' }
Write-Output 'Project-local official CLI installed. Run run_organizer.py doctor to check.'
Write-Output 'Known dependency advisories remain for official CLI 0.1.7; see SECURITY.md before account use.'
