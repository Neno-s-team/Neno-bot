# Opens Neno's own Chrome profile in a VISIBLE window, for the human to register / log in by hand
# (mailbox, X account, captchas). The login state stays in data\browser\neno; her background
# browsing (agent-browser on a hidden desktop) uses the same profile afterwards.
#
# While this window is open, data\browser\neno.manual exists and the backend will not start her
# background browser (two Chromes cannot share one profile).
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\open-neno-browser.ps1 [url]

param([string]$Url = "about:blank")

$root = Split-Path $PSScriptRoot -Parent
$dir = Join-Path $root "data\browser\neno"
$lock = Join-Path $root "data\browser\neno.manual"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Write-Host "Chrome not found."; exit 1 }

# Her background browser (if any) must let go of the profile first.
$ab = Join-Path $root "data\tools\agent-browser\node_modules\agent-browser\bin\agent-browser-win32-x64.exe"
if (Test-Path $ab) { & $ab --session neno close 2>$null | Out-Null }
$busy = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -like "*data\browser\neno*" -and $_.CommandLine -notlike "*neno-*" }
if ($busy) { Write-Host "Her Chrome profile is still in use (pid $($busy[0].ProcessId)). Try again in a minute."; exit 1 }

Set-Content -Path $lock -Value (Get-Date -Format o)
try {
    Write-Host "Neno's browser is open. Close the window when you are done."
    $p = Start-Process -FilePath $chrome -PassThru -ArgumentList @(
        "--user-data-dir=`"$dir`"", "--no-first-run", "--no-default-browser-check", $Url)
    $p.WaitForExit()
} finally {
    Remove-Item -Path $lock -ErrorAction SilentlyContinue
    Write-Host "Closed. Her login state is saved in data\browser\neno."
}
