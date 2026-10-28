<#
.SYNOPSIS
    Signs the IMURUN Windows binaries with a code-signing certificate.

.DESCRIPTION
    FISH ships as a shared library (fish.dll) that the server loads
    in-process. Nothing is executed from disk, so there is no "unsigned
    program" for a user to launch - but Windows still has to decide whether
    it trusts the code inside the library.

    Two mechanisms make that decision:

      * Smart App Control (SAC) and SmartScreen use a reputation service.
        A brand-new unsigned binary has no reputation, so SAC blocks it with
        WinError 4551 / CodeIntegrity event 3077. This is what makes a fresh
        local build fail to load at random.
      * Application Control (WDAC) policies check the publisher against
        signing rules.

    Signing with a certificate from a public CA (OV or EV code signing for
    ImuTechz.in) gives the library a verifiable publisher, which is what both
    mechanisms look for. The signature is timestamped so it stays valid after
    the certificate expires.

    The private key never leaves the machine that holds it: this script takes
    a PFX path or a store thumbprint, not the key itself.

.PARAMETER Path
    The file or folder to sign. A folder signs every executable inside it.

.PARAMETER PfxPath
    Path to the .pfx file that holds the code-signing certificate.

.PARAMETER PfxPassword
    Password for the .pfx file. Read from the environment variable
    IMURUN_CERT_PASSWORD when this parameter is omitted, so the password
    does not end up in the shell history.

.PARAMETER Thumbprint
    SHA-1 thumbprint of a certificate already in the Windows store.

.PARAMETER TimestampUrl
    The RFC 3161 timestamp service. The default is DigiCert's.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\sign_windows.ps1 `
        -Path .\build\engine -PfxPath C:\certs\imutechz.pfx

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\sign_windows.ps1 `
        -Path .\build\engine -Thumbprint 0123456789ABCDEF...
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Path,

    [string] $PfxPath = "",
    [string] $PfxPassword = "",
    [string] $Thumbprint = "",
    [string] $TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

function Write-Step([string] $message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Resolve-Targets([string] $target) {
    if (-not (Test-Path $target)) {
        throw "The path does not exist: $target"
    }
    $item = Get-Item $target
    if ($item.PSIsContainer) {
        $files = Get-ChildItem -Path $target -Include *.exe, *.dll -Recurse -File
        if (-not $files) { throw "No executables or libraries found in $target" }
        return $files
    }
    return @($item)
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $kits = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
    foreach ($kit in $kits) {
        $candidate = Join-Path $kit.FullName "x64\signtool.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    throw "signtool.exe was not found. Install the Windows SDK, or add it to PATH."
}

if (-not $PfxPath -and -not $Thumbprint) {
    throw "Give either -PfxPath or -Thumbprint. The certificate decides who publishes the signature."
}

if ($PfxPath -and -not (Test-Path $PfxPath)) {
    throw "The certificate file does not exist: $PfxPath"
}

if (-not $PfxPassword -and $env:IMURUN_CERT_PASSWORD) {
    $PfxPassword = $env:IMURUN_CERT_PASSWORD
}

$signTool = Find-SignTool
Write-Step "Using $signTool"

$targets = Resolve-Targets $Path
Write-Step "Signing $($targets.Count) file(s)"

foreach ($file in $targets) {
    $arguments = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")

    if ($PfxPath) {
        $arguments += @("/f", $PfxPath)
        if ($PfxPassword) { $arguments += @("/p", $PfxPassword) }
    }
    else {
        $arguments += @("/sha1", $Thumbprint)
    }

    $arguments += @("/d", "ImuTechz FISH inference engine", $file.FullName)

    Write-Host "    $($file.Name)"
    & $signTool @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed for $($file.FullName) with exit code $LASTEXITCODE"
    }
}

Write-Step "Verifying"
$allValid = $true
foreach ($file in $targets) {
    & $signTool "verify" "/pa" "/v" $file.FullName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    $($file.Name): NOT TRUSTED" -ForegroundColor Red
        $allValid = $false
        continue
    }
    $signature = Get-AuthenticodeSignature -FilePath $file.FullName
    Write-Host "    $($file.Name): $($signature.Status) - $($signature.SignerCertificate.Subject)" -ForegroundColor Green
}

if (-not $allValid) {
    throw "At least one file is not signed correctly."
}

Write-Step "Done. The binaries are signed and timestamped."

# Smart App Control is a separate, user-controlled switch. A correctly signed
# library passes it; an unsigned local build does not, no matter how the file
# was produced. Report it so a failing local build is not mistaken for a bug.
$sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" `
        -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
if ($sac -eq 1) {
    Write-Host ""
    Write-Host "Note: Smart App Control is ON (enforcement) on this machine." -ForegroundColor Yellow
    Write-Host "      Unsigned development builds may still be refused with WinError 4551." -ForegroundColor Yellow
    Write-Host "      Signed release builds are accepted. For local iteration, either sign each" -ForegroundColor Yellow
    Write-Host "      build or turn Smart App Control off in Windows Security." -ForegroundColor Yellow
}

