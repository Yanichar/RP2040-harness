<#
.SYNOPSIS
    Build the RP2040 Harness firmware with the Pico SDK 2.3.1 toolchain.

.DESCRIPTION
    The machine's PATH carries arm-none-eabi-gcc from the Cypress FX3 SDK, which
    is NOT compatible with the RP2040. This script therefore always runs the
    build inside the Pico Setup environment (`pico-env.cmd`), which puts the
    correct arm-gnu-toolchain, ninja and cmake first on PATH.

.PARAMETER Config
    Debug or Release (default Release).

.PARAMETER Clean
    Delete the build directory first.

.PARAMETER Flash
    After a successful build, reboot the attached Pico into BOOTSEL via picotool
    and download the freshly built harness.uf2.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean -Config Debug
    .\build.ps1 -Flash
#>
[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Config = 'Release',
    [switch]$Clean,
    [switch]$Flash
)

# NOTE: deliberately NOT 'Stop'. The Pico Setup environment prints its banner to
# stderr, and PowerShell turns native stderr into a terminating error when
# ErrorActionPreference is Stop. Every native call is checked explicitly below.
$ErrorActionPreference = 'Continue'

$here = $PSScriptRoot
$picoSetup = if ($env:PICO_SETUP_DIR) { $env:PICO_SETUP_DIR } else { 'C:\Pico SDK v2.3.1' }
$picoEnvCmd = Join-Path $picoSetup 'pico-env.cmd'
$sdkPath = Join-Path $picoSetup 'pico-sdk'
$picotool = Join-Path $picoSetup 'pico-sdk-tools\picotool\picotool.exe'
$buildDir = Join-Path $here 'build'
$uf2 = Join-Path $buildDir 'harness.uf2'
$elf = Join-Path $buildDir 'harness.elf'

if (-not (Test-Path $picoEnvCmd)) {
    throw "Pico SDK environment not found at '$picoEnvCmd'. Set PICO_SETUP_DIR or install the Pico Setup."
}
if (-not (Test-Path $sdkPath)) {
    throw "Pico SDK not found at '$sdkPath'."
}

if ($Clean -and (Test-Path $buildDir)) {
    Write-Host "Removing $buildDir" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $buildDir
}

$configure = 'cmake -S "{0}" -B "{1}" -G Ninja -DCMAKE_BUILD_TYPE={2} -DPICO_SDK_PATH="{3}" -DPICO_BOARD=pico_w' -f `
    $here, $buildDir, $Config, $sdkPath
$compile = 'cmake --build "{0}" --parallel' -f $buildDir

Write-Host "==> Configuring ($Config)" -ForegroundColor Cyan
cmd.exe /s /c "`"$picoEnvCmd`" >nul && $configure"
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed ($LASTEXITCODE)." }

Write-Host "==> Building" -ForegroundColor Cyan
cmd.exe /s /c "`"$picoEnvCmd`" >nul && $compile"
if ($LASTEXITCODE -ne 0) { throw "Build failed ($LASTEXITCODE)." }

Write-Host ""
Write-Host "Build OK" -ForegroundColor Green
Write-Host "  UF2 : $uf2"
Write-Host "  ELF : $elf"

if ($Flash) {
    if (-not (Test-Path $picotool)) { throw "picotool not found at '$picotool'." }

    Write-Host ""
    Write-Host "==> Rebooting device into BOOTSEL (via the firmware's reset interface)" -ForegroundColor Cyan
    # If the harness firmware is running, this works with no button press.
    # The very first flash of a blank board still needs BOOTSEL held once.
    cmd.exe /s /c "`"$picotool`" reboot -f -u"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "picotool reset failed. Hold BOOTSEL, replug, then re-run with -Flash."
    }

    Start-Sleep -Seconds 2

    Write-Host "==> Downloading $uf2" -ForegroundColor Cyan
    cmd.exe /s /c "`"$picotool`" load -x `"$uf2`""
    if ($LASTEXITCODE -ne 0) { throw "picotool load failed ($LASTEXITCODE)." }

    Write-Host "Flashed." -ForegroundColor Green
}
