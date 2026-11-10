//! Flydigi APEX 6 Pro realtime voice-coil output (port of apex6.py).
//!
//! Packet layout and init sequence are ported from ApexSenseBridge (GPL-3.0-or-later, see
//! THIRD_PARTY_NOTICES.md): src/flydigi/Apex6Protocol.cpp, src/flydigi/Apex5Device.cpp and
//! src/dualsense/Apex6HapticBridge.cpp.
//!
//! Stream model: every 8 ms one 33-byte report carries eight 1 kHz subframes, each with a
//! trigger, left-grip and right-grip sample (signed, +-127 max). The trigger channel is a
//! single waveform, played on the left, the right or both triggers (Block::side).
//!
//! The pad's output runs at 2 kHz (docs/xhaptics-engine-notes.md section 11, measured
//! 2026-11-08 on firmware 9.0.6.8): with bit 7 of the first payload byte set, as Flydigi's
//! engine and ASB send it, it joins the eight samples into sixteen by straight lines; with it
//! clear it queues them as they are, so a report every 4 ms carries 2 kHz data
//! (Block::upsample, src/rate2k.rs).
//!
//! The reasons behind the numbers (the 94 ms wait, re-asserting the route, sending silence
//! every block, the time-critical thread) are written up in apex6.py.

use std::ffi::CString;
use std::time::{Duration, Instant};

use crate::t;

pub const VENDOR_ID: u16 = 0x37D7;
pub const PRODUCT_ID: u16 = 0x2502;
pub const USAGE_PAGE: u16 = 0xFFA0;

pub const REPORT_SIZE: usize = 33;
pub const MAGIC: [u8; 2] = [0x5A, 0xA5];
pub const CMD_GET_INFO: u8 = 0x01;
pub const CMD_MOTOR_ROUTE: u8 = 0x53;
pub const CMD_REALTIME_MOTOR: u8 = 0x57;

pub const PRO_DEVICE_TYPES: [(u8, &str); 2] = [(0x96, "APEX 6 Pro"), (0x98, "APEX 6 Pro (Phantom Blade Zero)")];
pub const FEATURE_GRIP_HAPTIC: u8 = 0x80;
pub const FEATURE_TRIGGER_HAPTIC: u8 = 0x10;
/// Protocol-frame offsets of the big-endian u16 versions in the 0x01 reply.
pub const FIRMWARE_OFFSETS: [(&str, usize); 7] =
    [("controller", 15), ("dongle", 17), ("si", 19), ("trigger", 21), ("screen", 23), ("adc", 25), ("rf", 27)];

pub const SAMPLE_RATE: u32 = 1000;
pub const SUBFRAMES: usize = 8;
pub const FRAME_PERIOD: f64 = SUBFRAMES as f64 / SAMPLE_RATE as f64; // 8 ms
/// Samples go out as int8 around 0x80; +-127 is the protocol's whole range.
pub const SAMPLE_LIMIT: i32 = 127;
/// What the synth calls full strength (the slope of the soft limit).
pub const MAX_SAMPLE: f64 = 112.0;
pub const ENABLE_DELAY: f64 = 0.094;
pub const REASSERT_IDLE_S: f64 = 1.0;
pub const REASSERT_ACTIVE_S: f64 = 4.0;
pub const THREAD_PRIORITY_TIME_CRITICAL: i32 = 15;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Route {
    Left = 0,
    Right = 1,
    Both = 2,
    Mute = 3,
}

impl Route {
    pub fn from_u8(value: u8) -> Option<Route> {
        match value {
            0 => Some(Route::Left),
            1 => Some(Route::Right),
            2 => Some(Route::Both),
            3 => Some(Route::Mute),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Route::Left => "LEFT",
            Route::Right => "RIGHT",
            Route::Both => "BOTH",
            Route::Mute => "MUTE",
        }
    }
}

pub type Report = [u8; REPORT_SIZE];

fn checksum(report: &Report) -> u8 {
    // Sum of protocol-frame bytes 2..30, i.e. report bytes 3..31.
    report[3..32].iter().fold(0u8, |sum, &b| sum.wrapping_add(b))
}

pub fn build_command(command: u8, payload: &[u8]) -> Report {
    let payload = &payload[..payload.len().min(27)];
    let mut report = [0u8; REPORT_SIZE];
    report[1] = MAGIC[0];
    report[2] = MAGIC[1];
    report[3] = command;
    report[4] = payload.len() as u8 + 2;
    report[5..5 + payload.len()].copy_from_slice(payload);
    report[32] = checksum(&report);
    report
}

pub const DEFAULT_TRIGGER_SIDE: Route = Route::Both;
pub const DEFAULT_TRIGGER_TARGET: u8 = 0x12;
pub const DEFAULT_TRIGGER_MODE: u8 = 0x02;

pub fn trigger_side_report(side: u8) -> Report {
    build_command(CMD_MOTOR_ROUTE, &[0x01, side, 0x03, 0x00, 0x40])
}

pub fn motor_enable_reports(trigger_mode: u8, trigger_target: u8, trigger_side: u8) -> [Report; 2] {
    [
        trigger_side_report(trigger_side),
        build_command(CMD_MOTOR_ROUTE, &[0x01, trigger_target, trigger_mode, 0x40, 0x00]),
    ]
}

pub fn motor_disable() -> [Report; 2] {
    [build_command(CMD_MOTOR_ROUTE, &[0x01, 0x02, 0x00]), build_command(CMD_MOTOR_ROUTE, &[0x01, 0x12, 0x00])]
}

fn encode(sample: f64) -> u8 {
    let value = crate::pymath::round_int(sample).clamp(-SAMPLE_LIMIT as i64, SAMPLE_LIMIT as i64);
    (value + 128) as u8 // 0x80 is electrical zero
}

/// 2 kHz samples in one 8 ms block.
pub const FINE: usize = 2 * SUBFRAMES;

/// The events drawn at 2 kHz (left grip, right grip, trigger), each sample what it adds on top
/// of the line through the block's 1 kHz samples; src/rate2k.rs lays them over it.
pub type Events = [[f64; FINE]; 3];

/// Eight subframes for the three output channels: 1 kHz, or 2 kHz when upsample is off.
#[derive(Clone, Debug, PartialEq)]
pub struct Block {
    pub trigger: [f64; SUBFRAMES],
    pub left_grip: [f64; SUBFRAMES],
    pub right_grip: [f64; SUBFRAMES],
    pub route: Route,
    /// None: set the trigger-valid bit only when routed. Some forces it (diagnostics).
    pub trigger_bit: Option<bool>,
    /// The trigger(s) the trigger channel plays on, set with the 0x53 trigger frame before
    /// this block goes out; None leaves it where it is.
    pub side: Option<Route>,
    /// Bit 7 of the first payload byte. Set (1 kHz samples, a report every 8 ms), the pad
    /// joins the eight samples into sixteen by straight lines; clear, they are 2 kHz samples
    /// and the block lasts 4 ms.
    pub upsample: bool,
    /// The synth's events, kept out of the samples, when it renders for the 2 kHz output.
    pub events: Option<Box<Events>>,
}

impl Default for Block {
    fn default() -> Block {
        Block {
            trigger: [0.0; SUBFRAMES],
            left_grip: [0.0; SUBFRAMES],
            right_grip: [0.0; SUBFRAMES],
            route: Route::Mute,
            trigger_bit: None,
            side: None,
            upsample: true,
            events: None,
        }
    }
}

impl Block {
    pub fn is_silent(&self) -> bool {
        self.trigger.iter().chain(&self.left_grip).chain(&self.right_grip).all(|&x| x == 0.0)
            && self.events.as_ref().is_none_or(|e| e.iter().flatten().all(|&x| x == 0.0))
    }

    /// How long the pad plays it, with a report period of `period` for 1 kHz blocks.
    pub fn period(&self, period: f64) -> f64 {
        if self.upsample { period } else { period / 2.0 }
    }

    /// Strongest sample of each channel (left grip, right grip, trigger), events included.
    pub fn peaks(&self) -> [f64; 3] {
        let mut peaks = [0.0f64; 3];
        for (i, channel) in [&self.left_grip, &self.right_grip, &self.trigger].into_iter().enumerate() {
            for x in channel {
                peaks[i] = crate::pymath::max(peaks[i], x.abs());
            }
            if let Some(events) = &self.events {
                for (k, e) in events[i].iter().enumerate() {
                    // over the line through the samples (the last block's is not known here)
                    let n = k / 2;
                    let under = if k % 2 == 1 || n == 0 { channel[n] } else { (channel[n - 1] + channel[n]) / 2.0 };
                    peaks[i] = crate::pymath::max(peaks[i], (under + e).abs());
                }
            }
        }
        peaks
    }
}

pub fn build_realtime(block: &Block, enable_trigger: bool, enable_grips: bool) -> Report {
    let enable_trigger = match block.trigger_bit {
        Some(bit) => bit,
        None => enable_trigger && block.route != Route::Mute,
    };
    let mut payload = [0u8; 25];
    payload[0] = if block.upsample { 0x80 } else { 0 }
        | if enable_grips { 0x10 | 0x08 } else { 0 }
        | if enable_trigger { 0x04 } else { 0 }
        | block.route as u8;
    for i in 0..SUBFRAMES {
        let offset = 1 + i * 3;
        payload[offset] = if enable_trigger { encode(block.trigger[i]) } else { 0x80 };
        payload[offset + 1] = if enable_grips { encode(block.left_grip[i]) } else { 0x80 };
        payload[offset + 2] = if enable_grips { encode(block.right_grip[i]) } else { 0x80 };
    }
    build_command(CMD_REALTIME_MOTOR, &payload)
}

pub fn silent_report() -> Report {
    build_realtime(&Block::default(), false, true)
}

#[derive(Clone, Debug)]
pub struct DeviceInfo {
    pub device_type: u8,
    pub connection_mode: u8,
    pub features: u8,
    /// The whole 0x01 reply, for diagnostics.
    pub raw: Vec<u8>,
}

impl DeviceInfo {
    /// Component firmware versions, one nibble per digit (0x90 0x55 -> 9.0.5.5); absent ones left out.
    pub fn firmware(&self) -> Vec<(&'static str, String)> {
        if self.raw.len() < 29 {
            return Vec::new();
        }
        FIRMWARE_OFFSETS
            .iter()
            .filter(|(_, i)| self.raw[*i] != 0 || self.raw[i + 1] != 0)
            .map(|(name, i)| {
                let digits = format!("{:02X}{:02X}", self.raw[*i], self.raw[i + 1]);
                (*name, digits.chars().map(String::from).collect::<Vec<_>>().join("."))
            })
            .collect()
    }

    pub fn name(&self) -> String {
        PRO_DEVICE_TYPES
            .iter()
            .find(|(t, _)| *t == self.device_type)
            .map(|(_, name)| name.to_string())
            .unwrap_or_else(|| format!("unknown 0x{:02X}", self.device_type))
    }

    pub fn is_pro(&self) -> bool {
        PRO_DEVICE_TYPES.iter().any(|(t, _)| *t == self.device_type)
    }

    pub fn grip_haptics(&self) -> bool {
        self.features & FEATURE_GRIP_HAPTIC != 0
    }

    pub fn trigger_haptics(&self) -> bool {
        self.features & FEATURE_TRIGGER_HAPTIC != 0
    }
}

fn protocol_frame(data: &[u8]) -> Option<&[u8]> {
    // hidapi may or may not keep the leading report-ID 0 byte.
    if data.len() >= REPORT_SIZE && data[0] == 0 && data[1..3] == MAGIC {
        return Some(&data[1..REPORT_SIZE]);
    }
    if data.len() >= REPORT_SIZE - 1 && data[0..2] == MAGIC {
        return Some(&data[..REPORT_SIZE - 1]);
    }
    None
}

pub fn parse_device_info(data: &[u8]) -> Option<DeviceInfo> {
    let frame = protocol_frame(data)?;
    if frame[2] != CMD_GET_INFO {
        return None;
    }
    let sum = frame[2..31].iter().fold(0u8, |s, &b| s.wrapping_add(b));
    if frame[31] != sum {
        return None;
    }
    Some(DeviceInfo { device_type: frame[5], connection_mode: frame[6], features: frame[29], raw: frame.to_vec() })
}

#[derive(Debug)]
pub enum PadError {
    /// Controller absent, asleep or disconnected; worth retrying later.
    Unavailable(String),
    /// Something that retrying will not fix (another controller model).
    Fatal(String),
}

impl std::fmt::Display for PadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PadError::Unavailable(text) | PadError::Fatal(text) => f.write_str(text),
        }
    }
}

impl std::error::Error for PadError {}

/// The HID handle as the stream needs it: a fake one in tests.
pub trait Link {
    fn write(&mut self, report: &[u8]) -> Result<usize, String>;
    /// Bytes read into buf; 0 when nothing came within timeout_ms.
    fn read(&mut self, buf: &mut [u8], timeout_ms: i32) -> Result<usize, String>;
}

pub struct HidLink {
    device: hidapi::HidDevice,
}

impl Link for HidLink {
    fn write(&mut self, report: &[u8]) -> Result<usize, String> {
        self.device.write(report).map_err(|e| e.to_string())
    }

    fn read(&mut self, buf: &mut [u8], timeout_ms: i32) -> Result<usize, String> {
        self.device.read_timeout(buf, timeout_ms).map_err(|e| e.to_string())
    }
}

/// True inside a process Steam injected its overlay into (see app.launch).
pub fn steam_overlay_loaded() -> bool {
    let name: Vec<u16> = "gameoverlayrenderer64.dll".encode_utf16().chain(Some(0)).collect();
    // SAFETY: a null-terminated wide string; GetModuleHandleW only looks the module up.
    unsafe { !windows_sys::Win32::System::LibraryLoader::GetModuleHandleW(name.as_ptr()).is_null() }
}

fn hid_api() -> Result<hidapi::HidApi, PadError> {
    hidapi::HidApi::new().map_err(|e| PadError::Unavailable(t!("컨트롤러를 열 수 없음 ({error})", error = e)))
}

/// The vendor interfaces' paths in the order Windows lists them, or why there is none. The
/// cable and the 2.4 GHz dongle show up alike, so with both plugged in there are two, and the
/// dongle's stays listed while the pad is on the cable or asleep.
pub fn find_paths(api: &hidapi::HidApi) -> Result<Vec<CString>, PadError> {
    let paths: Vec<CString> = api
        .device_list()
        .filter(|e| e.vendor_id() == VENDOR_ID && e.product_id() == PRODUCT_ID && e.usage_page() == USAGE_PAGE)
        .map(|e| e.path().to_owned())
        .collect();
    if !paths.is_empty() {
        return Ok(paths);
    }
    if steam_overlay_loaded() {
        return Err(PadError::Unavailable(t!(
            "컨트롤러 안 보임: Steam이 실행한 프로세스라 Steam Input이 패드를 숨김 (창을 따로 실행하거나 게임의 Steam Input 끄기)"
        )));
    }
    Err(PadError::Unavailable(t!("컨트롤러 없음 (유선/2.4G 동글 연결, 전원 확인)")))
}

/// The first candidate `open` gets an answering APEX 6 Pro from. When none does, the error of
/// one worth trying again (asleep, not answering) rather than of one that never will be (another
/// model), so the caller keeps trying while any could still wake.
pub fn first_that_answers<P, T>(candidates: impl IntoIterator<Item = P>,
                                mut open: impl FnMut(P) -> Result<T, PadError>) -> Result<T, PadError> {
    let mut failed: Option<PadError> = None;
    for candidate in candidates {
        match open(candidate) {
            Ok(found) => return Ok(found),
            Err(error) => {
                if matches!((&failed, &error), (None, _) | (Some(PadError::Fatal(_)), PadError::Unavailable(_))) {
                    failed = Some(error);
                }
            }
        }
    }
    Err(failed.unwrap_or_else(|| PadError::Unavailable(t!("컨트롤러 없음 (유선/2.4G 동글 연결, 전원 확인)"))))
}

/// Runs the calling thread at THREAD_PRIORITY_TIME_CRITICAL until dropped.
struct TimeCritical {
    before: Option<i32>,
}

impl TimeCritical {
    fn raise() -> TimeCritical {
        use windows_sys::Win32::System::Threading::{GetCurrentThread, GetThreadPriority, SetThreadPriority};
        // SAFETY: the pseudo-handle of the calling thread; priority calls take no pointers.
        unsafe {
            let thread = GetCurrentThread();
            let before = GetThreadPriority(thread);
            let raised = SetThreadPriority(thread, THREAD_PRIORITY_TIME_CRITICAL) != 0;
            TimeCritical { before: raised.then_some(before) }
        }
    }
}

impl Drop for TimeCritical {
    fn drop(&mut self) {
        if let Some(before) = self.before {
            use windows_sys::Win32::System::Threading::{GetCurrentThread, SetThreadPriority};
            // SAFETY: as in raise().
            unsafe {
                SetThreadPriority(GetCurrentThread(), before);
            }
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct StreamStats {
    pub frames: u64,
    /// started more than one period behind schedule
    pub late_frames: u64,
    pub max_lateness_ms: f64,
    pub max_write_ms: f64,
    pub total_write_ms: f64,
}

impl StreamStats {
    pub fn summary(&self) -> String {
        let avg = if self.frames > 0 { self.total_write_ms / self.frames as f64 } else { 0.0 };
        format!(
            "frames={} late={} max_lateness={:.2}ms write avg={:.2}ms max={:.2}ms",
            self.frames, self.late_frames, self.max_lateness_ms, avg, self.max_write_ms
        )
    }
}

/// What the stream waits with; a fake one lets tests run without sleeping.
pub trait Clock {
    fn now(&mut self) -> f64;
    fn sleep(&mut self, seconds: f64);
}

pub struct RealClock {
    start: Instant,
}

impl Default for RealClock {
    fn default() -> RealClock {
        RealClock { start: Instant::now() }
    }
}

impl Clock for RealClock {
    fn now(&mut self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }

    fn sleep(&mut self, seconds: f64) {
        // std sleeps on a high-resolution waitable timer on Windows 10 1803 and later
        std::thread::sleep(Duration::from_secs_f64(seconds));
    }
}

/// A 0x53 frame that waits for a free slot between 2 kHz blocks; built when it goes out, so it
/// carries the route as it is then.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Deferred {
    /// the two frames of reassert()
    Side,
    Target,
    /// a trigger the channel left: off, unless the channel came back to it meanwhile
    Release(u8),
}

fn plays_on(side: u8, one: u8) -> bool {
    side == one || side == Route::Both as u8
}

/// The vendor interface, verified, streaming motor blocks. Dropping it mutes and releases the motors.
pub struct Apex6<L: Link = HidLink, C: Clock = RealClock> {
    link: L,
    clock: C,
    pub info: Option<DeviceInfo>,
    enabled: bool,
    trigger_mode: u8,
    trigger_target: u8,
    trigger_side: u8,
    deferred: std::collections::VecDeque<Deferred>,
}

impl Apex6<HidLink, RealClock> {
    /// Open the controller and check it is an APEX 6 Pro: the first of its interfaces that
    /// answers (find_paths), so a dongle left plugged in does not stand in the cable's way.
    pub fn open() -> Result<Self, PadError> {
        let api = hid_api()?;
        first_that_answers(find_paths(&api)?, |path| {
            let device = api
                .open_path(&path)
                .map_err(|e| PadError::Unavailable(t!("컨트롤러를 열 수 없음 ({error})", error = e)))?;
            let mut pad = Apex6::with_link(HidLink { device }, RealClock::default());
            pad.info = Some(pad.identify(0.6)?);
            Ok(pad)
        })
    }
}

impl<L: Link, C: Clock> Apex6<L, C> {
    pub fn with_link(link: L, clock: C) -> Self {
        Apex6 {
            link,
            clock,
            info: None,
            enabled: false,
            trigger_mode: DEFAULT_TRIGGER_MODE,
            trigger_target: DEFAULT_TRIGGER_TARGET,
            trigger_side: DEFAULT_TRIGGER_SIDE as u8,
            deferred: Default::default(),
        }
    }

    pub fn link(&self) -> &L {
        &self.link
    }

    pub fn clock(&self) -> &C {
        &self.clock
    }

    /// Pretend enable() already ran (tests).
    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    fn write(&mut self, report: &[u8]) -> Result<(), PadError> {
        self.link
            .write(report)
            .map(|_| ())
            .map_err(|e| PadError::Unavailable(t!("컨트롤러 연결 끊김 ({error})", error = e)))
    }

    pub fn identify(&mut self, timeout: f64) -> Result<DeviceInfo, PadError> {
        let lost = |e: String| PadError::Unavailable(t!("컨트롤러 연결 끊김 ({error})", error = e));
        let mut buf = [0u8; 64];
        // Drop stale input before the exchange.
        for _ in 0..128 {
            if self.link.read(&mut buf, 1).map_err(lost)? == 0 {
                break;
            }
        }
        self.write(&build_command(CMD_GET_INFO, &[]))?;
        let deadline = self.clock.now() + timeout;
        while self.clock.now() < deadline {
            let n = self.link.read(&mut buf, 50).map_err(lost)?;
            if n > 0 {
                if let Some(info) = parse_device_info(&buf[..n]) {
                    if !info.is_pro() {
                        return Err(PadError::Fatal(crate::i18n::t(
                            "APEX 6 Pro가 아님 (device type 0x{type})",
                            &[("type", format!("{:02X}", info.device_type))],
                        )));
                    }
                    return Ok(info);
                }
            }
        }
        // The 2.4 GHz dongle stays enumerated while the controller sleeps.
        Err(PadError::Unavailable(t!("컨트롤러 응답 없음 (절전 중이면 버튼을 눌러 깨우세요)")))
    }

    pub fn enable(&mut self, trigger_mode: u8, trigger_target: u8, trigger_side: u8) -> Result<(), PadError> {
        self.trigger_mode = trigger_mode;
        self.trigger_target = trigger_target;
        self.trigger_side = trigger_side;
        self.reassert()?;
        self.enabled = true;
        // Silence every 8 ms through the wait, as stream() sends it.
        let silent = silent_report();
        for _ in 0..crate::pymath::round_int(ENABLE_DELAY / FRAME_PERIOD) {
            self.write(&silent)?;
            self.clock.sleep(FRAME_PERIOD);
        }
        Ok(())
    }

    /// Send the motor route again; harmless when it is still in place.
    pub fn reassert(&mut self) -> Result<(), PadError> {
        for report in motor_enable_reports(self.trigger_mode, self.trigger_target, self.trigger_side) {
            self.write(&report)?;
        }
        Ok(())
    }

    pub fn reconfigure(&mut self, trigger_mode: u8, trigger_target: u8, trigger_side: u8) -> Result<(), PadError> {
        if self.enabled {
            self.disable()?;
        }
        self.enable(trigger_mode, trigger_target, trigger_side)
    }

    /// Play the trigger channel on Route::Left, Right or Both from the next report on. The new
    /// side goes on before the one it leaves goes off; only a change is sent.
    pub fn set_trigger_side(&mut self, side: Route) -> Result<(), PadError> {
        self.change_trigger_side(side, false).map(|_| ())
    }

    /// set_trigger_side; with `defer`, the side left is let go in a later free slot. True when
    /// a frame went out.
    fn change_trigger_side(&mut self, side: Route, defer: bool) -> Result<bool, PadError> {
        let side = side as u8;
        if side == self.trigger_side {
            return Ok(false);
        }
        let old = self.trigger_side;
        self.trigger_side = side;
        self.write(&trigger_side_report(side))?;
        for one in [Route::Left as u8, Route::Right as u8] {
            if plays_on(old, one) && !plays_on(side, one) {
                if defer {
                    self.defer(Deferred::Release(one));
                } else {
                    self.write(&build_command(CMD_MOTOR_ROUTE, &[0x01, one, 0x00]))?;
                }
            }
        }
        Ok(true)
    }

    fn defer(&mut self, frame: Deferred) {
        if !self.deferred.contains(&frame) {
            self.deferred.push_back(frame);
        }
    }

    /// Send the oldest deferred frame that still applies; false when none is left.
    fn send_deferred(&mut self) -> Result<bool, PadError> {
        while let Some(next) = self.deferred.pop_front() {
            let [side, target] = motor_enable_reports(self.trigger_mode, self.trigger_target, self.trigger_side);
            let report = match next {
                Deferred::Side => side,
                Deferred::Target => target,
                Deferred::Release(one) if !plays_on(self.trigger_side, one) => {
                    build_command(CMD_MOTOR_ROUTE, &[0x01, one, 0x00])
                }
                Deferred::Release(_) => continue,
            };
            self.write(&report)?;
            return Ok(true);
        }
        Ok(false)
    }

    pub fn disable(&mut self) -> Result<(), PadError> {
        self.deferred.clear();
        let silent = silent_report();
        for _ in 0..3 {
            self.write(&silent)?;
            self.clock.sleep(FRAME_PERIOD);
        }
        // a side or an experimental target is released by name too, then the usual frames
        for (target, default) in
            [(self.trigger_side, DEFAULT_TRIGGER_SIDE as u8), (self.trigger_target, DEFAULT_TRIGGER_TARGET)]
        {
            if target != default {
                self.write(&build_command(CMD_MOTOR_ROUTE, &[0x01, target, 0x00]))?;
            }
        }
        for report in motor_disable() {
            self.write(&report)?;
        }
        self.enabled = false;
        Ok(())
    }

    pub fn write_block(&mut self, block: &Block) -> Result<(), PadError> {
        self.write(&build_realtime(block, true, true))
    }

    /// Call render(first_sample_index) every period (8 ms) and send its block; None stops.
    /// Writes are scheduled on absolute deadlines so one late frame does not shift every later one.
    ///
    /// A 2 kHz block (upsample off) lasts half a period, so the next is due 4 ms later. The link
    /// takes one report per 2 ms slot, which leaves room for one more frame between two such
    /// blocks: the route's reassert and the side a trigger change leaves go out one per block
    /// then, instead of together, which would hold the next block back past its turn.
    pub fn stream<F>(&mut self, mut render: F, max_seconds: Option<f64>, period: f64) -> Result<StreamStats, PadError>
    where
        F: FnMut(usize) -> Option<Block>,
    {
        if !self.enabled {
            self.enable(DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_TARGET, DEFAULT_TRIGGER_SIDE as u8)?;
        }
        let mut stats = StreamStats::default();
        let mut sample_index = 0usize;
        let mut silent_streak = 0u64;
        let start = self.clock.now();
        let mut deadline = start;
        let mut last_reassert = start;
        let _priority = TimeCritical::raise();
        while max_seconds.is_none_or(|max| deadline - start < max) {
            let Some(block) = render(sample_index) else { break };
            let lasts = block.period(period);
            let spread = !block.upsample;
            let now = self.clock.now();
            let lateness = (now - deadline) * 1000.0;
            stats.max_lateness_ms = crate::pymath::max(stats.max_lateness_ms, lateness);
            if lateness > lasts * 1000.0 {
                stats.late_frames += 1;
                deadline = now; // resync instead of bursting to catch up
            }
            let mut slots_used = 1;
            if let Some(side) = block.side {
                slots_used += self.change_trigger_side(side, spread)? as u32;
            }
            self.write_block(&block)?;
            let write_ms = (self.clock.now() - now) * 1000.0;
            stats.total_write_ms += write_ms;
            stats.max_write_ms = crate::pymath::max(stats.max_write_ms, write_ms);
            stats.frames += 1;
            silent_streak = if block.is_silent() { silent_streak + 1 } else { 0 };
            let every = if silent_streak > 0 { REASSERT_IDLE_S } else { REASSERT_ACTIVE_S };
            if now - last_reassert >= every {
                if spread {
                    self.defer(Deferred::Side);
                    self.defer(Deferred::Target);
                } else {
                    self.reassert()?;
                }
                last_reassert = now;
            }
            if spread {
                // the side frame already took this block's spare slot
                if slots_used == 1 {
                    self.send_deferred()?;
                }
            } else {
                while self.send_deferred()? {}
            }
            sample_index += SUBFRAMES;
            deadline += lasts;
            let remaining = deadline - self.clock.now();
            if remaining > 0.0 {
                self.clock.sleep(remaining);
            }
        }
        Ok(stats)
    }
}

impl<L: Link, C: Clock> Drop for Apex6<L, C> {
    fn drop(&mut self) {
        if self.enabled {
            let _ = self.disable(); // unplugged mid-session: nothing left to mute
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    pub struct FakeLink {
        pub reports: Vec<Vec<u8>>,
    }

    impl Link for FakeLink {
        fn write(&mut self, report: &[u8]) -> Result<usize, String> {
            self.reports.push(report.to_vec());
            Ok(report.len())
        }

        fn read(&mut self, _buf: &mut [u8], _timeout_ms: i32) -> Result<usize, String> {
            Ok(0)
        }
    }

    #[derive(Default)]
    pub struct FakeClock {
        pub now: f64,
    }

    impl Clock for FakeClock {
        fn now(&mut self) -> f64 {
            self.now
        }

        fn sleep(&mut self, seconds: f64) {
            self.now += seconds;
        }
    }

    fn pad() -> Apex6<FakeLink, FakeClock> {
        let mut pad = Apex6::with_link(FakeLink::default(), FakeClock::default());
        pad.set_enabled(true);
        pad
    }

    #[test]
    fn the_first_interface_that_answers_is_taken() {
        let asleep = || PadError::Unavailable("asleep".into());
        let other_model = || PadError::Fatal("another model".into());
        let mut tried = Vec::new();
        // the dongle is listed first and its pad does not answer; the cable's does
        let found = first_that_answers(["dongle", "cable", "more"], |path| {
            tried.push(path);
            if path == "cable" { Ok(path) } else { Err(asleep()) }
        });
        assert_eq!(found.unwrap(), "cable");
        assert_eq!(tried, ["dongle", "cable"]);
        // none answers: an error worth retrying wins over one that is not, whichever came first
        for errors in [[asleep(), other_model()], [other_model(), asleep()]] {
            let mut errors = errors.into_iter();
            let failed = first_that_answers(0..2, |_| Err::<(), _>(errors.next().unwrap()));
            assert!(matches!(failed, Err(PadError::Unavailable(text)) if text == "asleep"));
        }
        let failed = first_that_answers(0..1, |_| Err::<(), _>(other_model()));
        assert!(matches!(failed, Err(PadError::Fatal(_))));
    }

    #[test]
    fn silence_is_sent_every_block() {
        let mut pad = pad();
        let tone = Block { left_grip: [50.0; SUBFRAMES], ..Block::default() };
        let blocks: Vec<Block> = std::iter::repeat_n(tone.clone(), 5)
            .chain(std::iter::repeat_n(Block::default(), 40))
            .chain(std::iter::repeat_n(tone.clone(), 5))
            .collect();
        let stats = pad.stream(|first| blocks.get(first / SUBFRAMES).cloned(), None, FRAME_PERIOD).unwrap();
        let tone_report = build_realtime(&tone, true, true);
        let silent = build_realtime(&Block::default(), true, true);
        let realtime = pad.link().reports.iter().filter(|r| **r == tone_report || **r == silent).count();
        assert_eq!(realtime, blocks.len());
        assert_eq!(stats.frames, blocks.len() as u64);
        pad.set_enabled(false);
    }

    #[test]
    fn period_sets_the_pace() {
        let mut pad = pad();
        let stats = pad
            .stream(|first| (first < 10 * SUBFRAMES).then(Block::default), None, 0.002)
            .unwrap();
        assert_eq!((stats.frames, stats.late_frames), (10, 0));
        assert!((pad.clock().now - 0.02).abs() < 1e-12);
        pad.set_enabled(false);
    }

    #[test]
    fn two_khz_blocks_go_out_every_half_period() {
        let mut pad = pad();
        let half = Block { upsample: false, ..Block::default() };
        let stats = pad.stream(|first| (first < 10 * SUBFRAMES).then(|| half.clone()), None, FRAME_PERIOD).unwrap();
        assert_eq!((stats.frames, stats.late_frames), (10, 0));
        assert!((pad.clock().now - 0.04).abs() < 1e-12);
        let realtime: Vec<&Vec<u8>> = pad.link().reports.iter().filter(|r| r[3] == CMD_REALTIME_MOTOR).collect();
        assert!(realtime.iter().all(|r| r[5] & 0x80 == 0), "bit 7 clear");
        pad.set_enabled(false);
    }

    #[test]
    fn between_2k_blocks_goes_one_route_frame_at_most() {
        let mut pad = pad();
        let tone = |side| Block {
            trigger: [20.0; SUBFRAMES],
            route: Route::Both,
            side: Some(side),
            upsample: false,
            ..Block::default()
        };
        // a side change each block for a while, then 2 s of silence (a reassert falls due)
        let blocks: Vec<Block> = [Route::Left, Route::Right, Route::Left, Route::Both, Route::Left]
            .into_iter()
            .map(tone)
            .chain(std::iter::repeat_n(Block { upsample: false, ..Block::default() }, 500))
            .collect();
        pad.stream(|first| blocks.get(first / SUBFRAMES).cloned(), None, FRAME_PERIOD).unwrap();
        let reports = pad.link().reports.clone();
        let route = |r: &Vec<u8>| r[3] == CMD_MOTOR_ROUTE;
        // each block's own side goes on just before it
        let first = reports.iter().position(|r| !route(r)).unwrap();
        assert_eq!(reports[first - 1], trigger_side_report(Route::Left as u8).to_vec());
        // one frame at most between two blocks: a deferred one, or the next block's own side
        let mut since_block = 0;
        let mut realtime = 0;
        for (i, r) in reports.iter().enumerate() {
            if route(r) {
                since_block += 1;
                assert!(since_block <= 1, "report {i}: {since_block} frames");
            } else {
                since_block = 0;
                realtime += 1;
            }
        }
        assert_eq!(realtime, blocks.len());
        // the sides left wait for a free slot, once each; the channel ended on Left, so Left's
        // own release had gone stale by then and only Right is let go
        let release = |one: Route| build_command(CMD_MOTOR_ROUTE, &[0x01, one as u8, 0x00]).to_vec();
        assert_eq!(reports.iter().filter(|r| **r == release(Route::Right)).count(), 1);
        assert_eq!(reports.iter().filter(|r| **r == release(Route::Left)).count(), 0);
        // the reassert came, both its frames, with the side as it stands
        let [side, target] = motor_enable_reports(DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_TARGET, Route::Left as u8);
        assert!(reports.iter().filter(|r| **r == side.to_vec()).count() >= 3); // two changes to Left, a reassert
        assert!(reports.contains(&target.to_vec()));
        pad.set_enabled(false);
    }

    #[test]
    fn the_wait_after_the_route_is_silence_every_block() {
        let mut pad = Apex6::with_link(FakeLink::default(), FakeClock::default());
        pad.enable(DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_TARGET, DEFAULT_TRIGGER_SIDE as u8).unwrap();
        let reports = &pad.link().reports;
        let enable = motor_enable_reports(DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_TARGET, DEFAULT_TRIGGER_SIDE as u8);
        assert_eq!(reports[0], enable[0].to_vec());
        assert_eq!(reports[1], enable[1].to_vec());
        let silence = &reports[2..];
        assert!(silence.iter().all(|r| *r == silent_report().to_vec()));
        assert_eq!(silence.len() as i64, crate::pymath::round_int(ENABLE_DELAY / FRAME_PERIOD));
        pad.set_enabled(false);
    }

    #[test]
    fn dropping_the_pad_mutes_and_releases_the_motors() {
        let mut pad = pad();
        pad.disable().unwrap();
        let reports = pad.link().reports.clone();
        assert_eq!(&reports[..3], &[silent_report().to_vec(), silent_report().to_vec(), silent_report().to_vec()]);
        assert_eq!(reports[3..].to_vec(), motor_disable().iter().map(|r| r.to_vec()).collect::<Vec<_>>());
    }

    #[test]
    fn samples_use_the_whole_int8_range() {
        let block = Block {
            trigger: [127.0, -127.0, 200.0, -200.0, 0.5, 1.5, -0.5, 0.0],
            route: Route::Both,
            ..Block::default()
        };
        let report = build_realtime(&block, true, true);
        let triggers: Vec<u8> = (0..SUBFRAMES).map(|i| report[5 + 1 + i * 3]).collect();
        assert_eq!(triggers, vec![255, 1, 255, 1, 128, 130, 128, 128]);
    }

    #[test]
    fn reports_match_the_python_bytes() {
        // From apex6.py: SILENT_REPORT, the motor routes and one realtime block.
        let hex = |r: &Report| r.iter().map(|b| format!("{b:02x}")).collect::<String>();
        assert_eq!(hex(&silent_report()), GOLDEN_SILENT);
        let enable = motor_enable_reports(DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_TARGET, DEFAULT_TRIGGER_SIDE as u8);
        assert_eq!(hex(&enable[0]), GOLDEN_ENABLE[0]);
        assert_eq!(hex(&enable[1]), GOLDEN_ENABLE[1]);
        let block = Block {
            trigger: [1.4, -2.6, 50.0, 0.0, 0.0, 0.0, 0.0, -101.5],
            left_grip: [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0],
            right_grip: [-10.0, -20.0, -30.0, -40.0, -50.0, -60.0, -70.0, -126.5],
            route: Route::Both,
            ..Block::default()
        };
        assert_eq!(hex(&build_realtime(&block, true, true)), GOLDEN_BLOCK);
    }

    const GOLDEN_SILENT: &str = "005aa5571b9b80808080808080808080808080808080808080808080808000000d";
    const GOLDEN_ENABLE: [&str; 2] = [
        "005aa55307010203004000000000000000000000000000000000000000000000a0",
        "005aa55307011202400000000000000000000000000000000000000000000000af",
    ];
    const GOLDEN_BLOCK: &str = "005aa5571b9e818a767d946cb29e6280a85880b24e80bc4480c63a1ad0020000ac";
}
