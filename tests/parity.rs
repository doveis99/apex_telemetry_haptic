//! The Rust synth against the Python one, sample by sample.
//!
//! tools/make_golden.py records what effects.py makes of known drives (the scripted
//! drive at three frame rates and three settings, the scenarios, slices of real debug logs)
//! into tests/golden-data/*.jsonl. This feeds the same frames to the Rust synth and requires
//! every output sample to be the same float, bit for bit, along with the route, the trigger
//! side, the events and the steady-fade and ducking state. The frame sources (the scripted
//! drive, the scenarios, the debug log reader), the debug log's rows and the replay tool's
//! summary are held to the Python ones too. Without the data it says so and passes;
//! generate it with
//!
//!   .venv\Scripts\python.exe tools\make_golden.py

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use ats_haptics::apex6::{build_realtime, Block, FRAME_PERIOD};
use ats_haptics::blocklog::{json_indent1, BlockLog};
use ats_haptics::effects::{Equalizer, Gains, Synth};
use ats_haptics::pyrandom::PyRandom;
use ats_haptics::replay::{replay, settings_of, summary, LogReader};
use ats_haptics::scenarios::{scenario, ScenarioTelemetry};
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
    #[serde(default)]
    scenario: Option<String>,
    #[serde(default)]
    log_path: Option<String>,
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
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden-data")
}

fn golden_files() -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(golden_dir()) else { return Vec::new() };
    let mut files: Vec<PathBuf> =
        entries
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
            .filter(|p| p.file_stem().is_some_and(|s| s != "previews")) // the effect tests have their own test
            .collect();
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
        eprintln!("no golden data in {}: run tools/make_golden.py first", golden_dir().display());
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

/// The golden runs' frames, None for a block without one.
fn golden_frames(path: &Path) -> (Header, Vec<Option<Frame>>) {
    let mut lines = BufReader::new(File::open(path).unwrap()).lines();
    let header: Header = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    let mut frames: Vec<Option<Frame>> = Vec::new();
    for line in lines {
        let row: Row = serde_json::from_str(&line.unwrap()).unwrap();
        let frame = match row.f {
            None => None,
            Some(FrameField::Same(_)) => frames.last().cloned().flatten(),
            Some(FrameField::Frame(f)) => Some(*f),
        };
        frames.push(frame);
    }
    (header, frames)
}

fn fake_clock() -> (std::sync::Arc<std::sync::atomic::AtomicU64>, Box<dyn FnMut() -> f64 + Send>) {
    let block = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
    let shared = std::sync::Arc::clone(&block);
    (block, Box::new(move || shared.load(std::sync::atomic::Ordering::Relaxed) as f64 * FRAME_PERIOD))
}

#[test]
fn scenarios_match_python_frame_for_frame() {
    for path in golden_files() {
        let (header, expected) = golden_frames(&path);
        let Some(name) = header.scenario else { continue };
        let (block, clock) = fake_clock();
        let mut source = ScenarioTelemetry::with_clock(scenario(&name).unwrap(), Some(60.0), clock);
        for (n, want) in expected.iter().enumerate() {
            block.store(n as u64, std::sync::atomic::Ordering::Relaxed);
            assert_eq!(&source.read(), want, "{}: block {n}", header.name);
        }
        eprintln!("{}: {} frames match", header.name, expected.len());
    }
}

#[test]
fn log_reader_matches_python_frame_for_frame() {
    for path in golden_files() {
        let (header, expected) = golden_frames(&path);
        let Some(log) = header.log_path else { continue };
        if !Path::new(&log).exists() {
            eprintln!("{}: {log} is gone, skipped", header.name);
            continue;
        }
        let rows = LogReader::new(&log, None, None).rows().unwrap();
        let mut count = 0;
        for (n, ((_, _, frame), want)) in rows.zip(&expected).enumerate() {
            assert_eq!(Some(&frame), want.as_ref(), "{}: block {n}", header.name);
            count += 1;
        }
        assert_eq!(count, expected.len(), "{}", header.name);
        eprintln!("{}: {count} frames match", header.name);
    }
}

/// A CSV line without its first two fields (the wall clock and the gap, which no two runs share).
fn after_clock(line: &str) -> &str {
    line.splitn(3, ',').nth(2).unwrap_or("")
}

#[test]
fn block_log_matches_python_row_for_row() {
    let dir = std::env::temp_dir().join(format!("ats_haptics_blocklog_{}", std::process::id()));
    let mut checked = 0;
    for path in golden_files() {
        let python_csv = path.with_extension("blocks.csv");
        if !python_csv.exists() {
            continue;
        }
        let (header, frames) = golden_frames(&path);
        let info: Value = serde_json::from_str(&std::fs::read_to_string(path.with_extension("blocks.json")).unwrap()).unwrap();
        let mut synth = Synth::with_seeds(header.gains.effective(), Equalizer::new(&header.eq_curves), PyRandom::new(header.seed));
        let _ = std::fs::remove_dir_all(&dir);
        let mut log = BlockLog::new(&dir, Some(&info)).unwrap();
        for frame in &frames {
            let block: Block = synth.render(frame.as_ref());
            log.add(&block, frame.as_ref(), &synth.levels.events.clone(), Some(&synth), "").unwrap();
        }
        let ours = log.path.clone();
        log.close().unwrap();
        assert_eq!(
            std::fs::read_to_string(ours.with_extension("json")).unwrap(),
            std::fs::read_to_string(path.with_extension("blocks.json")).unwrap(),
            "{}: the .json beside the log",
            header.name
        );
        let ours = std::fs::read_to_string(&ours).unwrap();
        let theirs = std::fs::read_to_string(&python_csv).unwrap();
        let (ours, theirs): (Vec<&str>, Vec<&str>) = (ours.split_inclusive("\r\n").collect(), theirs.split_inclusive("\r\n").collect());
        assert_eq!(ours.len(), theirs.len(), "{}: rows", header.name);
        assert_eq!(ours[0], theirs[0], "{}: the header", header.name);
        for (n, (a, b)) in ours.iter().zip(&theirs).enumerate().skip(1) {
            assert_eq!(after_clock(a), after_clock(b), "{}: row {n}", header.name);
        }
        eprintln!("{}: {} log rows match", header.name, ours.len() - 1);
        checked += 1;
    }
    let _ = std::fs::remove_dir_all(&dir);
    if checked == 0 {
        eprintln!("no golden block logs: run tools/make_golden.py first");
    }
}

#[derive(Deserialize)]
struct ReplayGolden {
    log_path: String,
    end: String,
    seed: u64,
    knocks: f64,
    lines: Vec<String>,
}

#[test]
fn replay_summary_matches_python() {
    let Ok(entries) = std::fs::read_dir(golden_dir()) else { return };
    for entry in entries.filter_map(Result::ok) {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !(name.starts_with("replay_") && name.ends_with(".json")) {
            continue;
        }
        let golden: ReplayGolden = serde_json::from_str(&std::fs::read_to_string(entry.path()).unwrap()).unwrap();
        let log = PathBuf::from(&golden.log_path);
        if !log.exists() {
            eprintln!("{name}: {} is gone, skipped", log.display());
            continue;
        }
        let (gains, eq, version) = settings_of(&log);
        let mut lines = Vec::new();
        let (logged, replayed, blocks) = replay(&log, &gains, eq, None, Some(&golden.end), Some(golden.knocks),
                                                PyRandom::new(golden.seed), &mut |line| lines.push(line))
            .unwrap();
        lines.extend(summary(&log, &version, true, &logged, &replayed, blocks));
        assert_eq!(lines, golden.lines, "{name}");
        eprintln!("{name}: {} lines match ({} knocks of 0.5 or more)", lines.len(), lines.len() - 4);
    }
}

#[test]
fn json_is_written_as_python_writes_it() {
    let value: Value = serde_json::from_str(r#"{"a": [], "b": {}, "c": [1, 2.5, "ä"], "d": {"e": null}}"#).unwrap();
    assert_eq!(json_indent1(&value), "{\n \"a\": [],\n \"b\": {},\n \"c\": [\n  1,\n  2.5,\n  \"ä\"\n ],\n \"d\": {\n  \"e\": null\n }\n}");
}

#[test]
fn config_json_is_written_as_the_python_app_writes_it() {
    // tests/data/python_config.json: settings.save_gains(Gains()), then
    // settings.save_app_options(AppOptions(language="ja")), from the Python app
    let path = std::env::temp_dir().join(format!("ats_haptics_config_{}", std::process::id())).join("config.json");
    let _ = std::fs::remove_file(&path);
    let settings = ats_haptics::settings::Settings::at(&path);
    settings.save_gains(&Gains::default()).unwrap();
    settings.save_app_options(&ats_haptics::settings::AppOptions { language: "ja".into(), ..Default::default() }).unwrap();
    let ours = std::fs::read(&path).unwrap();
    let theirs = std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data/python_config.json")).unwrap();
    assert_eq!(String::from_utf8(ours).unwrap(), String::from_utf8(theirs).unwrap());
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
}

#[derive(Deserialize)]
struct PreviewGolden {
    effect: String,
    surfaces: Vec<String>,
    seed: u64,
    blocks: Vec<([f64; 8], [f64; 8], [f64; 8], u8, Option<u8>)>,
}

#[test]
fn effect_tests_match_python_sample_for_sample() {
    use ats_haptics::effects::Effect;
    use ats_haptics::preview::Preview;
    let Ok(text) = std::fs::read_to_string(golden_dir().join("previews.jsonl")) else { return };
    let eq = Equalizer::for_player(&Map::new());
    for line in text.lines() {
        let golden: PreviewGolden = serde_json::from_str(line).unwrap();
        let surfaces: Vec<&str> = golden.surfaces.iter().map(String::as_str).collect();
        let effect = Effect::from_name(&golden.effect).unwrap();
        let mut preview = Preview::with_synth(effect, &Gains::default(), &surfaces, |gains| {
            Synth::with_seeds(gains, eq.clone(), PyRandom::new(golden.seed))
        });
        let mut n = 0;
        while let Some(block) = preview.render() {
            let (t, l, r, route, side) = &golden.blocks[n];
            let label = format!("{} {:?} block {n}", golden.effect, golden.surfaces);
            assert!(same_bits(&block.trigger, t) && same_bits(&block.left_grip, l) && same_bits(&block.right_grip, r),
                    "{label}: samples differ");
            assert_eq!((block.route as u8, block.side.map(|s| s as u8)), (*route, *side), "{label}");
            n += 1;
        }
        assert_eq!(n, golden.blocks.len(), "{} {:?}", golden.effect, golden.surfaces);
    }
    eprintln!("{} effect tests match", text.lines().count());
}
