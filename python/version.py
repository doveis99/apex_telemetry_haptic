"""The version, kept in Cargo.toml alone: the Rust build compiles it in, build.ps1 names the
release zip after it and the Python reference implementation reads it from there."""

import tomllib
from pathlib import Path

with (Path(__file__).resolve().parent.parent / "Cargo.toml").open("rb") as _manifest:
    __version__ = tomllib.load(_manifest)["package"]["version"]
