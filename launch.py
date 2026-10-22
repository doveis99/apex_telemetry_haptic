"""Steam launch-option wrapper when running from source.

Set the game's Steam launch options (ATS or ETS2) to:

  "<repo>\\.venv\\Scripts\\pythonw.exe" "<repo>\\launch.py" %command%

The packaged build does the same with `ATSHaptics.exe --launch %command%`.
"""

from __future__ import annotations

import sys

from app import launch

if __name__ == "__main__":
    sys.exit(launch(sys.argv[1:]))
