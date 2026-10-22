"""Entry point for ATSHaptics.exe (and `pythonw app.py` from source).

  ATSHaptics.exe                       tuning window
  ATSHaptics.exe --launch %command%    Steam launch option: open the window
                                       minimized, then run the game and wait
  ATSHaptics.exe --dry-run | --autostart | --exit-with-game | --minimized
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WINDOW_FLAGS = ["--autostart", "--exit-with-game", "--minimized"]


def _window_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *WINDOW_FLAGS]
    here = Path(__file__).resolve().parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.is_file() else sys.executable), str(here / "app.py"), *WINDOW_FLAGS]


def _start_outside_steam(command: list[str]) -> bool:
    """Start command through WMI so it is not a child of this process.

    Steam injects its overlay into the launch-option process and everything
    it starts. With Steam Input on for the game, the overlay hides the
    controller's HID interfaces from those processes, so a window started
    as our child never finds the pad. A WMI-created process sits outside
    that tree.
    """
    line = subprocess.list2cmdline(command).replace("'", "''")
    folder = str(Path(command[0]).parent).replace("'", "''")
    script = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              f"@{{CommandLine='{line}'; CurrentDirectory='{folder}'}}; exit $r.ReturnValue")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def launch(game_command: list[str]) -> int:
    """Start the window detached, then run the game so Steam keeps tracking it."""
    command = _window_command()
    if not _start_outside_steam(command):
        subprocess.Popen(command, creationflags=subprocess.DETACHED_PROCESS)
    if not game_command:
        return 0
    return subprocess.call(game_command)


def selftest() -> int:
    """Check the bundle without opening a window; report to %TEMP%\\ATSHaptics-selftest.txt."""
    import os
    import traceback
    lines = []
    try:
        import hid
        import plugin_install
        from version import __version__
        import gui  # noqa: F401 - import check for tkinter and the window code
        lines.append(f"version {__version__}, frozen={getattr(sys, 'frozen', False)}")
        lines.append(f"bundled plugin: {plugin_install.bundled_plugin().is_file()}")
        lines.append(f"apex 6 interfaces: {sum(1 for d in hid.enumerate(0x37D7, 0x2502) if d['usage_page'] == 0xFFA0)}")
        for game in plugin_install.find_games():
            lines.append(f"{game.key}: {game.path} -> {game.describe()}")
        status = 0
    except Exception:
        lines.append(traceback.format_exc())
        status = 1
    report = Path(os.environ.get("TEMP", ".")) / "ATSHaptics-selftest.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    return status


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--launch":
        return launch(argv[1:])
    if argv and argv[0] == "--selftest":
        return selftest()
    import gui  # tkinter only when a window is wanted
    gui.main(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
