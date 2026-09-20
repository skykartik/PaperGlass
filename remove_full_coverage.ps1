#Requires -RunAsAdministrator
# Undoes setup_full_coverage.ps1: removes the installed app, the shortcut and the trusted certificate.
$ErrorActionPreference = "Continue"
$subject = "CN=PaperGlass Local Signing"

Get-Process PaperGlass -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
$dest = Join-Path $env:ProgramFiles "PaperGlass"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Remove-Item "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\PaperGlass.lnk" -Force -ErrorAction SilentlyContinue
foreach ($store in "Root", "TrustedPublisher", "My") {
    Get-ChildItem "Cert:\LocalMachine\$store" | Where-Object { $_.Subject -eq $subject } | Remove-Item -Force
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PaperGlass" -ErrorAction SilentlyContinue
Write-Host "PaperGlass and its certificate were removed. Your settings in %APPDATA%\PaperGlass were kept." -ForegroundColor Green
