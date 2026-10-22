"""Steam launch-option wrapper: open the haptics window, then run the game.

Set the game's Steam launch options (ATS or ETS2) to:

  "C:\\path\\to\\apex_telemetry_haptic\\.venv\\Scripts\\pythonw.exe" "C:\\path\\to\\apex_telemetry_haptic\\launch.py" %command%

Steam replaces %command% with the game executable and its arguments. The
window starts minimized, takes the controller once the game is up and
closes when the game quits; this wrapper waits for the game so Steam keeps
tracking it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    pythonw = HERE / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(HERE / "gui.py"), "--autostart", "--exit-with-game", "--minimized"],
                     cwd=HERE, creationflags=subprocess.DETACHED_PROCESS)
    if len(sys.argv) < 2:
        return 0  # started by hand without a game command: just the window
    return subprocess.call(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
