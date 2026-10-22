"""Persist effect gains in config.json next to the scripts."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from effects import Gains

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_gains(path: Path = CONFIG_PATH) -> Gains:
    gains = Gains()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return gains
    for f in fields(Gains):
        if f.name in data:
            setattr(gains, f.name, type(getattr(gains, f.name))(data[f.name]))
    return gains


def save_gains(gains: Gains, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(asdict(gains), indent=2), encoding="utf-8")
