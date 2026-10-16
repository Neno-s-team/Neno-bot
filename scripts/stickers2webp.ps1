# Transcode a sticker pack to Telegram-sendable static WebP stickers.
#
# Grounded in TDLib (third_party/tdlib-current):
#   - static stickers upload as image/webp, hard cap 512 KiB (StickerFormat.cpp:156)
#   - the send path guesses format from the extension and falls back to WebP
#     (StickersManager.cpp:3386-3393), so real WebP bytes behind a .webp name
#     are the only reliably accepted form
#   - sticker sets enforce 512x512 static frames; here we only cap the longest
#     side at 512 for chat sends (--SetSquare also pads to 512x512 for sets)
#
# Usage:
#   pwsh -File scripts\stickers2webp.ps1 -InputDir <pack> -OutputDir <dst>
#   pwsh -File scripts\stickers2webp.ps1 -InputDir <pack> -OutputDir <dst> -SetSquare

param(
    [Parameter(Mandatory = $true)][string]$InputDir,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [int]$MaxSide = 512,
    [int]$Quality = 82,
    [switch]$SetSquare
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw 'ffmpeg not found in PATH' }

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function Get-RealFormat([byte[]]$b) {
    if ($b.Length -ge 8 -and $b[0] -eq 0x89 -and $b[1] -eq 0x50) { return 'png' }
    if ($b.Length -ge 3 -and $b[0] -eq 0xFF -and $b[1] -eq 0xD8) { return 'jpeg' }
    if ($b.Length -ge 12 -and $b[0] -eq 0x52 -and [Text.Encoding]::ASCII.GetString($b[8..11]) -eq 'WEBP') { return 'webp' }
    return 'unknown'
}

$converted = 0; $copied = 0; $skipped = 0
Get-ChildItem $InputDir -File | ForEach-Object {
    if ($_.Name -eq 'custom_stickers.json') {
        Copy-Item $_.FullName (Join-Path $OutputDir $_.Name) -Force
        return
    }
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    if ($bytes.Length -gt (1024 * 1024 * 20)) { Write-Host "SKIP(huge) $($_.Name)"; $skipped++; return }
    $fmt = Get-RealFormat $bytes
    $target = Join-Path $OutputDir ($_.BaseName + '.webp')

    if ($fmt -eq 'webp' -and $bytes.Length -le 524288) {
        Copy-Item $_.FullName $target -Force
        Write-Host "COPY        $($_.Name)"
        $copied++
        return
    }

    $scale = if ($SetSquare) {
        "scale='min($MaxSide,iw)':'min($MaxSide,ih)':force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
    } else {
        "scale='min($MaxSide,iw)':'min($MaxSide,ih)':force_original_aspect_ratio=decrease"
    }

    & $ffmpeg -hide_banner -loglevel error -y -i $_.FullName `
        -vf "$scale,format=rgba" -frames:v 1 `
        -c:v libwebp -quality $Quality -compression_level 6 -lossless 0 `
        $target
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $target)) {
        Write-Host "FAIL        $($_.Name)"
        $skipped++
        return
    }
    $newSize = (Get-Item $target).Length
    if ($newSize -gt 524288) {
        # squeeze quality until under the TDLib cap
        & $ffmpeg -hide_banner -loglevel error -y -i $target -c:v libwebp -quality 60 -compression_level 6 $target
        $newSize = (Get-Item $target).Length
    }
    Write-Host ("CONVERT     {0,-45} {1,-4} -> webp  {2,7:N0}B" -f $_.Name, $fmt, $newSize)
    $converted++
}

Write-Host ""
Write-Host "converted: $converted  copied: $copied  skipped: $skipped"
Write-Host "output:    $OutputDir"
