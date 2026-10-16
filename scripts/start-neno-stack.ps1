$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptsDir = Join-Path $ProjectRoot "scripts"
$LogsDir = Join-Path $ProjectRoot "logs"
$BootLog = Join-Path $LogsDir "startup.log"
$Shell = (Get-Process -Id $PID).Path

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Write-StartupLog {
    param([string]$Message)
    Add-Content -LiteralPath $BootLog -Encoding utf8 -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message)
}

function Invoke-StartScript {
    param([string]$Name)

    $scriptPath = Join-Path $ScriptsDir $Name
    if (!(Test-Path -LiteralPath $scriptPath)) {
        throw "Missing startup script: $scriptPath"
    }

    $startArgs = @{
        FilePath = $Shell
        ArgumentList = @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $scriptPath)
        WorkingDirectory = $ProjectRoot
        WindowStyle = "Hidden"
        PassThru = $true
    }
    $process = Start-Process @startArgs
    $process.WaitForExit()

    if ($process.ExitCode -ne 0) {
        throw "$Name exited with code $($process.ExitCode)"
    }
}

try {
    Write-StartupLog "Starting Neno stack"
    Invoke-StartScript "start-neno-backend.ps1"

    $healthy = $false
    $deadline = (Get-Date).AddSeconds(45)
    do {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 | Out-Null
            $healthy = $true
        } catch {
            Start-Sleep -Milliseconds 750
        }
    } while (!$healthy -and (Get-Date) -lt $deadline)

    if (!$healthy) {
        throw "Neno Core did not become healthy within 45 seconds"
    }

    Invoke-StartScript "start-telegram-gateway.ps1"
    Start-Sleep -Seconds 2

    $gateway = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -eq "python.exe" -and
            $_.CommandLine -match "(?:^|\s)-m\s+telegram_gateway(?:\s|$)" -and
            $_.CommandLine -notmatch "--login"
        } |
        Select-Object -First 1

    if (!$gateway) {
        throw "Telegram Gateway process was not found after startup"
    }

    Write-StartupLog "Neno stack ready; gateway pid=$($gateway.ProcessId)"
} catch {
    Write-StartupLog "Startup failed: $($_.Exception.Message)"
    throw
}
