param(
    [switch]$Takeover
)

$ErrorActionPreference = "Stop"
$mutex = $null

try {
    $created = $false
    $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_codex_executor", [ref]$created)
    if (-not $mutex.WaitOne(0)) {
        Write-Host "Executor mutex already held; another executor launcher is active. Exit."
        return
    }
}
catch {
    throw
}

Set-Location (Join-Path $PSScriptRoot "..")

function Get-ExecutorPythonProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd = [string]$_.CommandLine
        $name -match '^python(\.exe)?$' -and (
            $cmd -like "*agent\\executor.py*" -or
            $cmd -like "*agent/executor.py*"
        )
    }
}

function Stop-ExecutorByLockPort {
    param(
        [int]$Port = 39101
    )
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listeners) { return }
    $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidVal in $pids) {
        Write-Host "Takeover: stopping lock-port owner PID=$pidVal ..."
        Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
        taskkill /F /T /PID $pidVal | Out-Null
    }
}

if (-not (Test-Path ".\.env")) {
    throw ".env not found. Create it from .env.example first."
}

Write-Host "Loading .env into current shell..."
Get-Content .\.env | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $pair = $_ -split '=', 2
    if ($pair.Length -eq 2) {
        [System.Environment]::SetEnvironmentVariable($pair[0], $pair[1], "Process")
    }
}

if (-not $env:CODEX_BIN) {
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        $env:CODEX_BIN = "codex.cmd"
    } else {
        $env:CODEX_BIN = "codex"
    }
}
$codexBinResolved = Get-Command $env:CODEX_BIN -ErrorAction SilentlyContinue
if (-not $codexBinResolved) {
    Write-Warning "Configured CODEX_BIN not found: $($env:CODEX_BIN). Install Codex CLI and run 'codex login' first, or set CODEX_BIN to a valid command."
    Write-Warning "Executor will start but task execution will fail until Codex CLI is available."
}
Write-Host "Using CODEX_BIN=$($env:CODEX_BIN)"

# 自动探测 CLAUDE_BIN（若 .env 未设置）
if (-not $env:CLAUDE_BIN) {
    $claudeFound = Get-Command claude.cmd -ErrorAction SilentlyContinue
    if (-not $claudeFound) { $claudeFound = Get-Command claude -ErrorAction SilentlyContinue }
    if ($claudeFound) {
        $env:CLAUDE_BIN = $claudeFound.Source
        Write-Host "Auto-detected CLAUDE_BIN=$($env:CLAUDE_BIN)"
    }
}

# 使用内嵌 Python（优先）或系统 Python
$PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
Write-Host "Using Python: $PYTHON"

$existing = @(Get-ExecutorPythonProcesses)
if ($Takeover) {
    $lockPort = 39101
    if ($env:EXECUTOR_SINGLETON_PORT -and ($env:EXECUTOR_SINGLETON_PORT -as [int])) {
        $lockPort = [int]$env:EXECUTOR_SINGLETON_PORT
    }
    Stop-ExecutorByLockPort -Port $lockPort
    Start-Sleep -Milliseconds 500
}
if ($existing.Count -gt 0 -and -not $Takeover) {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
    Write-Host "Executor already running (PID=$pids). Skip starting duplicate instance."
    return
}
if ($existing.Count -gt 0 -and $Takeover) {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId)
    foreach ($id in $pids) {
        Write-Host "Takeover: stopping existing executor PID=$id ..."
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        taskkill /F /T /PID $id | Out-Null
    }
    Start-Sleep -Milliseconds 700
}

if (-not $env:SHARED_VOLUME_PATH) {
    $env:SHARED_VOLUME_PATH = Join-Path (Get-Location).Path "shared-volume"
}
New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null

if (-not $env:CODEX_WORKSPACE) {
    $env:CODEX_WORKSPACE = (Get-Location).Path
}

if (-not $env:CODEX_SEARCH_WORKSPACE) {
    $env:CODEX_SEARCH_WORKSPACE = Join-Path $env:CODEX_WORKSPACE "search-workspace"
}
New-Item -ItemType Directory -Force -Path $env:CODEX_SEARCH_WORKSPACE | Out-Null
Write-Host "Executor search workspace: $env:CODEX_SEARCH_WORKSPACE"

$depsReady = $false
try {
    & $PYTHON -c "import requests" 2>&1 | Out-Null
    $depsReady = ($LASTEXITCODE -eq 0)
} catch { $depsReady = $false }
if (-not $depsReady) {
    Write-Host "Installing agent dependencies..."
    & $PYTHON -m pip install -r .\agent\requirements.txt --no-warn-script-location
} else {
    Write-Host "agent dependencies already satisfied."
}

Write-Host "Starting agent executor..."
try {
    # Start executor in background job so we can verify workspace registry
    $execJob = Start-Job -ScriptBlock {
        param($py, $wd)
        Set-Location $wd
        & $py .\agent\executor.py
    } -ArgumentList $PYTHON, (Get-Location).Path

    # Wait for executor API to start
    Start-Sleep -Seconds 3

    # Verify workspace registry (project_id routing)
    try {
        $wsResp = Invoke-RestMethod -Uri "http://localhost:40100/workspaces" -TimeoutSec 5 -ErrorAction SilentlyContinue
        if ($wsResp.count -gt 0) {
            Write-Host "Workspace registry: $($wsResp.count) workspace(s) registered"
            foreach ($ws in $wsResp.workspaces) {
                $pid = if ($ws.project_id) { $ws.project_id } else { "(none)" }
                Write-Host "  $($ws.label) -> project_id=$pid -> $($ws.path)"
            }
        } else {
            Write-Warning "No workspaces registered. Tasks may route to wrong workspace."
        }
    } catch {
        Write-Host "(workspace check skipped - executor API not yet ready)"
    }

    # Wait for executor to finish
    $execJob | Receive-Job -Wait -AutoRemoveJob
}
finally {
    if ($mutex -ne $null) {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
}
