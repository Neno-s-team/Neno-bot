# Neno 后端 · 关闭 + 启动（nereboot）
# 停掉占用 :8000 的旧实例，再隐藏后台起一个新的。改了 .env / prompt / app 代码后跑这个生效。
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$OutLog = Join-Path $ProjectRoot "logs\uvicorn-autostart.out.log"
$ErrLog = Join-Path $ProjectRoot "logs\uvicorn-autostart.err.log"

if (!(Test-Path -LiteralPath $Python)) {
    throw "找不到 venv python：$Python"
}

# --- 关闭：杀掉监听 8000 的进程（不限绑定地址，127.0.0.1 / 0.0.0.0 都抓）---
$pids = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "已停止旧实例 pid=$procId"
    } catch {
        Write-Host "停止 pid=$procId 失败（可能已退出）"
    }
}
if ($pids) { Start-Sleep -Milliseconds 800 }  # 等端口释放

# --- 启动：隐藏窗口、后台、日志重定向（旧日志先轮转，防止覆盖丢证据）---
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($log in @($OutLog, $ErrLog)) {
    if (Test-Path -LiteralPath $log) {
        Move-Item -LiteralPath $log -Destination "$log.$stamp.bak" -Force
    }
}
Start-Process -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog

Write-Host "Neno 后端已在 http://127.0.0.1:8000 重启（日志：logs\uvicorn-autostart.*.log）"
