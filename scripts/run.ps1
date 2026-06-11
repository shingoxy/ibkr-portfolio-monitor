# IBKR 持仓分析工具 - 启动脚本
param(
    [int]$Port = 8765,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " IBKR 持仓分析工具" -ForegroundColor Cyan
Write-Host " 模式: 只读" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan

# 检查 .env
if (-not (Test-Path "$projectDir\.env")) {
    Write-Host "警告: 未找到 .env 文件，使用默认配置" -ForegroundColor Yellow
}

# 启动
$url = "http://127.0.0.1:$Port"
Write-Host "启动地址: $url" -ForegroundColor White

if ($OpenBrowser) {
    Start-Process $url
}

python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --log-level info
