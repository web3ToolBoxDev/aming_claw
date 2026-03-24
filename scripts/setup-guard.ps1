<#
.SYNOPSIS
    将 amingclaw 注册为 Windows 服务（使用 NSSM）。

.DESCRIPTION
    通过 NSSM（Non-Sucking Service Manager）把 agent/service_manager.py
    注册为 Windows 服务，服务名 "amingclaw"，开机自动启动，崩溃后自动重启。

.NOTES
    前提条件（运行本脚本前请确认）：
      1. 管理员权限  —— 右键 PowerShell → "以管理员身份运行"，或在已提权的终端中执行。
      2. NSSM 已安装 —— 从 https://nssm.cc/download 下载，将 nssm.exe 放入 PATH，
                        或通过 Chocolatey：  choco install nssm
                        或通过 Scoop：       scoop install nssm
      3. Python 环境  —— 项目内嵌 Python（runtime\python\python.exe）或系统 Python 3.x。
      4. .env 文件    —— 项目根目录须存在 .env（可从 .env.example 复制）。

    兼容 PowerShell 5.1+
#>

#Requires -Version 5.1

$ErrorActionPreference = "Stop"

# ── 常量 ────────────────────────────────────────────────────────────────
$ServiceName  = "amingclaw"
$ScriptRoot   = $PSScriptRoot                              # scripts/
$ProjectRoot  = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
$EntryScript  = Join-Path $ProjectRoot "agent\service_manager.py"

# ── 1. 管理员权限检测 ────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Warning "请以管理员身份运行本脚本。"
    Write-Warning "右键 PowerShell → '以管理员身份运行'，然后重新执行此脚本。"
    exit 1
}

# ── 2. NSSM 可用性检测 ───────────────────────────────────────────────────
$nssmCmd = Get-Command "nssm" -ErrorAction SilentlyContinue
if (-not $nssmCmd) {
    Write-Host ""
    Write-Host "=======================================================" -ForegroundColor Yellow
    Write-Host "  NSSM 未找到，请先安装 NSSM 再运行本脚本。" -ForegroundColor Yellow
    Write-Host "=======================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "安装方式（任选其一）：" -ForegroundColor Cyan
    Write-Host "  A) Chocolatey（推荐）："
    Write-Host "       choco install nssm"
    Write-Host ""
    Write-Host "  B) Scoop："
    Write-Host "       scoop install nssm"
    Write-Host ""
    Write-Host "  C) 手动安装："
    Write-Host "       1. 访问 https://nssm.cc/download"
    Write-Host "       2. 下载对应位数的 nssm.exe（64-bit 推荐）"
    Write-Host "       3. 将 nssm.exe 复制到 C:\Windows\System32\ 或任意 PATH 目录"
    Write-Host ""
    Write-Host "安装完成后，重新以管理员身份运行本脚本。" -ForegroundColor Green
    exit 1
}

$nssmPath = $nssmCmd.Source
Write-Host "NSSM 已找到：$nssmPath" -ForegroundColor Green

# ── 3. Python 入口检测 ───────────────────────────────────────────────────
$pythonExe = & (Join-Path $ScriptRoot "_get_python.ps1")
Write-Host "使用 Python：$pythonExe" -ForegroundColor Green

if (-not (Test-Path $EntryScript)) {
    Write-Error "未找到入口脚本：$EntryScript"
    exit 1
}
Write-Host "入口脚本：$EntryScript" -ForegroundColor Green

# ── 4. 停止并移除已有服务（幂等）────────────────────────────────────────
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "检测到已有服务 '$ServiceName'，先停止并移除..." -ForegroundColor Yellow
    if ($existing.Status -eq "Running") {
        & $nssmPath stop $ServiceName confirm | Out-Null
        Start-Sleep -Seconds 2
    }
    & $nssmPath remove $ServiceName confirm | Out-Null
    Write-Host "已移除旧服务。" -ForegroundColor Yellow
}

# ── 5. 注册服务 ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "正在注册服务 '$ServiceName' ..." -ForegroundColor Cyan

& $nssmPath install $ServiceName $pythonExe $EntryScript
if ($LASTEXITCODE -ne 0) {
    Write-Error "NSSM install 失败（exit code $LASTEXITCODE）"
    exit 1
}

# 工作目录设为项目根
& $nssmPath set $ServiceName AppDirectory $ProjectRoot | Out-Null

# 标准输出 / 错误日志
$LogDir = Join-Path $ProjectRoot "shared-volume\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
& $nssmPath set $ServiceName AppStdout (Join-Path $LogDir "amingclaw-svc-stdout.log") | Out-Null
& $nssmPath set $ServiceName AppStderr (Join-Path $LogDir "amingclaw-svc-stderr.log") | Out-Null
& $nssmPath set $ServiceName AppRotateFiles 1       | Out-Null
& $nssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null  # 10 MB

# 启动类型：Automatic
& $nssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null

# 崩溃自动重启（重启延迟 5 秒，最多不限次数）
& $nssmPath set $ServiceName AppThrottle 5000 | Out-Null
& $nssmPath set $ServiceName AppRestartDelay 5000 | Out-Null

# 服务显示名称与描述
& $nssmPath set $ServiceName DisplayName "amingClaw Agent" | Out-Null
& $nssmPath set $ServiceName Description "amingClaw Telegram-bot AI task agent (managed by NSSM)" | Out-Null

Write-Host "服务注册完成。" -ForegroundColor Green

# ── 6. 启动服务 ──────────────────────────────────────────────────────────
Write-Host "正在启动服务 '$ServiceName' ..." -ForegroundColor Cyan
& $nssmPath start $ServiceName
$startCode = $LASTEXITCODE

# ── 7. 输出注册状态摘要 ──────────────────────────────────────────────────
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  服务注册状态摘要" -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "  服务名称   : $($svc.Name)"
    Write-Host "  显示名称   : $($svc.DisplayName)"
    Write-Host "  当前状态   : $($svc.Status)"
    Write-Host "  启动类型   : Automatic（开机自动）"
    Write-Host "  崩溃重启   : 已启用（延迟 5 秒）"
    Write-Host "  Python 路径: $pythonExe"
    Write-Host "  入口脚本   : $EntryScript"
    Write-Host "  工作目录   : $ProjectRoot"
    Write-Host "  日志目录   : $LogDir"
    if ($svc.Status -eq "Running") {
        Write-Host ""
        Write-Host "  [OK] 服务已成功启动并运行中。" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "  [WARN] 服务已注册但未处于 Running 状态（$($svc.Status)）。" -ForegroundColor Yellow
        Write-Host "  请检查日志：$LogDir" -ForegroundColor Yellow
        Write-Host "  手动启动：  nssm start $ServiceName" -ForegroundColor Yellow
    }
} else {
    Write-Host "  [ERROR] 无法查询服务状态，请手动检查。" -ForegroundColor Red
}
Write-Host "=======================================================" -ForegroundColor Cyan
