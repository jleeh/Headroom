<#
Builds dist\HeadroomSetup-<version>.exe: Headroom + its own Python runtime + the Anthropic SDK,
packed into a single installer. Needs only Windows (the .NET Framework C# compiler ships with it)
and a 64-bit Python 3.12 to copy the runtime from (python.org installer or `winget install Python.Python.3.12`).

  powershell -ExecutionPolicy Bypass -File installer\build.ps1 [-Version 1.1.0] [-Python C:\path\to\python.exe]
      [-ExeOnly]                      dev build: just Headroom.exe + WebView2 DLLs next to the sources
      [-CertThumbprint <sha1>]        Authenticode-sign Headroom.exe and the installer (signtool from the Windows SDK)
#>
param(
    [string]$Version = "1.0.0",
    [string]$Python = "",
    [string]$WebView2Version = "1.0.4314.35",
    [switch]$ExeOnly,
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "stage"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

function Sign($file) {
    if (-not $CertThumbprint) { return }
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName | Select-Object -Last 1
    if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK) - needed for -CertThumbprint" }
    & $signtool.FullName sign /sha1 $CertThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $file
    if ($LASTEXITCODE) { throw "Signing $file failed" }
}

# ---- Python to copy the runtime from
if (-not $Python -and -not $ExeOnly) {
    $py = (Get-Command py.exe -ErrorAction SilentlyContinue)
    if ($py) { $Python = (& py.exe -3.12 -c "import sys; print(sys.executable)").Trim() }
    if (-not $Python) { $Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe" }
}
if (-not $ExeOnly) {
    if (-not (Test-Path $Python)) { throw "Python 3.12 (64-bit) not found. Pass -Python C:\path\to\python.exe" }
    $pyver = (& $Python -c "import sys,struct; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P')*8))").Trim()
    if ($pyver -ne "3.12 64") { throw "Need 64-bit Python 3.12, got $pyver ($Python)" }
    $pyhome = Split-Path -Parent $Python
}

# ---- WebView2 SDK (NuGet) -> repo root, next to Headroom.exe. Pinned by hash: the build stops on a mismatch.
$wvHash = @{
    "microsoft.web.webview2.1.0.4314.35.nupkg" = "90778DEB1B77E36292828B4B5833C9255C27533F40E0DDD55891AE718944EDC4"
    "Microsoft.Web.WebView2.Core.dll"          = "ACDD517B951829F763D1255F32DF1E6A936CB8623A7EBFF6B8B24172DEDE6BD6"
    "Microsoft.Web.WebView2.WinForms.dll"      = "C66437F80886AECF074804B61AF4CC10AD90A95310C8E0A717F31EA377C26B03"
    "WebView2Loader.dll"                       = "D3FCE90BDEECB08596E377DEA6A316354DD8949E32FF09B89D51BE00C14E21B6"
}
function CheckHash($path) {
    $want = $wvHash[(Split-Path -Leaf $path)]
    if (-not $want) { throw "No pinned hash for $path (update `$wvHash when changing -WebView2Version)" }
    $got = (Get-FileHash $path -Algorithm SHA256).Hash
    if ($got -ne $want) { throw "Hash mismatch for ${path}: $got (expected $want)" }
}
$wv = @("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll")
if (($wv | Where-Object { -not (Test-Path (Join-Path $root $_)) }).Count) {
    Step "Downloading WebView2 SDK $WebView2Version from NuGet"
    $tmp = Join-Path $env:TEMP "wv2-$WebView2Version"
    $nupkg = Join-Path $env:TEMP "microsoft.web.webview2.$WebView2Version.nupkg"
    Invoke-WebRequest -UseBasicParsing "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$WebView2Version/microsoft.web.webview2.$WebView2Version.nupkg" -OutFile $nupkg
    CheckHash $nupkg
    Copy-Item $nupkg "$tmp.zip" -Force
    Expand-Archive "$tmp.zip" $tmp -Force
    Copy-Item "$tmp\lib\net462\Microsoft.Web.WebView2.Core.dll", "$tmp\lib\net462\Microsoft.Web.WebView2.WinForms.dll", "$tmp\runtimes\win-x64\native\WebView2Loader.dll" $root
}

foreach ($f in $wv) { CheckHash (Join-Path $root $f) }

# ---- Headroom.exe (built into dist\, so a copy running from the checkout doesn't block the build)
Step "Compiling Headroom.exe"
New-Item -ItemType Directory $dist -Force | Out-Null
Push-Location $root
& $csc /nologo /target:winexe /win32manifest:launcher\app.manifest /win32icon:assets\headroom.ico /platform:x64 `
    /r:Microsoft.Web.WebView2.Core.dll /r:Microsoft.Web.WebView2.WinForms.dll "/out:$dist\Headroom.exe" launcher\Headroom.cs
if ($LASTEXITCODE) { throw "Headroom.exe failed to compile" }
Pop-Location
Sign "$dist\Headroom.exe"
if ($ExeOnly) {
    Copy-Item "$dist\Headroom.exe" $root -Force
    Step "Dev build ready: $root\Headroom.exe (SDK: py -3.12 -m pip install --require-hashes --no-deps -r installer\requirements.txt --target vendor)"
    return
}

# ---- stage the app
Step "Staging files"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory $stage | Out-Null
Copy-Item "$dist\Headroom.exe" $stage
$files = @("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md") + $wv
foreach ($f in $files) { Copy-Item (Join-Path $root $f) $stage }
foreach ($d in @("headroom", "bin", "web", "profiles", "assets")) {
    Copy-Item (Join-Path $root $d) (Join-Path $stage $d) -Recurse
}
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# ---- private Python runtime (isolated by python312._pth: ignores any other Python on the PC)
Step "Copying Python runtime from $pyhome"
$rt = Join-Path $stage "runtime"
New-Item -ItemType Directory $rt | Out-Null
foreach ($f in @("python.exe", "pythonw.exe", "python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")) {
    Copy-Item (Join-Path $pyhome $f) $rt
}
New-Item -ItemType Directory "$rt\DLLs" | Out-Null
Get-ChildItem "$pyhome\DLLs" -File -Force | Where-Object { $_.Name -notmatch '^(_tkinter|tcl|tk|_test|_ctypes_test|xxlimited)' } |
    Copy-Item -Destination "$rt\DLLs"
$skip = @("site-packages", "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "__pycache__", "venv", "pydoc_data")
robocopy "$pyhome\Lib" "$rt\Lib" /E /NFL /NDL /NJH /NJS /NP /XD $skip | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying the Python library failed" }
New-Item -ItemType Directory "$rt\Lib\site-packages" | Out-Null
Set-Content "$rt\python312._pth" "Lib`r`nDLLs`r`n..\vendor" -Encoding ASCII

# ---- Anthropic SDK (for 'Research my hardware'): exact versions, hash-checked (installer\requirements.txt)
Step "Installing pinned packages into vendor\"
& $Python -m pip install --disable-pip-version-check --no-warn-script-location --quiet --no-deps --only-binary=:all: `
    --require-hashes -r (Join-Path $PSScriptRoot "requirements.txt") --target (Join-Path $stage "vendor")
if ($LASTEXITCODE) { throw "pip install failed" }
Get-ChildItem (Join-Path $stage "vendor") -Directory -Filter "bin" | Remove-Item -Recurse -Force

# ---- smoke test the staged runtime
Step "Checking the bundled runtime"
$check = & "$rt\python.exe" -c "import sys, ssl, ctypes; sys.path.insert(0, r'$stage'); import anthropic, headroom.server, headroom.research, headroom.scan; print('ok', anthropic.__version__)" 2>&1
if (-not ("$check" -match "^ok")) { throw "Runtime check failed: $check" }
Write-Host "    $check"
& "$rt\python.exe" -m compileall -q -j 0 "$rt\Lib" (Join-Path $stage "vendor") (Join-Path $stage "headroom") | Out-Null

# ---- payload + installer
Step "Packing payload"
$payload = Join-Path $dist "payload.zip"
if (Test-Path $payload) { Remove-Item $payload }
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression
# entries written by hand: .NET Framework's CreateFromDirectory stores '\' separators, which other unzip tools mishandle
$zs = [System.IO.File]::Open($payload, [System.IO.FileMode]::CreateNew)
$za = New-Object System.IO.Compression.ZipArchive($zs, [System.IO.Compression.ZipArchiveMode]::Create)
$base = (Resolve-Path $stage).Path.TrimEnd('\') + '\'
foreach ($it in Get-ChildItem $stage -Recurse -Force) {
    $rel = $it.FullName.Substring($base.Length).Replace('\', '/')
    if ($it.PSIsContainer) {
        if (-not (Get-ChildItem $it.FullName -Force | Select-Object -First 1)) { [void]$za.CreateEntry("$rel/") }
        continue
    }
    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($za, $it.FullName, $rel, [System.IO.Compression.CompressionLevel]::Optimal)
}
$za.Dispose(); $zs.Dispose()
Set-Content (Join-Path $dist "version.txt") $Version -Encoding ASCII -NoNewline

Step "Compiling the installer"
$out = Join-Path $dist "HeadroomSetup-$Version.exe"
& $csc /nologo /target:winexe /platform:x64 "/win32manifest:$PSScriptRoot\setup.manifest" "/win32icon:$root\assets\headroom.ico" `
    /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll `
    "/resource:$payload,payload.zip" "/resource:$(Join-Path $dist 'version.txt'),version.txt" "/out:$out" "$PSScriptRoot\Setup.cs"
if ($LASTEXITCODE) { throw "Setup.exe failed to compile" }
Sign $out
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
$sha = (Get-FileHash $out -Algorithm SHA256).Hash
Set-Content "$out.sha256" "$sha  $(Split-Path -Leaf $out)" -Encoding ASCII
Step "Built $out ($mb MB)  SHA256 $sha"
