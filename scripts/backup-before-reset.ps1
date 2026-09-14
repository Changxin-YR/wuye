# 演示库重置前的强制备份（captain 裁决第 3 条，方案 B：mysqldump）
#
#   .\scripts\backup-before-reset.ps1
#
# 产物：artifacts\platform\legacy_dump_before_reset.sql
# 说明：mysqldump 不在 PATH 时自动到 MySQL 安装目录找；找到就用 mysqldump，
#       找不到则退化为 Python 导出（scripts\backup_db.py，同样可回放）。

param(
    [string]$OutDir = 'artifacts\platform',
    [string]$OutFile = 'legacy_dump_before_reset.sql'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

# 从 .env 读连接信息（唯一配置入口，不硬编码）
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { Write-Host ".env 不存在，无法备份。" -ForegroundColor Red; exit 1 }
$url = $null
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    if ($line -match '^\s*DATABASE_URL\s*=\s*(.+)$') { $url = $Matches[1].Trim(); break }
}
if (-not $url) { Write-Host ".env 里没有 DATABASE_URL。" -ForegroundColor Red; exit 1 }

# mysql+pymysql://user:pass@host:port/db?charset=xxx
$m = [regex]::Match($url, '^mysql\+\w+://(?<user>[^:]+):(?<pass>[^@]*)@(?<host>[^:/]+):?(?<port>\d+)?/(?<db>[^?]+)')
if (-not $m.Success) {
    Write-Host "DATABASE_URL 不是 MySQL 形式，改用 Python 导出。" -ForegroundColor Yellow
    & $py (Join-Path $root 'scripts\backup_db.py') --note "reset 前备份（非 MySQL URL）"
    exit $LASTEXITCODE
}
$user = $m.Groups['user'].Value
$pass = $m.Groups['pass'].Value
$host_ = $m.Groups['host'].Value
$port = if ($m.Groups['port'].Success) { $m.Groups['port'].Value } else { '3306' }
$db = $m.Groups['db'].Value

$targetDir = Join-Path $root $OutDir
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
$target = Join-Path $targetDir $OutFile
if (Test-Path $target) { Remove-Item $target -Force }

Write-Host "[1/2] 查找 mysqldump ..." -ForegroundColor Cyan
$dump = (Get-Command mysqldump -ErrorAction SilentlyContinue).Source
if (-not $dump) {
    $dump = Get-ChildItem 'C:\Program Files\MySQL','C:\Program Files (x86)\MySQL' -Recurse -Filter 'mysqldump.exe' -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
}
if (-not $dump) {
    Write-Host "找不到 mysqldump，改用 Python 导出（scripts\backup_db.py）。" -ForegroundColor Yellow
    & $py (Join-Path $root 'scripts\backup_db.py') --note "reset 前备份（无 mysqldump）"
    exit $LASTEXITCODE
}
Write-Host "      使用：$dump"

Write-Host "[2/2] 备份 $db 到 $OutDir\$OutFile ..." -ForegroundColor Cyan
# 注意：$args 是 PowerShell 自动变量，不能赋值，这里用 $dumpArgs
$dumpArgs = @(
    "--user=$user", "--password=$pass", "--host=$host_", "--port=$port",
    '--single-transaction', '--default-character-set=utf8mb4', '--routines', '--events', '--databases', $db
)
& $dump @dumpArgs > $target 2> (Join-Path $targetDir 'mysqldump.err')

if (-not (Test-Path $target) -or (Get-Item $target).Length -lt 1000) {
    Write-Host "备份失败或文件过小，请看 $OutDir\mysqldump.err。**不要执行 reset**。" -ForegroundColor Red
    Get-Content (Join-Path $targetDir 'mysqldump.err') -Tail 8 -ErrorAction SilentlyContinue
    exit 1
}
$size = [math]::Round((Get-Item $target).Length / 1KB, 1)
$tables = (Select-String -Path $target -Pattern '^CREATE TABLE' -ErrorAction SilentlyContinue).Count
Write-Host "[OK] 备份成功：$OutDir\$OutFile（${size}KB，CREATE TABLE $tables 张）" -ForegroundColor Green
Write-Host "     现在可以安全执行： .venv\Scripts\python.exe seed_demo.py reset --seed"
