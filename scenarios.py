"""Test drives made of real driving situations, to feel and check the effects together.

The 60 s drive in telemetry.SimulatedTelemetry plays every effect once, mostly
one after another. On the road they come together: a downshift while braking
with the blinker on, the retarder over potholes on a descent, a long cruise in
which the steady fade settles. Each scenario here is one such stretch of
driving, a few minutes long, written as what the driver does over time:

  speed, throttle, brake    keyframes, linear in between (speed in km/h)
  surface, retarder         the value from each time on, until the next
  blinkers, engine brake... (start, end) spans
  potholes, joints, rails   when the front axle reaches them

Road features stay where the cab met them: the axles behind reach them as far
back as they sit, so at walking pace the trailer knocks seconds after the cab
and at 100 km/h a fraction of a second. Gears follow the speed through a
12-speed gearbox that shifts like an automated one (by rpm, later under more
throttle), and rpm follows speed and gear.

The scenarios play in a loop with a second of pause between rounds, as on a
loading screen, so the restart is not felt as a jolt.

Forward driving only: road positions come from the distance driven.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass
from typing import Callable

from effects import DT
from telemetry import SIMULATED_FPS, Frame, Wheel

TAU = 2.0 * math.pi
RATIOS = (14.93, 11.67, 9.02, 7.06, 5.63, 4.40, 3.39, 2.65, 2.05, 1.60, 1.28, 1.0)
RPM_PER_KMH = 13.0  # in top gear: 1235 rpm at 95 km/h
IDLE, RPM_MAX = 650.0, 2100.0
SHIFT_GAP = 1.5  # s between shifts; the gearbox waits for the last one to settle
SPLIT_LEAD = 0.3  # s the splitter is flipped before the clutch goes in
FLASH = 0.4  # s the blinker lamp stays on, then off
WHEEL_RADIUS = 0.5
# (m behind the front axle, pos_x): steer axle, two drive axles, the trailer's tandem
TRUCK_AXLES = ((0.0, 1.0), (5.0, 0.9), (6.3, 0.9))
TRAILER_AXLES = ((13.0, 0.98), (14.3, 0.98))
# Fine unevenness of each surface, m of suspension movement: a long wave and a short one,
# fixed to the road so every axle rides the same ground. Like the 60 s drive's, paved roads
# stay under the bump threshold (0.2 m/s) up to 105 km/h; how rough the game's own roads
# read is for a recorded drive to show.
ROUGH = {"road": 0.003, "concrete": 0.003, "road_coarse": 0.0035, "gravel": 0.006, "metal": 0.001}
ROUGH_WAVES = ((5.0, 0.8), (2.5, 0.2))  # (wavelength m, share)
STRIP_FLICKER = 2.6  # m: the game reports road under one groove in three

Keys = tuple[tuple[float, float], ...]
Spans = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Hit:
    """A spot on the road the suspension rides over."""

    at: float  # s, when the front axle reaches it
    side: int  # -1 the left wheels, 1 the right, 0 both
    depth: float  # m; negative drops into a hole, positive rides up a bump
    width: float = 0.6  # m along the road


@dataclass(frozen=True)
class Scenario:
    name: str
    seconds: float
    speed: Keys  # km/h
    throttle: Keys
    brake: Keys = ((0.0, 0.0),)
    surface: tuple[tuple[float, str], ...] = ((0.0, "road"),)
    retarder: tuple[tuple[float, int], ...] = ((0.0, 0),)
    engine_brake: Spans = ()
    blinker_left: Spans = ()
    blinker_right: Spans = ()
    park_brake: Spans = ()
    spin: Spans = ()  # the drive wheels spin up
    lock: Spans = ()  # the drive wheels lock
    rumble: tuple[tuple[float, float, int], ...] = ()  # (start, end, side) on a rumble strip
    hits: tuple[Hit, ...] = ()
    split_into: tuple[int, ...] = ()  # a shift into these gears also flips the splitter
    trailer: bool = True
    events: tuple[tuple[float, str], ...] = ()  # the plugin's one-shot game events
    about: str = ""


def keyframe(keys: Keys, t: float) -> float:
    """Linear between keys, held before the first and after the last."""
    times = [k for k, _ in keys]
    i = bisect.bisect_right(times, t)
    if i == 0:
        return keys[0][1]
    if i == len(keys):
        return keys[-1][1]
    (t0, v0), (t1, v1) = keys[i - 1], keys[i]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def step(steps: tuple[tuple[float, object], ...], t: float):
    """The value set last at or before t."""
    i = bisect.bisect_right([k for k, _ in steps], t)
    return steps[max(0, i - 1)][1]


def within(spans: Spans, t: float) -> bool:
    return any(start <= t < end for start, end in spans)


def gear_rpm(kmh: float, gear: int) -> float:
    return kmh * RPM_PER_KMH * RATIOS[gear - 1]


def hit_depth(hit: Hit, distance: float) -> float:
    return hit.depth * math.exp(-(distance / hit.width) ** 2)


class Drive:
    """A scenario worked out: the gearbox's shifts and the distance driven, then a Frame for any time."""

    def __init__(self, scenario: Scenario) -> None:
        self.s = scenario
        blocks = round(scenario.seconds / DT) + 1
        self.distance: list[float] = []  # m driven at the start of each block
        self.shifts: list[tuple[float, int]] = []  # (s, gear) for each gear change, the first gear at 0
        last, driven = -SHIFT_GAP, 0.0
        self.shifts.append((0.0, self._start_gear()))
        gear = self.shifts[0][1]
        for n in range(blocks):
            t = n * DT
            self.distance.append(driven)
            kmh = keyframe(scenario.speed, t)
            driven += max(0.0, kmh) / 3.6 * DT
            new = self._shift(gear, kmh, keyframe(scenario.throttle, t), t - last)
            if new != gear:
                gear, last = new, t
                self.shifts.append((t, gear))
        self.splits = [(t - SPLIT_LEAD, g) for t, g in self.shifts[1:] if g in scenario.split_into]
        # road features where the front axle met them
        self.hits = sorted(((self._at(h.at), h) for h in scenario.hits), key=lambda e: e[0])
        self.hit_at = [pos for pos, _ in self.hits]
        self.strips = [(self._at(start), self._at(end), side) for start, end, side in scenario.rumble]

    def _start_gear(self) -> int:
        kmh, gear = keyframe(self.s.speed, 0.0), 1
        while gear < len(RATIOS) and gear_rpm(kmh, gear) > 1500.0:
            gear += 1
        return gear

    @staticmethod
    def _shift(gear: int, kmh: float, throttle: float, since: float) -> int:
        if kmh < 3.0:
            return 1  # stopped or crawling: back in the starting gear
        if since < SHIFT_GAP:
            return gear
        rpm = gear_rpm(kmh, gear)
        if gear < len(RATIOS) and rpm > 1350.0 + 400.0 * throttle and gear_rpm(kmh, gear + 1) > 1000.0:
            return gear + 1
        if gear > 1 and rpm < 950.0 + 250.0 * throttle:  # climbing on full throttle it keeps 1200
            return gear - 1
        return gear

    def _at(self, t: float) -> float:
        """Distance driven at time t."""
        n = min(len(self.distance) - 1, max(0, int(t / DT)))
        return self.distance[n] + max(0.0, keyframe(self.s.speed, t)) / 3.6 * (t - n * DT)

    def _when(self, distance: float) -> float:
        """When the front axle was at this distance."""
        return min(len(self.distance) - 1, bisect.bisect_left(self.distance, distance)) * DT

    def gear(self, t: float) -> int:
        return step(tuple(self.shifts), t)

    def splitter(self, t: float) -> bool:
        return sum(at <= t for at, _ in self.splits) % 2 == 1

    def road_features(self) -> list[float]:
        """When the front axle reaches each hit, joint and rumble strip, for the checks."""
        return sorted([h.at for h in self.s.hits] + [start for start, _, _ in self.s.rumble])

    def frame(self, t: float) -> Frame:
        s = self.s
        kmh = max(0.0, keyframe(s.speed, t))
        throttle = keyframe(s.throttle, t)
        brake = keyframe(s.brake, t)
        gear = self.gear(t)
        spin, lock = within(s.spin, t), within(s.lock, t) and kmh > 12.0
        rpm = max(IDLE, gear_rpm(kmh, gear))
        if gear == 1 and rpm < 900.0:
            rpm = max(rpm, IDLE + 600.0 * throttle)  # the clutch slips as it pulls away
        if spin:
            rpm += 300.0
        flash = lambda spans: within(spans, t) and (t - next(a for a, b in spans if a <= t < b)) % (2 * FLASH) < FLASH
        frame = Frame(active=True, paused=False, speed=kmh / 3.6, rpm=min(RPM_MAX, rpm), rpm_max=RPM_MAX,
                      user_throttle=throttle, game_throttle=throttle, user_brake=brake, game_brake=brake,
                      gear=gear, gear_engine=gear, retarder=step(s.retarder, t), retarder_steps=4,
                      motor_brake=within(s.engine_brake, t), engine_on=True, park_brake=within(s.park_brake, t),
                      blinker_left=flash(s.blinker_left), blinker_right=flash(s.blinker_right),
                      shifter_toggle=(False, self.splitter(t)), trailer_attached=s.trailer,
                      game_events=frozenset(name for at, name in s.events if at <= t < at + 0.5))
        driven = self._at(t)
        frame.wheels = [self._wheel(driven - back, x * side, kmh, spin and back > 0, lock and back > 0)
                        for back, x in TRUCK_AXLES for side in (-1, 1)]
        if s.trailer:
            frame.trailer_wheels = [self._wheel(driven - back, x * side, kmh, False, False)
                                    for back, x in TRAILER_AXLES for side in (-1, 1)]
        return frame

    def _wheel(self, at: float, x: float, kmh: float, spin: bool, lock: bool) -> Wheel:
        surface = step(self.s.surface, self._when(at))
        side = -1 if x < 0 else 1
        rough = ROUGH.get(surface, 0.003)
        deflection = 0.02 + rough * sum(share * math.sin(TAU * at / length + side * (k + 1))
                                        for k, (length, share) in enumerate(ROUGH_WAVES))
        lo, hi = bisect.bisect_left(self.hit_at, at - 6.0), bisect.bisect_right(self.hit_at, at + 6.0)
        deflection += sum(hit_depth(hit, at - pos) for pos, hit in self.hits[lo:hi] if hit.side * side >= 0)
        on_strip = any(start <= at < end and strip_side == side for start, end, strip_side in self.strips)
        if on_strip and int(at / STRIP_FLICKER) % 3 != 0:
            surface = "rumble_stripe"
        speed = kmh / 3.6
        if spin:
            speed = max(speed * 1.5, speed + 3.0)
        elif lock:
            speed *= 0.2
        return Wheel(deflection=deflection, on_ground=True, substance=surface, pos_x=x,
                     velocity=speed / (TAU * WHEEL_RADIUS), radius=WHEEL_RADIUS)


def joints(start: float, end: float, every: float, depth: float = -0.012) -> tuple[Hit, ...]:
    """Expansion joints across the road, every `every` s of the cab's time."""
    return tuple(Hit(start + k * every, 0, depth, 0.3) for k in range(int((end - start) / every) + 1))


HIGHWAY = Scenario(
    name="highway",
    about="4 min on the interstate: merge, cruise, a bridge, a drift onto the shoulder strip, "
          "an overtake, a long cruise, the exit on the engine brake and retarder",
    seconds=240.0,
    speed=((0, 60), (14, 92), (20, 95), (50, 95.8), (80, 94.6), (110, 95.2), (112, 95), (124, 105), (130, 104),
           (160, 103.2), (200, 104), (204, 103), (214, 80), (224, 55), (232, 40), (240, 40)),
    throttle=((0, 0.9), (14, 0.85), (18, 0.45), (20, 0.42), (50, 0.44), (80, 0.40), (110, 0.42), (111, 1.0),
              (124, 1.0), (126, 0.48), (160, 0.5), (200, 0.47), (203, 0.0), (232, 0.0), (233, 0.3)),
    brake=((0, 0.0), (214, 0.0), (215, 0.3), (224, 0.35), (225, 0.0)),
    surface=((0, "road"), (50, "concrete"), (57, "road"), (175, "road_coarse"), (200, "road"), (220, "concrete")),
    retarder=((0, 0), (205, 1), (209, 2), (216, 0)),
    engine_brake=((205.0, 214.0),),
    blinker_left=((12.0, 17.0), (110.0, 114.0)),
    blinker_right=((125.0, 129.0), (200.0, 205.0)),
    rumble=((80.0, 81.6, 1),),
    hits=(Hit(50.0, 0, 0.02, 1.0),) + joints(50.4, 57.0, 0.68) + (Hit(160.0, -1, -0.05),),
)

CITY = Scenario(
    name="city",
    about="2.5 min of town: pull out from the curb, lights, a right turn while braking and downshifting, "
          "manholes, a level crossing, a speed bump, a left turn and pulling over",
    seconds=150.0,
    speed=((0, 0), (8, 0), (9, 3), (16, 22), (24, 40), (30, 50), (40, 50), (46, 15), (50, 15), (58, 35), (68, 40),
           (72, 40), (80, 0), (95, 0), (97, 6), (104, 30), (106, 30), (109, 12), (111, 12), (114, 35), (120, 40),
           (127, 18), (131, 18), (136, 35), (140, 35), (146, 0)),
    throttle=((0, 0), (8, 0), (8.3, 0.7), (24, 0.7), (28, 0.5), (30, 0.35), (40, 0.35), (40.5, 0), (50, 0),
              (50.5, 0.6), (66, 0.6), (68, 0.35), (72, 0.35), (72.5, 0), (95, 0), (95.2, 1.0), (98, 1.0),
              (99, 0.7), (104, 0.4), (106, 0), (111, 0), (111.5, 0.7), (118, 0.6), (120, 0.3), (121, 0), (131, 0),
              (131.5, 0.6), (136, 0.4), (140, 0.3), (140.5, 0)),
    brake=((0, 0.45), (8, 0.45), (8.2, 0), (41, 0), (41.5, 0.35), (46, 0.3), (46.5, 0), (72.5, 0), (73.5, 0.45),
           (79, 0.3), (80, 0.4), (95, 0.4), (95.1, 0), (106, 0), (106.5, 0.4), (109, 0.3), (109.5, 0), (121.5, 0),
           (122, 0.35), (127, 0.3), (127.5, 0), (141, 0), (141.5, 0.45), (146, 0.4)),
    surface=((0, "road"), (30, "concrete"), (46, "road"), (61.8, "metal"), (62.4, "road")),
    park_brake=((0.0, 6.0), (147.0, 150.0)),
    blinker_left=((3.0, 9.0), (118.0, 129.0)),
    blinker_right=((40.0, 49.0), (138.0, 147.0)),
    spin=((95.2, 95.9),),
    hits=(Hit(34.0, -1, -0.015, 0.7), Hit(37.0, 1, -0.015, 0.7),  # manholes
          # the rails, as wide as a tyre rides them: 0.15 m was over within one frame, and a
          # wheel up for one frame and straight back is the game's glitch (effects.SPIKE_MIN)
          Hit(62.0, 0, 0.02, 0.3), Hit(62.135, 0, 0.02, 0.3),
          Hit(110.0, 0, 0.06, 1.5)),  # a speed bump
    split_into=(3, 5),
)

MOUNTAIN = Scenario(
    name="mountain",
    about="3 min over a pass, loaded: a long climb on full throttle with downshifts, the crest, "
          "a descent on the engine brake and retarder with brake snubs, potholes and a hairpin",
    seconds=180.0,
    speed=((0, 80), (10, 72), (25, 60), (45, 48), (58, 45), (65, 52), (75, 62), (90, 64), (105, 62), (110, 55),
           (125, 62), (135, 60), (140, 30), (146, 30), (152, 50), (165, 58)),
    throttle=((0, 1.0), (58, 1.0), (60, 0.6), (70, 0.3), (74, 0.0), (150, 0.0), (151, 0.6), (160, 0.45)),
    brake=((0, 0.0), (104, 0.0), (104.5, 0.5), (109, 0.5), (109.5, 0.0), (137, 0.0), (137.5, 0.6), (142, 0.5),
           (142.5, 0.0)),
    surface=((0, "road"), (60, "road_coarse"), (130, "road")),
    retarder=((0, 0), (76, 1), (84, 2), (94, 3), (137, 4), (146, 3), (150, 0)),
    engine_brake=((76.0, 150.0),),
    rumble=((160.0, 161.5, -1),),  # cutting a corner over the centre line
    hits=(Hit(100.0, 1, -0.05), Hit(101.2, 1, -0.04), Hit(120.0, 0, 0.03, 2.5)),
)

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (HIGHWAY, CITY, MOUNTAIN)}


class ScenarioTelemetry:
    """Plays a scenario like the game's plugin would, in a loop with a pause between rounds.

    As in SimulatedTelemetry the data only changes once per rendered game frame;
    fps=None makes a new frame on every read.
    """

    PAUSE = 1.0  # s

    def __init__(self, scenario: Scenario, fps: float | None = SIMULATED_FPS,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.drive = Drive(scenario)
        self.fps = fps
        self._clock = clock
        self._start = clock()
        self.name = f"{scenario.name} {fps:g} fps" if fps else scenario.name

    def close(self) -> None:
        pass

    def read(self) -> Frame:
        elapsed = self._clock() - self._start
        if self.fps:
            elapsed = math.floor(elapsed * self.fps) / self.fps
        t = elapsed % (self.drive.s.seconds + self.PAUSE)
        if t >= self.drive.s.seconds:
            frame = Frame(active=True, paused=True)
        else:
            frame = self.drive.frame(t)
        frame.render_time = int(elapsed * 1e6)
        return frame
