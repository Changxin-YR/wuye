# 本地起一个生产口径的服务（Waitress + Secure Cookie）。
#
# 注意：production 配置下 cookie 是 Secure 的，http://127.0.0.1 直接打开会登不上；
#       本地演示请用 `python app.py`（脚本 .\scripts\start-demo.ps1 -NoTunnel），
#       需要 https 时用 .\scripts\start-demo.ps1 起 cloudflared 隧道。
#
#   .\scripts\start-waitress.ps1              # Waitress，默认 5000 端口
#   .\scripts\start-waitress.ps1 -Port 5001 -Threads 8

param(
    [int]$Port = 5000,
    [int]$Threads = 8
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$env:PORT = $Port
$env:HOST = '127.0.0.1'
$env:WAITRESS_THREADS = $Threads

Write-Host ("以生产口径启动 Waitress：http://127.0.0.1:" + $Port + "（Secure Cookie，需 https 才能登录）") -ForegroundColor Cyan
& $python (Join-Path $root 'serve.py')
