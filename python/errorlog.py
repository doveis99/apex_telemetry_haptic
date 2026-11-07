"""Unexpected errors, appended to %APPDATA%\\ATSHaptics\\logs\\error.log.

The packaged window has no console: without this a traceback vanishes and
the status line only shows the exception's message. Past MAX_BYTES the file
starts over, keeping the previous one as error.log.1.
"""

from __future__ import annotations

import traceback
from datetime import datetime

from blocklog import LOG_DIR
from version import __version__

ERROR_LOG = LOG_DIR / "error.log"
MAX_BYTES = 1_000_000


def log_exception(exc: BaseException, where: str) -> None:
    """Append exc with its traceback. Never raises: it runs inside error handling."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG.is_file() and ERROR_LOG.stat().st_size > MAX_BYTES:
            ERROR_LOG.replace(ERROR_LOG.with_name(ERROR_LOG.name + ".1"))
        with ERROR_LOG.open("a", encoding="utf-8") as out:
            out.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} ATSHaptics {__version__} · {where}\n")
            out.write("".join(traceback.format_exception(exc)))
    except OSError:
        pass
