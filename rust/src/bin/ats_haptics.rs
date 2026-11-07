//! ATS / ETS2 telemetry -> APEX 6 Pro voice-coil haptics, command line (port of ats_haptics.py).
//!
//!   ats_haptics                      run with the game (waits for it)
//!   ats_haptics --simulate           scripted 60 s test drive, no game needed
//!   ats_haptics --simulate --sim-fps 30   the same drive as a 30 fps game (0: new data every block)
//!   ats_haptics --scenario city      a few minutes of real driving: highway, city or mountain
//!   ats_haptics --replay LOG.csv     a drive recorded by the debug log, again (--from/--to HH:MM:SS)
//!   ats_haptics --dry-run            no controller; print levels only
//!   ats_haptics --gain 0.8 --road 0.4 --trigger 0.3 --monitor
//!   ats_haptics --exit-with-game
//!
//! The controller is only opened while ATS/ETS2 is running and released when the game quits.
//! Defaults come from config.json (saved by the window); flags override them. Close the
//! X-Haptics engine first: it streams to the same motors. Ctrl+C stops and releases the motors.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use ats_haptics::effects::{Equalizer, Gains, GEARBOXES};
use ats_haptics::runner::{run, Connection, Runner, Source};
use ats_haptics::scenarios::{scenario, scenarios};
use ats_haptics::settings::Settings;
use ats_haptics::t;
use ats_haptics::telemetry::SIMULATED_FPS;

const USAGE: &str = "usage: ats_haptics [--simulate] [--sim-fps FPS] [--dry-run] [--monitor] [--seconds S]
                   [--exit-with-game] [--gain G] [--grip-level G] [--trigger-level G]
                   [--engine G] [--road G] [--bumps G] [--rumble G] [--wheelspin G] [--gear G]
                   [--retarder G] [--impact G] [--trigger G] [--throttle-pulse P]
                   [--cylinders N] [--gearbox amt|converter] [--scenario highway|city|mountain]
                   [--replay LOG] [--from HH:MM:SS] [--to HH:MM:SS] [--log-blocks]

--scenario and --replay play without the game, as --simulate does. --log-blocks writes every
output block as CSV under %APPDATA%\\ATSHaptics\\logs.";

static STOP: AtomicBool = AtomicBool::new(false);

unsafe extern "system" fn on_ctrl(_kind: u32) -> i32 {
    STOP.store(true, Ordering::Relaxed);
    1 // handled: let the stream stop and release the motors
}

struct Args {
    simulate: bool,
    sim_fps: f64,
    dry_run: bool,
    monitor: bool,
    seconds: Option<f64>,
    exit_with_game: bool,
    scenario: Option<String>,
    replay: Option<String>,
    replay_from: Option<String>,
    replay_to: Option<String>,
    log_blocks: bool,
}

fn fail(message: &str) -> ! {
    eprintln!("{USAGE}\n\nerror: {message}");
    std::process::exit(2);
}

fn parse_args(gains: &mut Gains) -> Args {
    let mut args = Args {
        simulate: false,
        sim_fps: SIMULATED_FPS,
        dry_run: false,
        monitor: false,
        seconds: None,
        exit_with_game: false,
        scenario: None,
        replay: None,
        replay_from: None,
        replay_to: None,
        log_blocks: false,
    };
    let mut items = std::env::args().skip(1);
    while let Some(item) = items.next() {
        let (flag, inline) = match item.split_once('=') {
            Some((flag, value)) if flag.starts_with("--") => (flag.to_string(), Some(value.to_string())),
            _ => (item.clone(), None),
        };
        let mut value = |what: &str| -> String {
            inline.clone().or_else(|| items.next()).unwrap_or_else(|| fail(&format!("{flag} needs {what}")))
        };
        let mut number = |what: &str| -> f64 {
            let text = value(what);
            text.parse().unwrap_or_else(|_| fail(&format!("{flag}: not a number: {text}")))
        };
        match flag.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            "--simulate" => args.simulate = true,
            "--sim-fps" => args.sim_fps = number("a frame rate"),
            "--dry-run" => args.dry_run = true,
            "--monitor" => args.monitor = true,
            "--seconds" => args.seconds = Some(number("seconds")),
            "--exit-with-game" => args.exit_with_game = true,
            "--gain" => gains.master = number("a gain"),
            "--cylinders" => gains.cylinders = number("a count") as i64,
            "--gearbox" => {
                let gearbox = value("amt or converter");
                if !GEARBOXES.contains(&gearbox.as_str()) {
                    fail(&format!("--gearbox: choose from {}", GEARBOXES.join(", ")));
                }
                gains.gearbox = gearbox;
            }
            "--scenario" => {
                let name = value("a scenario");
                if scenario(&name).is_none() {
                    let names: Vec<_> = scenarios().iter().map(|s| s.name).collect();
                    fail(&format!("--scenario: choose from {}", names.join(", ")));
                }
                args.scenario = Some(name);
            }
            "--replay" => args.replay = Some(value("a debug log")),
            "--from" => args.replay_from = Some(value("HH:MM:SS")),
            "--to" => args.replay_to = Some(value("HH:MM:SS")),
            "--log-blocks" => args.log_blocks = true,
            _ => {
                let name = flag.trim_start_matches("--").replace('-', "_");
                let known = ["grip_level", "trigger_level", "engine", "road", "bumps", "rumble", "wheelspin", "gear",
                             "retarder", "impact", "trigger", "throttle_pulse"];
                if !flag.starts_with("--") || !known.contains(&name.as_str()) {
                    fail(&format!("unrecognized argument: {item}"));
                }
                *gains.float_mut(&name).unwrap() = number("a gain");
            }
        }
    }
    args
}

fn main() {
    let settings = Settings::default();
    let mut gains = settings.load_gains();
    let args = parse_args(&mut gains);
    let eq = Equalizer::for_player(&settings.load_eq());
    let sim_fps = (args.sim_fps != 0.0).then_some(args.sim_fps);
    let source = if let Some(log) = args.replay {
        Source::Replay { log: log.into(), from: args.replay_from, to: args.replay_to }
    } else if let Some(name) = args.scenario {
        Source::Scenario(scenario(&name).unwrap(), sim_fps)
    } else if args.simulate {
        Source::Simulated(sim_fps)
    } else {
        Source::Game
    };
    let mut runner = Runner::new(&gains, eq, source, args.seconds, args.log_blocks)
        .on_state(Box::new(|state| println!("\n[{}] {state}", ats_haptics::runner::clock_hms())));
    // SAFETY: the handler only stores to an atomic.
    unsafe {
        windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(on_ctrl), 1);
    }
    let stop = Arc::clone(&runner.stop_requested);
    std::thread::spawn(move || loop {
        if STOP.load(Ordering::Relaxed) {
            stop.store(true, Ordering::Relaxed);
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    });

    let monitor = args.monitor || args.dry_run;
    let mut next_status = Instant::now();
    let status_line = |runner: &mut Runner| {
        if !monitor {
            return;
        }
        while let Some(event) = runner.events.pop_front() {
            println!("\n  event: {event}");
        }
        let Some(frame) = runner.frame.clone() else { return };
        if Instant::now() < next_status {
            return;
        }
        next_status = Instant::now() + std::time::Duration::from_millis(500);
        let accel = runner.take_max_accel();
        let lv = &runner.synth.levels;
        let surface = if lv.surface.is_empty() { "-" } else { lv.surface.as_str() };
        print!(
            "\r  {:5.1} km/h  {:5.0} rpm  gear {:>2}  thr {:.2} brk {:.2}  engine {:.2} road L{:.2}/R{:.2}  \
             trig {}:{:.2}  surface {:10.10} accel {:4.1}",
            frame.speed.abs() * 3.6,
            frame.rpm,
            frame.gear,
            frame.user_throttle,
            frame.user_brake,
            lv.engine,
            lv.road_left,
            lv.road_right,
            lv.trigger_mode,
            lv.trigger,
            surface,
            accel
        );
        let _ = std::io::stdout().flush();
    };
    let on_connected = |connection: Connection| match connection {
        Connection::Taken(name) => println!("\n{}", t!("{name} 연결됨. Ctrl+C로 종료.", name = name)),
        Connection::Released(Some(stats)) => println!("\n{}", t!("컨트롤러 해제. {stats}", stats = stats.summary())),
        Connection::Released(None) => {}
    };
    let result = run(&mut runner, args.dry_run, on_connected, args.exit_with_game, status_line);
    runner.close();
    match result {
        Ok(()) => {
            if STOP.load(Ordering::Relaxed) {
                println!("\n{}", t!("종료: 모터 해제 완료."));
            }
        }
        Err(error) => {
            eprintln!("\n{error}");
            std::process::exit(1);
        }
    }
}
