//! Debug log of what went to the controller, one CSV row per 8 ms block (port of blocklog.py).
//!
//! Off by default (app option debug_log, or --log-blocks). Each session writes
//! %APPDATA%\ATSHaptics\logs\blocks-<date>-<time>.csv and beside it a .json with the version,
//! the telemetry source, the settings and the motor curves it started with; only the newest
//! KEEP sessions are kept. The columns are blocklog.py's, written the same way to the
//! character (tests/parity.rs), so replay.py and the analysis tools read either app's logs;
//! what each column means is written up there.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::Value;

use crate::apex6::Block;
use crate::effects::{Synth, TAU};
use crate::pymath as m;
use crate::telemetry::Frame;

pub const KEEP: usize = 10;
const FLUSH_S: f64 = 1.0;
pub const COLUMNS: [&str; 47] = [
    "time", "gap_ms", "new_frame", "route", "trigger", "left_grip", "right_grip", "speed_kmh", "rpm", "gear",
    "gear_engine", "throttle", "brake", "retarder", "engine_brake", "park_brake", "blinkers", "shifter",
    "engine_on", "trailer", "surface_l", "surface_r", "deflection_mm", "render_us", "game_throttle", "game_brake",
    "rpm_max", "retarder_steps", "air_pressure", "accel", "wheel_speed", "yaw", "velocity", "cabin_accel",
    "cabin_mm", "airborne", "substances", "game_events", "engine", "road_l", "road_r", "trigger_level",
    "trigger_mode", "fresh", "duck", "sounding", "events",
];
/// speed_kmh to game_events
const GAME_COLUMNS: usize = 31;
/// deflection_mm to game_events, written on a new frame only
const FRAME_COLUMNS: usize = 16;

pub fn log_dir() -> PathBuf {
    crate::settings::config_path().parent().map(Path::to_path_buf).unwrap_or_default().join("logs")
}

/// A CSV field as Python's csv module writes it (excel dialect, minimal quoting).
fn csv_field(field: &str, out: &mut String) {
    if field.contains([',', '"', '\r', '\n']) {
        out.push('"');
        out.push_str(&field.replace('"', "\"\""));
        out.push('"');
    } else {
        out.push_str(field);
    }
}

pub fn csv_row(fields: &[String]) -> String {
    let mut line = String::new();
    for (i, field) in fields.iter().enumerate() {
        if i > 0 {
            line.push(',');
        }
        csv_field(field, &mut line);
    }
    line.push_str("\r\n");
    line
}

fn peak(samples: &[f64]) -> String {
    m::round_int(samples.iter().fold(0.0f64, |p, x| m::max(p, x.abs()))).to_string()
}

fn flag(on: bool) -> String {
    if on { "1" } else { "" }.to_string()
}

/// The substance under the front axle's wheel on that side (-1 left, 1 right).
fn side_of(frame: &Frame, side: i32) -> String {
    frame.wheels.iter().find(|w| (w.pos_x < 0.0) == (side < 0)).map(|w| w.substance.clone()).unwrap_or_default()
}

fn joined<T>(items: impl IntoIterator<Item = T>, format: impl Fn(T) -> String) -> String {
    items.into_iter().map(format).collect::<Vec<_>>().join("/")
}

/// Text as Python's Path.write_text puts it in a file on Windows: every line ending CRLF.
/// JSON escapes the newlines inside its strings, so only the layout's own become CRLF.
pub fn text_mode(text: &str) -> String {
    text.replace('\n', "\r\n")
}

/// Write JSON as Python's json.dumps(indent=1, ensure_ascii=False).
pub fn json_indent1(value: &Value) -> String {
    let mut out = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
    let mut serializer = serde_json::Serializer::with_formatter(&mut out, formatter);
    serde::Serialize::serialize(value, &mut serializer).expect("JSON values always serialise");
    String::from_utf8(out).expect("serde_json writes UTF-8")
}

pub struct BlockLog {
    pub path: PathBuf,
    file: BufWriter<File>,
    last: Option<Instant>,
    last_render_time: Option<i64>,
    substances: String,
    flushed: Instant,
}

impl BlockLog {
    pub fn new(directory: &Path, info: Option<&Value>) -> std::io::Result<BlockLog> {
        std::fs::create_dir_all(directory)?;
        let mut old: Vec<PathBuf> = std::fs::read_dir(directory)?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| {
                let name = p.file_name().unwrap_or_default().to_string_lossy().to_lowercase();
                name.starts_with("blocks-") && name.ends_with(".csv")
            })
            .collect();
        old.sort();
        let drop = old.len().saturating_sub(KEEP - 1);
        for path in &old[..drop] {
            let _ = std::fs::remove_file(path);
            let _ = std::fs::remove_file(path.with_extension("json"));
        }
        let path = directory.join(format!("blocks-{}.csv", crate::clock::now().stamp()));
        if let Some(info) = info {
            std::fs::write(path.with_extension("json"), text_mode(&json_indent1(info)))?;
        }
        let mut file = BufWriter::new(File::create(&path)?);
        file.write_all(csv_row(&COLUMNS.map(String::from)).as_bytes())?;
        Ok(BlockLog { path, file, last: None, last_render_time: None, substances: String::new(), flushed: Instant::now() })
    }

    /// deflection_mm to game_events, written on a new frame only: in between the frame is the same.
    fn frame_columns(&mut self, frame: &Frame) -> Vec<String> {
        let wheels: Vec<_> = frame.wheels.iter().chain(&frame.trailer_wheels).collect();
        let substances = wheels.iter().map(|w| w.substance.as_str()).collect::<Vec<_>>().join("/");
        let changed = substances != self.substances;
        self.substances = substances.clone();
        vec![
            joined(&wheels, |w| format!("{:.1}", w.deflection * 1000.0)),
            frame.render_time.to_string(),
            format!("{:.2}", frame.game_throttle),
            format!("{:.2}", frame.game_brake),
            format!("{:.0}", frame.rpm_max),
            frame.retarder_steps.to_string(),
            format!("{:.0}", frame.air_pressure),
            joined(frame.accel, |a| format!("{a:.2}")),
            joined(&frame.wheels, |w| format!("{:.2}", w.velocity.abs() * TAU * w.radius)),
            format!("{:.3}", frame.yaw_rate),
            joined(frame.velocity, |v| format!("{v:.2}")),
            joined(frame.cabin_accel, |a| format!("{a:.4}")),
            joined(frame.cabin_offset, |x| format!("{:.2}", x * 1000.0)),
            joined(wheels.iter().enumerate().filter(|(_, w)| !w.on_ground), |(i, _)| i.to_string()),
            if changed { substances } else { String::new() },
            frame.game_events.iter().cloned().collect::<Vec<_>>().join(" "),
        ]
    }

    pub fn add(&mut self, block: &Block, frame: Option<&Frame>, events: &[String], synth: Option<&Synth>,
               preview: &str) -> std::io::Result<()> {
        let now = Instant::now();
        let gap = self.last.map_or(0.0, |last| (now - last).as_secs_f64() * 1000.0);
        self.last = Some(now);
        let mut new_frame = String::new();
        let mut row = vec![String::new(); 7]; // time .. right_grip, filled below
        if let Some(frame) = frame {
            new_frame = flag(Some(frame.render_time) != self.last_render_time);
            self.last_render_time = Some(frame.render_time);
            row.extend([
                format!("{:.1}", frame.speed * 3.6),
                format!("{:.0}", frame.rpm),
                frame.gear.to_string(),
                frame.gear_engine.to_string(),
                format!("{:.2}", frame.user_throttle),
                format!("{:.2}", frame.user_brake),
                if frame.retarder != 0 { frame.retarder.to_string() } else { String::new() },
                flag(frame.motor_brake),
                flag(frame.park_brake),
                format!("{}{}", if frame.blinker_left { "L" } else { "" }, if frame.blinker_right { "R" } else { "" }),
                format!("{}{}", if frame.shifter_toggle[0] { "R" } else { "" }, if frame.shifter_toggle[1] { "S" } else { "" }),
                flag(frame.engine_on),
                flag(frame.trailer_attached),
                side_of(frame, -1),
                side_of(frame, 1),
            ]);
            if new_frame == "1" {
                let columns = self.frame_columns(frame);
                row.extend(columns);
            } else {
                row.extend(std::iter::repeat_n(String::new(), FRAME_COLUMNS));
            }
        } else {
            row.extend(std::iter::repeat_n(String::new(), GAME_COLUMNS));
        }
        if let Some(synth) = synth {
            let lv = &synth.levels;
            let sounding: std::collections::BTreeSet<&str> =
                synth.bursts.iter().filter(|e| !e.burst.done()).map(|e| e.effect.name()).collect();
            let mut names: Vec<String> = Vec::new();
            if !preview.is_empty() {
                names.push(format!("▶{preview}"));
            }
            names.extend(sounding.into_iter().map(String::from));
            row.extend([
                format!("{:.2}", lv.engine),
                format!("{:.2}", lv.road_left),
                format!("{:.2}", lv.road_right),
                format!("{:.2}", lv.trigger),
                lv.trigger_mode.to_string(),
                joined(synth.fresh, |f| format!("{f:.2}")),
                joined(synth.duck, |d| format!("{d:.2}")),
                names.join(" "),
            ]);
        } else {
            row.extend(std::iter::repeat_n(String::new(), 8));
        }
        row.push(events.join("; "));
        let side = block.side.unwrap_or(block.route);
        row[0] = crate::clock::now().hms_millis();
        row[1] = format!("{gap:.1}");
        row[2] = new_frame;
        row[3] = side.name().to_string();
        row[4] = peak(&block.trigger);
        row[5] = peak(&block.left_grip);
        row[6] = peak(&block.right_grip);
        self.file.write_all(csv_row(&row).as_bytes())?;
        if self.flushed.elapsed().as_secs_f64() > FLUSH_S {
            self.file.flush()?;
            self.flushed = Instant::now();
        }
        Ok(())
    }

    pub fn close(mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}
