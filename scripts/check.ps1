[CmdletBinding()]
param([string]$DataDirectory, [string]$PythonExecutable)

$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgeCandidates = @()
if ($PythonExecutable) {
    $bridgeCandidates += @{ File = $PythonExecutable; Prefix = @() }
} else {
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notmatch '\\Microsoft\\WindowsApps\\') {
            $prefix = if ($name -eq 'py.exe') { @('-3') } else { @() }
            $bridgeCandidates += @{ File = $command.Source; Prefix = $prefix }
        }
    }
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled -PathType Leaf) {
        $bridgeCandidates += @{ File = $bundled; Prefix = @() }
    }
}
$bridgeRuntime = $null
foreach ($candidate in $bridgeCandidates) {
    try {
        $probe = @($candidate.Prefix) + @('-I', '-c', 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)')
        & $candidate.File @probe *> $null
        if ($LASTEXITCODE -eq 0) { $bridgeRuntime = $candidate; break }
    } catch { }
}
if ($null -eq $bridgeRuntime) {
    throw 'Python 3.11 or newer is required. Install Python on PATH or pass -PythonExecutable with its full path.'
}
$bridgeArguments = @($bridgeRuntime.Prefix) + @('-X', 'utf8', (Join-Path $bridgeRoot 'run_bridge.py'))
if ($DataDirectory) {
    $bridgeArguments += @('--data-dir', $DataDirectory)
}
$bridgeArguments += 'status'
& $bridgeRuntime.File @bridgeArguments
exit $LASTEXITCODE
