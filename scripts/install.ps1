# IBKR 持仓分析工具 - 安装脚本
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir

Write-Host "安装依赖..." -ForegroundColor Cyan
pip install -r "$projectDir\requirements.txt"

Write-Host "安装完成" -ForegroundColor Green
