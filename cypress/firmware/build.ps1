<#
.SYNOPSIS
    Build the FX2LP 24-line harness firmware with SDCC.

.DESCRIPTION
    Mirrors the proven reference build (firmware/build.py in
    JS-CY7C68013A-master-VV55) with the layout this project needs:

        code            0x0000 - 0x2FFF
        internal xdata  0x3000 - 0x3DFF   (rx_frame, tx_frame, state)
        USB descriptors 0x3E00            (DSCR_AREA, read by the EZ-USB core)
        INT2 jump table 0x3F00            (INT2JT, fx2lib autovector glue)

    fx2lib predates SDCC 4.x and writes bit addresses as ``__at 0x80 + 0``,
    which the current parser rejects at the "+".  The headers are staged into
    build/include and the offending forms are parenthesised, exactly like the
    reference build script does.

.PARAMETER Fx2Lib
    Path to the fx2lib-master checkout (include/ and lib/).  Defaults to
    $env:FX2LIB, then to the checkout inside JS-CY7C68013A-master-VV55.

.EXAMPLE
    pwsh -File cypress/firmware/build.ps1
#>
[CmdletBinding()]
param(
    [string]$Fx2Lib = $null
)

$ErrorActionPreference = 'Stop'

$Root    = $PSScriptRoot
$Src     = Join-Path $Root 'src'
$Build   = Join-Path $Root 'build'
$Include = Join-Path $Build 'include'
$LibBuild = Join-Path $Build 'lib'

# ------------------------------------------------------------------ fx2lib --
if (-not $Fx2Lib) { $Fx2Lib = $env:FX2LIB }
if (-not $Fx2Lib) {
    $Fx2Lib = 'C:\Users\Yanichar\work\JS-CY7C68013A-master-VV55\vendor\fx2lib-master'
}
$Fx2Lib    = (Resolve-Path -LiteralPath $Fx2Lib).Path
$LibSrc    = Join-Path $Fx2Lib 'lib'
$IncludeSrc = Join-Path $Fx2Lib 'include'
foreach ($p in @($LibSrc, $IncludeSrc)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw "fx2lib checkout is incomplete: '$p' not found (pass -Fx2Lib <dir>)"
    }
}

# -------------------------------------------------------------------- SDCC --
$maybe = @(
    'C:\Program Files\SDCC\bin',
    'C:\Program Files (x86)\SDCC\bin',
    'C:\SDCC\bin'
)
$Bindir = $null
foreach ($d in $maybe) {
    if (Test-Path -LiteralPath (Join-Path $d 'sdcc.exe')) { $Bindir = $d; break }
}
if (-not $Bindir) {
    $cmd = Get-Command sdcc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Bindir = Split-Path -Parent $cmd.Source }
}
if (-not $Bindir) { throw 'SDCC not found; install it or edit the $maybe list' }

$CC     = Join-Path $Bindir 'sdcc.exe'
$ASM    = Join-Path $Bindir 'sdas8051.exe'
$AR     = Join-Path $Bindir 'sdar.exe'
$Makebin = Join-Path $Bindir 'makebin.exe'
$Packihx = Join-Path $Bindir 'packihx.exe'

# sdcpp looks up cc1 through PATH: make SDCC's own bin dir visible.
$env:PATH = $Bindir + [IO.Path]::PathSeparator + $env:PATH

function Invoke-Tool {
    # NOTE: Windows PowerShell 5.1 turns native-command stderr into terminating
    # errors when $ErrorActionPreference is 'Stop', and SDCC writes *warnings*
    # to stderr.  Relax the preference for the duration of the call.
    param([string]$What, [string[]]$Argv, [string]$WorkDir)
    $previous = $ErrorActionPreference
    Push-Location -LiteralPath $WorkDir
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Argv[0] @($Argv[1..($Argv.Count - 1)]) 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        Pop-Location
    }
    if ($code -ne 0) {
        Write-Host $output
        throw "$What failed (exit $code): $($Argv -join ' ')"
    }
    return $output
}

Write-Host "fx2lib    : $Fx2Lib"
Write-Host "SDCC      : $Bindir"

# ------------------------------------------------------------- clean build --
if (Test-Path -LiteralPath $Build) { Remove-Item -LiteralPath $Build -Recurse -Force }
New-Item -ItemType Directory -Path $LibBuild -Force | Out-Null
Copy-Item -LiteralPath $IncludeSrc -Destination $Include -Recurse

# fx2lib predates SDCC 4.x: ``__at 0x80 + 0`` has to become ``__at(0x80+0)``.
$patched = 0
Get-ChildItem -LiteralPath $Include -Filter '*.h' | ForEach-Object {
    $text = Get-Content -LiteralPath $_.FullName -Raw
    $fixed = [regex]::Replace($text, '__at\s+(0x[0-9A-Fa-f]+)\s*\+\s*(\d+)', '__at($1+$2)')
    if ($fixed -ne $text) {
        Set-Content -LiteralPath $_.FullName -Value $fixed -NoNewline
        $script:patched++
    }
}
Write-Host "headers   : $Include ($patched patched for SDCC 4.x __at syntax)"

# ---------------------------------------------------------------- fx2.lib --
Write-Host 'building fx2.lib ...'
$libCSources = @('delay.c', 'eputils.c', 'setupdat.c', 'serial.c', 'i2c.c', 'gpif.c')
$isrSources  = Get-ChildItem -LiteralPath (Join-Path $LibSrc 'interrupts') -Filter '*.c' |
               Sort-Object Name | ForEach-Object { "interrupts/$($_.Name)" }
$objs = @()
foreach ($name in ($libCSources + $isrSources)) {
    $rel = [IO.Path]::GetFileNameWithoutExtension($name) + '.rel'
    Invoke-Tool "cc $name" @($CC, '-mmcs51', '-c', '-I', $Include,
        (Join-Path $LibSrc $name), '-o', (Join-Path $LibBuild $rel)) $LibBuild | Out-Null
    $objs += $rel
}

# usbav.a51 holds the autovector jump table; assemble it from the build dir
# because sdas8051 writes its .rel next to the input file.
Copy-Item -LiteralPath (Join-Path $LibSrc 'usbav.a51') -Destination $LibBuild
Invoke-Tool 'as usbav.a51' @($ASM, '-logs', 'usbav.a51') $LibBuild | Out-Null
$objs += 'usbav.rel'

Invoke-Tool 'sdar fx2.lib' (@($AR, '-rc', 'fx2.lib') + $objs) $LibBuild | Out-Null
Write-Host "          $($objs.Count) objects"

# ------------------------------------------------------------ our firmware --
Write-Host 'assembling descriptors ...'
Copy-Item -LiteralPath (Join-Path $Src 'dscr.a51') -Destination $Build
Invoke-Tool 'as dscr.a51' @($ASM, '-logs', 'dscr.a51') $Build | Out-Null
$rels = @('dscr.rel')

Write-Host 'compiling firmware ...'
Invoke-Tool 'cc harness_fx2.c' @($CC, '-mmcs51', '-c', '-I', $Include,
    (Join-Path $Src 'harness_fx2.c'), '-o', (Join-Path $Build 'harness_fx2.rel')) $Build | Out-Null
$rels += 'harness_fx2.rel'

Write-Host 'linking ...'
# NOTE: each -Wl option must arrive as ONE argv element ("-Wl-b AREA=addr"),
# otherwise SDCC treats "AREA=addr" as an input file.
$linkFlags = @(
    '-mmcs51',
    '--code-size', '0x3000',
    '--xram-loc',  '0x3000',
    '--xram-size', '0x0E00',
    '-Wl-b DSCR_AREA=0x3e00',
    '-Wl-b INT2JT=0x3f00'
)
$Ihx = Join-Path $Build 'harness_fx2.ihx'
Invoke-Tool 'link' (@($CC) + $linkFlags + @('-o', $Ihx) + $rels + @('fx2.lib', '-L', $LibBuild)) $Build | Out-Null
if (-not (Test-Path -LiteralPath $Ihx)) {
    # sdcc 4.5 exits 0 even when the linker rejects its input; the missing
    # artifact is the only reliable failure signal.
    throw "link produced no $Ihx - see the linker output above"
}

# ---------------------------------------------------------------- artifacts --
# packihx normalises the image; makebin gives a raw binary for size reports.
$IhxPacked = Join-Path $Build 'harness_fx2.packed.ihx'
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location -LiteralPath $Build
try {
    & $Packihx 'harness_fx2.ihx' 2>&1 | Set-Content -LiteralPath 'harness_fx2.packed.ihx'
    if ($LASTEXITCODE -ne 0) { Write-Warning 'packihx failed; keeping the raw ihx' }
    $Bin = Join-Path $Build 'harness_fx2.bin'
    & $Makebin -p 'harness_fx2.ihx' $Bin 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning 'makebin failed (the .ihx is the artifact)' }
} finally {
    $ErrorActionPreference = $previous
    Pop-Location
}

Write-Host ''
Write-Host "artifact  : $Ihx ($((Get-Item -LiteralPath $Ihx).Length) bytes)"
if (Test-Path -LiteralPath $IhxPacked) {
    Write-Host "packed    : $IhxPacked ($((Get-Item -LiteralPath $IhxPacked).Length) bytes)"
}
if (Test-Path -LiteralPath (Join-Path $Build 'harness_fx2.bin')) {
    $bin = Join-Path $Build 'harness_fx2.bin'
    Write-Host "binary    : $bin ($((Get-Item -LiteralPath $bin).Length) bytes)"
}

# Memory areas from the map file, so an overflow is obvious at a glance.
$map = Join-Path $Build 'harness_fx2.map'
if (Test-Path -LiteralPath $map) {
    Write-Host ''
    Write-Host 'memory areas:'
    Get-Content -LiteralPath $map | Select-String -Pattern 'DSCR_AREA|INT2JT|CSEG|XSEG|XISEG' |
        Select-Object -First 12 | ForEach-Object { Write-Host ('   ' + $_.Line.Trim()) }
}

Write-Host ''
Write-Host 'next: python -m fx2_harness load'
