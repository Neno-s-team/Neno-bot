param(
    [string]$SourceDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "third_party\tdlib-current"),
    [string]$BuildDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "third_party\tdlib-build-current"),
    [string]$RuntimeDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "runtime\tdlib"),
    [string]$VcpkgDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "third_party\vcpkg"),
    [string]$Tag = "master",
    [switch]$SkipClone
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required to fetch TDLib"
}
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    throw "cmake is required to build TDLib"
}
if (-not (Test-Path -LiteralPath (Join-Path $VcpkgDir "vcpkg.exe"))) {
    throw "vcpkg is required to provide TDLib build dependencies: $VcpkgDir"
}

$toolchain = Join-Path $VcpkgDir "scripts\buildsystems\vcpkg.cmake"
$gperf = Join-Path $VcpkgDir "installed\x64-windows\tools\gperf\gperf.exe"
if (-not (Test-Path -LiteralPath $gperf)) {
    throw "gperf is missing; install gperf:x64-windows with vcpkg"
}

if (-not (Test-Path -LiteralPath $SourceDir)) {
    if ($SkipClone) {
        throw "TDLib source does not exist: $SourceDir"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SourceDir) | Out-Null
    git clone --depth 1 --branch $Tag https://github.com/tdlib/td.git $SourceDir
    if ($LASTEXITCODE -ne 0) {
        throw "TDLib clone failed"
    }
}

cmake --fresh -S $SourceDir -B $BuildDir -G "Visual Studio 17 2022" -A x64 `
    -DCMAKE_BUILD_TYPE=Release `
    -DTD_ENABLE_LTO=OFF `
    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5" `
    "-DCMAKE_TOOLCHAIN_FILE=$toolchain" `
    "-DGPERF_EXECUTABLE=$gperf"
if ($LASTEXITCODE -ne 0) {
    throw "TDLib configure failed"
}

cmake --build $BuildDir --config Release --target tdjson --parallel
if ($LASTEXITCODE -ne 0) {
    throw "TDLib build failed"
}

$candidates = @(
    (Join-Path $BuildDir "tdjson\Release\tdjson.dll"),
    (Join-Path $BuildDir "Release\tdjson.dll"),
    (Join-Path $BuildDir "bin\Release\tdjson.dll")
)
$tdjson = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $tdjson) {
    throw "tdjson.dll was not found under $BuildDir"
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
Copy-Item -LiteralPath $tdjson -Destination (Join-Path $RuntimeDir "tdjson.dll") -Force

$runtimeDependencies = Get-ChildItem -LiteralPath (Split-Path -Parent $tdjson) -Filter "*.dll" -File -ErrorAction SilentlyContinue
foreach ($dependency in $runtimeDependencies) {
    if ($dependency.Name -ne "tdjson.dll") {
        Copy-Item -LiteralPath $dependency.FullName -Destination (Join-Path $RuntimeDir $dependency.Name) -Force
    }
}

$vcpkgRuntime = Join-Path $VcpkgDir "installed\x64-windows\bin"
Get-ChildItem -LiteralPath $vcpkgRuntime -Filter "*.dll" -File -ErrorAction SilentlyContinue |
    Copy-Item -Destination $RuntimeDir -Force

Write-Output (Join-Path $RuntimeDir "tdjson.dll")

