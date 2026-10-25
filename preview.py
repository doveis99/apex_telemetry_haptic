"""Play one effect alone for a few seconds: the ▶ test button beside each effect.

Each effect gets a short scripted moment that sets it off, rendered by the
same synth as driving, so it plays at its current strength and route and
with the motors' calibration. An effect that is switched off plays too.
Effects with a side go left first, then right. The steady fade is left out:
a few seconds are too short to judge it.

While the output runs, Runner plays a preview in place of the drive. While
it is stopped, Player takes the controller only for as long as one plays.
"""

from __future__ import annotations

import math
import threading
from dataclasses import replace
from typing import Callable

from apex6 import MAX_SAMPLE, SAMPLE_RATE, SUBFRAMES, Apex6, Block, DeviceUnavailable
from effects import DT, TOGGLEABLE, Equalizer, Gains, Synth, effective_gains
from errorlog import log_exception
from telemetry import Frame, Wheel

FADE_S = 0.1
CRUISE = 22.0  # m/s, 80 km/h
TRUCK_WHEELS = (-1.0, 1.0, -0.9, 0.9, -0.9, 0.9)  # pos_x: the steer axle, then the two drive axles
ROUGH = 0.003  # m of fine suspension movement at 6 Hz, as in the scripted drive
# (s, side, depth m, width s): a joint under the left front wheel, one under the right, then a pothole each
HITS = ((0.3, -1, 0.012, 0.02), (0.9, 1, 0.012, 0.02), (1.6, -1, 0.06, 0.04), (2.4, 1, 0.06, 0.04))


def _frame(t: float, speed: float = CRUISE, throttle: float = 0.3, rough: float = ROUGH, **values) -> Frame:
    """Cruising in tenth gear on asphalt, with values changed."""
    frame = Frame(active=True, paused=False, render_time=int(t * 1e6), rpm=1300.0, speed=speed, gear=10,
                  engine_on=True, user_throttle=throttle, game_throttle=throttle)
    frame.wheels = [Wheel(deflection=0.02 + rough * math.sin(t * math.tau * 6 + i), pos_x=x,
                          velocity=speed / (math.tau * 0.5), radius=0.5) for i, x in enumerate(TRUCK_WHEELS)]
    for key, value in values.items():
        setattr(frame, key, value)
    if "user_brake" in values:
        frame.game_brake = frame.user_brake
    return frame


def _parked(t: float, **values) -> Frame:
    return _frame(t, speed=0.0, throttle=0.0, **{"gear": 0, "rpm": 650.0, **values})


def _engine(t: float, _: Gains) -> Frame:
    # idle with its lope, then a rev under load and back down
    rise = min(1.0, max(0.0, (t - 1.2) / 0.8)) if t < 2.4 else max(0.0, 1.0 - (t - 2.4) / 0.6)
    return _parked(t, rpm=650.0 + 1050.0 * rise, user_throttle=0.8 if 1.2 <= t < 2.4 else 0.0,
                   game_throttle=0.8 if 1.2 <= t < 2.4 else 0.0)


def _road(t: float, _: Gains) -> Frame:
    gravel = t >= 1.6
    frame = _frame(t, rough=2 * ROUGH if gravel else ROUGH)
    for wheel in frame.wheels:
        wheel.substance = "gravel" if gravel else "road"
    return frame


def _bumps(t: float, _: Gains) -> Frame:
    frame = _frame(t, rough=0.0)
    for wheel in frame.wheels[:2]:  # the steer axle: the axles behind would knock again, later
        wheel.deflection -= sum(depth * math.exp(-((t - at) / width) ** 2)
                                for at, side, depth, width in HITS if side * wheel.pos_x > 0)
    return frame


def _rumble(t: float, _: Gains) -> Frame:
    frame = _frame(t)
    side = -1 if 0.2 <= t < 1.2 else 1 if 1.6 <= t < 2.6 else 0
    for wheel in frame.wheels:
        if side * wheel.pos_x > 0:
            wheel.substance = "rumble_stripe"
    return frame


def _wheelspin(t: float, _: Gains) -> Frame:
    spinning = 0.2 <= t < 1.8
    frame = _frame(t, speed=3.0, throttle=0.9 if spinning else 0.0, gear=1, rpm=1500.0 if spinning else 800.0)
    if spinning:
        for wheel in frame.wheels[2:]:
            wheel.velocity = 9.0 / (math.tau * wheel.radius)
    return frame


def _retarder(t: float, _: Gains) -> Frame:
    # the stages one by one, then the engine brake
    frame = _frame(t, speed=17.0, throttle=0.0, rpm=1500.0, retarder_steps=4, motor_brake=2.6 <= t < 3.8)
    if 0.2 <= t < 2.4:
        frame.retarder = min(4, 1 + int((t - 0.2) / 0.5))
    return frame


def _impact(t: float, gains: Gains) -> Frame:
    jump = gains.impact_threshold + 15.0  # past whatever the threshold is set to
    return _frame(t, accel=(0.0, 0.0, jump if 0.3 <= t < 0.4 else 0.0))


def _trigger(t: float, _: Gains) -> Frame:
    if t < 1.9:  # pressing the throttle in, slowly to the floor, and letting go
        return _frame(t, throttle=min(1.0, max(0.0, (t - 0.2) / 1.4)) if t < 1.6 else 0.0)
    frame = _frame(t, speed=15.0, throttle=0.0, user_brake=0.6)  # braking, the drive axles locking for a second
    if 2.1 <= t < 3.1:
        for wheel in frame.wheels[2:]:
            wheel.velocity *= 0.2
    return frame


def _pedal(t: float, _: Gains) -> Frame:
    return _frame(t, throttle=1.0 if 0.3 <= t < 0.9 else 0.0, user_brake=1.0 if 1.3 <= t < 1.9 else 0.0)


def _squeal(t: float, _: Gains) -> Frame:
    speed = max(0.3, 3.8 - 3.5 * max(0.0, t - 0.2) / 2.0)  # rolling to a stop on a light brake
    return _frame(t, speed=speed, throttle=0.0, gear=1, rpm=700.0, user_brake=0.3)


def _blinker(t: float, _: Gains) -> Frame:
    lamp = int(t / 0.4) % 2 == 1  # each side flashes twice: on, off, on, off
    return _frame(t, blinker_left=lamp and t < 1.8, blinker_right=lamp and 2.0 <= t < 3.4)


Script = Callable[[float, Gains], Frame]
# effect -> (moment that sets it off, seconds including its tail)
SCRIPTS: dict[str, tuple[Script, float]] = {
    "engine": (_engine, 3.2),
    "road": (_road, 3.2),
    "bumps": (_bumps, 3.2),
    "rumble": (_rumble, 3.0),
    "wheelspin": (_wheelspin, 2.2),
    "gear": (lambda t, _: _frame(t, gear=8 + sum(t >= at for at in (0.3, 0.9, 1.5))), 2.0),
    "retarder": (_retarder, 4.0),
    "impact": (_impact, 1.0),
    "trigger": (_trigger, 3.3),
    "pedal": (_pedal, 2.2),
    "squeal": (_squeal, 2.4),
    "trailer": (lambda t, _: _parked(t, trailer_attached=0.3 <= t < 1.5), 2.4),
    "park_brake": (lambda t, _: _parked(t, park_brake=0.3 <= t < 1.8), 2.6),
    "air_brake": (lambda t, _: _frame(t, speed=10.0, throttle=0.0,
                                      user_brake=0.5 if 0.2 <= t < 0.9 else 1.0 if 1.3 <= t < 2.0 else 0.0), 2.8),
    "blinker": (_blinker, 3.6),
    "engine_start": (lambda t, _: _parked(t, engine_on=0.3 <= t < 2.2, rpm=650.0 if 0.3 <= t < 2.2 else 0.0),
                     3.4),
    "shifter": (lambda t, _: _frame(t, shifter_toggle=(0.3 <= t < 1.5, t >= 0.9)), 2.0),
    "game_events": (lambda t, _: _parked(t, game_events=frozenset({"tollgate"}) if 0.3 <= t < 0.8 else frozenset()),
                    1.0),
}


class Preview:
    """One effect alone through its scripted moment, a block at a time."""

    def __init__(self, name: str, gains: Gains, eq: Equalizer) -> None:
        self.name = name
        self.script, self.seconds = SCRIPTS[name]
        self.gains = replace(gains, disabled=[other for other in TOGGLEABLE if other != name],
                             routes=dict(gains.routes), steady_fade=0.0)
        self.synth = Synth(effective_gains(self.gains), eq)
        self.blocks = 0

    def render(self, _first_sample: int = 0) -> Block | None:
        t = self.blocks * DT
        if t >= self.seconds:
            return None
        self.blocks += 1
        block = self.synth.render(self.script(t, self.gains))
        if self.seconds - t >= FADE_S + DT:
            return block
        # the last stretch fades out instead of stopping on a step (a click)
        fade = [max(0.0, min(1.0, (self.seconds - t - (n + 1) / SAMPLE_RATE) / FADE_S)) for n in range(SUBFRAMES)]
        return replace(block, **{name: [x * f for x, f in zip(getattr(block, name), fade)]
                                 for name in ("trigger", "left_grip", "right_grip")})


class Player:
    """Plays previews while the output is stopped, holding the controller only while one plays.

    A preview started while another plays takes over at the next block.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: Preview | None = None
        self.current: Preview | None = None
        self.name = ""  # the effect last started
        self.running = False
        self.thread: threading.Thread | None = None
        self.error: str | None = None  # set by the worker, picked up by the window
        self.peaks = [0.0, 0.0, 0.0]  # left grip, right grip, trigger; 0..1, held until read

    @property
    def busy(self) -> bool:
        return self.running

    def play(self, preview: Preview) -> None:
        with self.lock:
            self.pending, self.name = preview, preview.name
            if self.running:
                return
            self.running = True
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """End the preview and wait for the controller to be let go."""
        with self.lock:
            self.pending = self.current = None
        if self.thread:
            self.thread.join(timeout=2.0)

    def take_peaks(self) -> list[float]:
        peaks, self.peaks = self.peaks, [0.0, 0.0, 0.0]
        return peaks

    def _next_block(self, _first_sample: int) -> Block | None:
        with self.lock:
            if self.pending is not None:
                self.current, self.pending = self.pending, None
            block = self.current.render() if self.current else None
            if block is None:
                self.current = None
                return None
        for i, channel in enumerate((block.left_grip, block.right_grip, block.trigger)):
            self.peaks[i] = max(self.peaks[i], max(abs(x) for x in channel) / MAX_SAMPLE)
        return block

    def _work(self) -> None:
        error = None
        while error is None:
            try:
                with Apex6() as pad:
                    pad.stream(self._next_block)
            except DeviceUnavailable as exc:
                error = str(exc)
            except Exception as exc:  # the traceback goes to error.log
                log_exception(exc, "preview")
                error = f"{type(exc).__name__}: {exc}"
            with self.lock:
                if error or self.pending is None:  # one started while the controller was let go plays on
                    self.error = error
                    self.pending = self.current = None
                    self.running = False
                    return
