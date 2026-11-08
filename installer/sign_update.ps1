<#
.SYNOPSIS
  Maintainer tool for signed Headroom updates (Windows PowerShell 5.1, .NET Framework 4.x).

.EXAMPLE
  .\sign_update.ps1 -NewKey -KeyFile C:\secure\headroom-update.key
  .\sign_update.ps1 -Installer .\HeadroomSetup-1.1.0.exe -Version 1.1.0 -KeyFile C:\secure\headroom-update.key `
      -Url https://github.com/eviljarv89-ctrl/Headroom/releases/download/v1.1.0/HeadroomSetup-1.1.0.exe -Notes "What changed"

  -NewKey: creates an ECDSA P-256 key, writes the PRIVATE key blob to -KeyFile (keep it OUTSIDE the repo,
  never commit it) and prints the public key (64-byte X||Y hex) for UPDATE_PUBKEY in headroom/update.py.
  Signing: writes update.json and update.json.sig next to the installer. Upload both to the GitHub release.
#>
param(
    [switch]$NewKey,
    [string]$KeyFile,
    [string]$Installer,
    [string]$Version,
    [string]$Url,
    [string]$Notes = ""
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Core
if (-not $KeyFile) { throw "-KeyFile is required" }
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Get-PubHex($blob) {
    # BCRYPT_ECCKEY_BLOB: 4 byte magic, 4 byte cbKey (32), then X (32) and Y (32)
    $xy = New-Object byte[] 64
    [Array]::Copy($blob, 8, $xy, 0, 64)
    return (($xy | ForEach-Object { $_.ToString("x2") }) -join "")
}

if ($NewKey) {
    if (Test-Path $KeyFile) { throw "$KeyFile already exists. Refusing to overwrite a signing key." }
    $full = [IO.Path]::GetFullPath($KeyFile)
    if ($full.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Put the key OUTSIDE the repo." }
    $cp = New-Object System.Security.Cryptography.CngKeyCreationParameters
    $cp.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::AllowPlaintextExport
    $key = [System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::ECDsaP256, $null, $cp)
    [IO.File]::WriteAllBytes($full, $key.Export([System.Security.Cryptography.CngKeyBlobFormat]::EccPrivateBlob))
    Write-Host "Private key written to $full (back it up, keep it secret)."
    $pubBlob = $key.Export([System.Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
    Write-Host ('UPDATE_PUBKEY = "' + (Get-PubHex $pubBlob) + '"')
    $key.Dispose()
    return
}

if (-not ($Installer -and $Version -and $Url)) { throw "Signing needs -Installer, -Version, -Url and -KeyFile" }
if ($Version -notmatch '^\d+(\.\d+){1,3}$') { throw "Version must look like 1.2.3" }
$exe = (Resolve-Path $Installer).Path
$blob = [IO.File]::ReadAllBytes((Resolve-Path $KeyFile).Path)
$key = [System.Security.Cryptography.CngKey]::Import($blob, [System.Security.Cryptography.CngKeyBlobFormat]::EccPrivateBlob)
$ec = New-Object System.Security.Cryptography.ECDsaCng($key)
$ec.HashAlgorithm = [System.Security.Cryptography.CngAlgorithm]::Sha256

$sha = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
$manifest = [ordered]@{ version = $Version; url = $Url; sha256 = $sha; size = (Get-Item $exe).Length; notes = $Notes }
# update.json bytes are signed EXACTLY as written: UTF-8 without BOM, never re-saved by an editor.
$json = $manifest | ConvertTo-Json -Compress
$bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
$dir = Split-Path -Parent $exe
[IO.File]::WriteAllBytes((Join-Path $dir "update.json"), $bytes)
# SignData returns the IEEE P1363 r||s signature (64 bytes), which is what update.py expects.
$sig = $ec.SignData($bytes)
if ($sig.Length -ne 64) { throw "Unexpected signature length $($sig.Length)" }
[IO.File]::WriteAllBytes((Join-Path $dir "update.json.sig"), [Text.Encoding]::ASCII.GetBytes([Convert]::ToBase64String($sig)))
$ec.Dispose(); $key.Dispose()
Write-Host "Wrote update.json and update.json.sig in $dir. Upload both to the release."
