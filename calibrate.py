"""Equal-strength calibration of the motors, for effects.Equalizer.

The voice coils resonate (grips near 65 Hz, triggers near 100 Hz), so the
same drive feels weaker off resonance. Judging the strength of two very
different frequencies is vague, so each frequency is only ever compared with
its neighbour, which feels alike, working outward from the motor's reference
(80 against 65, 100 against 80, ...; 50 against 65, 40 against 50, ...). The
two play in random order and the player only says which was stronger; an
up-down staircase on those answers closes in on the gain at which the two
feel the same, and the neighbour's gain times it is that frequency's gain.
tone.py calibrate runs it in a console and the window in a dialog; both also
take the answers from the pad's own buttons.
"""

from __future__ import annotations

import ctypes
import math
import random
import time
from typing import Callable

from apex6 import Apex6, Block
from effects import Equalizer
from tone import tone_renderer

REFERENCE = {"grip": 65.0, "trigger": 100.0}
FREQS = {"grip": (30.0, 40.0, 50.0, 80.0, 100.0, 130.0, 160.0, 200.0, 250.0),
         "trigger": (50.0, 65.0, 80.0, 130.0, 160.0, 200.0, 250.0)}
TARGETS = {"grip": "grips", "trigger": "triggers"}  # tone.py targets: both grips, both triggers
GAP_MS = 300  # silence between the two tones
AMP = 25.0  # reference strength in percent, leaving headroom for the frequencies that need more
SECONDS = 0.8  # each tone

# answers
FIRST, SECOND, REPLAY, CANCEL = "first", "second", "replay", "cancel"


def neighbours(channel: str) -> list[tuple[float, float]]:
    """(frequency, neighbour) in the order they are matched: up from the reference, then down."""
    freqs = sorted(FREQS[channel] + (REFERENCE[channel],))
    ref = freqs.index(REFERENCE[channel])
    return ([(freqs[i], freqs[i - 1]) for i in range(ref + 1, len(freqs))]
            + [(freqs[i], freqs[i + 1]) for i in range(ref - 1, -1, -1)])


class Staircase:
    """The test tone's level against its neighbour, in dB, from answers to "which was stronger".

    Steps of START_DB until the first reversal, then FINE_DB. It ends after
    REVERSALS reversals and takes the mean of all but the first, or after
    MAX_TRIALS, or when the answers keep pushing against a limit.
    """

    START_DB, FINE_DB = 3.0, 1.5
    REVERSALS = 5
    MAX_TRIALS = 16

    def __init__(self, low_db: float, high_db: float) -> None:
        self.low_db, self.high_db = low_db, high_db
        self.db = min(max(0.0, low_db), high_db)
        self.trials = 0
        self.reversals: list[float] = []
        self.direction = 0  # +1 raising the test, -1 lowering it
        self.pinned = 0  # answers in a row that asked to go past a limit

    @property
    def done(self) -> bool:
        return len(self.reversals) >= self.REVERSALS or self.trials >= self.MAX_TRIALS or self.pinned >= 2

    @property
    def result_db(self) -> float:
        if self.pinned >= 2 or len(self.reversals) < 2:
            return self.db
        return sum(self.reversals[1:]) / len(self.reversals[1:])

    def answer(self, test_stronger: bool) -> None:
        self.trials += 1
        direction = -1 if test_stronger else 1
        if self.direction and direction != self.direction:
            self.reversals.append(self.db)
        self.direction = direction
        step = self.FINE_DB if self.reversals else self.START_DB
        wanted = self.db + direction * step
        self.db = min(max(wanted, self.low_db), self.high_db)
        self.pinned = self.pinned + 1 if wanted != self.db else 0


class Session:
    """Where a calibration stands: which pair is being compared, the staircase, the gains found."""

    def __init__(self, channels: list[str], amp: float = AMP, seconds: float = SECONDS,
                 rng: random.Random | None = None) -> None:
        self.channels, self.amp, self.seconds = channels, amp, seconds
        self.rng = rng or random.Random()
        self.steps = [(channel, freq, neighbour) for channel in channels for freq, neighbour in neighbours(channel)]
        self.index = 0
        self.curves = {channel: {f"{REFERENCE[channel]:g}": 1.0} for channel in channels}
        self.finished: list[str] = []  # channels with every frequency matched
        self._start_step()

    @property
    def current(self) -> tuple[str, float, float] | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    def gain(self, channel: str, freq: float) -> float:
        return self.curves[channel][f"{freq:g}"]

    def _start_step(self) -> None:
        if self.current is None:
            return
        channel, _, neighbour = self.current
        base = self.gain(channel, neighbour)
        low, high = Equalizer.LIMITS
        high = min(high, 100.0 / self.amp)  # the output cannot go past full scale
        self.staircase = Staircase(20 * math.log10(low / base), 20 * math.log10(high / base))
        self.test_first = self.rng.random() < 0.5

    def tones(self) -> list[tuple[float, float]]:
        """The two tones of this trial in play order, as (frequency, gain)."""
        channel, freq, neighbour = self.current
        base = self.gain(channel, neighbour)
        test, other = (freq, base * 10 ** (self.staircase.db / 20)), (neighbour, base)
        return [test, other] if self.test_first else [other, test]

    def answer(self, answer: str) -> str | None:
        """Take FIRST or SECOND; returns the channel this answer has just finished, if any."""
        if self.current is None or answer not in (FIRST, SECOND):
            return None
        self.staircase.answer((answer == FIRST) == self.test_first)
        if not self.staircase.done:
            self.test_first = self.rng.random() < 0.5
            return None
        channel, freq, neighbour = self.current
        low, high = Equalizer.LIMITS
        gain = self.gain(channel, neighbour) * 10 ** (self.staircase.result_db / 20)
        self.curves[channel][f"{freq:g}"] = round(min(max(gain, low), high), 3)
        self.index += 1
        self._start_step()
        if self.current is None or self.current[0] != channel:
            self.finished.append(channel)
            return channel
        return None


def play(pad: Apex6, session: Session, listen: Callable[[], str | None] = lambda: None) -> str | None:
    """The trial's two tones with a pause between; an answer heard on the way stops them and is returned."""
    target = TARGETS[session.current[0]]
    heard: list[str] = []

    def listening(render: Callable[[int], Block | None]) -> Callable[[int], Block | None]:
        def render_or_stop(first: int) -> Block | None:
            if not heard:
                answer = listen()
                if answer:
                    heard.append(answer)
            return None if heard else render(first)
        return render_or_stop

    (f1, g1), (f2, g2) = session.tones()
    for render in (tone_renderer(target, f1, f1, min(100.0, session.amp * g1), session.seconds),
                   lambda first: Block() if first < GAP_MS else None,
                   tone_renderer(target, f2, f2, min(100.0, session.amp * g2), session.seconds)):
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

    D-pad left or LB: the first tone was stronger, D-pad right or RB: the
    second, X: replay, B: stop. XInput works whichever window has the focus.
    A button already held when this starts, or still held, counts once when
    it goes down again.
    """

    MAP = ((0x0004, FIRST), (0x0100, FIRST), (0x0008, SECOND), (0x0200, SECOND), (0x4000, REPLAY), (0x2000, CANCEL))

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
