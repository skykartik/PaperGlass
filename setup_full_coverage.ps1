#Requires -RunAsAdministrator
# PaperGlass full-coverage setup
#
# Windows draws the Start menu and Quick Settings above ordinary windows. The only supported way for an
# app to draw over them is "UIAccess", which Windows grants only to programs that are (1) digitally signed
# with a certificate it trusts and (2) installed under Program Files.
#
# This script does exactly that, on this PC only:
#   1. copies dist_ui\PaperGlass to C:\Program Files\PaperGlass
#   2. creates a code-signing certificate just for PaperGlass and signs PaperGlass.exe with it
#   3. adds ONLY the public half of that certificate to this PC's trusted stores
#   4. deletes the private signing key, so nothing else can ever be signed with it
#   5. adds a Start menu shortcut and launches PaperGlass
# remove_full_coverage.ps1 undoes all of it.

$ErrorActionPreference = "Stop"
$src  = Join-Path $PSScriptRoot "dist_ui\PaperGlass"
$dest = Join-Path $env:ProgramFiles "PaperGlass"
$subject = "CN=PaperGlass Local Signing"

if (-not (Test-Path (Join-Path $src "PaperGlass.exe"))) {
    throw "dist_ui\PaperGlass\PaperGlass.exe was not found. Run build_full_coverage.bat first."
}

Write-Host ""
Write-Host "This will install PaperGlass to '$dest' and trust a certificate made on this PC" -ForegroundColor Yellow
Write-Host "so Windows lets PaperGlass draw over the Start menu and Quick Settings." -ForegroundColor Yellow
$answer = Read-Host "Type Y to continue"
if ($answer -notmatch '^[Yy]') { Write-Host "Cancelled."; exit 0 }

Get-Process PaperGlass -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item $src $dest -Recurse

# fresh certificate every time
Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -eq $subject } | Remove-Item -Force
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
        -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(10)

$cer = Join-Path $env:TEMP "PaperGlassLocalSigning.cer"
Export-Certificate -Cert $cert -FilePath $cer | Out-Null
foreach ($store in "Root", "TrustedPublisher") {
    Get-ChildItem "Cert:\LocalMachine\$store" | Where-Object { $_.Subject -eq $subject } | Remove-Item -Force
    Import-Certificate -FilePath $cer -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
}

$sig = Set-AuthenticodeSignature -FilePath (Join-Path $dest "PaperGlass.exe") -Certificate $cert -HashAlgorithm SHA256
if ($sig.Status -ne "Valid") { throw "Signing failed: $($sig.StatusMessage)" }

# delete the private key: the trusted certificate can no longer sign anything
Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -eq $subject } | Remove-Item -Force
Remove-Item $cer -Force -ErrorAction SilentlyContinue

$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:ProgramData\Microsoft\Windows\Start Menu\Programs\PaperGlass.lnk")
$lnk.TargetPath = Join-Path $dest "PaperGlass.exe"
$lnk.WorkingDirectory = $dest
$lnk.Save()

Write-Host ""
Write-Host "Done. Starting PaperGlass from Program Files..." -ForegroundColor Green
Write-Host "Open Settings in PaperGlass: 'Start menu and Quick Settings' should now say Covered."
Start-Process (Join-Path $dest "PaperGlass.exe")
