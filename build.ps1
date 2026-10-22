# Build ATSHaptics.exe with PyInstaller and pack a release zip.
# The zip carries the corresponding source (GPL-3.0-or-later) under source\.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv .venv }
& $py -m pip install -q -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE) { throw "pip install failed" }
$version = (& $py -c "from version import __version__; print(__version__)").Trim()

Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue
& $py -m PyInstaller --noconfirm --clean --windowed --name ATSHaptics `
    --add-data "third_party\scs-sdk-plugin;third_party\scs-sdk-plugin" app.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$out = "dist\ATSHaptics"
Copy-Item README.md, LICENSE, THIRD_PARTY_NOTICES.md $out

# Tracked and new (not ignored) files: what the repository would contain.
$src = Join-Path $out "source"
git ls-files --cached --others --exclude-standard | ForEach-Object {
    $dest = Join-Path $src $_
    New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
    Copy-Item -LiteralPath $_ -Destination $dest
}

$zip = "dist\ATSHaptics-$version.zip"
Compress-Archive -Path $out -DestinationPath $zip -Force
Write-Host "Built $zip"
