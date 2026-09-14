# 演示环境一键启动
#
#   .\scripts\start-demo.ps1                 # 本地 5000 + cloudflared 公网 https 隧道
#   .\scripts\start-demo.ps1 -NoTunnel       # 只起本地服务（http，可直接登录）
#   .\scripts\start-demo.ps1 -Port 5001      # 换端口
#
# 启动方式（重要）：
#   - 无隧道：用 `python app.py`（本地入口，回环地址上显式关闭 Secure Cookie），
#     http://127.0.0.1:<port> 可以直接用演示账号登录。
#   - 有隧道：用 `serve.py`（Waitress，Secure Cookie，与线上一致）；cloudflared 隧道会随机
#     分配一个 https 域名；重跑本脚本即可拿到新的公网链接。
#     注意：serve.py 在 production 下强制 Secure Cookie，直接 http 连本地端口是登不上的。

param(
    [int]$Port = 5000,
    [switch]$NoTunnel
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$logDir = Join-Path $root 'demo-logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 演示开关只在这里注入：不在 .env 里，避免影响测试与其它运行方式
$env:APP_DEMO_MODE = '1'
$env:PORT = "$Port"
$env:WAITRESS_THREADS = '8'

Write-Host "[1/3] 检查端口 $Port ..." -ForegroundColor Cyan
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "端口 $Port 已被占用（PID $($busy[0].OwningProcess)）。请先停止旧进程，或用 -Port 指定其它端口。" -ForegroundColor Yellow
    exit 1
}

# 先探测 cloudflared：没有隧道就用 app.py（http 可直接登录）
$cloudflared = $null
if (-not $NoTunnel) {
    $candidate = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    if (Test-Path $candidate) { $cloudflared = $candidate }
    else {
        $found = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($found) { $cloudflared = $found.Source }
    }
    if (-not $cloudflared) {
        Write-Host "未找到 cloudflared，退回本地 http 模式。" -ForegroundColor Yellow
    }
}

# 写演示数据（幂等），保证演示账号与 6 个状态的工单都在
Write-Host "[2/3] 写入/刷新演示数据 ..." -ForegroundColor Cyan
& $python (Join-Path $root 'seed_demo.py')
if ($LASTEXITCODE -ne 0) { Write-Host "演示数据写入失败，请先检查 .env 的 DATABASE_URL。" -ForegroundColor Red; exit 1 }

function Start-DemoServer {
    param([string]$Entry)
    $proc = Start-Process -FilePath $python -ArgumentList $Entry -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logDir 'server.out') `
        -RedirectStandardError (Join-Path $logDir 'server.err') -NoNewWindow -PassThru
    $ready = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        if ($proc.HasExited) { break }
        try {
            $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
            if ($health.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) {
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        return $null
    }
    return $proc
}

$entry = if ($cloudflared) { 'serve.py' } else { 'app.py' }
Write-Host "[3/3] 启动服务：$entry ..." -ForegroundColor Cyan
$server = Start-DemoServer -Entry $entry
if (-not $server) {
    if ($entry -eq 'serve.py') {
        Write-Host "serve.py 启动失败（production 强制 Secure Cookie，需要 https 隧道配合）。改用 app.py 起本地 http 服务。" -ForegroundColor Yellow
        $entry = 'app.py'
        $cloudflared = $null
        $server = Start-DemoServer -Entry $entry
    }
    if (-not $server) {
        Write-Host "服务启动失败，请看 $logDir\server.err" -ForegroundColor Red
        exit 1
    }
}
Write-Host "      本地入口 http://127.0.0.1:$Port （/health 正常，运行 $entry）" -ForegroundColor Green

if (-not $cloudflared) {
    Write-Host ''
    Write-Host "  本地演示入口： http://127.0.0.1:$Port" -ForegroundColor Black -BackgroundColor Green
    Write-Host "  演示账号见 README.md 第 5 节（统一口令 Demo-only-292!）"
    Write-Host "  说明：本地模式用 app.py 启动（回环地址已关闭 Secure Cookie），http 可直接登录。"
    Write-Host ''
    Write-Host "  停止演示： Get-NetTCPConnection -LocalPort $Port -State Listen | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }"
    exit 0
}

Write-Host "[4/4] 启动 cloudflared 临时隧道 ..." -ForegroundColor Cyan

$tunnelErr = Join-Path $logDir 'tunnel.err'
Remove-Item $tunnelErr -ErrorAction SilentlyContinue
Start-Process -FilePath $cloudflared `
    -ArgumentList 'tunnel','--url',"http://127.0.0.1:$Port",'--no-autoupdate','--protocol','http2' `
    -RedirectStandardOutput (Join-Path $logDir 'tunnel.out') `
    -RedirectStandardError $tunnelErr -NoNewWindow

$url = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    if (Test-Path $tunnelErr) {
        $match = Select-String -Path $tunnelErr -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($match) { $url = $match.Matches[0].Value; break }
    }
}
if (-not $url) {
    Write-Host "隧道未就绪，请看 $logDir\tunnel.err" -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host "  公网测试链接： $url" -ForegroundColor Black -BackgroundColor Green
Write-Host ''
Write-Host "  账号与演示动线见 README.md 第 5/6 节（统一口令 Demo-only-292!）"
Write-Host "  注意：隧道模式下服务是 serve.py（Secure Cookie），对外请只发上面这条 https 链接。"
Write-Host ''
Write-Host "  停止演示："
Write-Host "    Get-NetTCPConnection -LocalPort $Port -State Listen | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }"
Write-Host "    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force"
