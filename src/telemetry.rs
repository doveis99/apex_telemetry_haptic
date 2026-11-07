//! Read ATS/ETS2 telemetry from the scs-telemetry shared-memory plugin (port of telemetry.py).
//!
//! Offsets are for plugin revision 12 (scs-sdk-plugin V.1.12); the revision is checked on
//! open. What each field means and how it was checked against drives is in telemetry.py.

use std::collections::BTreeSet;
use std::time::Instant;

use serde::{Deserialize, Serialize};

use crate::pymath as m;
use crate::t;

pub const MAPPING_NAMES: [&str; 3] = ["Local\\SCSTelemetry", "Local\\TruckDashTelemetry", "Local\\SHSCSTelemetry"];
pub const SUPPORTED_REVISION: u32 = 12;
pub const MAX_WHEELS: usize = 16;
pub const SUBSTANCE_COUNT: usize = 25;
pub const STRING_SIZE: usize = 64;

mod offset {
    pub const SDK_ACTIVE: usize = 0;
    pub const PAUSED: usize = 4;
    pub const RENDER_TIME: usize = 24;
    pub const REVISION: usize = 40;
    pub const RETARDER_STEPS: usize = 76;
    pub const WHEEL_COUNT: usize = 80;
    pub const MAX_TRAILERS: usize = 92;
    pub const RETARDER: usize = 108;
    pub const GEAR_ENGINE: usize = 504;
    pub const GEAR: usize = 508;
    pub const RPM_MAX: usize = 740;
    pub const SPEED: usize = 948;
    pub const RPM: usize = 952;
    pub const USER_THROTTLE: usize = 960;
    pub const USER_BRAKE: usize = 964;
    pub const GAME_THROTTLE: usize = 976;
    pub const GAME_BRAKE: usize = 980;
    pub const AIR_PRESSURE: usize = 992;
    pub const PARK_BRAKE: usize = 1566;
    pub const MOTOR_BRAKE: usize = 1567;
    pub const ENGINE_ON: usize = 1576;
    pub const BLINKER_LEFT: usize = 1580;
    pub const BLINKER_RIGHT: usize = 1581;
    pub const VELOCITY: usize = 1868; // x, y, z
    pub const YAW_RATE: usize = 1884;
    pub const ACCEL: usize = 1892; // x, y, z
    pub const CABIN_ACCEL: usize = 1928; // x, y, z
    pub const CABIN: usize = 2000; // x, y, z
    // arrays
    pub const WHEEL_SUBSTANCE: usize = 120; // 16 u32
    pub const WHEEL_RADIUS: usize = 752; // 16 f32
    pub const WHEEL_DEFLECTION: usize = 1072;
    pub const WHEEL_VELOCITY: usize = 1136; // rotations per second
    pub const WHEEL_POWERED: usize = 1532; // 16 bool
    pub const WHEEL_ON_GROUND: usize = 1590;
    pub const WHEEL_POS_X: usize = 1676; // negative = left side of the truck
    pub const WHEEL_POS_Z: usize = 1804; // negative = ahead of the truck's origin
    pub const SHIFTER_TOGGLE: usize = 1606; // 2 bool: range, splitter
    pub const GAME_EVENTS: usize = 4303; // 6 bool
    pub const SUBSTANCES: usize = 4400;
    pub const TRAILERS: usize = 6000;
    pub const TRAILER_SIZE: usize = 1560;
    pub const TRAILER_ATTACHED: usize = 80;
    pub const TRAILER_WHEEL_ON_GROUND: usize = 64;
    pub const TRAILER_WHEEL_SUBSTANCE: usize = 84;
    pub const TRAILER_WHEEL_COUNT: usize = 148;
    pub const TRAILER_WHEEL_DEFLECTION: usize = 168;
    pub const TRAILER_WHEEL_VELOCITY: usize = 232;
    pub const TRAILER_WHEEL_RADIUS: usize = 552;
    pub const TRAILER_WHEEL_POS_X: usize = 676;
}

pub const GAME_EVENT_NAMES: [&str; 6] = ["delivered", "fined", "tollgate", "ferry", "train", "refuel"];
pub const MAX_TRAILERS: usize = 10;
const MAPPING_NEEDED: usize = offset::TRAILERS + MAX_TRAILERS * offset::TRAILER_SIZE;
/// Game frame rate the test drive holds its frames at.
pub const SIMULATED_FPS: f64 = 60.0;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Wheel {
    pub deflection: f64,
    pub on_ground: bool,
    pub substance: String,
    pub pos_x: f64,
    pub pos_z: f64,
    pub velocity: f64,
    pub radius: f64,
    /// Driven by the engine. Logs and older sources don't say, so it counts unless told otherwise.
    pub powered: bool,
}

impl Default for Wheel {
    fn default() -> Wheel {
        Wheel {
            deflection: 0.0,
            on_ground: true,
            substance: "road".into(),
            pos_x: 0.0,
            pos_z: 0.0,
            velocity: 0.0,
            radius: 0.5,
            powered: true,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Frame {
    pub active: bool,
    pub paused: bool,
    pub render_time: i64,
    pub rpm: f64,
    pub rpm_max: f64,
    /// m/s, negative when reversing
    pub speed: f64,
    pub user_throttle: f64,
    pub user_brake: f64,
    pub game_throttle: f64,
    pub game_brake: f64,
    /// as on the dashboard: what the driver selected
    pub gear: i64,
    /// what the gearbox has engaged
    pub gear_engine: i64,
    pub retarder: i64,
    pub retarder_steps: i64,
    pub engine_on: bool,
    pub motor_brake: bool,
    pub park_brake: bool,
    pub accel: [f64; 3],
    /// rad/s, positive turning left
    pub yaw_rate: f64,
    /// m/s in the truck's space: x right, y up, z back
    pub velocity: [f64; 3],
    /// rad/s^2 of the cab's tilt: pitch, yaw, roll
    pub cabin_accel: [f64; 3],
    /// m the cab sits off its rest on the chassis
    pub cabin_offset: [f64; 3],
    pub wheels: Vec<Wheel>,
    /// every attached trailer's, in order
    pub trailer_wheels: Vec<Wheel>,
    pub air_pressure: f64,
    pub blinker_left: bool,
    pub blinker_right: bool,
    pub shifter_toggle: [bool; 2],
    pub trailer_attached: bool,
    pub game_events: BTreeSet<String>,
}

impl Default for Frame {
    fn default() -> Frame {
        Frame {
            active: false,
            paused: true,
            render_time: 0,
            rpm: 0.0,
            rpm_max: 2100.0,
            speed: 0.0,
            user_throttle: 0.0,
            user_brake: 0.0,
            game_throttle: 0.0,
            game_brake: 0.0,
            gear: 0,
            gear_engine: 0,
            retarder: 0,
            retarder_steps: 0,
            engine_on: false,
            motor_brake: false,
            park_brake: false,
            accel: [0.0; 3],
            yaw_rate: 0.0,
            velocity: [0.0; 3],
            cabin_accel: [0.0; 3],
            cabin_offset: [0.0; 3],
            wheels: Vec::new(),
            trailer_wheels: Vec::new(),
            air_pressure: 120.0,
            blinker_left: false,
            blinker_right: false,
            shifter_toggle: [false; 2],
            trailer_attached: false,
            game_events: BTreeSet::new(),
        }
    }
}

impl Frame {
    pub fn live(&self) -> bool {
        self.active && !self.paused
    }
}

/// Where frames come from: the game, a scripted drive, a log.
pub trait TelemetrySource: Send {
    /// None until the game runs (or while its mapping is unusable).
    fn read(&mut self) -> Option<Frame>;
    fn close(&mut self) {}
    fn name(&self) -> String;
    /// Why no usable mapping was found, for the UI.
    fn problem(&self) -> Option<String> {
        None
    }
}

struct Mapping {
    handle: windows_sys::Win32::Foundation::HANDLE,
    view: windows_sys::Win32::System::Memory::MEMORY_MAPPED_VIEW_ADDRESS,
    size: usize,
}

// SAFETY: the mapping is read only, by one thread at a time (&mut self).
unsafe impl Send for Mapping {}

impl Mapping {
    fn open(name: &str) -> Option<Mapping> {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Memory::{
            MapViewOfFile, OpenFileMappingW, VirtualQuery, FILE_MAP_READ, MEMORY_BASIC_INFORMATION,
        };
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        // SAFETY: a null-terminated name; every handle and view is checked and released on failure.
        unsafe {
            let handle = OpenFileMappingW(FILE_MAP_READ, 0, wide.as_ptr());
            if handle.is_null() {
                return None;
            }
            let view = MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0);
            if view.Value.is_null() {
                CloseHandle(handle);
                return None;
            }
            let mut info: MEMORY_BASIC_INFORMATION = std::mem::zeroed();
            let got = VirtualQuery(view.Value, &mut info, std::mem::size_of::<MEMORY_BASIC_INFORMATION>());
            let size = if got == 0 { 0 } else { info.RegionSize };
            Some(Mapping { handle, view, size })
        }
    }

    /// A copy of the first `len` bytes (what the plugin wrote at this moment).
    fn snapshot(&self, len: usize, out: &mut Vec<u8>) {
        let len = len.min(self.size);
        out.resize(len, 0);
        // SAFETY: the view is at least `size` bytes long and stays mapped while self lives.
        unsafe {
            std::ptr::copy_nonoverlapping(self.view.Value as *const u8, out.as_mut_ptr(), len);
        }
    }
}

impl Drop for Mapping {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Memory::UnmapViewOfFile;
        // SAFETY: both were opened in Mapping::open and are released once.
        unsafe {
            UnmapViewOfFile(self.view);
            CloseHandle(self.handle);
        }
    }
}

struct Buf<'a>(&'a [u8]);

impl Buf<'_> {
    fn u32(&self, at: usize) -> u32 {
        u32::from_le_bytes(self.0[at..at + 4].try_into().unwrap())
    }
    fn i32(&self, at: usize) -> i32 {
        i32::from_le_bytes(self.0[at..at + 4].try_into().unwrap())
    }
    fn u64(&self, at: usize) -> u64 {
        u64::from_le_bytes(self.0[at..at + 8].try_into().unwrap())
    }
    fn f32(&self, at: usize) -> f64 {
        f32::from_le_bytes(self.0[at..at + 4].try_into().unwrap()) as f64
    }
    fn bool(&self, at: usize) -> bool {
        self.0[at] != 0
    }
    fn f32x3(&self, at: usize) -> [f64; 3] {
        [self.f32(at), self.f32(at + 4), self.f32(at + 8)]
    }
}

/// Polls the plugin's shared memory. read() returns None until the game runs.
pub struct SharedMemoryTelemetry {
    names: Vec<String>,
    mem: Option<Mapping>,
    pub mapping_name: Option<String>,
    substances: Vec<String>,
    substances_read_at: Option<Instant>,
    next_open_attempt: Option<Instant>,
    problem: Option<String>,
    buf: Vec<u8>,
}

impl Default for SharedMemoryTelemetry {
    fn default() -> Self {
        SharedMemoryTelemetry::new(MAPPING_NAMES.iter().map(|s| s.to_string()).collect())
    }
}

impl SharedMemoryTelemetry {
    pub fn new(names: Vec<String>) -> Self {
        SharedMemoryTelemetry {
            names,
            mem: None,
            mapping_name: None,
            substances: Vec::new(),
            substances_read_at: None,
            next_open_attempt: None,
            problem: None,
            buf: Vec::new(),
        }
    }

    fn open(&mut self) -> bool {
        let now = Instant::now();
        if self.next_open_attempt.is_some_and(|at| now < at) {
            return false;
        }
        self.next_open_attempt = Some(now + std::time::Duration::from_secs(1));
        self.problem = None;
        for name in &self.names {
            let Some(mem) = Mapping::open(name) else { continue };
            if mem.size < MAPPING_NEEDED {
                continue;
            }
            mem.snapshot(offset::REVISION + 4, &mut self.buf);
            let revision = Buf(&self.buf).u32(offset::REVISION);
            if revision == 0 {
                // Mapping exists (e.g. created by SimHub) but the game has not filled it yet.
                continue;
            }
            if revision != SUPPORTED_REVISION {
                // Another plugin build may still be compatible; keep looking.
                self.problem = Some(t!(
                    "{name} 플러그인 버전 {revision} (지원: {supported})",
                    name = name.rsplit('\\').next().unwrap_or(name),
                    revision = revision,
                    supported = SUPPORTED_REVISION
                ));
                continue;
            }
            self.mem = Some(mem);
            self.mapping_name = Some(name.clone());
            return true;
        }
        false
    }

    fn read_substances(&mut self) {
        let buf = &self.buf;
        self.substances = (0..SUBSTANCE_COUNT)
            .map(|i| {
                let raw = &buf[offset::SUBSTANCES + i * STRING_SIZE..offset::SUBSTANCES + (i + 1) * STRING_SIZE];
                let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
                String::from_utf8_lossy(&raw[..end]).into_owned()
            })
            .collect();
        self.substances_read_at = Some(Instant::now());
    }

    #[allow(clippy::too_many_arguments)]
    fn wheel(&self, substance: u32, deflection: f64, on_ground: bool, pos_x: f64, velocity: f64, radius: f64,
             powered: bool, pos_z: f64) -> Wheel {
        let substance = self.substances.get(substance as usize).cloned().unwrap_or_default();
        Wheel {
            deflection,
            on_ground,
            substance,
            pos_x,
            pos_z,
            velocity,
            radius: if radius != 0.0 { radius } else { 0.5 },
            powered,
        }
    }
}

impl TelemetrySource for SharedMemoryTelemetry {
    fn read(&mut self) -> Option<Frame> {
        if self.mem.is_none() && !self.open() {
            return None;
        }
        let mut buf = std::mem::take(&mut self.buf);
        self.mem.as_ref().unwrap().snapshot(MAPPING_NEEDED, &mut buf);
        self.buf = buf;
        if self.substances_read_at.is_none_or(|at| at.elapsed().as_secs_f64() > 2.0) {
            self.read_substances();
        }
        let b = Buf(&self.buf);
        let count = (b.u32(offset::WHEEL_COUNT) as usize).min(MAX_WHEELS);
        let powered: Vec<bool> = (0..MAX_WHEELS).map(|i| b.bool(offset::WHEEL_POWERED + i)).collect();
        // a truck with no driven wheel is configuration not filled in yet: count them all
        let any_powered = powered[..count].iter().any(|&p| p);
        let wheels: Vec<Wheel> = (0..count)
            .map(|i| {
                self.wheel(
                    b.u32(offset::WHEEL_SUBSTANCE + 4 * i),
                    b.f32(offset::WHEEL_DEFLECTION + 4 * i),
                    b.bool(offset::WHEEL_ON_GROUND + i),
                    b.f32(offset::WHEEL_POS_X + 4 * i),
                    b.f32(offset::WHEEL_VELOCITY + 4 * i),
                    b.f32(offset::WHEEL_RADIUS + 4 * i),
                    if any_powered { powered[i] } else { true },
                    b.f32(offset::WHEEL_POS_Z + 4 * i),
                )
            })
            .collect();
        let mut trailer_attached = false;
        let mut trailer_wheels = Vec::new();
        let max_trailers = match b.u32(offset::MAX_TRAILERS) as usize {
            0 => 1,
            n => n,
        };
        for i in 0..max_trailers.min(MAX_TRAILERS) {
            let base = offset::TRAILERS + i * offset::TRAILER_SIZE;
            if !b.bool(base + offset::TRAILER_ATTACHED) {
                continue;
            }
            trailer_attached = true;
            let count = (b.u32(base + offset::TRAILER_WHEEL_COUNT) as usize).min(MAX_WHEELS);
            for w in 0..count {
                trailer_wheels.push(self.wheel(
                    b.u32(base + offset::TRAILER_WHEEL_SUBSTANCE + 4 * w),
                    b.f32(base + offset::TRAILER_WHEEL_DEFLECTION + 4 * w),
                    b.bool(base + offset::TRAILER_WHEEL_ON_GROUND + w),
                    b.f32(base + offset::TRAILER_WHEEL_POS_X + 4 * w),
                    b.f32(base + offset::TRAILER_WHEEL_VELOCITY + 4 * w),
                    b.f32(base + offset::TRAILER_WHEEL_RADIUS + 4 * w),
                    false,
                    0.0,
                ));
            }
        }
        let rpm_max = b.f32(offset::RPM_MAX);
        Some(Frame {
            active: b.bool(offset::SDK_ACTIVE),
            paused: b.bool(offset::PAUSED),
            render_time: b.u64(offset::RENDER_TIME) as i64,
            rpm: b.f32(offset::RPM),
            rpm_max: if rpm_max != 0.0 { rpm_max } else { 2100.0 },
            speed: b.f32(offset::SPEED),
            user_throttle: b.f32(offset::USER_THROTTLE),
            user_brake: b.f32(offset::USER_BRAKE),
            game_throttle: b.f32(offset::GAME_THROTTLE),
            game_brake: b.f32(offset::GAME_BRAKE),
            gear: b.i32(offset::GEAR) as i64,
            gear_engine: b.i32(offset::GEAR_ENGINE) as i64,
            retarder: b.u32(offset::RETARDER) as i64,
            retarder_steps: b.u32(offset::RETARDER_STEPS) as i64,
            engine_on: b.bool(offset::ENGINE_ON),
            motor_brake: b.bool(offset::MOTOR_BRAKE),
            park_brake: b.bool(offset::PARK_BRAKE),
            accel: b.f32x3(offset::ACCEL),
            yaw_rate: b.f32(offset::YAW_RATE),
            velocity: b.f32x3(offset::VELOCITY),
            cabin_accel: b.f32x3(offset::CABIN_ACCEL),
            cabin_offset: b.f32x3(offset::CABIN),
            wheels,
            trailer_wheels,
            air_pressure: b.f32(offset::AIR_PRESSURE),
            blinker_left: b.bool(offset::BLINKER_LEFT),
            blinker_right: b.bool(offset::BLINKER_RIGHT),
            shifter_toggle: [b.bool(offset::SHIFTER_TOGGLE), b.bool(offset::SHIFTER_TOGGLE + 1)],
            trailer_attached,
            game_events: GAME_EVENT_NAMES
                .iter()
                .enumerate()
                .filter(|(i, _)| b.bool(offset::GAME_EVENTS + i))
                .map(|(_, name)| name.to_string())
                .collect(),
        })
    }

    fn close(&mut self) {
        self.mem = None;
        self.substances_read_at = None; // another game names its substances differently
    }

    fn name(&self) -> String {
        self.mapping_name.clone().unwrap_or_default()
    }

    fn problem(&self) -> Option<String> {
        self.problem.clone()
    }
}

/// Scripted 60 s delivery run for testing without the game, then loop (telemetry.py has the
/// timeline). Like the real plugin, the data only changes once per rendered game frame;
/// fps None makes a new frame on every read.
pub struct SimulatedTelemetry {
    pub fps: Option<f64>,
    clock: Box<dyn FnMut() -> f64 + Send>,
    start: f64,
}

const SIM_PERIOD: f64 = 60.0;
const SIM_SURFACES: [(f64, &str); 7] =
    [(22.0, "road"), (25.0, "concrete"), (28.0, "gravel"), (30.0, "wood"), (32.0, "snow"), (34.0, "ice"),
     (SIM_PERIOD, "road")];
const SIM_TRUCK_WHEELS: [(f64, f64); 6] = [(-1.0, 0.0), (1.0, 0.0), (-0.9, 0.2), (0.9, 0.2), (-0.9, 0.25), (0.9, 0.25)];
const SIM_TRAILER_WHEELS: [(f64, f64); 4] = [(-0.98, 0.45), (0.98, 0.45), (-0.98, 0.5), (0.98, 0.5)];

impl SimulatedTelemetry {
    pub fn new(fps: Option<f64>) -> Self {
        let start = Instant::now();
        SimulatedTelemetry::with_clock(fps, Box::new(move || start.elapsed().as_secs_f64()))
    }

    pub fn with_clock(fps: Option<f64>, mut clock: Box<dyn FnMut() -> f64 + Send>) -> Self {
        let start = clock();
        SimulatedTelemetry { fps, clock, start }
    }

    fn frame_at(&self, elapsed: f64) -> Frame {
        let t = m::rem(elapsed, SIM_PERIOD);
        let mut frame = Frame {
            active: true,
            paused: false,
            render_time: (elapsed * 1e6) as i64,
            rpm_max: 2100.0,
            ..Frame::default()
        };
        let (mut speed, mut throttle, mut brake, mut gear, mut rpm) = (0.0, 0.0, 0.0, 0i64, 650.0);
        let (mut lock, mut spin) = (false, false);
        if t < 5.0 {
        } else if t < 15.0 {
            throttle = if t < 7.0 { 1.0 } else { 0.8 };
            speed = (t - 5.0) * 2.2;
            gear = 1 + ((t - 5.0) / 1.25) as i64;
            rpm = 1100.0 + m::rem(t - 5.0, 1.25) / 1.25 * 700.0;
            spin = t < 6.0;
        } else if t < 25.0 {
            (throttle, speed, gear, rpm) = (0.4, 22.0, 9, 1350.0);
        } else if t < 28.0 {
            (throttle, speed, gear, rpm) = (0.15, 22.0 - (t - 25.0) * 2.0, 9, 1350.0 - (t - 25.0) * 80.0);
        } else if t < 34.0 {
            (throttle, speed, gear, rpm) = (0.4, 16.0, 8, 1400.0);
            if (32.6..33.4).contains(&t) {
                (throttle, rpm, spin) = (0.9, 1800.0, true);
            }
        } else if t < 38.0 {
            (speed, gear, rpm) = (16.0, 8, 1200.0);
            if t < 36.0 {
                frame.retarder = 3;
            } else {
                frame.motor_brake = true;
            }
        } else if t < 42.0 {
            brake = if t < 39.0 { 1.0 } else { 0.9 };
            speed = m::max(0.0, 16.0 - (t - 38.0) * 4.4);
            gear = if speed > 3.0 { 8 } else { 0 };
            rpm = m::max(650.0, 1200.0 - (t - 38.0) * 200.0);
            lock = speed > 4.0;
        } else if t < 45.0 {
            brake = 0.3;
        } else if t < 48.6 {
            (throttle, gear, rpm) = (0.3, -1, 900.0);
            speed = -m::min(2.0, (t - 45.0) * 2.0);
        } else if t < 49.5 {
            brake = 0.3;
        }
        frame.speed = speed;
        frame.user_throttle = throttle;
        frame.game_throttle = throttle;
        frame.user_brake = brake;
        frame.game_brake = brake;
        frame.gear = gear;
        frame.gear_engine = gear;
        frame.retarder_steps = 4;
        frame.engine_on = (0.8..55.0).contains(&t);
        frame.rpm = if frame.engine_on { rpm } else { 0.0 };
        frame.trailer_attached = (2.0..51.0).contains(&t);
        frame.park_brake = t < 4.5 || t >= 49.5;
        frame.shifter_toggle = [(12.5..40.0).contains(&t), (10.0..12.0).contains(&t)];
        let flash = ((t / 0.4) as i64) % 2 == 0;
        let hazards = (45.0..49.5).contains(&t);
        frame.blinker_left = flash && ((15.0..17.4).contains(&t) || hazards);
        frame.blinker_right = flash && ((25.5..27.0).contains(&t) || hazards);
        frame.accel = [0.0, 0.0, if (48.6..48.7).contains(&t) { 18.0 } else { 0.0 }];
        frame.game_events = [("tollgate", 43.0), ("delivered", 52.5)]
            .iter()
            .filter(|(_, at)| *at <= t && t < at + 0.5)
            .map(|(name, _)| name.to_string())
            .collect();
        let surface = SIM_SURFACES.iter().find(|(end, _)| t < *end).map(|(_, name)| *name).unwrap();

        let hit = |at: f64, center: f64, depth: f64, width: f64| -> f64 {
            -depth * m::exp(-m::pow((at - center) / width, 2.0))
        };
        let wheel = |i: usize, x: f64, delay: f64| -> Wheel {
            let u = t - delay; // when this axle reaches a spot the front axle passed at t
            let jitter = if surface == "gravel" { 0.003 } else { 0.0006 };
            let mut deflection = 0.02 + jitter * m::sin(t * 2.0 * m::PI * 6.0 + i as f64);
            if x < 0.0 {
                deflection += hit(u, 17.0, 0.06, 0.04) + hit(u, 19.5, 0.06, 0.04);
            }
            for (first, last, pitch, depth, width) in [(22.0, 25.0, 0.55, 0.012, 0.02), (28.0, 30.0, 0.25, 0.006, 0.015)]
            {
                let center = first + m::round_int((u - first) / pitch) as f64 * pitch;
                if first <= center && center < last {
                    deflection += hit(u, center, depth, width);
                }
            }
            deflection += hit(t, 48.6, 0.03, 0.03); // the dock jolts the whole rig at once
            let on_strip = x > 0.0 && (20.0..21.5).contains(&u) && ((u / 0.12) as i64) % 3 != 0;
            Wheel {
                deflection,
                on_ground: true,
                substance: if on_strip { "rumble_stripe".into() } else { surface.into() },
                pos_x: x,
                pos_z: 0.0,
                velocity: speed / (2.0 * m::PI * 0.5),
                radius: 0.5,
                powered: i >= 2,
            }
        };
        frame.wheels = SIM_TRUCK_WHEELS.iter().enumerate().map(|(i, &(x, delay))| wheel(i, x, delay)).collect();
        for w in frame.wheels[2..].iter_mut() {
            if lock {
                w.velocity *= 0.2;
            } else if spin {
                w.velocity = m::max(speed.abs() * 1.5, speed.abs() + 3.0) / (2.0 * m::PI * 0.5);
            }
        }
        if frame.trailer_attached {
            frame.trailer_wheels =
                SIM_TRAILER_WHEELS.iter().enumerate().map(|(i, &(x, delay))| wheel(i, x, delay)).collect();
        }
        frame
    }
}

impl TelemetrySource for SimulatedTelemetry {
    fn read(&mut self) -> Option<Frame> {
        let mut elapsed = (self.clock)() - self.start;
        if let Some(fps) = self.fps {
            elapsed = (elapsed * fps).floor() / fps; // when the game rendered its last frame
        }
        Some(self.frame_at(elapsed))
    }

    fn name(&self) -> String {
        match self.fps {
            Some(fps) => format!("simulated {} fps", format_g(fps)),
            None => "simulated".into(),
        }
    }
}

/// Python's format(x, "g") for the frame rates shown in names.
pub fn format_g(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e16 {
        format!("{}", x as i64)
    } else {
        format!("{x}")
    }
}
