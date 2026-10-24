"""Turn telemetry frames into 1 kHz waveforms for the APEX 6 Pro.

Outputs: left grip, right grip and one trigger channel (on this firmware the
trigger channel always drives both triggers). Each effect is routed to the
grips, the trigger or both (Gains.routes). Telemetry is read once per 8 ms
block; amplitudes are ramped across the block and oscillator phases carry
over, so parameter changes do not click.

Resonances from the xHaptics reverse-engineering notes: grips ~65 Hz,
triggers ~95-110 Hz. Effects are placed near those to get the most feel per
unit of drive.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace

from apex6 import MAX_SAMPLE, SAMPLE_RATE, SUBFRAMES, Block, Route
from i18n import N_, t
from telemetry import Frame

DT = SUBFRAMES / SAMPLE_RATE  # 8 ms per telemetry update
TAU = 2.0 * math.pi
# Steady fade: engine and trigger settle toward a floor while their inputs
# hold still (long cruises tire the hands) and come back when they move.
STEADY_TAU = 3.0  # s for the reference to catch up with an input that stopped moving
STEADY_K = 1.0 - math.exp(-DT / STEADY_TAU)
# The plugin rewrites shared memory once per rendered game frame, so at 60 fps
# about every other block reads the frame it read before. Suspension rates are
# taken over the game time between new frames (Frame.render_time), not over a
# block: dividing by DT made each change look up to 125/fps times faster, and
# at 30 fps the bump pulses never stopped ringing.
MAX_FRAME_GAP = 0.2  # s; across a longer hitch, or the game's timer restarting, only take a new reference
RATE_AVG_TAU = 0.2  # s, the slow suspension movement that bumps stand out from
ROUGH_TAU = 0.076  # s, smoothing of the road roughness estimate

# Relative texture strength per substance (matched by substring, first match
# wins, so the specific road_* variants come before plain "road").
SURFACES = (
    ("road_smooth", 0.2), ("road_coarse", 0.45), ("road_dirt", 0.55),
    ("ice", 0.15), ("snow", 0.45),
    ("rumble", 0.3),  # the strip itself is the rumble effect; the texture stays that of asphalt
    ("road", 0.3), ("asphalt", 0.3), ("concrete", 0.35),
    ("wood", 0.55), ("metal", 0.6), ("dirt", 0.75), ("grass", 0.7), ("gravel", 1.0), ("rough", 0.9),
)
DEFAULT_SURFACE = 0.4
# Rumble strips: the game reports a "rumble_stripe" substance under a wheel.
# It flickers back to road between the grooves, so a short hold bridges the
# gaps. Grooves are milled about 30 cm apart: 83 Hz at 90 km/h.
RUMBLE_SPACING = 0.3  # m
RUMBLE_HOLD = 0.25  # s
# Trailer wheels reach the cab through the coupling: their bumps and strips
# follow the truck's own, softer.
TRAILER_FEEL = 0.5


def surface_factor(name: str) -> float:
    name = name.lower()
    if not name or name == "static":
        return 0.0
    for key, value in SURFACES:
        if key in name:
            return value
    return DEFAULT_SURFACE


TOGGLEABLE = ("engine", "road", "bumps", "rumble", "wheelspin", "gear", "retarder", "impact", "trigger",
              "pedal", "squeal", "trailer", "park_brake", "blinker", "engine_start",
              "shifter", "game_events")

# Event bursts duck the continuous effects on their channel so they stand out
# over the driving bed (sidechain ducking). Bumps come from the road itself
# and fire constantly on rough surfaces, so they do not duck it.
DUCK_EXEMPT = ("bumps",)
DUCK_FULL = 0.25  # burst envelope that ducks by the full Gains.ducking
DUCK_ATTACK = 1.0 - math.exp(-1.0 / (0.001 * SAMPLE_RATE))  # 1 ms, avoids a step in the bed
DUCK_RELEASE = math.exp(-1.0 / (0.08 * SAMPLE_RATE))  # 80 ms back to full
DUCK_LOOKAHEAD = 0.02  # delayed bursts start ducking this early, leaving a gap

GRIP, TRIGGER, BOTH = "grip", "trigger", "both"
ROUTES = (GRIP, TRIGGER, BOTH)
DEFAULT_ROUTES = {name: TRIGGER if name in ("trigger", "pedal", "squeal") else GRIP for name in TOGGLEABLE}


@dataclass
class Gains:
    master: float = 1.0
    grip_level: float = 1.0  # final output strength per channel, after all effects
    trigger_level: float = 1.0
    engine: float = 0.5
    road: float = 0.6
    bumps: float = 1.0
    rumble: float = 0.8
    wheelspin: float = 0.7
    gear: float = 0.8
    retarder: float = 0.5
    impact: float = 1.0
    trigger: float = 0.4  # xHaptics keeps triggers near 36% of full scale
    # how deep the throttle trigger pulses at the engine firing rate; 0 keeps
    # it steady, 0.8 is the fixed pulse of earlier versions
    throttle_pulse: float = 0.0
    # how far engine and throttle/brake fade while rpm, load and pedals hold
    # steady; 0 keeps them constant
    steady_fade: float = 0.6
    cylinders: int = 6
    bump_threshold: float = 0.25  # m/s of sudden suspension travel
    impact_threshold: float = 12.0  # m/s^2 change from one game frame to the next
    # event effects
    pedal: float = 0.6
    squeal: float = 0.4
    trailer: float = 0.9
    park_brake: float = 0.7
    blinker: float = 0.5
    engine_start: float = 0.8
    shifter: float = 0.5
    game_events: float = 0.5
    # how far the continuous effects dip while an event plays; 0 never
    ducking: float = 0.7
    # effects switched off; their gains are kept so switching back restores them
    disabled: list[str] = field(default_factory=list)
    # effect -> GRIP, TRIGGER or BOTH; missing or unknown entries use DEFAULT_ROUTES
    routes: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTES))


def route_of(gains: Gains, name: str) -> str:
    route = gains.routes.get(name)
    return route if route in ROUTES else DEFAULT_ROUTES[name]


def effective_gains(gains: Gains) -> Gains:
    """The gains the synth should use: disabled effects at zero."""
    result = replace(gains, disabled=list(gains.disabled), routes=dict(gains.routes))
    for name in gains.disabled:
        if name in TOGGLEABLE:
            setattr(result, name, 0.0)
    return result


GAME_EVENT_LABELS = {"delivered": N_("배송 완료"), "fined": N_("벌금"), "tollgate": N_("톨게이트"),
                     "ferry": N_("페리"), "train": N_("기차"), "refuel": N_("주유")}


class Oscillator:
    def __init__(self) -> None:
        self.phase = 0.0

    def next(self, freq: float) -> float:
        value = math.sin(TAU * self.phase)
        self.phase = (self.phase + freq / SAMPLE_RATE) % 1.0
        return value


class BandNoise:
    """White noise through a 2-pole band-pass (RBJ biquad)."""

    def __init__(self, center: float, q: float, seed: int) -> None:
        w = TAU * center / SAMPLE_RATE
        alpha = math.sin(w) / (2 * q)
        a0 = 1 + alpha
        self.b0, self.b2 = alpha / a0, -alpha / a0
        self.a1, self.a2 = -2 * math.cos(w) / a0, (1 - alpha) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0
        self.rng = random.Random(seed)

    def next(self) -> float:
        x = self.rng.uniform(-1.0, 1.0)
        y = self.b0 * x + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1, self.y2, self.y1 = self.x1, x, self.y1, y
        return y * 2.5  # roughly unit RMS-to-peak after the narrow filter


class Burst:
    """Decaying sine pulse for discrete events, optionally delayed.

    phase 0.25 starts at the crest instead of zero: a hard edge, not a swell.
    """

    def __init__(self, freq: float, amplitude: float, decay_s: float, delay_s: float = 0.0,
                 phase: float = 0.0) -> None:
        self.freq, self.amplitude, self.decay_s = freq, amplitude, decay_s
        self.age = -delay_s
        self.phase = phase

    def _envelope(self) -> float:
        return self.amplitude * math.exp(-self.age / self.decay_s)

    @property
    def level(self) -> float:
        """Current envelope, before next() advances it; full just ahead of a delayed start."""
        if self.age >= 0:
            return self._envelope()
        return self.amplitude if self.age >= -DUCK_LOOKAHEAD else 0.0

    def _carrier(self) -> float:
        value = math.sin(TAU * self.phase)
        self.phase = (self.phase + self.freq / SAMPLE_RATE) % 1.0
        return value

    def retrigger(self, amplitude: float) -> None:
        """Restart the envelope at a new peak; the carrier keeps running."""
        self.amplitude, self.age = amplitude, 0.0

    def next(self) -> float:
        if self.age < 0:
            self.age += 1.0 / SAMPLE_RATE
            return 0.0
        value = self._carrier() * self._envelope()
        self.age += 1.0 / SAMPLE_RATE
        return value

    @property
    def done(self) -> bool:
        return self.age > self.decay_s * 5


class NoiseBurst(Burst):
    """Decaying band-limited noise: air hiss, cranking."""

    def __init__(self, center: float, amplitude: float, decay_s: float, delay_s: float = 0.0) -> None:
        super().__init__(center, amplitude, decay_s, delay_s)
        self.noise = BandNoise(center, 0.8, seed=random.randrange(1 << 30))

    def _carrier(self) -> float:
        return self.noise.next()


@dataclass
class Levels:
    """Last block's effect levels, for the monitor line."""

    engine: float = 0.0
    road_left: float = 0.0
    road_right: float = 0.0
    trigger: float = 0.0
    trigger_mode: str = "-"
    surface: str = ""
    max_accel: float = 0.0
    events: list[str] = field(default_factory=list)


class Synth:
    def __init__(self, gains: Gains) -> None:
        self.g = gains
        self.engine_osc = Oscillator()
        self.engine_harm = Oscillator()
        self.trigger_osc = Oscillator()
        self.trigger_mod = Oscillator()
        self.retarder_osc = Oscillator()
        self.squeal_osc = Oscillator()
        self.squeal_mod = Oscillator()
        self.noise_left = BandNoise(80.0, 1.2, seed=1)
        self.noise_right = BandNoise(80.0, 1.2, seed=2)
        self.rumble_osc = (Oscillator(), Oscillator())
        self.spin_noise = BandNoise(45.0, 1.5, seed=3)
        self.spin_chatter = Oscillator()
        # (effect, side, burst); side "L"/"R" is one grip, "C" both. The
        # trigger has no sides and takes every burst of an effect routed there.
        self.bursts: list[tuple[str, str, Burst]] = []
        # previous-block state
        self.prev_amp = {"engine": 0.0, "road_l": 0.0, "road_r": 0.0, "rumble_l": 0.0, "rumble_r": 0.0,
                         "spin": 0.0, "retarder": 0.0, "trigger": 0.0, "squeal": 0.0}
        self.prev_state: dict[str, object] = {}
        self.blinker_idle = 0.0
        self.was_live = False
        self.prev_render_time: int | None = None
        self.prev_deflection: list[float] = []
        self.deflection_rate_avg: list[float] = []
        self.bump_bursts: list[Burst | None] = []  # each wheel's pulse while it rings
        self.roughness = [0.0, 0.0]
        # s left of each side's rumble after its wheels left the strip; truck, then trailer
        self.rumble_hold = ([0.0, 0.0], [0.0, 0.0])
        self.spin = 0.0
        self.prev_gear: int | None = None  # None until the first live frame sets the reference
        self.gear_cooldown = 0.0
        self.prev_accel: tuple[float, float, float] | None = None
        self.impact_cooldown = 0.0
        self.lock_clock = 0.0
        self.steady_ref: dict[str, float] = {}
        self.duck = [0.0, 0.0, 0.0]  # left grip, right grip, trigger; 0 = no ducking
        self.levels = Levels()

    # -- event detection, once per block ------------------------------------

    def _rebaseline(self) -> None:
        """Forget the edge detectors' last values as telemetry comes back.

        A gap (pause, menu, loading, a ferry, a new session or reconnect) can
        end with the truck elsewhere; comparing against the old values would
        fire bumps on every wheel, an impact or a gear change that never
        happened. The first live frame only becomes the new reference. Game
        events are one-shot flags, not states, so they still fire.
        """
        self.prev_render_time = None
        self.prev_deflection = []
        self.prev_accel = None
        self.prev_gear = None
        self.prev_state = {key: value for key, value in self.prev_state.items() if key == "game_events"}

    def _frame_dt(self, frame: Frame) -> float | None:
        """Game time in s since the last new frame; None when this frame was read before.

        0 when there is no usable previous frame (the first one, a hitch
        longer than MAX_FRAME_GAP, the game's timer restarting): the rate
        detectors then only take a new reference.
        """
        previous, self.prev_render_time = self.prev_render_time, frame.render_time
        if previous == frame.render_time:
            return None
        dt = 0.0 if previous is None else (frame.render_time - previous) / 1e6
        return dt if 0.0 < dt <= MAX_FRAME_GAP else 0.0

    def _wheel_events(self, frame: Frame, dt: float) -> None:
        wheels = frame.wheels + frame.trailer_wheels
        if len(self.prev_deflection) != len(wheels):
            self.prev_deflection = [w.deflection for w in wheels]
            self.deflection_rate_avg = [0.0] * len(wheels)
            self.bump_bursts = [None] * len(wheels)
            return
        if not dt:  # no rate across a gap: the travel since then is not one movement
            self.prev_deflection = [w.deflection for w in wheels]
            return
        avg_k = 1.0 - math.exp(-dt / RATE_AVG_TAU)
        rough = [0.0, 0.0]
        counts = [0, 0]
        for i, wheel in enumerate(wheels):
            rate = (wheel.deflection - self.prev_deflection[i]) / dt  # m/s
            self.prev_deflection[i] = wheel.deflection
            avg = self.deflection_rate_avg[i]
            avg = self.deflection_rate_avg[i] = avg + (rate - avg) * avg_k
            high = abs(rate - avg)
            side = 0 if wheel.pos_x < 0 else 1
            trailer = i >= len(frame.wheels)
            if not trailer:  # road texture is what the truck's own tyres feel
                rough[side] += high
                counts[side] += 1
            if not wheel.on_ground:
                continue
            threshold = self.g.bump_threshold
            if high > threshold:  # a step in suspension travel: pothole, kerb, joint
                strength = min(1.0, (high - threshold) / 0.8) * self.g.bumps * (TRAILER_FEEL if trailer else 1.0)
                current = self.bump_bursts[i]
                if current is not None and not current.done:
                    # One pulse per wheel: a pothole spans several blocks and rough
                    # ground trips every block, so lift the ringing pulse to the
                    # stronger hit instead of stacking more. Keeps the work bounded
                    # by the wheel count once trailers multiply the wheels.
                    if strength > current.level:
                        current.retrigger(strength)
                    continue
                where = "C" if abs(wheel.pos_x) < 0.15 else "LR"[side]
                burst = Burst(freq=60.0, amplitude=strength, decay_s=0.035)
                self.bursts.append(("bumps", where, burst))
                self.bump_bursts[i] = burst
        rough_k = 1.0 - math.exp(-dt / ROUGH_TAU)
        for side in (0, 1):
            value = rough[side] / counts[side] if counts[side] else 0.0
            self.roughness[side] += (value - self.roughness[side]) * rough_k

    def _gear_event(self, frame: Frame) -> None:
        self.gear_cooldown = max(0.0, self.gear_cooldown - DT)
        if self.prev_gear is None:  # first live frame: already in this gear, nothing shifted
            self.prev_gear = frame.gear
            return
        if frame.gear != self.prev_gear and frame.gear != 0 and self.gear_cooldown == 0.0:
            # Clutch gap, then the dog clutch engaging: the bed ducks 20 ms
            # ahead, a hard-edged hit near the grip resonance lands in the
            # gap and a low body follows.
            g = self.g.gear
            self._both("gear", Burst(62.0, 1.3 * g, 0.018, delay_s=0.02, phase=0.25))
            self._both("gear", Burst(38.0, 0.5 * g, 0.05, delay_s=0.02))
            self.gear_cooldown = 0.15
            self.levels.events.append(t("변속 {gear}단", gear=frame.gear))
        if frame.gear != 0:
            self.prev_gear = frame.gear

    def _impact_event(self, frame: Frame, dt: float) -> None:
        self.impact_cooldown = max(0.0, self.impact_cooldown - dt)
        ax, _, az = frame.accel
        horizontal = math.hypot(ax, az)
        self.levels.max_accel = max(self.levels.max_accel, horizontal)
        if dt and self.prev_accel is not None and self.impact_cooldown == 0.0:
            px, _, pz = self.prev_accel
            jump = math.hypot(ax - px, az - pz)
            threshold = self.g.impact_threshold
            if jump > threshold:
                strength = min(1.0, (jump - threshold) / 30.0 + 0.4) * self.g.impact
                self._both("impact", Burst(50.0, strength, 0.06))
                self.impact_cooldown = 0.3
                self.levels.events.append(t("충돌 {accel} m/s²", accel=f"{jump:.0f}"))
        self.prev_accel = frame.accel

    def _changed(self, key: str, value: object) -> bool:
        """True when value differs from the last frame's (never on the first frame)."""
        old = self.prev_state.get(key, value)
        self.prev_state[key] = value
        return old != value

    def _both(self, effect: str, burst: Burst) -> None:
        self.bursts.append((effect, "C", burst))

    def _state_events(self, frame: Frame) -> None:
        g, log = self.g, self.levels.events

        for key, value in (("throttle", frame.user_throttle), ("brake", frame.user_brake)):
            floored = value >= 0.97
            if self._changed(f"{key}_floor", floored) and floored and g.pedal:
                self._both("pedal", Burst(130.0, 0.8 * g.pedal, 0.012))
                log.append(t("페달 바닥"))

        if self._changed("park_brake", frame.park_brake) and g.park_brake:
            s = g.park_brake
            self._both("park_brake", Burst(90.0, 1.0 * s, 0.015))
            hiss = 0.25 if frame.park_brake else 0.1
            self._both("park_brake", NoiseBurst(250.0, 0.5 * s, hiss, delay_s=0.03))
            log.append(t("파킹 브레이크 체결") if frame.park_brake else t("파킹 브레이크 해제"))

        if self._changed("trailer", frame.trailer_attached) and g.trailer:
            s = g.trailer
            if frame.trailer_attached:
                self._both("trailer", Burst(40.0, 1.0 * s, 0.08))
                log.append(t("트레일러 연결"))
            else:
                self._both("trailer", Burst(50.0, 0.9 * s, 0.05))
                self._both("trailer", NoiseBurst(250.0, 0.4 * s, 0.15, delay_s=0.15))
                log.append(t("트레일러 분리"))

        # The lamp flag toggles with every flash; the relay clicks on both edges.
        self.blinker_idle += DT
        for key, on, side in (("blinker_l", frame.blinker_left, "L"),
                              ("blinker_r", frame.blinker_right, "R")):
            if self._changed(key, on) and g.blinker:
                self.bursts.append(("blinker", side, Burst(160.0, (1.0 if on else 0.7) * g.blinker, 0.01)))
                if self.blinker_idle > 1.5:
                    log.append(t("방향지시등 좌") if key == "blinker_l" else t("방향지시등 우"))
                self.blinker_idle = 0.0

        if self._changed("engine_on", frame.engine_on) and g.engine_start:
            s = g.engine_start
            if frame.engine_on:
                self._both("engine_start", NoiseBurst(60.0, 0.8 * s, 0.35))
                self._both("engine_start", Burst(35.0, 0.9 * s, 0.25, delay_s=0.25))
                log.append(t("시동"))
            else:
                self._both("engine_start", Burst(30.0, 0.7 * s, 0.3))
                log.append(t("시동 끔"))

        for i, toggle in enumerate(frame.shifter_toggle):
            if self._changed(f"shifter_{i}", toggle) and g.shifter:
                s = g.shifter
                self._both("shifter", Burst(120.0, 1.0 * s, 0.012))
                log.append(t("레인지 셀렉터") if i == 0 else t("스플리터"))

        previous = self.prev_state.get("game_events", frozenset())
        self.prev_state["game_events"] = frame.game_events
        for name in sorted(frame.game_events - previous):
            if g.game_events:
                s = g.game_events
                self._both("game_events", Burst(80.0, 1.0 * s, 0.03))
                self._both("game_events", Burst(80.0, 1.0 * s, 0.03, delay_s=0.12))
            log.append(t(GAME_EVENT_LABELS[name]) if name in GAME_EVENT_LABELS else name)

    def _novelty(self, key: str, value: float, scale: float) -> float:
        """1 just after value moved by scale or more, falling to 0 as it holds steady."""
        ref = self.steady_ref.setdefault(key, value)
        self.steady_ref[key] = ref + (value - ref) * STEADY_K
        return min(1.0, abs(value - ref) / scale)

    def _steady_gain(self, novelty: float) -> float:
        floor = 1.0 - min(1.0, max(0.0, self.g.steady_fade))
        return floor + (1.0 - floor) * novelty

    def _wheel_spin(self, frame: Frame) -> float:
        """Share of grounded wheels turning clearly faster than the truck moves."""
        speed = abs(frame.speed)
        grounded = [w for w in frame.wheels if w.on_ground]
        if not grounded:
            return 0.0
        spinning = 0
        for wheel in grounded:
            excess = abs(wheel.velocity) * TAU * wheel.radius - speed
            # 25% clears the outer wheels of a tight turn; 1 m/s the creep off the line
            if excess > max(1.0, 0.25 * speed):
                spinning += 1
        return spinning / len(grounded)

    def _wheel_lock(self, frame: Frame) -> float:
        speed = abs(frame.speed)
        if speed < 3.0:
            return 0.0
        grounded = [w for w in frame.wheels if w.on_ground]
        if not grounded:
            return 0.0
        locked = 0
        for wheel in grounded:
            surface_speed = abs(wheel.velocity) * TAU * wheel.radius
            if (speed - surface_speed) / speed > 0.3:
                locked += 1
        return locked / len(grounded)

    # -- block rendering ------------------------------------------------------

    def render(self, frame: Frame | None) -> Block:
        self.levels.events = []
        live = frame is not None and frame.live
        g = self.g

        engine_amp = road_l = road_r = rumble_l = rumble_r = spin_amp = retarder_amp = trigger_amp = squeal_amp = 0.0
        engine_freq = rumble_freq = 40.0
        trigger_mode = "-"
        lock = 0.0
        if live and not self.was_live:
            self._rebaseline()
        self.was_live = live
        if live:
            dt = self._frame_dt(frame)
            if dt is not None:  # the same frame read again holds no new movement
                self._wheel_events(frame, dt)
                self._impact_event(frame, dt)
            self._gear_event(frame)
            self._state_events(frame)

            # Brake squeal: light braking while rolling to a stop.
            speed = abs(frame.speed)
            braking = max(frame.user_brake, frame.game_brake)
            if braking > 0.08 and 0.3 < speed < 4.0:
                squeal_amp = g.squeal * 0.35 * math.sqrt(1.0 - speed / 4.0)

            load = max(frame.game_throttle, 0.0)
            rpm_frac = min(1.0, frame.rpm / frame.rpm_max)
            engine_moved = max(self._novelty("load", load, 0.15), self._novelty("rpm", rpm_frac, 0.06))
            if frame.engine_on and frame.rpm > 100:
                # Firing frequency of a 4-stroke engine: rpm/60 * cylinders/2.
                engine_freq = max(25.0, min(140.0, frame.rpm / 60.0 * g.cylinders / 2.0))
                engine_amp = g.engine * (0.25 + 0.55 * load + 0.2 * rpm_frac) * self._steady_gain(engine_moved)

            speed_factor = min(1.0, abs(frame.speed) / 25.0) ** 0.7
            sides = ([w for w in frame.wheels if w.pos_x < 0 and w.on_ground],
                     [w for w in frame.wheels if w.pos_x >= 0 and w.on_ground])
            road = []
            for side, wheels in enumerate(sides):
                surface = sum(surface_factor(w.substance) for w in wheels) / len(wheels) if wheels else 0.0
                texture = surface * speed_factor + min(1.0, self.roughness[side] * 1.5)
                road.append(g.road * min(1.0, texture))
            road_l, road_r = road
            rumble_freq = max(25.0, min(120.0, abs(frame.speed) / RUMBLE_SPACING))
            trailer_sides = ([w for w in frame.trailer_wheels if w.pos_x < 0 and w.on_ground],
                             [w for w in frame.trailer_wheels if w.pos_x >= 0 and w.on_ground])
            rumble = [0.0, 0.0]
            for hold, groups, feel in ((self.rumble_hold[0], sides, 1.0),
                                       (self.rumble_hold[1], trailer_sides, TRAILER_FEEL)):
                for side, wheels in enumerate(groups):
                    if any("rumble" in w.substance.lower() for w in wheels):
                        hold[side] = RUMBLE_HOLD
                    else:
                        hold[side] = max(0.0, hold[side] - DT)
                    if hold[side] > 0:
                        rumble[side] = max(rumble[side], feel)
            strip = g.rumble * min(1.0, abs(frame.speed) / 15.0) ** 0.5 if abs(frame.speed) > 1.0 else 0.0
            rumble_l, rumble_r = strip * rumble[0], strip * rumble[1]

            # Wheelspin: driven wheels outrunning the truck under power (ice, mud, a hard launch).
            powered = frame.engine_on and max(frame.user_throttle, frame.game_throttle) > 0.05
            spin = self._wheel_spin(frame) if powered else 0.0
            self.spin = max(spin, self.spin - DT / 0.15)  # let go over 150 ms, not on one frame
            if self.spin > 0:
                spin_amp = g.wheelspin * min(1.0, 0.4 + self.spin)
            if frame.wheels:
                self.levels.surface = frame.wheels[0].substance

            if frame.retarder and frame.retarder_steps:
                retarder_amp = g.retarder * frame.retarder / frame.retarder_steps * min(1.0, abs(frame.speed) / 5)
            elif frame.motor_brake:
                retarder_amp = g.retarder * 0.3 * min(1.0, abs(frame.speed) / 5)

            # One trigger channel: follow whichever pedal the player is pressing.
            # A wheel lock is a warning and never fades.
            lock = self._wheel_lock(frame) if frame.user_brake > 0.05 else 0.0
            throttle_moved = self._novelty("throttle", frame.user_throttle, 0.1)
            brake_moved = self._novelty("brake", frame.user_brake, 0.1)
            if frame.user_brake > 0.05 and frame.user_brake >= frame.user_throttle:
                trigger_mode = "brake"
                if lock > 0:
                    trigger_amp = g.trigger * min(1.0, 0.6 + lock)
                else:
                    trigger_amp = (g.trigger * 0.25 * frame.user_brake * speed_factor
                                   * self._steady_gain(brake_moved))
            elif frame.user_throttle > 0.05 and frame.engine_on:
                trigger_mode = "throttle"
                trigger_amp = g.trigger * (0.2 + 0.6 * frame.user_throttle) * self._steady_gain(throttle_moved)

        self.lock_clock = (self.lock_clock + DT) % (1.0 / 12.0)  # 12 Hz ABS-like chop
        routes = {name: route_of(g, name) for name in TOGGLEABLE}
        trigger, left, right = [], [], []
        p = self.prev_amp
        for n in range(SUBFRAMES):
            k = (n + 1) / SUBFRAMES
            e = p["engine"] + (engine_amp - p["engine"]) * k
            rl = p["road_l"] + (road_l - p["road_l"]) * k
            rr = p["road_r"] + (road_r - p["road_r"]) * k
            ul = p["rumble_l"] + (rumble_l - p["rumble_l"]) * k
            ur = p["rumble_r"] + (rumble_r - p["rumble_r"]) * k
            sp = p["spin"] + (spin_amp - p["spin"]) * k
            rt = p["retarder"] + (retarder_amp - p["retarder"]) * k
            tr = p["trigger"] + (trigger_amp - p["trigger"]) * k
            sq = p["squeal"] + (squeal_amp - p["squeal"]) * k

            out = [0.0, 0.0, 0.0]  # left grip, right grip, trigger: events
            bed = [0.0, 0.0, 0.0]  # continuous effects, ducked under the events
            engine = e * (self.engine_osc.next(engine_freq) + 0.3 * self.engine_harm.next(engine_freq * 2))
            _mix(bed, routes["engine"], engine, engine, engine)
            retarder = rt * self.retarder_osc.next(50.0)
            _mix(bed, routes["retarder"], retarder, retarder, retarder)
            # Independent noises: 0.7 keeps their sum near one side's strength.
            noise_l, noise_r = rl * self.noise_left.next(), rr * self.noise_right.next()
            _mix(bed, routes["road"], noise_l, noise_r, 0.7 * (noise_l + noise_r))
            if ul or ur:
                # Soft-clipped sine: each groove lands as a hard edge, a buzz, not a hum.
                strip_l = ul * math.tanh(2.5 * self.rumble_osc[0].next(rumble_freq))
                strip_r = ur * math.tanh(2.5 * self.rumble_osc[1].next(rumble_freq))
                _mix(bed, routes["rumble"], strip_l, strip_r, 0.7 * (strip_l + strip_r))
            if sp:
                # Low grinding noise, chattering as the tyres hop between grip and slip.
                # 2x: noise is spiky, and this has to stand out over a floored engine.
                grind = 2.0 * sp * self.spin_noise.next() * (0.55 + 0.45 * self.spin_chatter.next(13.0))
                _mix(bed, routes["wheelspin"], grind, grind, grind)

            if trigger_mode == "brake" and lock > 0:
                chop = 1.0 if self.lock_clock < 1.0 / 24.0 else 0.0
                t = tr * chop * self.trigger_osc.next(110.0)
            elif trigger_mode == "throttle":
                # 100 Hz carrier, optionally pulsed at the engine firing rate.
                depth = min(1.0, max(0.0, g.throttle_pulse))
                pulse = 1.0 - depth * 0.5 * (1.0 - self.trigger_mod.next(engine_freq))
                t = tr * pulse * self.trigger_osc.next(100.0)
            else:
                t = tr * self.trigger_osc.next(100.0)
            _mix(bed, routes["trigger"], t, t, t)
            if sq:
                wobble = 0.6 + 0.4 * self.squeal_mod.next(5.0)
                squeal = sq * wobble * self.squeal_osc.next(230.0)
                _mix(out, routes["squeal"], squeal, squeal, squeal)
            drive = [0.0, 0.0, 0.0]
            for effect, side, burst in self.bursts:
                if effect not in DUCK_EXEMPT:
                    d = burst.level
                    _mix(drive, routes[effect], 0.0 if side == "R" else d, 0.0 if side == "L" else d, d)
                b = burst.next()
                _mix(out, routes[effect], 0.0 if side == "R" else b, 0.0 if side == "L" else b, b)
            for i in range(3):
                target = min(1.0, drive[i] / DUCK_FULL) * g.ducking
                if target > self.duck[i]:
                    self.duck[i] += (target - self.duck[i]) * DUCK_ATTACK
                else:
                    self.duck[i] = max(target, self.duck[i] * DUCK_RELEASE)
                out[i] += bed[i] * (1.0 - self.duck[i])

            left.append(self._out(out[0], self.g.grip_level))
            right.append(self._out(out[1], self.g.grip_level))
            trigger.append(self._out(out[2], self.g.trigger_level))
        self.prev_amp = {"engine": engine_amp, "road_l": road_l, "road_r": road_r,
                         "rumble_l": rumble_l, "rumble_r": rumble_r, "spin": spin_amp, "retarder": retarder_amp,
                         "trigger": trigger_amp, "squeal": squeal_amp}
        self.bursts = [entry for entry in self.bursts if not entry[2].done]

        lv = self.levels
        lv.engine, lv.road_left, lv.road_right = engine_amp, road_l, road_r
        lv.trigger, lv.trigger_mode = trigger_amp, trigger_mode
        any_trigger = any(trigger)
        return Block(trigger=trigger, left_grip=left, right_grip=right,
                     route=Route.BOTH if any_trigger else Route.MUTE)

    def _out(self, x: float, channel_level: float) -> float:
        # Soft limit so stacked effects saturate smoothly instead of clipping;
        # never exceeds MAX_SAMPLE.
        return math.tanh(x * self.g.master * channel_level) * MAX_SAMPLE


def _mix(out: list[float], route: str, left: float, right: float, trigger: float) -> None:
    """Add one effect's sample to the channels its route selects."""
    if route != TRIGGER:
        out[0] += left
        out[1] += right
    if route != GRIP:
        out[2] += trigger
