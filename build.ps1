<#
.SYNOPSIS
    Build EazyPassEWT portable distribution
.DESCRIPTION
    1. Check/install PyInstaller
    2. Clean old artifacts
    3. Package as exe
    4. Copy Chromium browser
    5. Clean temp files
.NOTES
    Author: teea418
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $ProjectRoot "dist"
$AppName = "EazyPassEWT"
$ChromeSource = "$env:LOCALAPPDATA\ms-playwright\chromium-1228\chrome-win64"

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)  { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg){ Write-Host "  [!] $msg" -ForegroundColor Yellow }

Write-Step "Check PyInstaller"
$havePyInstaller = pip show pyinstaller 2>$null
if (-not $havePyInstaller) {
    Write-Warn "PyInstaller not found, installing..."
    pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller install failed" }
    Write-OK "PyInstaller installed"
} else {
    Write-OK "PyInstaller ready"
}

Write-Step "Clean old artifacts"
foreach ($dir in @("dist", "build", "__pycache__")) {
    $p = Join-Path $ProjectRoot $dir
    if (Test-Path $p) { Remove-Item -Recurse -Force $p; Write-OK "Deleted: $dir" }
}
Get-ChildItem $ProjectRoot -Filter "*.spec" | Remove-Item -Force
Write-OK "Clean done"

Write-Step "PyInstaller packaging"
Set-Location $ProjectRoot
pyinstaller --onedir --name "$AppName" --add-data ".env.example;." --console main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller packaging failed" }
Write-OK "Package done"

Write-Step "Copy Chromium browser"
$ChromeTarget = Join-Path (Join-Path $DistDir $AppName) "chrome-win64"
if (-not (Test-Path $ChromeSource)) {
    Write-Warn "Local Chromium not found ($ChromeSource), skip browser copy"
} else {
    if (Test-Path $ChromeTarget) { Remove-Item -Recurse -Force $ChromeTarget }
    robocopy $ChromeSource $ChromeTarget /E /NJH /NFL /NDL /NP
    if ($LASTEXITCODE -ge 8) { throw "Chromium copy failed" }
    Write-OK "Chromium ready"
}

Write-Step "Clean temp files"
if (Test-Path (Join-Path $ProjectRoot "build")) { Remove-Item -Recurse -Force (Join-Path $ProjectRoot "build") }
Get-ChildItem $ProjectRoot -Filter "*.spec" | Remove-Item -Force
Write-OK "Clean done"

Write-Step "Build complete"
$finalDir = Join-Path $DistDir $AppName
$size = (Get-ChildItem $finalDir -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host "Output: $finalDir" -ForegroundColor Green
Write-Host "Size:   $([math]::Round($size/1MB, 1)) MB" -ForegroundColor Green
Get-ChildItem $finalDir -Depth 0 | Select-Object Mode, @{N="SizeMB";E={
    if ($_.PSIsContainer) {
        $s = (Get-ChildItem $_.FullName -Recurse | Measure-Object -Property Length -Sum).Sum; [math]::Round($s/1MB, 1)
    } else { [math]::Round($_.Length/1MB, 1) }
}}, Name | Format-Table -AutoSize
Write-Host "Copy $finalDir folder to any Windows PC and run." -ForegroundColor Green
