"""Prune target/ to what this project's builds use now.

cargo never deletes outputs it stops using. Each change to Cargo.toml, a dependency, the
toolchain or the repository's location builds a new copy of every crate beside the old one,
and rustc's incremental cache leaves a new folder for our own crates the same way; the Rust
port grew target/ to 16 GB like this. This runs the builds the project makes, asks cargo
which files they use, and deletes the copies nothing uses:

    python tools/prune_target.py            # prune
    python tools/prune_target.py --dry-run  # list what would go, delete nothing

The builds, in the order they run:

    cargo build --release --bins --examples   build.ps1, the README's cargo run --release
    cargo build                                plain debug builds
    cargo test --no-run                        the tests (profile.test, its own crate copies)

Nothing is rebuilt from scratch. To tell the incremental caches in use from stale ones, our
own crates are recompiled once: src/lib.rs and build.rs are touched, then given their old
times back so the release build stays fresh. A cache that recompile did not write to is
stale. Nothing written after the script started is deleted, so a build another session runs
meanwhile keeps its files. After pruning, the builds run again and report anything cargo had
to rebuild. cargo's .fingerprint records (a few MB) and the examples folders stay.

cargo build and cargo test rebuild our three programs whenever they alternate: profile.test
builds them for the tests under the same file names the debug build uses. The check leaves
those out.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The release build first: the touch below makes everything after it recompile our crates.
BUILDS = [
    ["build", "--release", "--bins", "--examples"],
    ["build"],
    ["test", "--no-run"],
]
TOUCHED = ["src/lib.rs", "build.rs"]
OURS = set()  # package ids of this workspace, from cargo metadata

# the unit hash cargo puts in file and folder names: libfoo-0123456789abcdef.rlib
HASH = re.compile(r"-([0-9a-f]{16})(?=[./\\]|$)")


class BuildFailed(Exception):
    pass


def cargo_json(args):
    """Run one cargo build; return the paths of the files it uses and the units it rebuilt."""
    cmd = ["cargo", *args, "--message-format=json-render-diagnostics"]
    print(">", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    if proc.returncode:
        raise BuildFailed(" ".join(cmd))
    paths, rebuilt = [], []
    for line in proc.stdout.splitlines():
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        if msg.get("reason") == "compiler-artifact":
            paths += msg["filenames"]
            if msg.get("executable"):
                paths.append(msg["executable"])
            target = msg["target"]
            if not msg["fresh"] and not (target["kind"] == ["bin"] and msg["package_id"] in OURS):
                rebuilt.append(target["name"])
        elif msg.get("reason") == "build-script-executed":
            paths.append(msg["out_dir"])  # build/<pkg>-<hash>/out, where the script ran
    return paths, rebuilt


def size_of(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def newest_mtime(folder):
    """The latest write to an incremental crate folder: rustc adds a session folder in it and
    removes the old one on every compile."""
    return max([folder.stat().st_mtime] + [c.stat().st_mtime for c in folder.iterdir()])


def remove(path):
    def writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.is_dir():
        shutil.rmtree(path, onexc=writable)
    else:
        path.unlink()


def human(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list what would go, delete nothing")
    args = ap.parse_args()

    meta = json.loads(subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=ROOT, stdout=subprocess.PIPE, check=True, text=True, encoding="utf-8").stdout)
    OURS.update(meta["workspace_members"])
    build_dir = Path(meta.get("build_directory") or meta["target_directory"])
    before = size_of(build_dir)

    run_start = time.time()
    try:
        paths, _ = cargo_json(BUILDS[0])
        saved = {f: os.stat(ROOT / f) for f in TOUCHED}
        touched_at = time.time()
        touched = {}
        try:
            for f in TOUCHED:
                os.utime(ROOT / f)
            touched = {f: os.stat(ROOT / f).st_mtime_ns for f in TOUCHED}
            for build in BUILDS[1:]:
                paths += cargo_json(build)[0]
        finally:
            for f, st in saved.items():
                # a file edited meanwhile keeps its new time, or cargo would miss the edit
                if os.stat(ROOT / f).st_mtime_ns == touched.get(f):
                    os.utime(ROOT / f, ns=(st.st_atime_ns, st.st_mtime_ns))
                else:
                    print(f"{f} changed during the run; its time is left as it is")
    except BuildFailed as e:
        sys.exit(f"{e} failed; nothing deleted")

    keep = set()
    profiles = set()
    for p in paths:
        keep.update(HASH.findall(p))
        try:
            profiles.add(Path(p).relative_to(build_dir).parts[0])
        except ValueError:
            pass

    doomed = {"deps": [], "build": [], "incremental": []}
    for prof in sorted(profiles):
        pdir = build_dir / prof
        if (pdir / "deps").is_dir():
            for f in (pdir / "deps").iterdir():
                hashes = HASH.findall(f.name)
                if hashes and not keep.intersection(hashes) and f.stat().st_mtime < run_start:
                    doomed["deps"].append(f)
        if (pdir / "build").is_dir():
            for d in (pdir / "build").iterdir():
                hashes = HASH.findall(d.name)
                if (d.is_dir() and hashes and not keep.intersection(hashes)
                        and newest_mtime(d) < run_start):
                    doomed["build"].append(d)
        inc = pdir / "incremental"
        if inc.is_dir():
            folders = [d for d in inc.iterdir() if d.is_dir()]
            stale = [d for d in folders if newest_mtime(d) < touched_at]
            if folders and len(stale) == len(folders):
                # the recompile wrote to none of them: incremental is off, so we can't tell
                print(f"{inc}: no cache was written to, left as it is")
            else:
                doomed["incremental"] += stale

    total = 0
    for area, items in doomed.items():
        freed = sum(size_of(p) for p in items)
        total += freed
        print(f"{area:12} {len(items):5} stale  {human(freed)}")
    print(f"{'total':12} {sum(map(len, doomed.values())):5} stale  {human(total)}"
          f"  (of {human(before)})")

    if args.dry_run:
        return
    for items in doomed.values():
        for p in items:
            remove(p)
    print(f"target is now {human(size_of(build_dir))}")

    print("checking that the builds are still fresh")
    rebuilt = []
    try:
        for build in BUILDS:
            rebuilt += cargo_json(build)[1]
    except BuildFailed as e:
        sys.exit(f"{e} failed after pruning")
    if rebuilt:
        print("rebuilt (pruned too much?):", ", ".join(sorted(set(rebuilt))))
    else:
        print("all fresh: nothing rebuilt but our programs (see the note at the top)")


if __name__ == "__main__":
    main()
