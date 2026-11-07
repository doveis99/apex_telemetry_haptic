"""Detect whether ATS or ETS2 is running (Windows process list via Toolhelp).

Only used while the haptics program itself is open: it tells the program
when to take the controller and when the game has quit. Nothing stays
resident after the program exits.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

GAME_PROCESSES = {"amtrucks.exe": "ATS", "eurotrucks2.exe": "ETS2"}
POLL_S = 2.0  # one snapshot costs ~10 ms, so keep it off the 8 ms audio loop

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = wintypes.HANDLE(-1).value


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
_kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def running_game() -> str | None:
    """Return "ATS" or "ETS2" if one of the games is running, else None."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        return None
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            game = GAME_PROCESSES.get(entry.szExeFile.lower())
            if game:
                return game
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        _kernel32.CloseHandle(snapshot)


class GameWatcher:
    """Background thread keeping `game` current every POLL_S seconds."""

    def __init__(self) -> None:
        self.game: str | None = running_game()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            self.game = running_game()

    def close(self) -> None:
        self._stop.set()
