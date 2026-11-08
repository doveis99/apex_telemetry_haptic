//! One block per 8 ms tick, and the loop that holds the controller only while the game runs
//! (port of ats_haptics.py's Runner and run()). An effect test (preview.rs) plays in place of
//! the drive while the drive renders on underneath.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::apex6::{Apex6, Block, PadError, StreamStats, FRAME_PERIOD, MAX_SAMPLE};
use crate::blocklog::{log_dir, BlockLog};
use crate::effects::{Equalizer, Gains, Synth, FLOAT_FIELDS};
use crate::gamewatch::GameWatcher;
use crate::preview::Preview;
use crate::pymath as m;
use crate::rate2k::Output2k;
use crate::replay::LogTelemetry;
use crate::scenarios::{Scenario, ScenarioTelemetry};
use crate::t;
use crate::telemetry::{Frame, SharedMemoryTelemetry, SimulatedTelemetry, TelemetrySource};

/// game minimized, loading or closed: render_time stops moving
pub const STALE_AFTER_S: f64 = 0.3;
/// wait between attempts to reach an absent or sleeping controller
pub const RETRY_S: f64 = 2.0;
const EVENTS_KEPT: usize = 50;

/// The version the debug log's .json records: the Python app's number, marked as this port.
pub fn version() -> String {
    format!("{} (rust)", env!("CARGO_PKG_VERSION"))
}

/// The local time of day as HH:MM:SS, for event lines.
pub fn clock_hms() -> String {
    crate::clock::now().hms()
}

/// Where the frames come from. Anything but the game is played without waiting for it.
pub enum Source {
    Game,
    /// the scripted 60 s drive; the frame rate it holds its frames at, None for every block
    Simulated(Option<f64>),
    Scenario(Scenario, Option<f64>),
    /// a debug log, optionally from and to a time of day (HH:MM:SS)
    Replay { log: PathBuf, from: Option<String>, to: Option<String> },
}

pub type StateCallback = Box<dyn FnMut(&str) + Send>;

/// Produces one block per 8 ms tick and keeps state for a monitor or UI.
pub struct Runner {
    gains: Gains,
    eq: Equalizer,
    pub synth: Synth,
    pub simulated: bool,
    source: Box<dyn TelemetrySource>,
    watcher: Option<GameWatcher>,
    on_state: Option<StateCallback>,
    deadline: Option<Instant>,
    /// set from any thread (Ctrl+C, the window) to stop at the next block
    pub stop_requested: Arc<AtomicBool>,
    pub game_ended: bool,
    pub last_stats: Option<StreamStats>,
    pub state: String,
    pub frame: Option<Frame>,
    /// left grip, right grip, trigger; 0..1, held until read
    pub peaks: [f64; 3],
    pub events: VecDeque<String>,
    pub max_accel: f64,
    last_render_time: Option<i64>,
    last_render_change: Instant,
    /// may be flipped from any thread; the output thread follows it at the next block
    pub log_blocks: Arc<AtomicBool>,
    pub log_dir: PathBuf,
    pub block_log: Option<BlockLog>,
    /// the controller is held, so a log may be open
    log_here: bool,
    /// state changes and edited settings, for the log's next row
    log_notes: Vec<String>,
    /// an effect test playing in place of the drive
    pub preview: Option<Preview>,
    /// may be flipped from any thread: from the next 8 ms block on, the output goes out at
    /// 2 kHz (src/rate2k.rs), a report every 4 ms
    pub output_2k: Arc<AtomicBool>,
    rate2k: Option<Output2k>,
}

impl Runner {
    pub fn new(gains: &Gains, eq: Equalizer, source: Source, seconds: Option<f64>, log_blocks: bool) -> Runner {
        let simulated = !matches!(source, Source::Game);
        let source: Box<dyn TelemetrySource> = match source {
            Source::Game => Box::new(SharedMemoryTelemetry::default()),
            Source::Simulated(fps) => Box::new(SimulatedTelemetry::new(fps)),
            Source::Scenario(scenario, fps) => Box::new(ScenarioTelemetry::new(scenario, fps)),
            Source::Replay { log, from, to } => Box::new(LogTelemetry::new(log, from.as_deref(), to.as_deref())),
        };
        let runner = Runner::with_source(gains.effective(), eq, source, simulated, seconds);
        runner.log_blocks.store(log_blocks, Ordering::Relaxed);
        runner
    }

    pub fn with_source(gains: Gains, eq: Equalizer, source: Box<dyn TelemetrySource>, simulated: bool,
                       seconds: Option<f64>) -> Runner {
        Runner {
            synth: Synth::new(gains.clone(), eq.clone()),
            gains,
            eq,
            simulated,
            source,
            watcher: if simulated { None } else { Some(GameWatcher::new()) },
            on_state: None,
            deadline: seconds.map(|s| Instant::now() + Duration::from_secs_f64(s)),
            stop_requested: Arc::new(AtomicBool::new(false)),
            game_ended: false,
            last_stats: None,
            state: String::new(),
            frame: None,
            peaks: [0.0; 3],
            events: VecDeque::with_capacity(EVENTS_KEPT),
            max_accel: 0.0,
            last_render_time: None,
            last_render_change: Instant::now(),
            log_blocks: Arc::new(AtomicBool::new(false)),
            log_dir: log_dir(),
            block_log: None,
            log_here: false,
            log_notes: Vec::new(),
            preview: None,
            output_2k: Arc::new(AtomicBool::new(false)),
            rate2k: None,
        }
    }

    pub fn on_state(mut self, callback: StateCallback) -> Runner {
        self.on_state = Some(callback);
        self
    }

    pub fn gains(&self) -> &Gains {
        &self.gains
    }

    pub fn source_name(&self) -> String {
        self.source.name()
    }

    pub fn push_event(&mut self, line: String) {
        if self.events.len() == EVENTS_KEPT {
            self.events.pop_front();
        }
        self.events.push_back(line);
    }

    fn read_frame(&mut self) -> Option<Frame> {
        let frame = self.source.read();
        let now = Instant::now();
        let Some(frame) = frame else {
            let state = match self.source.problem() {
                Some(problem) => t!("텔레메트리 없음: {problem} → '텔레메트리 플러그인'에서 설치", problem = problem),
                None => t!("텔레메트리 대기 중 (게임 로딩 중이거나 플러그인 없음)"),
            };
            self.set_state(state);
            return None;
        };
        if Some(frame.render_time) != self.last_render_time {
            self.last_render_time = Some(frame.render_time);
            self.last_render_change = now;
        }
        if !frame.live() {
            self.set_state(t!("일시정지/메뉴"));
            return None;
        }
        if (now - self.last_render_change).as_secs_f64() > STALE_AFTER_S {
            self.set_state(t!("텔레메트리 멈춤 (최소화/로딩)"));
            return None;
        }
        self.set_state(t!("주행 중 ({source})", source = self.source.name()));
        Some(frame)
    }

    pub fn set_state(&mut self, state: String) {
        if state != self.state {
            self.state = state.clone();
            self.log_notes.push(state.clone());
            if let Some(callback) = &mut self.on_state {
                callback(&state);
            }
        }
    }

    /// Copy edited gains into the running synth (disabled effects at zero); the next block uses them.
    pub fn apply(&mut self, gains: &Gains) {
        let effective = gains.effective();
        let mut changed = Vec::new();
        for name in FLOAT_FIELDS {
            let (old, new) = (self.gains.float(name).unwrap(), effective.float(name).unwrap());
            if old != new {
                changed.push(format!("{name} {old}->{new}"));
            }
        }
        if self.gains.cylinders != effective.cylinders {
            changed.push(format!("cylinders {}->{}", self.gains.cylinders, effective.cylinders));
        }
        if self.gains.gearbox != effective.gearbox {
            changed.push(format!("gearbox {}->{}", self.gains.gearbox, effective.gearbox));
        }
        if self.gains.disabled != effective.disabled {
            changed.push(format!("disabled {:?}->{:?}", self.gains.disabled, effective.disabled));
        }
        if self.gains.routes != effective.routes {
            changed.push(format!("routes {:?}->{:?}", self.gains.routes, effective.routes));
        }
        self.gains = effective.clone();
        self.synth.g = effective;
        if !changed.is_empty() {
            self.log_notes.push(format!("settings: {}", changed.join(", ")));
        }
    }

    pub fn finished(&self) -> bool {
        self.stop_requested.load(Ordering::Relaxed) || self.deadline.is_some_and(|d| Instant::now() > d)
    }

    pub fn game_running(&self) -> bool {
        self.watcher.as_ref().is_none_or(|w| w.game().is_some())
    }

    /// Block until ATS/ETS2 runs. False if stopped first.
    pub fn wait_for_game(&mut self) -> bool {
        while !self.finished() {
            if self.game_running() {
                return true;
            }
            self.frame = None;
            self.set_state(t!("게임 대기 중 (ATS/ETS2 실행 전)"));
            std::thread::sleep(Duration::from_millis(250));
        }
        false
    }

    pub fn start_session(&mut self) {
        self.game_ended = false;
        self.synth = Synth::new(self.gains.clone(), self.eq.clone()); // fresh edge detectors: no stale events
        self.rate2k = None;
        self.last_render_time = None;
        // Map the plugin afresh: the next game may write another plugin's mapping.
        self.source.close();
    }

    /// Start a debug log once the controller is taken: one file per connection, not per attempt.
    pub fn open_log(&mut self) {
        self.close_log();
        self.log_here = true;
        self.follow_log_switch();
    }

    /// Open or close the log when log_blocks was switched mid-drive (output thread only).
    fn follow_log_switch(&mut self) {
        let wanted = self.log_blocks.load(Ordering::Relaxed);
        if !self.log_here || wanted == self.block_log.is_some() {
            return;
        }
        let stamp = clock_hms();
        if let Some(log) = self.block_log.take() {
            let _ = log.close();
            self.push_event(format!("{stamp} {}", t!("디버그 기록 종료")));
            return;
        }
        let info: Value = json!({
            "version": version(),
            "source": self.source.name(),
            "gains": serde_json::to_value(&self.gains).unwrap_or(Value::Null),
            "eq_curves": Value::Object(self.eq.curves.clone()),
        });
        match BlockLog::new(&self.log_dir, Some(&info)) {
            Ok(log) => {
                let name = log.path.file_name().unwrap_or_default().to_string_lossy().into_owned();
                self.block_log = Some(log);
                self.push_event(format!("{stamp} {}", t!("디버그 기록: {name}", name = name)));
            }
            Err(error) => {
                self.log_here = false; // not again every 8 ms; the next connection tries afresh
                self.push_event(format!("{stamp} {}", t!("디버그 기록을 열 수 없음 ({error})", error = error)));
            }
        }
    }

    pub fn close_log(&mut self) {
        self.log_here = false;
        if let Some(log) = self.block_log.take() {
            let _ = log.close();
        }
    }

    /// The next block to send: a new 8 ms one, or at 2 kHz each one's two halves in turn.
    pub fn render(&mut self) -> Option<Block> {
        if let Some(second) = self.rate2k.as_mut().and_then(Output2k::pending) {
            return Some(second);
        }
        // the rate changes between two 8 ms blocks, never inside one
        let two_k = self.output_2k.load(Ordering::Relaxed);
        if two_k != self.rate2k.is_some() {
            self.rate2k = two_k.then(Output2k::new);
        }
        self.synth.set_events_2k(two_k);
        if let Some(preview) = &mut self.preview {
            preview.set_events_2k(two_k);
        }
        let block = self.render_block()?;
        Some(match &mut self.rate2k {
            Some(output) => output.first(&block),
            None => block,
        })
    }

    fn render_block(&mut self) -> Option<Block> {
        if self.finished() {
            return None;
        }
        if !self.game_running() {
            self.game_ended = true;
            self.frame = None;
            return None;
        }
        let frame = self.read_frame();
        let mut block = self.synth.render(frame.as_ref());
        self.max_accel = m::max(self.max_accel, self.synth.levels.max_accel);
        self.synth.levels.max_accel = 0.0;
        if !self.synth.levels.events.is_empty() {
            let stamp = clock_hms();
            for event in self.synth.levels.events.clone() {
                self.push_event(format!("{stamp} {event}"));
            }
        }
        // the drive renders on underneath a test, so it carries on where it was
        let playing = self.preview.as_ref().map_or("", |p| p.effect.name());
        if let Some(preview) = &mut self.preview {
            match preview.render() {
                Some(solo) => block = solo,
                None => self.preview = None,
            }
        }
        for (held, peak) in self.peaks.iter_mut().zip(block.peaks()) {
            *held = m::max(*held, peak / MAX_SAMPLE);
        }
        self.follow_log_switch();
        let notes = std::mem::take(&mut self.log_notes);
        if let Some(log) = &mut self.block_log {
            let events: Vec<String> = self.synth.levels.events.iter().cloned().chain(notes).collect();
            if let Err(error) = log.add(&block, frame.as_ref(), &events, Some(&self.synth), playing) {
                // a full disk or a pulled drive: stop logging, keep the motors going
                self.close_log();
                let line = format!("{} {}", clock_hms(), t!("디버그 기록을 열 수 없음 ({error})", error = error));
                self.push_event(line);
            }
        }
        self.frame = frame;
        Some(block)
    }

    pub fn take_peaks(&mut self) -> [f64; 3] {
        std::mem::take(&mut self.peaks)
    }

    pub fn take_max_accel(&mut self) -> f64 {
        std::mem::take(&mut self.max_accel)
    }

    pub fn close(&mut self) {
        self.close_log();
        self.source.close();
        if let Some(watcher) = &mut self.watcher {
            watcher.close();
        }
    }
}

/// What run() reports: the controller taken (its name) or released (with the stream's stats).
pub enum Connection<'a> {
    Taken(&'a str),
    Released(Option<&'a StreamStats>),
}

/// Hold the controller only while the game runs; repeat for each game launch. Returns when
/// stopped, or after the game quits if exit_with_game; Err only for what retrying cannot fix.
/// `after_block` runs after every block (the command line's monitor line).
pub fn run(runner: &mut Runner, dry_run: bool, mut on_connected: impl FnMut(Connection), exit_with_game: bool,
           mut after_block: impl FnMut(&mut Runner)) -> Result<(), PadError> {
    while runner.wait_for_game() {
        runner.start_session();
        if dry_run {
            runner.open_log();
            let mut deadline = Instant::now();
            loop {
                let block = runner.render();
                after_block(runner);
                let Some(block) = block else { break };
                deadline += Duration::from_secs_f64(block.period(FRAME_PERIOD));
                if let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
                    std::thread::sleep(remaining);
                }
            }
            runner.close_log();
        } else {
            let streamed = Apex6::open().and_then(|mut pad| {
                runner.open_log();
                let name = pad.info.as_ref().map(|i| i.name()).unwrap_or_default();
                on_connected(Connection::Taken(&name));
                let render = |_first: usize| {
                    let block = runner.render();
                    after_block(runner);
                    block
                };
                pad.stream(render, None, FRAME_PERIOD)
                // dropping the pad mutes and releases the motors
            });
            runner.close_log(); // flushed now, not when the next game starts
            match streamed {
                Ok(stats) => runner.last_stats = Some(stats),
                Err(PadError::Fatal(error)) => {
                    on_connected(Connection::Released(None));
                    return Err(PadError::Fatal(error));
                }
                Err(PadError::Unavailable(error)) => {
                    // Asleep, unplugged or reconnecting: keep trying while the game runs.
                    on_connected(Connection::Released(None));
                    runner.frame = None;
                    runner.set_state(t!("{error} · {seconds}초 후 재시도", error = error, seconds = format!("{RETRY_S:.0}")));
                    let line = format!("{} {error}", clock_hms());
                    runner.push_event(line);
                    let deadline = Instant::now() + Duration::from_secs_f64(RETRY_S);
                    while !runner.finished() && Instant::now() < deadline {
                        std::thread::sleep(Duration::from_millis(100));
                    }
                    if runner.game_running() {
                        continue;
                    }
                    runner.game_ended = true; // quit without ever reaching the pad
                }
            }
        }
        on_connected(Connection::Released(runner.last_stats.as_ref()));
        if !runner.game_ended || exit_with_game {
            return Ok(());
        }
        runner.set_state(t!("게임 종료됨, 컨트롤러 해제"));
    }
    Ok(())
}
