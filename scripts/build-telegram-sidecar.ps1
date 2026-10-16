param(
    [string]$SourceDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "telegram_sidecar"),
    [string]$BuildDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "telegram_sidecar\build"),
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    throw "cmake is required to build the TDLib sidecar"
}

$generator = "Visual Studio 17 2022"
cmake -S $SourceDir -B $BuildDir -G $generator -A x64
if ($LASTEXITCODE -ne 0) {
    throw "cmake configure failed"
}

cmake --build $BuildDir --config $Configuration --target neno-tdlib-sidecar --parallel
if ($LASTEXITCODE -ne 0) {
    throw "cmake build failed"
}

$binary = Join-Path $BuildDir "$Configuration\neno-tdlib-sidecar.exe"
if (-not (Test-Path -LiteralPath $binary)) {
    throw "sidecar binary was not produced: $binary"
}

Write-Output $binary
