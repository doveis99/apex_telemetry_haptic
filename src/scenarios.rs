//! Test drives made of real driving situations (port of scenarios.py): a downshift while
//! braking with the blinker on, the retarder over potholes on a descent, a long cruise in
//! which the steady fade settles. Each is a few minutes of what the driver does over time:
//!
//!   speed, throttle, brake    keyframes, linear in between (speed in km/h)
//!   surface, retarder         the value from each time on, until the next
//!   blinkers, engine brake... (start, end) spans
//!   potholes, joints, rails   when the front axle reaches them
//!
//! Road features stay where the cab met them, so the axles behind reach them as far back
//! as they sit. Gears follow the speed through a 12-speed gearbox that shifts like an
//! automated one. The scenarios play in a loop with a second of pause between rounds.
//! Forward driving only: road positions come from the distance driven.

use std::time::Instant;

use crate::effects::DT;
use crate::pymath as m;
use crate::telemetry::{format_g, Frame, TelemetrySource, Wheel};

const TAU: f64 = 2.0 * m::PI;
const RATIOS: [f64; 12] = [14.93, 11.67, 9.02, 7.06, 5.63, 4.40, 3.39, 2.65, 2.05, 1.60, 1.28, 1.0];
const RPM_PER_KMH: f64 = 13.0; // in top gear: 1235 rpm at 95 km/h
const IDLE: f64 = 650.0;
const RPM_MAX: f64 = 2100.0;
pub const SHIFT_GAP: f64 = 1.5; // s between shifts; the gearbox waits for the last one to settle
pub const SPLIT_LEAD: f64 = 0.3; // s the splitter is flipped before the clutch goes in
const FLASH: f64 = 0.4; // s the blinker lamp stays on, then off
const WHEEL_RADIUS: f64 = 0.5;
/// (m behind the front axle, pos_x): steer axle, two drive axles, the trailer's tandem
pub const TRUCK_AXLES: [(f64, f64); 3] = [(0.0, 1.0), (5.0, 0.9), (6.3, 0.9)];
pub const TRAILER_AXLES: [(f64, f64); 2] = [(13.0, 0.98), (14.3, 0.98)];
/// Fine unevenness of each surface, m of suspension movement (scenarios.py has the reasoning).
const ROUGH: [(&str, f64); 5] =
    [("road", 0.0009), ("concrete", 0.0009), ("road_coarse", 0.00105), ("gravel", 0.0018), ("metal", 0.0003)];
const ROUGH_OTHER: f64 = 0.003;
const ROUGH_WAVES: [(f64, f64); 2] = [(5.0, 0.8), (2.5, 0.2)]; // (wavelength m, share)
const STRIP_FLICKER: f64 = 2.6; // m: the game reports road under one groove in three

/// A spot on the road the suspension rides over.
#[derive(Clone, Copy, Debug)]
pub struct Hit {
    /// s, when the front axle reaches it
    pub at: f64,
    /// -1 the left wheels, 1 the right, 0 both
    pub side: i32,
    /// m; negative drops into a hole, positive rides up a bump
    pub depth: f64,
    /// m along the road
    pub width: f64,
}

const fn hit(at: f64, side: i32, depth: f64) -> Hit {
    Hit { at, side, depth, width: 0.6 }
}

const fn hit_w(at: f64, side: i32, depth: f64, width: f64) -> Hit {
    Hit { at, side, depth, width }
}

#[derive(Clone, Debug)]
pub struct Scenario {
    pub name: &'static str,
    pub seconds: f64,
    /// km/h
    pub speed: Vec<(f64, f64)>,
    pub throttle: Vec<(f64, f64)>,
    pub brake: Vec<(f64, f64)>,
    pub surface: Vec<(f64, &'static str)>,
    pub retarder: Vec<(f64, i64)>,
    pub engine_brake: Vec<(f64, f64)>,
    pub blinker_left: Vec<(f64, f64)>,
    pub blinker_right: Vec<(f64, f64)>,
    pub park_brake: Vec<(f64, f64)>,
    /// the drive wheels spin up
    pub spin: Vec<(f64, f64)>,
    /// the drive wheels lock
    pub lock: Vec<(f64, f64)>,
    /// (start, end, side) on a rumble strip
    pub rumble: Vec<(f64, f64, i32)>,
    pub hits: Vec<Hit>,
    /// a shift into these gears also flips the splitter
    pub split_into: Vec<i64>,
    pub trailer: bool,
    /// the plugin's one-shot game events
    pub events: Vec<(f64, &'static str)>,
    pub about: &'static str,
}

impl Default for Scenario {
    fn default() -> Scenario {
        Scenario {
            name: "",
            seconds: 0.0,
            speed: Vec::new(),
            throttle: Vec::new(),
            brake: vec![(0.0, 0.0)],
            surface: vec![(0.0, "road")],
            retarder: vec![(0.0, 0)],
            engine_brake: Vec::new(),
            blinker_left: Vec::new(),
            blinker_right: Vec::new(),
            park_brake: Vec::new(),
            spin: Vec::new(),
            lock: Vec::new(),
            rumble: Vec::new(),
            hits: Vec::new(),
            split_into: Vec::new(),
            trailer: true,
            events: Vec::new(),
            about: "",
        }
    }
}

/// Linear between keys, held before the first and after the last.
pub fn keyframe(keys: &[(f64, f64)], t: f64) -> f64 {
    let i = keys.partition_point(|(k, _)| *k <= t); // bisect_right
    if i == 0 {
        return keys[0].1;
    }
    if i == keys.len() {
        return keys[keys.len() - 1].1;
    }
    let ((t0, v0), (t1, v1)) = (keys[i - 1], keys[i]);
    v0 + (v1 - v0) * (t - t0) / (t1 - t0)
}

/// The value set last at or before t.
pub fn step<T: Copy>(steps: &[(f64, T)], t: f64) -> T {
    let i = steps.partition_point(|(k, _)| *k <= t);
    steps[i.max(1) - 1].1
}

pub fn within(spans: &[(f64, f64)], t: f64) -> bool {
    spans.iter().any(|&(start, end)| start <= t && t < end)
}

fn gear_rpm(kmh: f64, gear: i64) -> f64 {
    kmh * RPM_PER_KMH * RATIOS[(gear - 1) as usize]
}

fn hit_depth(hit: &Hit, distance: f64) -> f64 {
    hit.depth * m::exp(-m::pow(distance / hit.width, 2.0))
}

/// Expansion joints across the road, every `every` s of the cab's time.
fn joints(start: f64, end: f64, every: f64, depth: f64) -> Vec<Hit> {
    (0..=((end - start) / every) as i64).map(|k| hit_w(start + k as f64 * every, 0, depth, 0.3)).collect()
}

fn keys(pairs: &[(f64, f64)]) -> Vec<(f64, f64)> {
    pairs.to_vec()
}

pub fn highway() -> Scenario {
    let mut hits = vec![hit_w(50.0, 0, 0.02, 1.0)];
    hits.extend(joints(50.4, 57.0, 0.68, -0.012));
    hits.push(hit(160.0, -1, -0.05));
    Scenario {
        name: "highway",
        about: "4 min on the interstate: merge, cruise, a bridge, a drift onto the shoulder strip, \
                an overtake, a long cruise, the exit on the engine brake and retarder",
        seconds: 240.0,
        speed: keys(&[(0.0, 60.0), (14.0, 92.0), (20.0, 95.0), (50.0, 95.8), (80.0, 94.6), (110.0, 95.2),
                      (112.0, 95.0), (124.0, 105.0), (130.0, 104.0), (160.0, 103.2), (200.0, 104.0), (204.0, 103.0),
                      (214.0, 80.0), (224.0, 55.0), (232.0, 40.0), (240.0, 40.0)]),
        throttle: keys(&[(0.0, 0.9), (14.0, 0.85), (18.0, 0.45), (20.0, 0.42), (50.0, 0.44), (80.0, 0.40),
                         (110.0, 0.42), (111.0, 1.0), (124.0, 1.0), (126.0, 0.48), (160.0, 0.5), (200.0, 0.47),
                         (203.0, 0.0), (232.0, 0.0), (233.0, 0.3)]),
        brake: keys(&[(0.0, 0.0), (214.0, 0.0), (215.0, 0.3), (224.0, 0.35), (225.0, 0.0)]),
        surface: vec![(0.0, "road"), (50.0, "concrete"), (57.0, "road"), (175.0, "road_coarse"), (200.0, "road"),
                      (220.0, "concrete")],
        retarder: vec![(0.0, 0), (205.0, 1), (209.0, 2), (216.0, 0)],
        engine_brake: vec![(205.0, 214.0)],
        blinker_left: vec![(12.0, 17.0), (110.0, 114.0)],
        blinker_right: vec![(125.0, 129.0), (200.0, 205.0)],
        rumble: vec![(80.0, 81.6, 1)],
        hits,
        ..Scenario::default()
    }
}

pub fn city() -> Scenario {
    Scenario {
        name: "city",
        about: "2.5 min of town: pull out from the curb, lights, a right turn while braking and downshifting, \
                manholes, a level crossing, a speed bump, a left turn and pulling over",
        seconds: 150.0,
        speed: keys(&[(0.0, 0.0), (8.0, 0.0), (9.0, 3.0), (16.0, 22.0), (24.0, 40.0), (30.0, 50.0), (40.0, 50.0),
                      (46.0, 15.0), (50.0, 15.0), (58.0, 35.0), (68.0, 40.0), (72.0, 40.0), (80.0, 0.0),
                      (95.0, 0.0), (97.0, 6.0), (104.0, 30.0), (106.0, 30.0), (109.0, 12.0), (111.0, 12.0),
                      (114.0, 35.0), (120.0, 40.0), (127.0, 18.0), (131.0, 18.0), (136.0, 35.0), (140.0, 35.0),
                      (146.0, 0.0)]),
        throttle: keys(&[(0.0, 0.0), (8.0, 0.0), (8.3, 0.7), (24.0, 0.7), (28.0, 0.5), (30.0, 0.35),
                         (40.0, 0.35), (40.5, 0.0), (50.0, 0.0), (50.5, 0.6), (66.0, 0.6), (68.0, 0.35),
                         (72.0, 0.35), (72.5, 0.0), (95.0, 0.0), (95.2, 1.0), (98.0, 1.0), (99.0, 0.7),
                         (104.0, 0.4), (106.0, 0.0), (111.0, 0.0), (111.5, 0.7), (118.0, 0.6), (120.0, 0.3),
                         (121.0, 0.0), (131.0, 0.0), (131.5, 0.6), (136.0, 0.4), (140.0, 0.3), (140.5, 0.0)]),
        brake: keys(&[(0.0, 0.45), (8.0, 0.45), (8.2, 0.0), (41.0, 0.0), (41.5, 0.35), (46.0, 0.3),
                      (46.5, 0.0), (72.5, 0.0), (73.5, 0.45), (79.0, 0.3), (80.0, 0.4), (95.0, 0.4),
                      (95.1, 0.0), (106.0, 0.0), (106.5, 0.4), (109.0, 0.3), (109.5, 0.0), (121.5, 0.0),
                      (122.0, 0.35), (127.0, 0.3), (127.5, 0.0), (141.0, 0.0), (141.5, 0.45), (146.0, 0.4)]),
        surface: vec![(0.0, "road"), (30.0, "concrete"), (46.0, "road"), (61.8, "metal"), (62.4, "road")],
        park_brake: vec![(0.0, 6.0), (147.0, 150.0)],
        blinker_left: vec![(3.0, 9.0), (118.0, 129.0)],
        blinker_right: vec![(40.0, 49.0), (138.0, 147.0)],
        spin: vec![(95.2, 95.9)],
        hits: vec![
            hit_w(34.0, -1, -0.015, 0.7), // manholes
            hit_w(37.0, 1, -0.015, 0.7),
            hit_w(62.0, 0, 0.02, 0.3), // the rails, as wide as a tyre rides them
            hit_w(62.135, 0, 0.02, 0.3),
            hit_w(110.0, 0, 0.06, 1.5), // a speed bump
        ],
        split_into: vec![3, 5],
        ..Scenario::default()
    }
}

pub fn mountain() -> Scenario {
    Scenario {
        name: "mountain",
        about: "3 min over a pass, loaded: a long climb on full throttle with downshifts, the crest, \
                a descent on the engine brake and retarder with brake snubs, potholes and a hairpin",
        seconds: 180.0,
        speed: keys(&[(0.0, 80.0), (10.0, 72.0), (25.0, 60.0), (45.0, 48.0), (58.0, 45.0), (65.0, 52.0),
                      (75.0, 62.0), (90.0, 64.0), (105.0, 62.0), (110.0, 55.0), (125.0, 62.0), (135.0, 60.0),
                      (140.0, 30.0), (146.0, 30.0), (152.0, 50.0), (165.0, 58.0)]),
        throttle: keys(&[(0.0, 1.0), (58.0, 1.0), (60.0, 0.6), (70.0, 0.3), (74.0, 0.0), (150.0, 0.0),
                         (151.0, 0.6), (160.0, 0.45)]),
        brake: keys(&[(0.0, 0.0), (104.0, 0.0), (104.5, 0.5), (109.0, 0.5), (109.5, 0.0), (137.0, 0.0),
                      (137.5, 0.6), (142.0, 0.5), (142.5, 0.0)]),
        surface: vec![(0.0, "road"), (60.0, "road_coarse"), (130.0, "road")],
        retarder: vec![(0.0, 0), (76.0, 1), (84.0, 2), (94.0, 3), (137.0, 4), (146.0, 3), (150.0, 0)],
        engine_brake: vec![(76.0, 150.0)],
        rumble: vec![(160.0, 161.5, -1)], // cutting a corner over the centre line
        hits: vec![hit(100.0, 1, -0.05), hit(101.2, 1, -0.04), hit_w(120.0, 0, 0.03, 2.5)],
        ..Scenario::default()
    }
}

/// The scenarios by name, in the order the window lists them.
pub fn scenarios() -> Vec<Scenario> {
    vec![highway(), city(), mountain()]
}

pub fn scenario(name: &str) -> Option<Scenario> {
    scenarios().into_iter().find(|s| s.name == name)
}

/// A scenario worked out: the gearbox's shifts and the distance driven, then a Frame for any time.
#[derive(Clone, Debug)]
pub struct Drive {
    pub s: Scenario,
    /// m driven at the start of each block
    distance: Vec<f64>,
    /// (s, gear) for each gear change, the first gear at 0
    pub shifts: Vec<(f64, i64)>,
    splits: Vec<(f64, i64)>,
    /// road features where the front axle met them
    hits: Vec<(f64, Hit)>,
    hit_at: Vec<f64>,
    strips: Vec<(f64, f64, i32)>,
}

impl Drive {
    pub fn new(scenario: Scenario) -> Drive {
        let blocks = m::round_int(scenario.seconds / DT) + 1;
        let mut drive = Drive {
            s: scenario,
            distance: Vec::with_capacity(blocks as usize),
            shifts: Vec::new(),
            splits: Vec::new(),
            hits: Vec::new(),
            hit_at: Vec::new(),
            strips: Vec::new(),
        };
        let (mut last, mut driven) = (-SHIFT_GAP, 0.0);
        let mut gear = drive.start_gear();
        drive.shifts.push((0.0, gear));
        for n in 0..blocks {
            let t = n as f64 * DT;
            drive.distance.push(driven);
            let kmh = keyframe(&drive.s.speed, t);
            driven += m::max(0.0, kmh) / 3.6 * DT;
            let new = Drive::shift(gear, kmh, keyframe(&drive.s.throttle, t), t - last);
            if new != gear {
                gear = new;
                last = t;
                drive.shifts.push((t, gear));
            }
        }
        drive.splits = drive.shifts[1..]
            .iter()
            .filter(|(_, g)| drive.s.split_into.contains(g))
            .map(|&(t, g)| (t - SPLIT_LEAD, g))
            .collect();
        let mut hits: Vec<(f64, Hit)> = drive.s.hits.iter().map(|h| (drive.at(h.at), *h)).collect();
        hits.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap()); // stable, as Python's sorted
        drive.hit_at = hits.iter().map(|(pos, _)| *pos).collect();
        drive.hits = hits;
        drive.strips = drive.s.rumble.iter().map(|&(start, end, side)| (drive.at(start), drive.at(end), side)).collect();
        drive
    }

    fn start_gear(&self) -> i64 {
        let kmh = keyframe(&self.s.speed, 0.0);
        let mut gear = 1;
        while gear < RATIOS.len() as i64 && gear_rpm(kmh, gear) > 1500.0 {
            gear += 1;
        }
        gear
    }

    fn shift(gear: i64, kmh: f64, throttle: f64, since: f64) -> i64 {
        if kmh < 3.0 {
            return 1; // stopped or crawling: back in the starting gear
        }
        if since < SHIFT_GAP {
            return gear;
        }
        let rpm = gear_rpm(kmh, gear);
        if gear < RATIOS.len() as i64 && rpm > 1350.0 + 400.0 * throttle && gear_rpm(kmh, gear + 1) > 1000.0 {
            return gear + 1;
        }
        if gear > 1 && rpm < 950.0 + 250.0 * throttle {
            // climbing on full throttle it keeps 1200
            return gear - 1;
        }
        gear
    }

    /// Distance driven at time t.
    fn at(&self, t: f64) -> f64 {
        let n = ((t / DT) as i64).max(0).min(self.distance.len() as i64 - 1) as usize;
        self.distance[n] + m::max(0.0, keyframe(&self.s.speed, t)) / 3.6 * (t - n as f64 * DT)
    }

    /// When the front axle was at this distance.
    fn when(&self, distance: f64) -> f64 {
        let i = self.distance.partition_point(|d| *d < distance); // bisect_left
        i.min(self.distance.len() - 1) as f64 * DT
    }

    pub fn gear(&self, t: f64) -> i64 {
        step(&self.shifts, t)
    }

    pub fn splitter(&self, t: f64) -> bool {
        self.splits.iter().filter(|(at, _)| *at <= t).count() % 2 == 1
    }

    /// When the front axle reaches each hit, joint and rumble strip, for the checks.
    pub fn road_features(&self) -> Vec<f64> {
        let mut features: Vec<f64> =
            self.s.hits.iter().map(|h| h.at).chain(self.s.rumble.iter().map(|(start, _, _)| *start)).collect();
        features.sort_by(|a, b| a.partial_cmp(b).unwrap());
        features
    }

    pub fn frame(&self, t: f64) -> Frame {
        let s = &self.s;
        let kmh = m::max(0.0, keyframe(&s.speed, t));
        let throttle = keyframe(&s.throttle, t);
        let brake = keyframe(&s.brake, t);
        let gear = self.gear(t);
        let (spin, lock) = (within(&s.spin, t), within(&s.lock, t) && kmh > 12.0);
        let mut rpm = m::max(IDLE, gear_rpm(kmh, gear));
        if gear == 1 && rpm < 900.0 {
            rpm = m::max(rpm, IDLE + 600.0 * throttle); // the clutch slips as it pulls away
        }
        if spin {
            rpm += 300.0;
        }
        let flash = |spans: &[(f64, f64)]| {
            within(spans, t) && {
                let start = spans.iter().find(|&&(a, b)| a <= t && t < b).unwrap().0;
                m::rem(t - start, 2.0 * FLASH) < FLASH
            }
        };
        let mut frame = Frame {
            active: true,
            paused: false,
            speed: kmh / 3.6,
            rpm: m::min(RPM_MAX, rpm),
            rpm_max: RPM_MAX,
            user_throttle: throttle,
            game_throttle: throttle,
            user_brake: brake,
            game_brake: brake,
            gear,
            gear_engine: gear,
            retarder: step(&s.retarder, t),
            retarder_steps: 4,
            motor_brake: within(&s.engine_brake, t),
            engine_on: true,
            park_brake: within(&s.park_brake, t),
            blinker_left: flash(&s.blinker_left),
            blinker_right: flash(&s.blinker_right),
            shifter_toggle: [false, self.splitter(t)],
            trailer_attached: s.trailer,
            game_events: s.events.iter().filter(|(at, _)| *at <= t && t < at + 0.5).map(|(_, n)| n.to_string()).collect(),
            ..Frame::default()
        };
        let driven = self.at(t);
        frame.wheels = TRUCK_AXLES
            .iter()
            .flat_map(|&(back, x)| {
                [-1.0, 1.0].map(|side| self.wheel(driven - back, x * side, kmh, spin && back > 0.0, lock && back > 0.0))
            })
            .collect();
        if s.trailer {
            frame.trailer_wheels = TRAILER_AXLES
                .iter()
                .flat_map(|&(back, x)| [-1.0, 1.0].map(|side| self.wheel(driven - back, x * side, kmh, false, false)))
                .collect();
        }
        frame
    }

    fn wheel(&self, at: f64, x: f64, kmh: f64, spin: bool, lock: bool) -> Wheel {
        let mut surface = step(&self.s.surface, self.when(at));
        let side: i32 = if x < 0.0 { -1 } else { 1 };
        let rough = ROUGH.iter().find(|(name, _)| *name == surface).map_or(ROUGH_OTHER, |(_, r)| *r);
        let mut deflection = 0.02
            + rough
                * m::sum(ROUGH_WAVES.iter().enumerate().map(|(k, &(length, share))| {
                    share * m::sin(TAU * at / length + (side * (k as i32 + 1)) as f64)
                }));
        let lo = self.hit_at.partition_point(|p| *p < at - 6.0);
        let hi = self.hit_at.partition_point(|p| *p <= at + 6.0);
        deflection += m::sum(
            self.hits[lo..hi.max(lo)]
                .iter()
                .filter(|(_, hit)| hit.side * side >= 0)
                .map(|(pos, hit)| hit_depth(hit, at - pos)),
        );
        let on_strip = self.strips.iter().any(|&(start, end, strip_side)| start <= at && at < end && strip_side == side);
        if on_strip && ((at / STRIP_FLICKER) as i64).rem_euclid(3) != 0 {
            surface = "rumble_stripe";
        }
        let mut speed = kmh / 3.6;
        if spin {
            speed = m::max(speed * 1.5, speed + 3.0);
        } else if lock {
            speed *= 0.2;
        }
        Wheel {
            deflection,
            on_ground: true,
            substance: surface.to_string(),
            pos_x: x,
            velocity: speed / (TAU * WHEEL_RADIUS),
            radius: WHEEL_RADIUS,
            ..Wheel::default()
        }
    }
}

/// Plays a scenario like the game's plugin would, in a loop with a pause between rounds. As in
/// SimulatedTelemetry the data only changes once per rendered game frame; fps None makes a
/// new frame on every read.
pub struct ScenarioTelemetry {
    pub drive: Drive,
    pub fps: Option<f64>,
    clock: Box<dyn FnMut() -> f64 + Send>,
    start: f64,
}

const PAUSE: f64 = 1.0; // s

impl ScenarioTelemetry {
    pub fn new(scenario: Scenario, fps: Option<f64>) -> ScenarioTelemetry {
        let start = Instant::now();
        ScenarioTelemetry::with_clock(scenario, fps, Box::new(move || start.elapsed().as_secs_f64()))
    }

    pub fn with_clock(scenario: Scenario, fps: Option<f64>, mut clock: Box<dyn FnMut() -> f64 + Send>) -> Self {
        let start = clock();
        ScenarioTelemetry { drive: Drive::new(scenario), fps, clock, start }
    }
}

impl TelemetrySource for ScenarioTelemetry {
    fn read(&mut self) -> Option<Frame> {
        let mut elapsed = (self.clock)() - self.start;
        if let Some(fps) = self.fps {
            elapsed = (elapsed * fps).floor() / fps;
        }
        let t = m::rem(elapsed, self.drive.s.seconds + PAUSE);
        let mut frame = if t >= self.drive.s.seconds {
            Frame { active: true, paused: true, ..Frame::default() }
        } else {
            self.drive.frame(t)
        };
        frame.render_time = (elapsed * 1e6) as i64;
        Some(frame)
    }

    fn name(&self) -> String {
        match self.fps {
            Some(fps) => format!("{} {} fps", self.drive.s.name, format_g(fps)),
            None => self.drive.s.name.to_string(),
        }
    }
}
