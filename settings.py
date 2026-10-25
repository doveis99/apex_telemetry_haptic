"""Persist effect gains and app options in %APPDATA%\\ATSHaptics\\config.json.

Layout: gain fields at the top level plus an "app" object and the motors'
"eq" calibration (tone.py calibrate). Each save rewrites only its own part so
they never clobber each other. The
packaged exe may live in a read-only folder, hence the per-user location;
a config.json left next to the scripts by earlier versions is moved over
once.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from effects import Gains

CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "ATSHaptics" / "config.json"
LEGACY_CONFIG_PATH = Path(__file__).with_name("config.json")


def _migrate_legacy() -> None:
    if CONFIG_PATH.exists() or not LEGACY_CONFIG_PATH.is_file():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LEGACY_CONFIG_PATH, CONFIG_PATH)
    LEGACY_CONFIG_PATH.unlink()


@dataclass
class AppOptions:
    auto_start_on_game: bool = False
    language: str = ""  # i18n code; empty follows the Windows display language
    debug_log: bool = False  # write every output block to a CSV (blocklog.py)


def _read(path: Path | None) -> dict:
    if path is None:
        _migrate_legacy()
    path = path or CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # missing, unreadable, not UTF-8 or not JSON: start from defaults
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path | None, data: dict) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside it and swap in, so a crash or power cut mid-write never
    # leaves a truncated file that would reset every setting.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _set_fields(target: object, stored: dict) -> None:
    """Copy the stored values that fit; a hand-edited one of the wrong type keeps its default."""
    for f in fields(target):
        if f.name not in stored:
            continue
        current, value = getattr(target, f.name), stored[f.name]
        try:
            if isinstance(current, dict):  # keep defaults for keys an older config lacks
                current.update(value if isinstance(value, dict) else {})
            elif isinstance(current, list):
                setattr(target, f.name, [str(item) for item in value] if isinstance(value, list) else current)
            else:
                setattr(target, f.name, type(current)(value))
        except (TypeError, ValueError):
            pass


def load_gains(path: Path | None = None) -> Gains:
    gains = Gains()
    _set_fields(gains, _read(path))
    return gains


def save_gains(gains: Gains, path: Path | None = None) -> None:
    data = _read(path)
    data.update(asdict(gains))
    _write(path, data)


def load_eq(path: Path | None = None) -> dict:
    """The player's own motor calibration, {channel: {Hz: gain}}; a motor not in it gets Equalizer.DEFAULT."""
    stored = _read(path).get("eq", {})
    return stored if isinstance(stored, dict) else {}


def save_eq(curves: dict, path: Path | None = None) -> None:
    data = _read(path)
    data["eq"] = curves
    _write(path, data)


def load_app_options(path: Path | None = None) -> AppOptions:
    options = AppOptions()
    stored = _read(path).get("app", {})
    _set_fields(options, stored if isinstance(stored, dict) else {})
    return options


def save_app_options(options: AppOptions, path: Path | None = None) -> None:
    data = _read(path)
    data["app"] = asdict(options)
    _write(path, data)
