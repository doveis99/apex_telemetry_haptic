# Build the telemetry plugin (plugin\) with MSVC, run its test host against the result, and
# put the DLL where the app embeds it from: resources\scs-sdk-plugin\scs-telemetry.dll.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_plugin.ps1
#
# Needs the Visual Studio C++ build tools (the ones Rust's MSVC target already needs). The CRT
# is linked in (/MT), so the game needs no particular Visual C++ runtime for it, and /Brepro
# keeps time stamps out: the same source and compiler give the same DLL.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$plugin = Join-Path $repo "plugin"
$out = Join-Path $repo "target\plugin"
New-Item -ItemType Directory -Force $out | Out-Null

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found: install the Visual Studio C++ build tools" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "no Visual Studio install with the C++ x64 tools" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"

$include = "/I`"$plugin\scs_sdk\include`" /I`"$plugin\inc`""
$sources = (Get-ChildItem (Join-Path $plugin "src") -Filter *.cpp | ForEach-Object { "`"$($_.FullName)`"" }) -join " "
$dll = "cl /nologo /std:c++17 /O2 /MT /EHsc /W3 /Brepro /DWIN32 /DNDEBUG /D_WINDOWS /D_USRDLL /DTELEMETRY_EXPORTS " +
       "/DUNICODE /D_UNICODE /D_CRT_SECURE_NO_WARNINGS $include $sources /LD /Fo`"$out\\`" /Fe`"$out\scs-telemetry.dll`" " +
       "/link /DEF:`"$plugin\scs-telemetry.def`" /Brepro /OPT:REF /OPT:ICF /IMPLIB:`"$out\scs-telemetry.lib`""
$hostExe = "cl /nologo /std:c++17 /O2 /MT /EHa /W3 $include `"$plugin\test\host.cpp`" /Fo`"$out\\`" /Fe`"$out\host.exe`""

cmd /c "`"$vcvars`" >nul 2>&1 && $dll && $hostExe"
if ($LASTEXITCODE -ne 0) { throw "the plugin did not build" }

foreach ($game in "--ets2", "--ats") {
    & "$out\host.exe" "$out\scs-telemetry.dll" $game --check
    if ($LASTEXITCODE -ne 0) { throw "the built plugin failed its check ($game)" }
}

Copy-Item "$out\scs-telemetry.dll" (Join-Path $repo "resources\scs-sdk-plugin\scs-telemetry.dll") -Force
Copy-Item (Join-Path $plugin "LICENSE") (Join-Path $repo "resources\scs-sdk-plugin\LICENSE") -Force
$hash = (Get-FileHash "$out\scs-telemetry.dll" -Algorithm SHA256).Hash
"scs-telemetry.dll -> resources\scs-sdk-plugin ($((Get-Item "$out\scs-telemetry.dll").Length) bytes, SHA-256 $hash)"
