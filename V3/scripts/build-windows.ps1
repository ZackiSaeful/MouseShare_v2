param([string]$CertificateThumbprint = $env:WINDOWS_CODESIGN_CERT_THUMBPRINT)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Build = Join-Path $Root ".build"
$Venv = Join-Path $Build "venv-windows"
$PythonExe = Join-Path $Venv "Scripts\python.exe"
$Version = "3.0.0"
New-Item -ItemType Directory -Force -Path (Join-Path $Build "spec"), (Join-Path $Root "release") | Out-Null

if (-not (Test-Path $PythonExe)) { py -3.12 -m venv $Venv }
& $PythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed ($LASTEXITCODE)." }
& $PythonExe -m pip install -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { throw "Windows dependency installation failed ($LASTEXITCODE)." }
& $PythonExe -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name MousehareV3 --distpath (Join-Path $Root "dist") `
    --workpath (Join-Path $Build "pyinstaller") --specpath (Join-Path $Build "spec") `
    --paths $Root --collect-all cryptography --collect-all pynput (Join-Path $Root "launch.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)." }

$Certificate = $null
if ($CertificateThumbprint) {
    $Thumbprint = $CertificateThumbprint.Replace(" ", "")
    $Certificate = Get-ChildItem "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction SilentlyContinue
    if (-not $Certificate -or -not $Certificate.HasPrivateKey) { throw "Signing certificate not found or has no private key." }
    Get-ChildItem (Join-Path $Root "dist\MousehareV3") -Recurse -File -Include "*.exe", "*.dll", "*.pyd" |
        ForEach-Object {
            Set-AuthenticodeSignature -FilePath $_.FullName -Certificate $Certificate -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256 | Out-Null
            if ((Get-AuthenticodeSignature $_.FullName).Status -ne "Valid") { throw "Signing failed: $($_.FullName)" }
        }
}

$IsccPath = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $IsccPath) {
    $Candidates = @()
    $ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($ProgramFilesX86) { $Candidates += Join-Path $ProgramFilesX86 "Inno Setup 6\ISCC.exe" }
    if ($env:ProgramFiles) { $Candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }
    $IsccPath = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $IsccPath) { throw "Install Inno Setup 6; ISCC.exe was not found." }

& $IsccPath "/DAppVersion=$Version" (Join-Path $Root "packaging\windows\MousehareV3.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)." }

$Installer = Join-Path $Root "release\MousehareV3-Setup-$Version.exe"
if ($Certificate) {
    Set-AuthenticodeSignature -FilePath $Installer -Certificate $Certificate -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256 | Out-Null
    if ((Get-AuthenticodeSignature $Installer).Status -ne "Valid") { throw "Installer signing failed." }
    Write-Host "Built and signed $Installer"
    Get-AuthenticodeSignature $Installer | Format-List Status, StatusMessage, SignerCertificate
} else {
    Write-Warning "Built unsigned installer $Installer. Smart App Control may block it; use a trusted publisher certificate for distribution."
}
