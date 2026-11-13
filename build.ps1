# Build the executables with cargo and pack a release zip.
# The zip carries the corresponding source (GPL-3.0-or-later) under source\.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Needs the Rust toolchain (rustup, stable x86_64-pc-windows-msvc) and git.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The version is kept in Cargo.toml alone; the programs read it at compile time.
$version = (Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"' |
    Select-Object -First 1).Matches[0].Groups[1].Value

# inside the repository, where cargo finds .cargo\config.toml (the VC runtime linked in)
cargo build --release
if ($LASTEXITCODE) { throw "cargo build failed" }

Remove-Item dist -Recurse -Force -ErrorAction SilentlyContinue
$out = "dist\SCSHaptics"
New-Item -ItemType Directory $out | Out-Null
# the window, the command line and the debug-log summary; the plugin DLL is inside SCSHaptics.exe
Copy-Item target\release\SCSHaptics.exe, target\release\scs_haptics.exe, target\release\replay.exe $out
Copy-Item README.md, README.en.md, README.ja.md, README.zh.md, LICENSE, THIRD_PARTY_NOTICES.md $out
Copy-Item docs -Destination $out -Recurse  # screenshots the READMEs show
& powershell -NoProfile -ExecutionPolicy Bypass -File tools\collect_licenses.ps1 `
    (Join-Path $out "THIRD_PARTY_LICENSES.txt") $version
if ($LASTEXITCODE) { throw "collecting the license texts failed" }

# The in-app update takes a folder for an installed release only when it holds _internal, and
# a release zip only when its SCSHaptics.exe has that folder beside it (releases up to 0.19,
# a Python program, kept their runtime there). Compress-Archive leaves empty folders out, so
# it holds a note.
$internal = New-Item -ItemType Directory (Join-Path $out "_internal")
Set-Content -Encoding ASCII (Join-Path $internal "README.txt") @(
    "This folder is intentionally almost empty. Please keep it."
    ""
    "The in-app update knows an installed release by this folder, and accepts a new"
    "release only when the folder sits beside SCSHaptics.exe. Releases up to 0.19, a"
    "Python program then called ATS Haptics, kept their runtime here."
)

# Tracked and new (not ignored) files: what the repository would contain.
$src = Join-Path $out "source"
git ls-files --cached --others --exclude-standard |
    Where-Object { $_ -notmatch '^(target|tests/golden-data)/' -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    ForEach-Object {
        $dest = Join-Path $src $_
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item -LiteralPath $_ -Destination $dest
    }
if ($LASTEXITCODE) { throw "git ls-files failed" }

$zip = "dist\SCSHaptics-$version.zip"
Compress-Archive -Path $out -DestinationPath $zip -Force

# Check the layout the updaters rely on, in the zip itself.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $zip))
try { $entries = $archive.Entries | ForEach-Object { $_.FullName -replace '\\', '/' } } finally { $archive.Dispose() }
foreach ($need in "SCSHaptics/SCSHaptics.exe", "SCSHaptics/_internal/README.txt") {
    if ($entries -notcontains $need) { throw "$zip has no $need" }
}
Write-Host "Built $zip"
