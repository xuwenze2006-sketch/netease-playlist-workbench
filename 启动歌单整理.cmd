@echo off
setlocal
set "ORGANIZER_LAUNCH_SCRIPT=%~f0"
set "ORGANIZER_PROJECT=%~dp0"
start "" /B powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try { $lines=[IO.File]::ReadAllLines($env:ORGANIZER_LAUNCH_SCRIPT,[Text.Encoding]::UTF8); $marker=[Array]::IndexOf($lines,'# ORGANIZER_POWERSHELL'); if($marker -lt 0){throw 'Invalid launcher'}; . ([scriptblock]::Create([string]::Join([Environment]::NewLine,$lines[($marker+1)..($lines.Length-1)]))); Start-OrganizerWorkbench -ProjectDirectory $env:ORGANIZER_PROJECT } catch { Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show('The local workbench could not start. Please check the local validation steps in README. No account task was retried.','Playlist Workbench') }"
exit /b 0

# ORGANIZER_POWERSHELL
function Invoke-OrganizerPythonProbe {
    param([string]$Executable, [string]$Arguments)
    $process = New-Object Diagnostics.Process
    try {
        $process.StartInfo.FileName = $Executable
        $process.StartInfo.Arguments = $Arguments
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.CreateNoWindow = $true
        $process.StartInfo.WindowStyle = 'Hidden'
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        if (-not $process.Start()) { return $null }
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            return $null
        }
        return @{ ExitCode = $process.ExitCode; Output = $process.StandardOutput.ReadToEnd() }
    } catch { return $null }
    finally { $process.Dispose() }
}

function Get-OrganizerLocalCandidates {
    foreach ($command in (Get-Command -Name pythonw.exe,python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $command.Source
    }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher -and $launcher.Source -notmatch '\\Microsoft\\WindowsApps\\') {
        # -0p lists existing installations; it does not request an installation.
        $listed = Invoke-OrganizerPythonProbe -Executable $launcher.Source -Arguments '-0p'
        if ($listed -and $listed.ExitCode -eq 0) {
            foreach ($match in [regex]::Matches($listed.Output, '(?im)((?:[A-Z]:\\|\\\\)[^\r\n]+?\\python(?:w)?\.exe)\s*$')) {
                $match.Groups[1].Value
            }
        }
    }
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in (Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            try {
                $install = Get-Item -LiteralPath (Join-Path $version.PSPath 'InstallPath') -ErrorAction Stop
                $location = $install.GetValue('')
                if ($location) { Join-Path $location 'python.exe' }
            } catch { }
        }
    }
}

function Find-OrganizerPython {
    param(
        [string]$ProfilePath = $env:USERPROFILE,
        [string[]]$LocalCandidates,
        [scriptblock]$PathExists = { param($path) Test-Path -LiteralPath $path -PathType Leaf },
        [scriptblock]$VersionProbe = {
            param($path)
            $result = Invoke-OrganizerPythonProbe -Executable $path -Arguments '-I -c "import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)"'
            return $result -and $result.ExitCode -eq 0
        }
    )
    $codex = Join-Path $ProfilePath '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'
    $candidates = @($codex)
    $searchedLocal = $false
    do {
      foreach ($candidate in $candidates) {
        if (-not $candidate -or $candidate -match '\\Microsoft\\WindowsApps\\') { continue }
        try {
            $absolute = [IO.Path]::GetFullPath($candidate)
            if ([IO.Path]::GetFileName($absolute) -notin @('python.exe', 'pythonw.exe')) { continue }
            $directory = [IO.Path]::GetDirectoryName($absolute)
            $windowless = Join-Path $directory 'pythonw.exe'
            $console = Join-Path $directory 'python.exe'
            if ((& $PathExists $windowless) -and (& $PathExists $console) -and (& $VersionProbe $console)) {
                return $windowless
            }
        } catch { }
      }
      if ($searchedLocal) { break }
      $searchedLocal = $true
      $candidates = if ($PSBoundParameters.ContainsKey('LocalCandidates')) { @($LocalCandidates) } else { @(Get-OrganizerLocalCandidates) }
    } while ($candidates.Count -gt 0)
    return $null
}

function Show-OrganizerRuntimeError {
    param([string]$Code)
    $message = if ($Code -eq 'runtime_missing') {
        '没有找到可用的 Python 3.11 或更高版本。请安装带 pythonw.exe 的本机 Python，或恢复现有 Codex 运行时，然后重新打开。程序不会自动下载或安装。'
    } else {
        '歌单工作台暂未能打开。请检查项目目录与文件访问权限，并参照 README 中的本地验证步骤。'
    }
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show(($message + "`n`n本次启动不会自动重发账号整理操作。"), '歌单工作台 · 启动未完成')
}

function Start-OrganizerWorkbench {
    param(
        [string]$ProjectDirectory,
        [scriptblock]$ResolveRuntime = { Find-OrganizerPython },
        [scriptblock]$Launch = {
            param($executable, $arguments, $directory)
            Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $directory -WindowStyle Hidden -ErrorAction Stop
        },
        [scriptblock]$Notify = { param($code) Show-OrganizerRuntimeError -Code $code }
    )
    try {
        $project = (Resolve-Path -LiteralPath $ProjectDirectory -ErrorAction Stop).ProviderPath
        $entry = Join-Path $project 'run_organizer.py'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Missing entry' }
        $runtime = & $ResolveRuntime
        if (-not $runtime) { & $Notify 'runtime_missing'; return }
        & $Launch $runtime @('-X', 'utf8', ('"' + $entry + '"'), 'gui') $project
    } catch { & $Notify 'startup_failed' }
}
