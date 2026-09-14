# 重建前端静态预览站点（含静态资源），供浏览器/截图核对与布局体检使用。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$prev = "artifacts\frontend-preview"
if (Test-Path "$prev\static") { Remove-Item "$prev\static" -Recurse -Force }
& .\.venv\Scripts\python.exe tests\build_preview_site.py
Copy-Item -Path "static" -Destination "$prev\static" -Recurse -Force
Write-Host "preview ready: $prev"