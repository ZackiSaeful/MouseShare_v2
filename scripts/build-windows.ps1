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

# ══════════════════════════════════════════════════════════════════════════════
#  Self-signed Authenticode code signing
#
#  Creates a local self-signed certificate for code signing if none exists,
#  then signs the main executable and all DLLs/PYDs. This satisfies Windows
#  SmartScreen and Smart App Control when the certificate is trusted on the
#  target machine.
# ══════════════════════════════════════════════════════════════════════════════
$CertSubject = "CN=Mousehare Development, O=Mousehare, L=Local"
$ExistingCert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq $CertSubject -and $_.NotAfter -gt (Get-Date) } |
    Select-Object -First 1

if (-not $ExistingCert) {
    Write-Host "Creating self-signed code signing certificate..."
    $ExistingCert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $CertSubject `
        -FriendlyName "Mousehare Code Signing" `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddYears(3) `
        -KeyUsage DigitalSignature `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -HashAlgorithm SHA256
    # Export and import to Trusted Root and Trusted Publishers so the local
    # machine trusts the signed binaries.
    $CertExportPath = Join-Path $Root ".build\mousehare-codesign.cer"
    Export-Certificate -Cert $ExistingCert -FilePath $CertExportPath | Out-Null
    try {
        Import-Certificate -FilePath $CertExportPath -CertStoreLocation Cert:\CurrentUser\Root -Confirm:$false | Out-Null
        Import-Certificate -FilePath $CertExportPath -CertStoreLocation Cert:\CurrentUser\TrustedPublisher -Confirm:$false | Out-Null
        Write-Host "Certificate created and trusted locally: $($ExistingCert.Thumbprint)"
    } catch {
        Write-Host "WARNING: Could not auto-trust certificate. Manually import $CertExportPath to Trusted Root and Trusted Publishers."
    }
} else {
    Write-Host "Using existing code signing certificate: $($ExistingCert.Thumbprint)"
}

# Sign the main executable and all native libraries
$DistDir = Join-Path $Root "dist\Mousehare"
if (Test-Path $DistDir) {
    $MainExe = Join-Path $DistDir "Mousehare.exe"
    if (Test-Path $MainExe) {
        Write-Host "Signing Mousehare.exe..."
        try {
            Set-AuthenticodeSignature -FilePath $MainExe -Certificate $ExistingCert `
                -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256 | Out-Null
        } catch {
            # Fallback without timestamp server if offline
            Set-AuthenticodeSignature -FilePath $MainExe -Certificate $ExistingCert `
                -HashAlgorithm SHA256 | Out-Null
        }
    }
    # Sign all DLLs and PYD files
    Get-ChildItem $DistDir -Recurse -Include "*.dll","*.pyd" | ForEach-Object {
        Set-AuthenticodeSignature -FilePath $_.FullName -Certificate $ExistingCert `
            -HashAlgorithm SHA256 -ErrorAction SilentlyContinue | Out-Null
    }
    Write-Host "Code signing complete."
}

# ══════════════════════════════════════════════════════════════════════════════
#  Inno Setup installer
# ══════════════════════════════════════════════════════════════════════════════
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

# Sign the installer itself
$InstallerExe = Join-Path $Root "release\Mousehare-Setup-1.0.0.exe"
if (Test-Path $InstallerExe) {
    Write-Host "Signing installer..."
    try {
        Set-AuthenticodeSignature -FilePath $InstallerExe -Certificate $ExistingCert `
            -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256 | Out-Null
    } catch {
        Set-AuthenticodeSignature -FilePath $InstallerExe -Certificate $ExistingCert `
            -HashAlgorithm SHA256 | Out-Null
    }
}

Write-Host "Built $Root\release\Mousehare-Setup-1.0.0.exe"
