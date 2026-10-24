"""Equal-strength calibration of the motors, for effects.Equalizer.

The voice coils resonate (grips near 65 Hz, triggers near 100 Hz), so the
same drive feels weaker off resonance. Each test tone follows the motor's
reference tone at the same drive and is made stronger or weaker by feel until
the two match; the factor that took is the gain the synth then gives that
frequency on that motor. tone.py calibrate runs it in a console and the
window in a dialog; both also take the answers from the pad's own buttons.
"""

from __future__ import annotations

import ctypes
import time
from typing import Callable

from apex6 import Apex6, Block
from effects import Equalizer
from tone import tone_renderer

REFERENCE = {"grip": 65.0, "trigger": 100.0}
FREQS = {"grip": (30.0, 40.0, 50.0, 80.0, 100.0, 130.0, 160.0, 200.0, 250.0),
         "trigger": (50.0, 65.0, 80.0, 130.0, 160.0, 200.0, 250.0)}
TARGETS = {"grip": "grips", "trigger": "triggers"}  # tone.py targets: both grips, both triggers
STEP_DB = 1.5
GAP_MS = 400  # silence between the reference and the test tone
AMP = 25.0  # reference strength in percent: up to 4x of it still fits in 100

# answers
STRONGER, WEAKER, SAME, REPLAY, CANCEL = "stronger", "weaker", "same", "replay", "cancel"


def step(gain: float, answer: str) -> float:
    """The test tone's next gain: STRONGER when it felt weaker than the reference, WEAKER when stronger."""
    factor = 10 ** (STEP_DB / 20)
    low, high = Equalizer.LIMITS
    return min(high, max(low, gain * factor if answer == STRONGER else gain / factor))


class Session:
    """Where a calibration stands: the motor and frequency being matched, the test gain, the results."""

    def __init__(self, channels: list[str], amp: float = AMP, seconds: float = 1.0) -> None:
        self.channels, self.amp, self.seconds = channels, amp, seconds
        self.steps = [(channel, freq) for channel in channels for freq in FREQS[channel]]
        self.index = 0
        self.gain = 1.0
        self.curves = {channel: {f"{REFERENCE[channel]:g}": 1.0} for channel in channels}
        self.finished: list[str] = []  # channels with every frequency matched

    @property
    def current(self) -> tuple[str, float] | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def at_limit(self) -> bool:
        return self.gain in Equalizer.LIMITS

    def answer(self, answer: str) -> str | None:
        """Take STRONGER, WEAKER or SAME; returns the channel SAME has just finished, if any."""
        if self.current is None:
            return None
        if answer in (STRONGER, WEAKER):
            self.gain = step(self.gain, answer)  # stays at a limit until SAME takes it
            return None
        if answer != SAME:
            return None
        channel, freq = self.current
        self.curves[channel][f"{freq:g}"] = round(self.gain, 3)
        self.index += 1
        self.gain = 1.0
        if self.current is None or self.current[0] != channel:
            self.finished.append(channel)
            return channel
        return None


def play(pad: Apex6, session: Session, listen: Callable[[], str | None] = lambda: None) -> str | None:
    """The reference, a pause and the test tone; an answer heard on the way stops them and is returned."""
    channel, freq = session.current
    target, reference = TARGETS[channel], REFERENCE[channel]
    heard: list[str] = []

    def listening(render: Callable[[int], Block | None]) -> Callable[[int], Block | None]:
        def render_or_stop(first: int) -> Block | None:
            if not heard:
                answer = listen()
                if answer:
                    heard.append(answer)
            return None if heard else render(first)
        return render_or_stop

    for render in (tone_renderer(target, reference, reference, session.amp, session.seconds),
                   lambda first: Block() if first < GAP_MS else None,
                   tone_renderer(target, freq, freq, min(100.0, session.amp * session.gain), session.seconds)):
        pad.stream(listening(render))
        if heard:
            return heard[0]
    return None


def wait(listen: Callable[[], str | None], cancelled: Callable[[], bool] = lambda: False) -> str:
    """Poll for the next answer; CANCEL once cancelled() says so."""
    while not cancelled():
        answer = listen()
        if answer:
            return answer
        time.sleep(0.01)
    return CANCEL


def save(session: Session) -> None:
    """Store the finished channels, keeping a motor measured before and not redone now."""
    from settings import load_eq, save_eq
    curves = load_eq()
    curves.update({channel: session.curves[channel] for channel in session.finished})
    save_eq(curves)


class _Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Gamepad)]


class PadButtons:
    """New presses on any XInput pad, which the APEX is in its usual Xbox mode.

    D-pad up: stronger, down: weaker, A: same, X: replay, B: cancel. XInput
    works whichever window has the focus. A button already held when this
    starts, or still held, counts once when it goes down again.
    """

    MAP = ((0x0001, STRONGER), (0x0002, WEAKER), (0x1000, SAME), (0x4000, REPLAY), (0x2000, CANCEL))

    def __init__(self) -> None:
        self._dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._dll = getattr(ctypes.windll, name)
                break
            except (AttributeError, OSError):
                continue
        self.connected = False
        self._held = self._buttons()

    def _buttons(self) -> int:
        if self._dll is None:
            return 0
        buttons, connected, state = 0, False, _State()
        for index in range(4):
            if self._dll.XInputGetState(index, ctypes.byref(state)) == 0:
                connected = True
                buttons |= state.Gamepad.wButtons
        self.connected = connected
        return buttons

    def poll(self) -> str | None:
        buttons = self._buttons()
        pressed, self._held = buttons & ~self._held, buttons
        return next((answer for mask, answer in self.MAP if pressed & mask), None)
