$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".build\venv-windows"
$PythonExe = Join-Path $Venv "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path (Join-Path $Root ".build"), (Join-Path $Root "release") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root ".build\spec") | Out-Null
if (-not (Test-Path $PythonExe)) {
    if ($env:PYTHON_BIN) { & $env:PYTHON_BIN -m venv $Venv } else { py -3.12 -m venv $Venv }
}
& $PythonExe -m pip install -r requirements-windows.txt
& $PythonExe scripts/make_icons.py --platform windows
& $PythonExe -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name Mousehare --icon (Join-Path $Root ".build\icons\mouseshare.ico") --specpath (Join-Path $Root ".build\spec") `
    --paths $Root --collect-all pystray --collect-all cryptography mouseshare_app.py

$IsccPath = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $IsccPath) {
    $InnoCandidates = @()
    $ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($ProgramFilesX86) { $InnoCandidates += (Join-Path $ProgramFilesX86 "Inno Setup 6\ISCC.exe") }
    if ($env:ProgramFiles) { $InnoCandidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe") }
    foreach ($Candidate in $InnoCandidates) {
        if (Test-Path $Candidate) { $IsccPath = $Candidate; break }
    }
}
if (-not $IsccPath) {
    throw "Inno Setup 6 compiler (ISCC.exe) is required. Install Inno Setup 6, then rerun this script."
}
& $IsccPath "/DAppVersion=1.0.0" "packaging\windows\Mousehare.iss"
Write-Host "Built $Root\release\Mousehare-Setup-1.0.0.exe"
