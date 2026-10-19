param([switch]$CheckOnly, [switch]$StartOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataPath = Join-Path $projectRoot 'data'
$runtimePath = Join-Path $dataPath 'local-runtime.json'
$modulePattern = '(?:^|\s)-m\s+x_operator\.main(?:\s|$)'

function Get-ServiceOwners {
    @(Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-VerifiedProcesses {
    param([int[]]$Owners)
    $verified = @()
    foreach ($ownerId in $Owners) {
        $child = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId"
        if ($null -eq $child -or $child.CommandLine -notmatch $modulePattern) {
            throw 'Port occupied by another application; no process was stopped.'
        }
        $rootProcess = $child
        if ($child.ExecutablePath -ne $pythonPath) {
            $rootProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.ParentProcessId)"
        }
        # Windows venv Python redirects to a child using the base interpreter.
        # Trust the live process tree, never a saved PID which may be stale/reused.
        if ($null -eq $rootProcess -or $rootProcess.ExecutablePath -ne $pythonPath -or
            $rootProcess.CommandLine -notmatch $modulePattern -or
            $rootProcess.CreationDate -gt $child.CreationDate) {
            throw 'Cannot verify project ownership of this port; no process was stopped.'
        }
        $verified += $child
        $verified += $rootProcess
    }
    @($verified | Sort-Object ProcessId -Unique)
}

function Read-Health {
    try {
        Invoke-RestMethod -Uri "$serviceUrl/healthz" -TimeoutSec 2 -ErrorAction Stop
    } catch { return $null }
}

$lock = $null
$locked = $false
try {
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python environment is missing. Run start.bat first.' }
    # Read configuration without importing NiceGUI or initializing the application.
    $configJson = & $pythonPath -X utf8 -c "import json,tomllib,pathlib; from x_operator.version import __version__; p=pathlib.Path('config/settings.toml'); c=tomllib.loads(p.read_text(encoding='utf-8')) if p.exists() else {}; a=c.get('app',{}); print(json.dumps(dict(host=a.get('host','127.0.0.1'),port=a.get('port',8080),version=__version__)))"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read project configuration.' }
    $config = $configJson | ConvertFrom-Json
    $servicePort = [int]$config.port
    $connectHost = [string]$config.host
    if ($connectHost -eq '0.0.0.0') { $connectHost = '127.0.0.1' }
    if ($connectHost -eq '::') { $connectHost = '::1' }
    if ($connectHost.Contains(':')) { $connectHost = "[$connectHost]" }
    $serviceUrl = "http://${connectHost}:$servicePort"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $key = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-','')
    $sha.Dispose()
    $lock = New-Object System.Threading.Mutex($false, "Local\x-operator-$key")
    try { $locked = $lock.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another start/restart is running. Wait for it to finish.' }

    $owners = @(Get-ServiceOwners)
    $processes = @(Get-VerifiedProcesses -Owners $owners)
    if ($CheckOnly) {
        Write-Host "[x-operator] Verified project: $projectRoot; listener: $($owners -join ', '); source version: $($config.version)"
        Write-Host '[x-operator] Read-only check complete. No process was stopped or started.'
        exit 0
    }
    if ($owners.Count -gt 0 -and $StartOnly) {
        Write-Host "[x-operator] Already running: $serviceUrl. Use restart.bat to load code updates."
        exit 0
    }
    if ($owners.Count -gt 0) {
        Write-Host '[x-operator] Stopping the verified project service...'
        # Recheck start times immediately before stopping to guard against PID reuse.
        foreach ($process in $processes) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
            if ($null -ne $current) {
                if ($current.CreationDate -ne $process.CreationDate -or $current.CommandLine -ne $process.CommandLine) {
                    throw 'Process identity changed during restart; stopped processing.'
                }
                Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            }
        }
        $deadline = [datetime]::UtcNow.AddSeconds(15)
        while ((Get-ServiceOwners).Count -gt 0 -and [datetime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
        if ((Get-ServiceOwners).Count -gt 0) { throw 'Port is still occupied; no new service was started.' }
    }
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout = Join-Path $dataPath "server-$stamp.stdout.log"
    $stderr = Join-Path $dataPath "server-$stamp.stderr.log"
    Write-Host '[x-operator] Starting the local project service...'
    $env:PYTHONUTF8 = '1'
    $server = Start-Process -FilePath $pythonPath -ArgumentList '-X','utf8','-m','x_operator.main' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $record = [ordered]@{pid=$server.Id; started_at=$server.StartTime.ToUniversalTime().ToString('o'); repository=$projectRoot; python=$pythonPath; url=$serviceUrl; version=$config.version; stdout=$stdout; stderr=$stderr; status='starting'}
    if ((Test-Path -LiteralPath (Join-Path $projectRoot ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $record.branch = git branch --show-current 2>$null
        $record.commit = git rev-parse HEAD 2>$null
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $runtimePath -Encoding UTF8
    $deadline = [datetime]::UtcNow.AddSeconds(60)
    while ([datetime]::UtcNow -lt $deadline) {
        $server.Refresh()
        if ($server.HasExited) { throw "Service exited before becoming ready. See $stderr" }
        $health = Read-Health
        if ($null -ne $health -and $health.repository -eq $projectRoot -and $health.version -eq $config.version) {
            $owners = @(Get-ServiceOwners)
            if ($owners -contains [int]$health.pid) {
                $processes = @(Get-VerifiedProcesses -Owners $owners)
                if ($processes.ProcessId -contains $server.Id) {
                    $record.status = 'ready'
                    $record.listener_pid = $health.pid
                    $record.verified_at = [datetime]::UtcNow.ToString('o')
                    $record | ConvertTo-Json | Set-Content -LiteralPath $runtimePath -Encoding UTF8
                    Write-Host "[x-operator] Ready: $serviceUrl (v$($health.version))"
                    Write-Host '[x-operator] Refresh your browser. You can close this window.'
                    exit 0
                }
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Health check timed out. The process may still be starting; do not assume it is ready. See $stderr"
} catch {
    Write-Host "[x-operator] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($stderr -and (Test-Path -LiteralPath $stderr)) { Get-Content -LiteralPath $stderr -Tail 15 }
    exit 1
} finally {
    if ($locked) { $lock.ReleaseMutex() }
    if ($null -ne $lock) { $lock.Dispose() }
}
