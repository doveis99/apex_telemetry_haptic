//! The output on its own thread: a Runner in run()'s loop, as the command line runs it. The
//! window hands it edited gains and effect tests and reads back what to show, through Shared;
//! each block takes the lock once, for a few microseconds.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use ats_haptics::apex6::StreamStats;
use ats_haptics::effects::{Equalizer, Gains};
use ats_haptics::errorlog::panic_message;
use ats_haptics::preview::Preview;
use ats_haptics::pymath as m;
use ats_haptics::runner::{run, Connection, Runner, Source};
use ats_haptics::telemetry::Frame;

/// What the window shows of the drive.
#[derive(Clone, Debug, Default)]
pub struct Status {
    pub state: String,
    /// the controller's name while it is held
    pub connected: Option<String>,
    pub frame: Option<Frame>,
    pub surface: String,
    pub trigger_mode: &'static str,
    /// left grip, right grip, trigger; 0..1, held until read
    pub peaks: [f64; 3],
    pub events: Vec<String>,
    pub max_accel: f64,
    /// the stream's stats, set once when the controller is let go
    pub session_stats: Option<StreamStats>,
    pub game_ended: bool,
    pub error: Option<String>,
}

#[derive(Default)]
pub struct Shared {
    pub gains: Option<Gains>,
    pub preview: Option<Preview>,
    pub status: Status,
}

pub struct Worker {
    thread: Option<JoinHandle<()>>,
    pub shared: Arc<Mutex<Shared>>,
    pub stop: Arc<AtomicBool>,
    pub log_blocks: Arc<AtomicBool>,
    /// 2 kHz output, followed from the next block on
    pub output_2k: Arc<AtomicBool>,
    /// the motor curves it started with, for effect tests played in place of the drive
    pub eq: Equalizer,
}

impl Worker {
    pub fn start(gains: &Gains, eq: Equalizer, source: Source, log_blocks: bool, output_2k: bool, dry_run: bool,
                 exit_with_game: bool) -> Worker {
        let mut runner = Runner::new(gains, eq.clone(), source, None, log_blocks);
        runner.output_2k.store(output_2k, Ordering::Relaxed);
        let two_k = Arc::clone(&runner.output_2k);
        let shared = Arc::new(Mutex::new(Shared::default()));
        let stop = Arc::clone(&runner.stop_requested);
        let log = Arc::clone(&runner.log_blocks);
        let for_thread = Arc::clone(&shared);
        let thread = std::thread::Builder::new()
            .name("output".into())
            .spawn(move || {
                let shared = for_thread;
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let on_connected = |connection: Connection| {
                        let mut shared = shared.lock().unwrap();
                        match connection {
                            Connection::Taken(name) => shared.status.connected = Some(name.to_string()),
                            Connection::Released(stats) => {
                                shared.status.connected = None;
                                if let Some(stats) = stats {
                                    shared.status.session_stats = Some(stats.clone());
                                }
                            }
                        }
                    };
                    let after_block = |runner: &mut Runner| {
                        let mut shared = shared.lock().unwrap();
                        if let Some(gains) = shared.gains.take() {
                            runner.apply(&gains);
                        }
                        if let Some(preview) = shared.preview.take() {
                            runner.preview = Some(preview);
                        }
                        let status = &mut shared.status;
                        status.state = runner.state.clone();
                        for (held, peak) in status.peaks.iter_mut().zip(runner.take_peaks()) {
                            *held = m::max(*held, peak);
                        }
                        status.events.extend(runner.events.drain(..));
                        status.frame = runner.frame.clone();
                        status.surface = runner.synth.levels.surface.clone();
                        status.trigger_mode = runner.synth.levels.trigger_mode;
                        status.max_accel = m::max(status.max_accel, runner.take_max_accel());
                        status.game_ended = runner.game_ended;
                    };
                    let result = run(&mut runner, dry_run, on_connected, exit_with_game, after_block);
                    let mut shared = shared.lock().unwrap();
                    shared.status.game_ended = runner.game_ended;
                    shared.status.state = runner.state.clone();
                    shared.status.events.extend(runner.events.drain(..));
                    if let Err(error) = result {
                        shared.status.error = Some(error.to_string());
                    }
                    drop(shared);
                    runner.close();
                }));
                if let Err(payload) = result {
                    // the panic hook has written the backtrace to error.log
                    let mut shared = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                    shared.status.error = Some(panic_message(payload.as_ref()));
                }
            })
            .expect("output thread");
        Worker { thread: Some(thread), shared, stop, log_blocks: log, output_2k: two_k, eq }
    }

    pub fn running(&self) -> bool {
        self.thread.as_ref().is_some_and(|t| !t.is_finished())
    }

    pub fn stopping(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    pub fn request_stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }

    /// Wait up to `seconds` for the thread to let the controller go.
    pub fn join(&mut self, seconds: f64) {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(seconds);
        while self.running() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        if !self.running() {
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    /// Take what the window shows: the status, its peaks and events handed over.
    pub fn take_status(&self) -> Status {
        let mut shared = self.shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let status = shared.status.clone();
        shared.status.peaks = [0.0; 3];
        shared.status.events.clear();
        shared.status.max_accel = 0.0;
        shared.status.session_stats = None;
        status
    }
}
