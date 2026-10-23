"""Read ATS/ETS2 telemetry from an existing scs-telemetry shared-memory plugin.

Both plugins already installed in ATS are RenCloud scs-sdk-plugin builds that
only differ in the mapping name, so no extra plugin is needed. Offsets are for
plugin revision 12 (scs-sdk-plugin V.1.12, layout as in the truck-telemetry
PyPI package); the revision is checked on open.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from multiprocessing.shared_memory import SharedMemory

from i18n import t

MAPPING_NAMES = ("Local\\SCSTelemetry", "Local\\TruckDashTelemetry", "Local\\SHSCSTelemetry")
SUPPORTED_REVISION = 12
MAX_WHEELS = 16
SUBSTANCE_COUNT = 25
STRING_SIZE = 64

# offset, struct format
_FIELDS = {
    "sdk_active": (0, "?"),
    "paused": (4, "?"),
    "render_time": (24, "Q"),
    "revision": (40, "I"),
    "retarder_steps": (76, "I"),
    "max_trailers": (92, "I"),
    "wheel_count": (80, "I"),
    "retarder": (108, "I"),
    "gear": (504, "i"),
    "rpm_max": (740, "f"),
    "speed": (948, "f"),
    "rpm": (952, "f"),
    "user_throttle": (960, "f"),
    "user_brake": (964, "f"),
    "game_throttle": (976, "f"),
    "game_brake": (980, "f"),
    "air_pressure": (992, "f"),  # psi
    "park_brake": (1566, "?"),
    "motor_brake": (1567, "?"),
    "engine_on": (1576, "?"),
    "blinker_left": (1580, "?"),  # lamp state, toggles with each flash
    "blinker_right": (1581, "?"),
    "accel_x": (1892, "f"),
    "accel_y": (1896, "f"),
    "accel_z": (1900, "f"),
}
_ARRAYS = {
    "wheel_substance": (120, "16I"),
    "wheel_radius": (752, "16f"),
    "wheel_deflection": (1072, "16f"),
    "wheel_velocity": (1136, "16f"),  # rotations per second
    "wheel_on_ground": (1590, "16?"),
    "wheel_pos_x": (1676, "16f"),  # negative = left side of the truck
    "shifter_toggle": (1606, "2?"),  # H-shifter range / splitter switches
    # Gameplay event flags; the plugin raises them briefly when the event fires.
    "game_events": (4303, "6?"),
}
GAME_EVENT_NAMES = ("delivered", "fined", "tollgate", "ferry", "train", "refuel")
_SUBSTANCES_OFFSET = 4400
_TRAILERS_OFFSET = 6000
_TRAILER_SIZE = 1560
_TRAILER_ATTACHED = 80
MAX_TRAILERS = 10


@dataclass
class Wheel:
    deflection: float = 0.0
    on_ground: bool = True
    substance: str = "road"
    pos_x: float = 0.0
    velocity: float = 0.0
    radius: float = 0.5


@dataclass
class Frame:
    active: bool = False
    paused: bool = True
    render_time: int = 0
    rpm: float = 0.0
    rpm_max: float = 2100.0
    speed: float = 0.0  # m/s, negative when reversing
    user_throttle: float = 0.0
    user_brake: float = 0.0
    game_throttle: float = 0.0
    game_brake: float = 0.0
    gear: int = 0
    retarder: int = 0
    retarder_steps: int = 0
    engine_on: bool = False
    motor_brake: bool = False
    park_brake: bool = False
    accel: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wheels: list[Wheel] = field(default_factory=list)
    air_pressure: float = 120.0
    blinker_left: bool = False
    blinker_right: bool = False
    shifter_toggle: tuple[bool, bool] = (False, False)
    trailer_attached: bool = False
    game_events: frozenset[str] = frozenset()

    @property
    def live(self) -> bool:
        return self.active and not self.paused


class SharedMemoryTelemetry:
    """Poll the plugin's shared memory. read() returns None until the game runs."""

    def __init__(self, names: tuple[str, ...] = MAPPING_NAMES) -> None:
        self._names = names
        self._mem: SharedMemory | None = None
        self.name: str | None = None
        self._substances: list[str] = []
        self._substances_read_at = 0.0
        self._next_open_attempt = 0.0
        self.problem: str | None = None  # why no usable mapping was found, for the UI

    def _open(self) -> bool:
        if time.monotonic() < self._next_open_attempt:
            return False
        self._next_open_attempt = time.monotonic() + 1.0
        self.problem = None
        for name in self._names:
            try:
                mem = SharedMemory(name=name, create=False)
            except FileNotFoundError:
                continue
            revision = struct.unpack_from("I", mem.buf, _FIELDS["revision"][0])[0]
            if revision == 0:
                # Mapping exists (e.g. created by SimHub) but the game has not
                # filled it yet; try again later.
                mem.close()
                continue
            if revision != SUPPORTED_REVISION:
                # Another plugin build may still be compatible; keep looking.
                mem.close()
                self.problem = t("{name} 플러그인 버전 {revision} (지원: {supported})", name=name.split(chr(92))[-1],
                                 revision=revision, supported=SUPPORTED_REVISION)
                continue
            self._mem, self.name = mem, name
            return True
        return False

    def close(self) -> None:
        if self._mem is not None:
            self._mem.close()
            self._mem = None

    def _read_substances(self, buf) -> None:
        names = []
        for i in range(SUBSTANCE_COUNT):
            raw = bytes(buf[_SUBSTANCES_OFFSET + i * STRING_SIZE:_SUBSTANCES_OFFSET + (i + 1) * STRING_SIZE])
            names.append(raw.split(b"\x00", 1)[0].decode("utf-8", "replace"))
        self._substances = names
        self._substances_read_at = time.monotonic()

    def read(self) -> Frame | None:
        if self._mem is None and not self._open():
            return None
        buf = self._mem.buf
        values = {key: struct.unpack_from(fmt, buf, off)[0] for key, (off, fmt) in _FIELDS.items()}
        arrays = {key: struct.unpack_from(fmt, buf, off) for key, (off, fmt) in _ARRAYS.items()}
        if time.monotonic() - self._substances_read_at > 2.0:
            self._read_substances(buf)

        count = min(values["wheel_count"], MAX_WHEELS)
        wheels = []
        for i in range(count):
            index = arrays["wheel_substance"][i]
            wheels.append(Wheel(
                deflection=arrays["wheel_deflection"][i],
                on_ground=arrays["wheel_on_ground"][i],
                substance=self._substances[index] if index < len(self._substances) else "",
                pos_x=arrays["wheel_pos_x"][i],
                velocity=arrays["wheel_velocity"][i],
                radius=arrays["wheel_radius"][i] or 0.5,
            ))
        return Frame(
            active=values["sdk_active"], paused=values["paused"], render_time=values["render_time"],
            rpm=values["rpm"], rpm_max=values["rpm_max"] or 2100.0, speed=values["speed"],
            user_throttle=values["user_throttle"], user_brake=values["user_brake"],
            game_throttle=values["game_throttle"], game_brake=values["game_brake"],
            gear=values["gear"], retarder=values["retarder"], retarder_steps=values["retarder_steps"],
            engine_on=values["engine_on"], motor_brake=values["motor_brake"], park_brake=values["park_brake"],
            accel=(values["accel_x"], values["accel_y"], values["accel_z"]),
            wheels=wheels,
            air_pressure=values["air_pressure"],
            blinker_left=values["blinker_left"], blinker_right=values["blinker_right"],
            shifter_toggle=arrays["shifter_toggle"],
            trailer_attached=any(
                buf[_TRAILERS_OFFSET + i * _TRAILER_SIZE + _TRAILER_ATTACHED]
                for i in range(min(values["max_trailers"] or 1, MAX_TRAILERS))),
            game_events=frozenset(name for name, flag in zip(GAME_EVENT_NAMES, arrays["game_events"]) if flag),
        )


class SimulatedTelemetry:
    """Scripted 40 s drive for testing without the game.

    0-5 idle, 5-15 accelerate through gears, 15-22 cruise on asphalt with two
    potholes on the left, 22-28 gravel, 28-32 hard braking with a wheel lock,
    32-36 retarder, then loop.

    Events: engine start 0.8 s, trailer coupled 2 s, park brake released
    4.5 s, throttle floored 5-7 s, splitter 10 s / 12 s, left blinker
    15-22 s, brake squeal near the stop ~31.5 s, tollgate
    34 s, park brake set 37 s.
    """

    PERIOD = 40.0

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.name = "simulated"

    def close(self) -> None:
        pass

    def read(self) -> Frame:
        t = (time.perf_counter() - self._start) % self.PERIOD
        frame = Frame(active=True, paused=False, render_time=int(time.perf_counter() * 1e6),
                      engine_on=True, rpm_max=2100.0)
        speed, throttle, brake, gear, retarder, substance = 0.0, 0.0, 0.0, 0, 0, "road"
        lock = False
        if t < 5:
            frame.rpm = 650.0
        elif t < 15:
            throttle = 1.0 if t < 7 else 0.8
            speed = (t - 5) * 2.2
            gear = 1 + int((t - 5) / 1.25)
            frame.rpm = 1100 + ((t - 5) % 1.25) / 1.25 * 700
        elif t < 28:
            throttle, speed, gear, frame.rpm = 0.4, 22.0, 9, 1350.0
            if t >= 22:
                substance = "gravel"
        elif t < 32:
            brake, gear, frame.rpm = 0.9, 9, 1000.0
            speed = max(0.0, 22.0 - (t - 28) * 5.0)
            lock = speed > 4
        elif t < 36:
            gear, frame.rpm, retarder = 9, 1200.0, 3
            speed = 15.0
        else:
            frame.rpm = 650.0
        frame.speed, frame.user_throttle, frame.game_throttle = speed, throttle, throttle
        frame.user_brake, frame.game_brake, frame.gear = brake, brake, gear
        frame.retarder, frame.retarder_steps = retarder, 4
        frame.engine_on = t >= 0.8
        frame.trailer_attached = t >= 2.0
        frame.park_brake = t < 4.5 or t >= 37.0
        frame.shifter_toggle = (False, 10.0 <= t < 12.0)
        frame.blinker_left = 15.0 <= t < 22.0 and int(t / 0.4) % 2 == 0
        frame.game_events = frozenset({"tollgate"}) if 34.0 <= t < 34.5 else frozenset()
        if not frame.engine_on:
            frame.rpm = 0.0

        def pothole(center: float) -> float:
            return -0.06 * math.exp(-((t - center) / 0.04) ** 2)

        wheels = []
        for i, x in enumerate((-1.0, 1.0, -0.9, 0.9, -0.9, 0.9)):
            deflection = 0.02 + 0.003 * math.sin(t * 37 + i)
            if x < 0:
                deflection += pothole(17.0) + pothole(19.5)
            velocity = speed / (2 * math.pi * 0.5)
            if lock and i >= 2:
                velocity *= 0.2
            wheels.append(Wheel(deflection=deflection, on_ground=True, substance=substance,
                                pos_x=x, velocity=velocity, radius=0.5))
        frame.wheels = wheels
        return frame
