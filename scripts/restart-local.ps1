param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimePath = Join-Path $projectRoot 'data\local-runtime.json'
# -m starts with a non-word character, so \b-m does not match a normal command line.
$modulePattern = '(?:^|\s)-m\s+x_operator\.main(?:\s|$)'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment is missing. Run start.bat first.'
}

$appAddress = & $pythonPath -c "from x_operator.main import load_toml; c=load_toml().get('app',{}); print(str(c.get('host','127.0.0.1')) + ':' + str(c.get('port',8080)))"
if ($LASTEXITCODE -ne 0) { throw 'Could not read project configuration.' }
$servicePort = [int]($appAddress -split ':')[-1]
$listener = @(Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue)
if ($listener.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Service identity record is missing; no process was stopped.' }
    $oldRecord = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
    if ($oldRecord.repository -ne $projectRoot) { throw 'Service belongs to another project; no process was stopped.' }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($oldRecord.pid)"
    if ($null -eq $parent -or $parent.ExecutablePath -ne $pythonPath -or $parent.CommandLine -notmatch $modulePattern) {
        throw 'Service process identity changed; no process was stopped.'
    }
    $expectedStart = [datetimeoffset]::Parse($oldRecord.started_at)
    if ([math]::Abs(($parent.CreationDate.ToUniversalTime() - $expectedStart.UtcDateTime).TotalSeconds) -gt 5) {
        throw 'Service process ID was reused; no process was stopped.'
    }
    $targets = @()
    foreach ($owner in ($listener.OwningProcess | Select-Object -Unique)) {
        $child = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
        if ($null -eq $child -or $child.CommandLine -notmatch $modulePattern -or
            ($child.ProcessId -ne $parent.ProcessId -and $child.ParentProcessId -ne $parent.ProcessId)) {
            throw 'The port belongs to an unexpected process; no process was stopped.'
        }
        $targets += $child.ProcessId
    }
    if ($CheckOnly) {
        Write-Host "[x-operator] Identity verified: parent $($parent.ProcessId), listener $($targets -join ', '), project $projectRoot"
        Write-Host '[x-operator] Read-only check complete. No process was stopped or started.'
        exit 0
    }
    Write-Host '[x-operator] Stopping the current project service...'
    foreach ($serviceProcess in (($targets + @($parent.ProcessId)) | Select-Object -Unique)) {
        Stop-Process -Id $serviceProcess -Force -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue) {
        throw 'The port did not become available; no new service was started.'
    }
}

if ($CheckOnly) {
    Write-Host '[x-operator] No listener found. No process was stopped or started.'
    exit 0
}

Write-Host '[x-operator] Starting the local project service...'
$env:PYTHONUTF8 = '1'
$server = Start-Process -FilePath $pythonPath -ArgumentList '-X','utf8','-m','x_operator.main' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\local-server.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'data\local-server.stderr.log')
$record = [ordered]@{
    pid = $server.Id
    started_at = $server.StartTime.ToUniversalTime().ToString('o')
    repository = $projectRoot
    python = $pythonPath
    url = "http://$appAddress"
    mock = ($env:X_OPERATOR_MOCK -eq '1')
}
if (Get-Command git -ErrorAction SilentlyContinue) {
    $record.branch = git branch --show-current
    $record.commit = git rev-parse HEAD
    $record.local_changes = @(git -c core.quotepath=false status --short)
}
$record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $runtimePath -Encoding UTF8
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $server.Refresh()
    if ($server.HasExited) { throw 'Service exited. See data\local-server.stderr.log.' }
    $newListener = @(Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue)
    foreach ($owner in ($newListener.OwningProcess | Select-Object -Unique)) {
        $serving = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
        if ($null -ne $serving -and ($serving.ProcessId -eq $server.Id -or $serving.ParentProcessId -eq $server.Id) -and $serving.CommandLine -match $modulePattern) {
            Write-Host "[x-operator] Ready: http://$appAddress"
            Write-Host '[x-operator] Refresh your browser. You can close this window.'
            exit 0
        }
    }
    Start-Sleep -Milliseconds 500
}
throw 'Startup is taking longer than expected. See data\local-server.stderr.log.'
