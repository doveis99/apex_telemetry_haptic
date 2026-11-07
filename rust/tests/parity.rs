//! The Rust synth against the Python one, sample by sample.
//!
//! rust/golden/make_golden.py records what effects.py makes of known drives (the scripted
//! drive at three frame rates and three settings, the scenarios, slices of real debug logs)
//! into rust/golden-data/*.jsonl. This feeds the same frames to the Rust synth and requires
//! every output sample to be the same float, bit for bit, along with the route, the trigger
//! side, the events and the steady-fade and ducking state. Without the data it says so and
//! passes; generate it with
//!
//!   .venv\Scripts\python.exe rust\golden\make_golden.py

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use ats_haptics::apex6::{build_realtime, FRAME_PERIOD};
use ats_haptics::effects::{Equalizer, Gains, Synth};
use ats_haptics::pyrandom::PyRandom;
use ats_haptics::telemetry::{Frame, SimulatedTelemetry, TelemetrySource};
use serde::Deserialize;
use serde_json::{Map, Value};

#[derive(Deserialize)]
struct Header {
    name: String,
    seed: u64,
    gains: Gains,
    eq_curves: Map<String, Value>,
    #[serde(default)]
    sim_fps: Option<f64>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum FrameField {
    /// "f": 1, the frame the block before was given
    Same(#[allow(dead_code)] u8),
    Frame(Box<Frame>),
}

#[derive(Deserialize)]
struct Row {
    f: Option<FrameField>,
    t: [f64; 8],
    l: [f64; 8],
    r: [f64; 8],
    route: u8,
    side: Option<u8>,
    ev: Vec<String>,
    lv: (f64, f64, f64, f64, String, String, f64),
    fr: [f64; 3],
    dk: [f64; 3],
}

fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("golden-data")
}

fn golden_files() -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(golden_dir()) else { return Vec::new() };
    let mut files: Vec<PathBuf> =
        entries.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == "jsonl")).collect();
    files.sort();
    files
}

struct Report {
    name: String,
    blocks: usize,
    sample_blocks: usize,
    int_blocks: usize,
    max_diff: f64,
    other_blocks: usize,
    first: Option<String>,
}

impl Report {
    fn clean(&self) -> bool {
        self.sample_blocks == 0 && self.other_blocks == 0
    }
}

fn same_bits(a: &[f64], b: &[f64]) -> bool {
    a.iter().zip(b).all(|(x, y)| x.to_bits() == y.to_bits() || (*x == 0.0 && *y == 0.0))
}

fn check(path: &Path) -> Report {
    let mut lines = BufReader::new(File::open(path).unwrap()).lines();
    let header: Header = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    let eq = Equalizer::new(&header.eq_curves);
    let mut synth = Synth::with_seeds(header.gains.effective(), eq, PyRandom::new(header.seed));
    let mut report = Report {
        name: header.name.clone(),
        blocks: 0,
        sample_blocks: 0,
        int_blocks: 0,
        max_diff: 0.0,
        other_blocks: 0,
        first: None,
    };
    let mut frame: Option<Frame> = None;
    for (n, line) in lines.enumerate() {
        let row: Row = serde_json::from_str(&line.unwrap()).unwrap_or_else(|e| panic!("{}: block {n}: {e}", header.name));
        frame = match row.f {
            None => None,
            Some(FrameField::Same(_)) => frame,
            Some(FrameField::Frame(f)) => Some(*f),
        };
        let block = synth.render(frame.as_ref());
        report.blocks += 1;
        let mut problems = Vec::new();
        let samples_same = same_bits(&block.trigger, &row.t) && same_bits(&block.left_grip, &row.l)
            && same_bits(&block.right_grip, &row.r);
        if !samples_same {
            report.sample_blocks += 1;
            for (ours, theirs) in [(&block.trigger, &row.t), (&block.left_grip, &row.l), (&block.right_grip, &row.r)] {
                for (a, b) in ours.iter().zip(theirs.iter()) {
                    report.max_diff = report.max_diff.max((a - b).abs());
                }
            }
            let python = ats_haptics::apex6::Block {
                trigger: row.t,
                left_grip: row.l,
                right_grip: row.r,
                route: ats_haptics::apex6::Route::from_u8(row.route).unwrap(),
                trigger_bit: None,
                side: row.side.and_then(ats_haptics::apex6::Route::from_u8),
            };
            if build_realtime(&block, true, true) != build_realtime(&python, true, true) {
                report.int_blocks += 1;
            }
            problems.push(format!(
                "samples\n    rust   t{:?}\n    python t{:?}\n    rust   l{:?}\n    python l{:?}",
                block.trigger, row.t, block.left_grip, row.l
            ));
        }
        if block.route as u8 != row.route || block.side.map(|s| s as u8) != row.side {
            problems.push(format!("route/side rust {:?}/{:?} python {}/{:?}", block.route, block.side, row.route, row.side));
        }
        if synth.levels.events != row.ev {
            problems.push(format!("events rust {:?} python {:?}", synth.levels.events, row.ev));
        }
        let lv = &synth.levels;
        let ours = (lv.engine, lv.road_left, lv.road_right, lv.trigger, lv.trigger_mode.to_string(), lv.surface.clone(),
                    lv.max_accel);
        if ours != row.lv {
            problems.push(format!("levels rust {ours:?} python {:?}", row.lv));
        }
        if !same_bits(&synth.fresh, &row.fr) || !same_bits(&synth.duck, &row.dk) {
            problems.push(format!("fresh/duck rust {:?}/{:?} python {:?}/{:?}", synth.fresh, synth.duck, row.fr, row.dk));
        }
        if !problems.is_empty() {
            if samples_same {
                report.other_blocks += 1;
            }
            if report.first.is_none() {
                report.first = Some(format!("block {n} ({:.3} s): {}", n as f64 * FRAME_PERIOD, problems.join("\n  ")));
            }
        }
    }
    report
}

#[test]
fn synth_matches_python_sample_for_sample() {
    let files = golden_files();
    if files.is_empty() {
        eprintln!("no golden data in {}: run rust/golden/make_golden.py first", golden_dir().display());
        return;
    }
    let reports: Vec<Report> =
        std::thread::scope(|s| files.iter().map(|f| s.spawn(move || check(f))).collect::<Vec<_>>().into_iter().map(|h| h.join().unwrap()).collect());
    let mut failed = false;
    for r in &reports {
        eprintln!(
            "{:40} {:7} blocks  samples differ in {:5} (int8 {:5}, max {:.3e})  other {:5}",
            r.name, r.blocks, r.sample_blocks, r.int_blocks, r.max_diff, r.other_blocks
        );
        if let Some(first) = &r.first {
            eprintln!("  first: {first}");
        }
        failed |= !r.clean();
    }
    assert!(!failed, "the Rust synth differs from the Python one (see above)");
}

#[test]
fn scripted_drive_matches_python_frame_for_frame() {
    for path in golden_files() {
        let mut lines = BufReader::new(File::open(&path).unwrap()).lines();
        let header: Header = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
        if !header.name.starts_with("sim") {
            continue;
        }
        let mut block = 0u64;
        let clock = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
        let shared = std::sync::Arc::clone(&clock);
        let mut source = SimulatedTelemetry::with_clock(
            header.sim_fps,
            Box::new(move || shared.load(std::sync::atomic::Ordering::Relaxed) as f64 * FRAME_PERIOD),
        );
        let mut expected: Option<Frame> = None;
        for line in lines {
            let row: Row = serde_json::from_str(&line.unwrap()).unwrap();
            if let Some(FrameField::Frame(f)) = row.f {
                expected = Some(*f);
            }
            clock.store(block, std::sync::atomic::Ordering::Relaxed);
            let ours = source.read();
            assert_eq!(ours, expected, "{}: block {block}", header.name);
            block += 1;
        }
        eprintln!("{}: {block} frames match", header.name);
    }
}
