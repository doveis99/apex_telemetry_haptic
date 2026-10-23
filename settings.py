"""Persist effect gains and app options in %APPDATA%\\ATSHaptics\\config.json.

Layout: gain fields at the top level plus an "app" object. Each save
rewrites only its own part so the two never clobber each other. The
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
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path | None, data: dict) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_gains(path: Path | None = None) -> Gains:
    gains = Gains()
    data = _read(path)
    for f in fields(Gains):
        if f.name in data:
            current = getattr(gains, f.name)
            if isinstance(current, dict):  # keep defaults for keys an older config lacks
                current.update(data[f.name] if isinstance(data[f.name], dict) else {})
            else:
                setattr(gains, f.name, type(current)(data[f.name]))
    return gains


def save_gains(gains: Gains, path: Path | None = None) -> None:
    data = _read(path)
    data.update(asdict(gains))
    _write(path, data)


def load_app_options(path: Path | None = None) -> AppOptions:
    options = AppOptions()
    stored = _read(path).get("app", {})
    for f in fields(AppOptions):
        if f.name in stored:
            setattr(options, f.name, type(getattr(options, f.name))(stored[f.name]))
    return options


def save_app_options(options: AppOptions, path: Path | None = None) -> None:
    data = _read(path)
    data["app"] = asdict(options)
    _write(path, data)
