<#
.SYNOPSIS
    Builds the FISH engine and installs the library into bin/.

.DESCRIPTION
    Configures and builds model/fish with CMake, copies the shared library to
    bin/ where the Python host looks for it, and verifies that the loader
    accepts it.

    If Windows refuses the library with WinError 4551, the build itself is
    fine: Smart App Control rejected an unsigned file with no reputation.
    Sign it with scripts/sign_windows.ps1, or turn Smart App Control off for
    development.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_fish.ps1
#>
[CmdletBinding()]
param(
    [string] $BuildDir = "build/fish",
    [string] $OutDir = "bin",
    [switch] $SkipTests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $generator = if (Get-Command ninja -ErrorAction SilentlyContinue) { "Ninja" } else { "MinGW Makefiles" }
    Write-Host "==> Configuring ($generator)" -ForegroundColor Cyan
    cmake -S model/fish -B $BuildDir -G $generator -DCMAKE_BUILD_TYPE=Release
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

    Write-Host "==> Building" -ForegroundColor Cyan
    cmake --build $BuildDir
    if ($LASTEXITCODE -ne 0) { throw "build failed" }

    $name = if ($IsWindows -or $env:OS -eq "Windows_NT") { "fish.dll" } else { "libfish.so" }
    $built = Join-Path $BuildDir $name
    if (-not (Test-Path $built)) { throw "expected $built" }

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    Copy-Item $built (Join-Path $OutDir $name) -Force
    Write-Host "==> Installed $OutDir/$name" -ForegroundColor Green

    if (-not $SkipTests) {
        Write-Host "==> Verifying the loader accepts the library" -ForegroundColor Cyan
        $probe = Join-Path $env:TEMP "fish_load_probe.py"
        @"
import ctypes, sys
try:
    lib = ctypes.CDLL(r'$((Resolve-Path (Join-Path $OutDir $name)).Path)')
    lib.fish_kernel_name.restype = ctypes.c_char_p
    print('    loaded, kernel =', lib.fish_kernel_name().decode())
except OSError as exc:
    print('    REFUSED:', exc)
    sys.exit(3)
"@ | Set-Content -Encoding ASCII $probe
        python $probe
        if ($LASTEXITCODE -eq 3) {
            Write-Host ""
            Write-Host "Windows refused the library (WinError 4551)." -ForegroundColor Yellow
            Write-Host "This is Smart App Control, not a build error. Sign the library:" -ForegroundColor Yellow
            Write-Host "    scripts\sign_windows.ps1 -Path bin -PfxPath <cert.pfx>" -ForegroundColor Yellow
            exit 3
        }
    }
}
finally {
    Pop-Location
}
