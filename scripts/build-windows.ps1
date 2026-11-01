param(
    [string]$CertificateThumbprint = $env:WINDOWS_CODESIGN_CERT_THUMBPRINT,
    [switch]$DevelopmentCertificate
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".build\venv-windows"
$PythonExe = Join-Path $Venv "Scripts\python.exe"
$Version = "1.0.0"

New-Item -ItemType Directory -Force -Path (Join-Path $Root ".build"),
    (Join-Path $Root ".build\spec"), (Join-Path $Root "release") | Out-Null

if (-not (Test-Path $PythonExe)) {
    if ($env:PYTHON_BIN) { & $env:PYTHON_BIN -m venv $Venv }
    else { py -3.12 -m venv $Venv }
}

& $PythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed ($LASTEXITCODE)." }
& $PythonExe -m pip install -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { throw "Windows dependency installation failed ($LASTEXITCODE)." }
& $PythonExe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Windows test suite failed ($LASTEXITCODE)." }
& $PythonExe scripts/make_icons.py --platform windows
if ($LASTEXITCODE -ne 0) { throw "Windows icon generation failed ($LASTEXITCODE)." }
& $PythonExe -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name Mousehare --icon (Join-Path $Root ".build\icons\mouseshare.ico") `
    --specpath (Join-Path $Root ".build\spec") --paths $Root `
    --collect-all cryptography mouseshare_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)." }

$Certificate = $null
if ($CertificateThumbprint) {
    $cleanThumbprint = $CertificateThumbprint.Replace(" ", "")
    $Certificate = Get-ChildItem "Cert:\CurrentUser\My\$cleanThumbprint" -ErrorAction SilentlyContinue
    if (-not $Certificate -or -not $Certificate.HasPrivateKey) {
        throw "Code-signing certificate '$CertificateThumbprint' was not found in Cert:\CurrentUser\My or has no private key."
    }
    Write-Host "Signing with the supplied certificate: $($Certificate.Subject)"
} elseif ($DevelopmentCertificate) {
    $subject = "CN=Mousehare Development, O=Mousehare"
    $Certificate = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object { $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date) } |
        Select-Object -First 1
    if (-not $Certificate) {
        $Certificate = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
            -FriendlyName "Mousehare Development" -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(1) -KeyUsage DigitalSignature `
            -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256
    }
    Write-Warning "Development certificate selected. It is valid only for local testing and will not satisfy Smart App Control on another PC."
} else {
    Write-Warning "Building an unsigned installer. Supply -CertificateThumbprint for a distributable release, or -DevelopmentCertificate for local testing."
}

function Sign-MousehareFile([string]$Path) {
    if (-not $Certificate) { return }
    try {
        Set-AuthenticodeSignature -FilePath $Path -Certificate $Certificate `
            -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256 | Out-Null
    } catch {
        Set-AuthenticodeSignature -FilePath $Path -Certificate $Certificate `
            -HashAlgorithm SHA256 | Out-Null
    }
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($signature.Status -ne "Valid") {
        throw "Signing failed for '$Path': $($signature.StatusMessage)"
    }
}

$DistDir = Join-Path $Root "dist\Mousehare"
Get-ChildItem $DistDir -Recurse -File -Include "*.exe", "*.dll", "*.pyd" |
    ForEach-Object { Sign-MousehareFile $_.FullName }

$IsccPath = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $IsccPath) {
    $candidates = @()
    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($programFilesX86) {
        $candidates += Join-Path $programFilesX86 "Inno Setup 6\ISCC.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
    }
    $candidates = $candidates | Where-Object { Test-Path $_ }
    $IsccPath = $candidates | Select-Object -First 1
}
if (-not $IsccPath) {
    throw "Inno Setup 6 (ISCC.exe) is required. Install it, then run this script again."
}

& $IsccPath "/DAppVersion=$Version" "packaging\windows\Mousehare.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)." }
$Installer = Join-Path $Root "release\Mousehare-Setup-$Version.exe"
Sign-MousehareFile $Installer

Write-Host "Built $Installer"
if ($Certificate) {
    Get-AuthenticodeSignature $Installer | Format-List Status, StatusMessage, SignerCertificate
} else {
    Write-Warning "The installer is unsigned and Smart App Control may block it."
}
