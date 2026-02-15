#requires -Version 5.1
<##
Build a standalone Windows launcher (.exe) for Archive Helper.

Usage (from PowerShell on Windows):
  powershell -ExecutionPolicy Bypass -File .\launchers\build_windows_exe.ps1

The script expects a project virtual environment at .venv.
##>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "Missing $venvPython. Create it first with: py -3 -m venv .venv"
}

Push-Location $repoRoot
try {
    & $venvPython -m pip install --upgrade pip pyinstaller

    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onefile `
        --name 'ArchiveHelper' `
        --paths $repoRoot `
        "$repoRoot\rip_and_encode_gui.py"

    Write-Host ''
    Write-Host 'Build complete.' -ForegroundColor Green
    Write-Host "Executable: $repoRoot\dist\ArchiveHelper.exe"
}
finally {
    Pop-Location
}
