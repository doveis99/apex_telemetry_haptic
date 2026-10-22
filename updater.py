"""Check GitHub for a newer release and install it over the running copy.

Only the packaged exe updates itself: the release zip is downloaded, checked
against the SHA-256 digest GitHub publishes for the asset and unpacked to
%TEMP%. A small PowerShell script then waits until every ATSHaptics.exe from
the install folder has exited (the window and a Steam --launch wrapper),
copies the new files over it and starts the new version. Running from
source, or from a folder that is not writable, only points at the release
page.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import __version__

REPO = "doveis99/apex_telemetry_haptic"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_NAME = "ATSHaptics.exe"
TIMEOUT_S = 10
CHUNK = 256 * 1024

UPDATE_SCRIPT = r"""
param([string]$Source, [string]$Target, [string]$Log)
$exe = Join-Path $Target "ATSHaptics.exe"
"waiting for $exe to exit" | Out-File $Log
for ($i = 0; $i -lt 240; $i++) {
    $running = Get-Process ATSHaptics -ErrorAction SilentlyContinue | Where-Object { $_.Path -ieq $exe }
    if (-not $running) { break }
    Start-Sleep -Milliseconds 500
}
# _internal is mirrored so files dropped by the new version disappear; the
# top level is only overwritten, never purged.
robocopy (Join-Path $Source "_internal") (Join-Path $Target "_internal") /MIR /R:10 /W:1 /NP /NFL /NDL | Out-File $Log -Append
$internal = $LASTEXITCODE
robocopy $Source $Target /E /XD _internal /R:10 /W:1 /NP /NFL /NDL | Out-File $Log -Append
"robocopy exit codes: $internal, $LASTEXITCODE (below 8 is success)" | Out-File $Log -Append
Start-Process $exe
"""


@dataclass
class Release:
    tag: str
    page: str
    notes: str
    asset_url: str | None
    asset_size: int
    sha256: str | None

    @property
    def version(self) -> str:
        return self.tag.lstrip("vV")


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text))


def _get(url: str):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": f"ATSHaptics/{__version__}"})
    return urllib.request.urlopen(request, timeout=TIMEOUT_S)


def latest_release() -> Release:
    with _get(API_URL) as response:
        data = json.load(response)
    asset = next((a for a in data.get("assets", [])
                  if a["name"].startswith("ATSHaptics-") and a["name"].endswith(".zip")), None)
    digest = (asset or {}).get("digest") or ""
    return Release(tag=data["tag_name"], page=data["html_url"], notes=(data.get("body") or "").strip(),
                   asset_url=asset["browser_download_url"] if asset else None,
                   asset_size=asset["size"] if asset else 0,
                   sha256=digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else None)


def newer_release() -> Release | None:
    """The latest release if it is newer than this build; raises on network errors."""
    release = latest_release()
    return release if parse_version(release.tag) > parse_version(__version__) else None


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def can_self_update(release: Release) -> bool:
    return (getattr(sys, "frozen", False) and release.asset_url is not None and release.sha256 is not None
            and os.access(install_dir(), os.W_OK))


def download(release: Release, progress: Callable[[float], None] | None = None) -> Path:
    """Fetch, verify and unpack the release zip; return the folder holding the new exe."""
    work = Path(tempfile.gettempdir()) / f"ATSHaptics-update-{release.version}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    archive = work / "release.zip"
    digest = hashlib.sha256()
    done = 0
    with _get(release.asset_url) as response, archive.open("wb") as out:
        while chunk := response.read(CHUNK):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress and release.asset_size:
                progress(done / release.asset_size)
    if digest.hexdigest() != release.sha256:
        raise RuntimeError("다운로드한 파일의 해시가 릴리스와 다릅니다")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work / "unpacked")
    exe = next((work / "unpacked").rglob(EXE_NAME), None)
    if exe is None or not (exe.parent / "_internal").is_dir():
        raise RuntimeError(f"릴리스 zip에 {EXE_NAME}이 없습니다")
    return exe.parent


def apply_on_exit(source: Path) -> Path:
    """Start the copy script; it waits for this program to exit. Returns its log path."""
    script = source.parent / "apply-update.ps1"
    script.write_text(UPDATE_SCRIPT, encoding="utf-8-sig")
    log = source.parent / "apply-update.log"
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(script), "-Source", str(source), "-Target", str(install_dir()),
                      "-Log", str(log)],
                     creationflags=subprocess.DETACHED_PROCESS)
    return log
