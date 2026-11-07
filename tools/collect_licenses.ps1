# Write the license texts of everything compiled into the release executables to one
# text file for the release zip: the Rust crates they link and the telemetry plugin
# DLL that ATSHaptics.exe embeds.
#
#   powershell -ExecutionPolicy Bypass -File tools\collect_licenses.ps1 OUT.txt VERSION
#
# The crates are cargo tree's normal (not build or proc-macro) dependencies on Windows,
# as Cargo.lock pins them; the texts come from each crate's package in cargo's
# registry, so run it after a build has fetched them. A text shared by several crates
# is written once, under all their names.

param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot

$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE ".cargo" }
$registries = @(Get-ChildItem (Join-Path $cargoHome "registry\src") -Directory | ForEach-Object { $_.FullName })

$tree = cargo tree --manifest-path (Join-Path $repo "Cargo.toml") -e "normal,no-proc-macro" `
    --target x86_64-pc-windows-msvc --prefix none --format "{p}|{l}|{r}"
if ($LASTEXITCODE) { throw "cargo tree failed" }

$crates = $tree |
    ForEach-Object { $_ -replace ' \(\*\)$', '' } |
    Where-Object { $_ -and $_ -notmatch '^\S+ v\S+ \(' } |   # the app itself: "name v1.0 (path)"
    Sort-Object -Unique |
    ForEach-Object {
        $package, $license, $repository = $_ -split '\|', 3
        $name, $ver = $package -split ' v', 2
        [pscustomobject]@{ Name = $name; Version = $ver; License = $license; Repository = $repository }
    }

$sep = "-" * 78
$texts = [ordered]@{}   # normalised text -> the "crate version (file)" names it came from
$bare = @()             # crates whose package carries no license file

function Add-Text([string]$Path, [string]$From) {
    $text = ([IO.File]::ReadAllText($Path) -replace "`r`n", "`n").Trim()
    if (-not $texts.Contains($text)) { $texts[$text] = New-Object System.Collections.ArrayList }
    [void]$texts[$text].Add($From)
}

Add-Text (Join-Path $repo "resources\scs-sdk-plugin\LICENSE") "scs-sdk-plugin V.1.12.1, scs-telemetry.dll (LICENSE)"

foreach ($crate in $crates) {
    $dir = $registries | ForEach-Object { Join-Path $_ "$($crate.Name)-$($crate.Version)" } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $dir) { throw "no sources for $($crate.Name) $($crate.Version) under $cargoHome\registry\src" }
    $files = @(Get-ChildItem -LiteralPath $dir -File |
        Where-Object { $_.Name -match '^(licen[cs]e|copying|copyright|notice|unlicense)' })
    $folder = Join-Path $dir "LICENSES"
    if (Test-Path -LiteralPath $folder) { $files += @(Get-ChildItem -LiteralPath $folder -File) }
    if ($crate.License -match 'LicenseRef-Slint') {
        # Slint is offered under GPL-3.0 or two Slint licenses; this program takes GPL-3.0.
        $files = @($files | Where-Object { $_.Name -like "GPL-3.0*" })
    }
    if ($crate.Name -eq "hidapi") {
        # the crate compiles the HIDAPI C library, which has its own licenses
        $files += @(Get-ChildItem -LiteralPath (Join-Path $dir "etc\hidapi") -File -Filter "LICENSE*")
    }
    if (-not $files) {
        $manifest = [IO.File]::ReadAllText((Join-Path $dir "Cargo.toml"))
        $authors = if ($manifest -match '(?s)\nauthors\s*=\s*\[(.*?)\]') {
            (@($Matches[1] -split "`n" | ForEach-Object { $_.Trim().Trim(',').Trim('"') }) -ne "") -join ", "
        } else { "" }
        $bare += "  $($crate.Name) $($crate.Version): $($crate.License)" + $(if ($authors) { ", by $authors" } else { "" })
        continue
    }
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($dir.Length + 1)
        Add-Text $file.FullName "$($crate.Name) $($crate.Version) ($relative)"
    }
}

$lines = New-Object System.Collections.ArrayList
$add = { param($text) [void]$lines.Add($text) }
& $add "Third-party licenses in ATS Haptics $Version"
& $add ""
& $add "ATS Haptics is licensed under GPL-3.0-or-later (LICENSE); THIRD_PARTY_NOTICES.md says"
& $add "what it includes and why. The executables link the Rust crates listed below, and"
& $add "ATSHaptics.exe embeds RenCloud's scs-telemetry.dll, which its plugin window installs into"
& $add "the game. Each license text follows once, under every package it came from."
& $add ""
& $add "Rust crates (version, license as the crate declares it, repository):"
foreach ($crate in $crates) {
    & $add ("  {0} {1}: {2}  {3}" -f $crate.Name, $crate.Version, $crate.License, $crate.Repository).TrimEnd()
}
if ($bare) {
    & $add ""
    & $add "These crates' packages carry no license file. The licenses they declare are the"
    & $add "standard texts that appear below under other crates:"
    $bare | ForEach-Object { & $add $_ }
}
foreach ($entry in $texts.GetEnumerator()) {
    & $add ""
    & $add $sep
    $entry.Value | ForEach-Object { & $add $_ }
    & $add $sep
    & $add ""
    & $add $entry.Key
}

$utf8 = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText($Out, (($lines -join "`n") -replace "`n", "`r`n") + "`r`n", $utf8)
Write-Host "$Out`: $($crates.Count) crates, $($texts.Count) license texts"
