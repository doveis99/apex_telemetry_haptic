"""Find ATS/ETS2 installs and make sure a compatible telemetry plugin is present.

The haptics read the shared memory of a RenCloud scs-sdk-plugin (revision
12) build. TruckDash ships such a build under its own mapping name; SimHub's
plugin uses yet another layout revision that has not been verified. When no
compatible plugin is found, the bundled RenCloud scs-telemetry.dll (MIT,
V.1.12.1) can be copied into the game's plugins folder.
"""

from __future__ import annotations

import re
import shutil
import sys
import winreg
from dataclasses import dataclass, field
from pathlib import Path

STEAM_APPS = {"ATS": (270880, "American Truck Simulator"), "ETS2": (227300, "Euro Truck Simulator 2")}
PLUGIN_FILE = "scs-telemetry.dll"
# shared-memory name inside the DLL (UTF-16) -> provider
PROVIDERS = {"SCSTelemetry": "RenCloud", "TruckDashTelemetry": "TruckDash", "SHSCSTelemetry": "SimHub"}
COMPATIBLE = {"RenCloud", "TruckDash"}


def resource_dir() -> Path:
    """Folder holding bundled files, both from source and in the PyInstaller build."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def bundled_plugin() -> Path:
    return resource_dir() / "third_party" / "scs-sdk-plugin" / PLUGIN_FILE


@dataclass
class GameInstall:
    key: str  # "ATS" or "ETS2"
    name: str
    path: Path
    plugins: dict[str, str] = field(default_factory=dict)  # dll file name -> provider

    @property
    def plugins_dir(self) -> Path:
        return self.path / "bin" / "win_x64" / "plugins"

    @property
    def compatible(self) -> bool:
        return any(provider in COMPATIBLE for provider in self.plugins.values())

    def describe(self) -> str:
        if not self.plugins:
            return "텔레메트리 플러그인 없음"
        names = ", ".join(sorted(set(self.plugins.values())))
        return f"{names}" + ("" if self.compatible else " (호환 미확인)")


def _steam_root() -> Path | None:
    for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                return Path(winreg.QueryValueEx(handle, value)[0])
        except OSError:
            continue
    return None


def _library_folders(steam: Path) -> list[Path]:
    folders = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return folders
    for raw in re.findall(r'"path"\s+"([^"]+)"', text):
        path = Path(raw.replace("\\\\", "\\"))
        if path not in folders:
            folders.append(path)
    return folders


def scan_plugins(plugins_dir: Path) -> dict[str, str]:
    found = {}
    if not plugins_dir.is_dir():
        return found
    for dll in plugins_dir.glob("*.dll"):
        try:
            data = dll.read_bytes()
        except OSError:
            continue
        for mapping, provider in PROVIDERS.items():
            if ("Local\\" + mapping).encode("utf-16-le") in data:
                found[dll.name] = provider
                break
    return found


def game_from_folder(folder: Path) -> GameInstall | None:
    """Accept a game folder picked by hand (non-Steam or unusual layouts)."""
    for key, (_, name) in STEAM_APPS.items():
        exe = "amtrucks.exe" if key == "ATS" else "eurotrucks2.exe"
        if (folder / "bin" / "win_x64" / exe).is_file():
            game = GameInstall(key, name, folder)
            game.plugins = scan_plugins(game.plugins_dir)
            return game
    return None


def find_games() -> list[GameInstall]:
    steam = _steam_root()
    if steam is None:
        return []
    games = []
    for library in _library_folders(steam):
        for key, (app_id, name) in STEAM_APPS.items():
            manifest = library / "steamapps" / f"appmanifest_{app_id}.acf"
            try:
                match = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            path = library / "steamapps" / "common" / (match.group(1) if match else name)
            if path.is_dir() and all(g.key != key for g in games):
                game = GameInstall(key, name, path)
                game.plugins = scan_plugins(game.plugins_dir)
                games.append(game)
    return games


def install(game: GameInstall) -> Path:
    """Copy the bundled plugin into the game. Raises PermissionError if the
    folder needs administrator rights."""
    source = bundled_plugin()
    if not source.is_file():
        raise FileNotFoundError(f"번들 플러그인이 없습니다: {source}")
    game.plugins_dir.mkdir(parents=True, exist_ok=True)
    target = game.plugins_dir / PLUGIN_FILE
    shutil.copyfile(source, target)
    game.plugins = scan_plugins(game.plugins_dir)
    return target
