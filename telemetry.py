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
# per-trailer wheel arrays, offsets within one trailer block (scsTrailer_t)
_TRAILER_WHEEL_ON_GROUND = 64
_TRAILER_WHEEL_SUBSTANCE = 84
_TRAILER_WHEEL_COUNT = 148
_TRAILER_WHEEL_DEFLECTION = 168
_TRAILER_WHEEL_VELOCITY = 232
_TRAILER_WHEEL_RADIUS = 552
_TRAILER_WHEEL_POS_X = 676
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
    trailer_wheels: list[Wheel] = field(default_factory=list)  # every attached trailer's, in order
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

    def _wheel(self, substance: int, deflection: float, on_ground: bool, pos_x: float, velocity: float,
               radius: float) -> Wheel:
        name = self._substances[substance] if substance < len(self._substances) else ""
        return Wheel(deflection=deflection, on_ground=on_ground, substance=name, pos_x=pos_x,
                     velocity=velocity, radius=radius or 0.5)

    def read(self) -> Frame | None:
        if self._mem is None and not self._open():
            return None
        buf = self._mem.buf
        values = {key: struct.unpack_from(fmt, buf, off)[0] for key, (off, fmt) in _FIELDS.items()}
        arrays = {key: struct.unpack_from(fmt, buf, off) for key, (off, fmt) in _ARRAYS.items()}
        if time.monotonic() - self._substances_read_at > 2.0:
            self._read_substances(buf)

        count = min(values["wheel_count"], MAX_WHEELS)
        wheels = [self._wheel(arrays["wheel_substance"][i], arrays["wheel_deflection"][i],
                              arrays["wheel_on_ground"][i], arrays["wheel_pos_x"][i],
                              arrays["wheel_velocity"][i], arrays["wheel_radius"][i]) for i in range(count)]
        trailer_attached = False
        trailer_wheels = []
        for i in range(min(values["max_trailers"] or 1, MAX_TRAILERS)):
            base = _TRAILERS_OFFSET + i * _TRAILER_SIZE
            if not buf[base + _TRAILER_ATTACHED]:
                continue
            trailer_attached = True
            count = min(struct.unpack_from("I", buf, base + _TRAILER_WHEEL_COUNT)[0], MAX_WHEELS)
            columns = (struct.unpack_from(f"{count}{fmt}", buf, base + off) for fmt, off in (
                ("I", _TRAILER_WHEEL_SUBSTANCE), ("f", _TRAILER_WHEEL_DEFLECTION), ("?", _TRAILER_WHEEL_ON_GROUND),
                ("f", _TRAILER_WHEEL_POS_X), ("f", _TRAILER_WHEEL_VELOCITY), ("f", _TRAILER_WHEEL_RADIUS)))
            trailer_wheels += [self._wheel(*wheel) for wheel in zip(*columns)]
        return Frame(
            active=values["sdk_active"], paused=values["paused"], render_time=values["render_time"],
            rpm=values["rpm"], rpm_max=values["rpm_max"] or 2100.0, speed=values["speed"],
            user_throttle=values["user_throttle"], user_brake=values["user_brake"],
            game_throttle=values["game_throttle"], game_brake=values["game_brake"],
            gear=values["gear"], retarder=values["retarder"], retarder_steps=values["retarder_steps"],
            engine_on=values["engine_on"], motor_brake=values["motor_brake"], park_brake=values["park_brake"],
            accel=(values["accel_x"], values["accel_y"], values["accel_z"]),
            wheels=wheels,
            trailer_wheels=trailer_wheels,
            air_pressure=values["air_pressure"],
            blinker_left=values["blinker_left"], blinker_right=values["blinker_right"],
            shifter_toggle=arrays["shifter_toggle"],
            trailer_attached=trailer_attached,
            game_events=frozenset(name for name, flag in zip(GAME_EVENT_NAMES, arrays["game_events"]) if flag),
        )


class SimulatedTelemetry:
    """Scripted 60 s delivery run for testing without the game, then loop.

     0-5   yard: engine start 0.8, trailer coupled 2, park brake released 4.5
     5-15  launch: throttle floored 5-7 with wheelspin 5-6, gears 1-9,
           splitter 10 / 12, range selector 12.5
    15-22  highway asphalt: left blinker 15-17.4, potholes on the left 17 /
           19.5, rumble strip on the right 20-21.5
    22-25  concrete with expansion joints every 0.55 s
    25-28  gravel, easing off; right blinker 25.5-27
    28-34  downshift to 8 at 28, then a wooden bridge 28-30, snow 30-32 and
           ice 32-34 with wheelspin under throttle 32.6-33.4
    34-38  downhill: retarder 34-36, engine brake 36-38
    38-42  hard stop: brake floored 38, wheel lock, squeal just before 41.6,
           range selector back at 40
    42-45  toll booth, tollgate event 43
    45-49  reversing onto the dock with hazards on, bumping it at 48.6
    49-60  park brake 49.5, trailer uncoupled 51, delivered 52.5, engine
           off 55, quiet

    Front axle first, the drive axles 0.2 s later and the trailer's two axles
    0.45 / 0.5 s later, like a 22 m/s rig rolling over the same spot.
    """

    PERIOD = 60.0
    SURFACES = ((22.0, "road"), (25.0, "concrete"), (28.0, "gravel"), (30.0, "wood"), (32.0, "snow"),
                (34.0, "ice"), (PERIOD, "road"))
    TRUCK_WHEELS = ((-1.0, 0.0), (1.0, 0.0), (-0.9, 0.2), (0.9, 0.2), (-0.9, 0.25), (0.9, 0.25))  # (pos_x, delay)
    TRAILER_WHEELS = ((-0.98, 0.45), (0.98, 0.45), (-0.98, 0.5), (0.98, 0.5))

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.name = "simulated"

    def close(self) -> None:
        pass

    def read(self) -> Frame:
        t = (time.perf_counter() - self._start) % self.PERIOD
        frame = Frame(active=True, paused=False, render_time=int(time.perf_counter() * 1e6), rpm_max=2100.0)
        speed, throttle, brake, gear, rpm = 0.0, 0.0, 0.0, 0, 650.0
        lock = spin = False
        if t < 5:
            pass
        elif t < 15:
            throttle = 1.0 if t < 7 else 0.8
            speed = (t - 5) * 2.2
            gear = 1 + int((t - 5) / 1.25)
            rpm = 1100 + ((t - 5) % 1.25) / 1.25 * 700
            spin = t < 6
        elif t < 25:
            throttle, speed, gear, rpm = 0.4, 22.0, 9, 1350.0
        elif t < 28:
            throttle, speed, gear, rpm = 0.15, 22.0 - (t - 25) * 2.0, 9, 1350.0 - (t - 25) * 80
        elif t < 34:
            throttle, speed, gear, rpm = 0.4, 16.0, 8, 1400.0
            if 32.6 <= t < 33.4:
                throttle, rpm, spin = 0.9, 1800.0, True
        elif t < 38:
            speed, gear, rpm = 16.0, 8, 1200.0
            if t < 36:
                frame.retarder = 3
            else:
                frame.motor_brake = True
        elif t < 42:
            brake = 1.0 if t < 39 else 0.9
            speed = max(0.0, 16.0 - (t - 38) * 4.4)
            gear, rpm = (8 if speed > 3 else 0), max(650.0, 1200.0 - (t - 38) * 200)
            lock = speed > 4
        elif t < 45:
            brake = 0.3
        elif t < 48.6:
            throttle, gear, rpm = 0.3, -1, 900.0
            speed = -min(2.0, (t - 45) * 2.0)
        elif t < 49.5:
            brake = 0.3
        frame.speed, frame.user_throttle, frame.game_throttle = speed, throttle, throttle
        frame.user_brake, frame.game_brake, frame.gear = brake, brake, gear
        frame.retarder_steps = 4
        frame.engine_on = 0.8 <= t < 55.0
        frame.rpm = rpm if frame.engine_on else 0.0
        frame.trailer_attached = 2.0 <= t < 51.0
        frame.park_brake = t < 4.5 or t >= 49.5
        frame.shifter_toggle = (12.5 <= t < 40.0, 10.0 <= t < 12.0)  # (range, splitter)
        flash = int(t / 0.4) % 2 == 0
        hazards = 45.0 <= t < 49.5
        frame.blinker_left = flash and (15.0 <= t < 17.4 or hazards)
        frame.blinker_right = flash and (25.5 <= t < 27.0 or hazards)
        frame.accel = (0.0, 0.0, 18.0 if 48.6 <= t < 48.7 else 0.0)  # the dock bumper stops the trailer
        frame.game_events = frozenset(name for name, at in (("tollgate", 43.0), ("delivered", 52.5))
                                      if at <= t < at + 0.5)
        surface = next(name for end, name in self.SURFACES if t < end)

        def hit(at: float, center: float, depth: float, width: float) -> float:
            return -depth * math.exp(-((at - center) / width) ** 2)

        def wheel(i: int, x: float, delay: float) -> Wheel:
            u = t - delay  # when this axle reaches a spot the front axle passed at t
            jitter = 0.006 if surface == "gravel" else 0.003
            # 6 Hz: a whole number of cycles per loop, so the restart does not jolt
            deflection = 0.02 + jitter * math.sin(t * 2 * math.pi * 6 + i)
            if x < 0:
                deflection += hit(u, 17.0, 0.06, 0.04) + hit(u, 19.5, 0.06, 0.04)
            # Expansion joints and bridge planks: the nearest one, if it lies on that stretch, so
            # the first and last arrive smoothly instead of as a step.
            for first, last, pitch, depth, width in ((22.0, 25.0, 0.55, 0.012, 0.02), (28.0, 30.0, 0.25, 0.006, 0.015)):
                center = first + round((u - first) / pitch) * pitch
                if first <= center < last:
                    deflection += hit(u, center, depth, width)
            deflection += hit(t, 48.6, 0.03, 0.03)  # the dock jolts the whole rig at once
            # The game flickers back to road between the strip's grooves.
            on_strip = x > 0 and 20.0 <= u < 21.5 and int(u / 0.12) % 3 != 0
            return Wheel(deflection=deflection, on_ground=True, substance="rumble_stripe" if on_strip else surface,
                         pos_x=x, velocity=speed / (2 * math.pi * 0.5), radius=0.5)

        frame.wheels = [wheel(i, x, delay) for i, (x, delay) in enumerate(self.TRUCK_WHEELS)]
        for w in frame.wheels[2:]:  # the drive axles
            if lock:
                w.velocity *= 0.2
            elif spin:
                w.velocity = max(abs(speed) * 1.5, abs(speed) + 3.0) / (2 * math.pi * 0.5)
        if frame.trailer_attached:
            frame.trailer_wheels = [wheel(i, x, delay) for i, (x, delay) in enumerate(self.TRAILER_WHEELS)]
        return frame
