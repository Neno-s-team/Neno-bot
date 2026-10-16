$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$OutLog = Join-Path $ProjectRoot "logs\telegram-gateway.out.log"
$ErrLog = Join-Path $ProjectRoot "logs\telegram-gateway.err.log"

if (!(Test-Path -LiteralPath $Python)) {
    throw "Missing venv python: $Python"
}

$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -match "(?:^|\s)-m\s+telegram_gateway(?:\s|$)" -and
        $_.CommandLine -notmatch "--login"
    }
if ($existing) {
    exit 0
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($log in @($OutLog, $ErrLog)) {
    if (Test-Path -LiteralPath $log) {
        Move-Item -LiteralPath $log -Destination "$log.$stamp.bak" -Force
    }
}
Start-Process -FilePath $Python `
    -ArgumentList @("-m", "telegram_gateway") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog
