<#
.SYNOPSIS
    Builds and installs OpenEB from source on Windows 11 (x64).

.DESCRIPTION
    Automates the mechanical parts of the official OpenEB Windows build:
    https://docs.prophesee.ai/stable/installation/windows_openeb.html

    This does NOT fully replace that page — a few steps genuinely need a human
    (installing Visual Studio with the right workload, picking a camera driver
    in Zadig/wdi-simple with the device plugged in, optional CUDA). The script
    checks for those prerequisites and stops with clear instructions if they're
    missing, rather than guessing.

.PARAMETER OpenebVersion
    Git tag/branch of prophesee-ai/openeb to build. Default: 5.2.0

.PARAMETER OpenebSrcDir
    Where to clone the openeb source. Default: $env:USERPROFILE\openeb\src

.PARAMETER InstallDir
    CMake install prefix. Default: $env:USERPROFILE\openeb\install
    (matches this repo's OPENEB_INSTALL_DIR convention — see README.md)

.PARAMETER VcpkgDir
    Where to clone/bootstrap vcpkg. Default: $env:USERPROFILE\vcpkg

.PARAMETER PyVenvDir
    Where to create the Python virtualenv used for the build and for running
    metavision_core/metavision_hal afterwards. Default: $env:USERPROFILE\openeb\py3venv

.PARAMETER SkipVcpkgInstall
    Skip `vcpkg install` (use if dependencies are already installed — this step
    can take well over an hour on a clean machine).

.PARAMETER SkipBuild
    Only run the CMake configure step, don't build/install. Useful for
    iterating on configuration before committing to a full build.

.EXAMPLE
    .\install_openeb_windows.ps1
    Full build with all defaults.

.EXAMPLE
    .\install_openeb_windows.ps1 -OpenebVersion 5.2.0 -SkipVcpkgInstall
    Rebuild after dependencies are already in place.
#>

[CmdletBinding()]
param(
    [string]$OpenebVersion   = "5.2.0",
    [string]$OpenebSrcDir    = "$env:USERPROFILE\openeb\src",
    [string]$InstallDir      = "$env:USERPROFILE\openeb\install",
    [string]$VcpkgDir        = "$env:USERPROFILE\vcpkg",
    [string]$PyVenvDir       = "$env:USERPROFILE\openeb\py3venv",
    [int]$Parallel           = [Environment]::ProcessorCount,
    [switch]$SkipVcpkgInstall,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Warn2($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------------------
# 1. Prerequisite checks — fail fast with an actionable message rather than
#    letting vcpkg/cmake fail 40 minutes into a dependency build.
# ---------------------------------------------------------------------------
Write-Step "Checking prerequisites"

if (-not (Test-Command git))   { throw "git not found on PATH. Install Git for Windows and re-run." }
if (-not (Test-Command cmake)) { throw "cmake not found on PATH. Install CMake 3.26 and re-run (newer versions have known incompatibilities with OpenEB's vcpkg toolchain)." }

$cmakeVersionLine = (cmake --version | Select-Object -First 1)
if ($cmakeVersionLine -notmatch "3\.26") {
    Write-Warn2 "Detected '$cmakeVersionLine' — OpenEB's docs specifically call out CMake 3.26; other versions may fail to configure."
}

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    throw "Visual Studio Installer not found. Install Visual Studio 2022 (>=17.14) with the 'Desktop development with C++' workload, then re-run."
}
$vsInstall = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsInstall) {
    throw "No Visual Studio install with the MSVC x64 C++ toolset was found. Install the 'Desktop development with C++' workload in Visual Studio Installer, then re-run."
}
Write-Host "  Visual Studio: $vsInstall"

if (-not (Test-Command python)) { throw "python not found on PATH. Install Python 3.10, 3.11, or 3.12 and re-run." }
$pyVersion = (python --version)
Write-Host "  Python: $pyVersion"
if ($pyVersion -notmatch "3\.(10|11|12)") {
    Write-Warn2 "OpenEB supports Python 3.10-3.12; detected '$pyVersion'. Build may fail."
}

if (-not (Test-Command ffmpeg)) {
    Write-Warn2 "ffmpeg not found on PATH. Download a Windows build from https://ffmpeg.org/download.html#build-windows and add its 'bin' folder to PATH before running the viewer (not required for the OpenEB build itself)."
}

# ---------------------------------------------------------------------------
# 2. Enable Win32 long paths (needed — vcpkg/boost path depths exceed 260
#    chars). Requires admin; if not elevated, warn and continue, since the
#    build may still succeed depending on install location depth.
# ---------------------------------------------------------------------------
Write-Step "Checking Win32 long path support"

$longPathsKey = "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem"
$longPathsValue = (Get-ItemProperty -Path $longPathsKey -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
if ($longPathsValue -ne 1) {
    if (Test-IsAdmin) {
        Write-Host "  Enabling LongPathsEnabled in the registry..."
        Set-ItemProperty -Path $longPathsKey -Name LongPathsEnabled -Value 1 -Type DWord
        Write-Warn2 "Long paths were just enabled. A reboot may be required for it to fully take effect."
    } else {
        Write-Warn2 "Long paths are not enabled and this script isn't running as Administrator. Re-run this script elevated, or enable it yourself: gpedit.msc -> Computer Configuration > Administrative Templates > System > Filesystem > 'Enable Win32 long paths'."
    }
} else {
    Write-Host "  Already enabled."
}

# ---------------------------------------------------------------------------
# 3. vcpkg
# ---------------------------------------------------------------------------
Write-Step "Setting up vcpkg at $VcpkgDir"

if (-not (Test-Path $VcpkgDir)) {
    git clone https://github.com/microsoft/vcpkg.git $VcpkgDir --branch 2024.11.16
}
Push-Location $VcpkgDir
if (-not (Test-Path "$VcpkgDir\vcpkg.exe")) {
    & .\bootstrap-vcpkg.bat
}
.\vcpkg.exe update
Pop-Location

# ---------------------------------------------------------------------------
# 4. OpenEB source
# ---------------------------------------------------------------------------
Write-Step "Fetching OpenEB source ($OpenebVersion) into $OpenebSrcDir"

if (-not (Test-Path $OpenebSrcDir)) {
    git clone https://github.com/prophesee-ai/openeb.git $OpenebSrcDir --branch $OpenebVersion
} else {
    Write-Host "  $OpenebSrcDir already exists, skipping clone."
}

$vcpkgManifest = "$OpenebSrcDir\utils\windows\11\vcpkg-openeb.json"
if (-not (Test-Path $vcpkgManifest)) {
    throw "Expected vcpkg manifest not found at $vcpkgManifest — OpenEB's repo layout may have changed since this script was written. Check the current docs at https://docs.prophesee.ai/stable/installation/windows_openeb.html"
}
Copy-Item $vcpkgManifest "$VcpkgDir\vcpkg.json" -Force

# ---------------------------------------------------------------------------
# 5. vcpkg dependencies (slow — boost/opencv/hdf5 etc. built from source)
# ---------------------------------------------------------------------------
if (-not $SkipVcpkgInstall) {
    Write-Step "Installing vcpkg dependencies (this can take well over an hour)"
    Push-Location $VcpkgDir
    .\vcpkg.exe install --triplet x64-windows --x-install-root installed
    Pop-Location
} else {
    Write-Step "Skipping vcpkg install (-SkipVcpkgInstall)"
}

# ---------------------------------------------------------------------------
# 6. Python virtualenv + build-time Python deps
# ---------------------------------------------------------------------------
Write-Step "Creating Python virtualenv at $PyVenvDir"

if (-not (Test-Path $PyVenvDir)) {
    python -m venv $PyVenvDir --system-site-packages
}
$venvPython = "$PyVenvDir\Scripts\python.exe"
$env:PYTHONNOUSERSITE = "true"

& $venvPython -m pip install pip --upgrade
& $venvPython -m pip install `
    -r "$OpenebSrcDir\utils\python\requirements_openeb.txt" `
    -r "$OpenebSrcDir\utils\python\requirements_pytorch_cpu.txt"

$pySitePackages = & $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
Write-Host "  Python site-packages target: $pySitePackages"

# ---------------------------------------------------------------------------
# 7. CMake configure / build / install
# ---------------------------------------------------------------------------
Write-Step "Configuring CMake build"

$buildDir = "$OpenebSrcDir\build"
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
Push-Location $buildDir

cmake .. -A x64 `
    -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_TOOLCHAIN_FILE="$OpenebSrcDir\cmake\toolchains\vcpkg.cmake" `
    -DVCPKG_DIRECTORY="$VcpkgDir" `
    -DCMAKE_INSTALL_PREFIX="$InstallDir" `
    -DPYTHON3_SITE_PACKAGES="$pySitePackages" `
    -DBUILD_TESTING=OFF

if ($SkipBuild) {
    Write-Warn2 "Configure-only run (-SkipBuild). Skipping build/install."
    Pop-Location
    exit 0
}

Write-Step "Building (this can take 30-60+ minutes)"
cmake --build . --config Release --parallel $Parallel

Write-Step "Installing to $InstallDir"
cmake --build . --config Release --target install

Pop-Location

# ---------------------------------------------------------------------------
# 8. Persist environment variables for the current user
# ---------------------------------------------------------------------------
Write-Step "Setting persistent user environment variables"

function Add-ToUserPath($dir) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($current -notlike "*$dir*") {
        [Environment]::SetEnvironmentVariable("Path", "$current;$dir", "User")
        Write-Host "  Added to PATH: $dir"
    }
}

Add-ToUserPath "$InstallDir\bin"
[Environment]::SetEnvironmentVariable("MV_HAL_PLUGIN_PATH", "$InstallDir\lib\metavision\hal\plugins", "User")
[Environment]::SetEnvironmentVariable("HDF5_PLUGIN_PATH", "$InstallDir\lib\hdf5\plugin", "User")
[Environment]::SetEnvironmentVariable("PYTHONPATH", $pySitePackages, "User")
[Environment]::SetEnvironmentVariable("OPENEB_INSTALL_DIR", $InstallDir, "User")

Write-Host "`nDone. Open a NEW terminal for the environment variables to take effect." -ForegroundColor Green

Write-Warn2 @"

Remaining manual steps:

1. Camera USB driver (device must be plugged in): run, as Administrator,
   the wdi-simple.exe utility from Prophesee's file server against your
   EVK's VID/PID (see the 'Camera Drivers' section of
   https://docs.prophesee.ai/stable/installation/windows_openeb.html).

2. This repo's main.py bootstraps its own environment (LD_LIBRARY_PATH,
   dist-packages layout) assuming a LINUX OpenEB install tree — that logic
   does not currently look in the right places on Windows. You built
   OpenEB successfully, but you'll likely need to adjust main.py's
   bootstrap block (or just run it inside the venv created above with
   PYTHONPATH already set, skipping its self-exec logic) before the
   viewer will import metavision_hal/metavision_core here.

3. Optional GPU support: install CUDA 12.8 + matching cuDNN before the
   CMake configure step if you want it (not covered by this script).
"@
