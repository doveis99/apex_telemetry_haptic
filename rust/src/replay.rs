//! Play a debug log back as telemetry (port of replay.py): feel a recorded drive again, or run
//! it through today's synth and see what a change does to a real drive.
//!
//! Each row was one 8 ms block, so a row is read per block and the drive keeps its own pace
//! and frame timing. Logs from before 0.16.0 hold only the columns up to deflection_mm; what
//! that leaves out (and why every wheel counts as driven) is written up in replay.py.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{Map, Value};

use crate::apex6::MAX_SAMPLE;
use crate::effects::{py_float_str, Effect, Equalizer, Gains, Synth, DT, EQ_CHANNELS, TAU};
use crate::pymath as m;
use crate::pyrandom::PyRandom;
use crate::telemetry::{Frame, TelemetrySource, Wheel};

/// m; the rim speed is what the log keeps, any radius gives it back
const WHEEL_RADIUS: f64 = 0.5;
/// how many of an old log's wheels are the truck's (a 6x4 tractor)
const OLD_TRUCK_WHEELS: usize = 6;
const EQ_LEVEL: [f64; 2] = [0.4, 1.0];

/// HH:MM:SS(.fff) as seconds of the day.
pub fn seconds_of(clock: &str) -> f64 {
    let mut parts = clock.trim().splitn(3, ':');
    let h: i64 = parts.next().and_then(|p| p.trim().parse().ok()).unwrap_or(0);
    let mn: i64 = parts.next().and_then(|p| p.trim().parse().ok()).unwrap_or(0);
    let s = parts.next().and_then(py_float_str).unwrap_or(0.0);
    (h * 3600 + mn * 60) as f64 + s
}

fn floats(text: &str) -> Vec<f64> {
    if text.is_empty() {
        return Vec::new();
    }
    text.split('/').map(|x| py_float_str(x).unwrap_or(0.0)).collect()
}

fn float_or(text: &str, default: f64) -> f64 {
    py_float_str(text).unwrap_or(default)
}

fn int_or(text: &str, default: i64) -> i64 {
    text.trim().parse().unwrap_or(default)
}

fn triple(values: Vec<f64>) -> [f64; 3] {
    if values.len() == 3 {
        [values[0], values[1], values[2]]
    } else {
        [0.0; 3]
    }
}

/// Python's csv reader (excel dialect) over a file, record by record.
pub struct CsvReader<R: BufRead> {
    reader: R,
    line: String,
}

impl<R: BufRead> CsvReader<R> {
    pub fn new(reader: R) -> Self {
        CsvReader { reader, line: String::new() }
    }

    /// The next record; an empty Vec for a blank line, None at the end.
    pub fn next_record(&mut self) -> Option<Vec<String>> {
        let mut fields = Vec::new();
        let mut field = String::new();
        let mut quoted = false; // inside a quoted field
        let mut started = false; // read anything of this record
        loop {
            self.line.clear();
            if self.reader.read_line(&mut self.line).ok()? == 0 {
                if !started {
                    return None;
                }
                fields.push(field);
                return Some(fields);
            }
            started = true;
            let mut chars = self.line.chars().peekable();
            let mut at_field_start = field.is_empty() && !quoted;
            while let Some(c) = chars.next() {
                if quoted {
                    if c == '"' {
                        if chars.peek() == Some(&'"') {
                            chars.next();
                            field.push('"');
                        } else {
                            quoted = false;
                        }
                    } else {
                        field.push(c);
                    }
                    continue;
                }
                match c {
                    '"' if at_field_start => {
                        quoted = true;
                        at_field_start = false;
                    }
                    ',' => {
                        fields.push(std::mem::take(&mut field));
                        at_field_start = true;
                    }
                    '\r' | '\n' => {
                        if fields.is_empty() && field.is_empty() {
                            return Some(Vec::new());
                        }
                        fields.push(field);
                        return Some(fields);
                    }
                    _ => {
                        field.push(c);
                        at_field_start = false;
                    }
                }
            }
            if !quoted {
                fields.push(field);
                return Some(fields);
            }
            // a quoted field runs on over the line break
        }
    }
}

/// One log row by column name, as csv.DictReader gives it.
#[derive(Clone, Debug)]
pub struct LogRow {
    columns: Arc<HashMap<String, usize>>,
    values: Vec<String>,
}

impl LogRow {
    /// None when the log has no such column.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.columns.get(name).map(|&i| self.values.get(i).map_or("", String::as_str))
    }

    /// The value, or "" when the log has no such column.
    pub fn val(&self, name: &str) -> &str {
        self.get(name).unwrap_or("")
    }

    pub fn has(&self, name: &str) -> bool {
        self.columns.contains_key(name)
    }
}

/// A block log's rows as the frames the game sent, one per 8 ms block. A row without a new
/// frame gives the frame before it again; a row without game data gives a paused frame.
#[derive(Clone, Debug)]
pub struct LogReader {
    pub path: PathBuf,
    pub start: Option<f64>,
    pub end: Option<f64>,
}

impl LogReader {
    pub fn new(path: impl Into<PathBuf>, start: Option<&str>, end: Option<&str>) -> LogReader {
        LogReader { path: path.into(), start: start.map(seconds_of), end: end.map(seconds_of) }
    }

    /// (time of day, row, frame) for each block in the chosen stretch.
    pub fn rows(&self) -> std::io::Result<LogRows> {
        let mut csv = CsvReader::new(BufReader::new(File::open(&self.path)?));
        let header = csv.next_record().unwrap_or_default();
        let columns = header.iter().enumerate().map(|(i, name)| (name.clone(), i)).collect();
        Ok(LogRows {
            csv,
            columns: Arc::new(columns),
            start: self.start,
            end: self.end,
            frame: None,
            state: LogState::default(),
            first: None,
            done: false,
        })
    }
}

pub struct LogRows {
    csv: CsvReader<BufReader<File>>,
    columns: Arc<HashMap<String, usize>>,
    start: Option<f64>,
    end: Option<f64>,
    frame: Option<Frame>,
    state: LogState,
    first: Option<f64>,
    done: bool,
}

impl Iterator for LogRows {
    type Item = (String, LogRow, Frame);

    fn next(&mut self) -> Option<Self::Item> {
        while !self.done {
            let values = self.csv.next_record()?;
            if values.is_empty() {
                continue; // DictReader skips blank lines
            }
            let row = LogRow { columns: Arc::clone(&self.columns), values };
            let mut at = seconds_of(row.val("time"));
            let first = *self.first.get_or_insert(at);
            if at < first - 43200.0 {
                at += 86400.0; // past midnight
            }
            let stale = self.frame.as_ref().is_none_or(|f| f.paused);
            if row.val("new_frame") == "1" || stale || row.val("speed_kmh").is_empty() {
                self.frame = Some(self.state.frame(&row, at));
            }
            if self.start.is_some_and(|start| at < start) {
                continue;
            }
            if self.end.is_some_and(|end| at > end) {
                self.done = true;
                return None;
            }
            return Some((row.val("time").to_string(), row, self.frame.clone().unwrap()));
        }
        None
    }
}

/// What a log holds over from one new frame to the next.
#[derive(Default)]
struct LogState {
    substances: Vec<String>,
    render_time: i64,
    deflections: Vec<f64>,
    /// the frame columns from 0.16.0 on, last written
    extra: Option<LogRow>,
}

impl LogState {
    fn frame(&mut self, row: &LogRow, at: f64) -> Frame {
        if row.val("speed_kmh").is_empty() {
            self.render_time += 1; // moves on, so the next live frame is new
            return Frame { active: true, paused: true, render_time: self.render_time, ..Frame::default() };
        }
        let full = row.has("render_us");
        if full && !row.val("render_us").is_empty() {
            self.render_time = int_or(row.val("render_us"), self.render_time);
            self.extra = Some(row.clone());
        } else if !full && row.val("new_frame") == "1" {
            self.render_time = m::round_int(at * 1e6);
        }
        let empty = LogRow { columns: Arc::new(HashMap::new()), values: Vec::new() };
        let x = if full { self.extra.as_ref().unwrap_or(&empty) } else { &empty };
        let flag = |name: &str| row.val(name) == "1";
        let speed = float_or(row.val("speed_kmh"), 0.0) / 3.6;
        let (throttle, brake) = (float_or(row.val("throttle"), 0.0), float_or(row.val("brake"), 0.0));
        let retarder = int_or(row.val("retarder"), 0);
        let or_default = |name: &str, default: f64| {
            let v = x.val(name);
            if v.is_empty() { default } else { float_or(v, default) }
        };
        let mut frame = Frame {
            active: true,
            paused: false,
            render_time: self.render_time,
            speed,
            rpm: float_or(row.val("rpm"), 0.0),
            rpm_max: or_default("rpm_max", 2100.0),
            user_throttle: throttle,
            user_brake: brake,
            game_throttle: or_default("game_throttle", throttle),
            game_brake: or_default("game_brake", brake),
            gear: int_or(row.val("gear"), 0),
            gear_engine: int_or(row.val("gear_engine"), 0),
            retarder,
            retarder_steps: if x.val("retarder_steps").is_empty() {
                retarder.max(4)
            } else {
                int_or(x.val("retarder_steps"), 4)
            },
            engine_on: flag("engine_on"),
            motor_brake: flag("engine_brake"),
            park_brake: flag("park_brake"),
            accel: triple(floats(x.val("accel"))),
            yaw_rate: or_default("yaw", 0.0),
            velocity: triple(floats(x.val("velocity"))),
            cabin_accel: triple(floats(x.val("cabin_accel"))),
            cabin_offset: triple(floats(x.val("cabin_mm")).into_iter().map(|v| v / 1000.0).collect()),
            air_pressure: or_default("air_pressure", 120.0),
            blinker_left: row.val("blinkers").contains('L'),
            blinker_right: row.val("blinkers").contains('R'),
            shifter_toggle: [row.val("shifter").contains('R'), row.val("shifter").contains('S')],
            trailer_attached: flag("trailer"),
            game_events: x.val("game_events").split_whitespace().map(String::from).collect::<BTreeSet<_>>(),
            ..Frame::default()
        };
        let logged = floats(row.val("deflection_mm"));
        if !logged.is_empty() {
            self.deflections = logged;
        }
        let deflections = &self.deflections;
        let rims = floats(x.val("wheel_speed"));
        let truck = if full {
            rims.len()
        } else if frame.trailer_attached {
            OLD_TRUCK_WHEELS
        } else {
            deflections.len()
        };
        if !x.val("substances").is_empty() {
            self.substances = x.val("substances").split('/').map(String::from).collect();
        }
        let airborne: HashSet<usize> = x.val("airborne").split('/').filter(|i| !i.is_empty()).filter_map(|i| i.trim().parse().ok()).collect();
        let mut wheels: Vec<Wheel> = deflections
            .iter()
            .enumerate()
            .map(|(i, &deflection)| {
                let side = if i % 2 == 0 { -1.0 } else { 1.0 }; // left then right, axle by axle
                let substance = if full && i < self.substances.len() {
                    self.substances[i].clone()
                } else if side < 0.0 {
                    row.val("surface_l").to_string()
                } else {
                    row.val("surface_r").to_string()
                };
                let rim = rims.get(i).copied().unwrap_or(speed);
                Wheel {
                    deflection: deflection / 1000.0,
                    on_ground: !airborne.contains(&i),
                    substance,
                    pos_x: side,
                    velocity: rim / (TAU * WHEEL_RADIUS),
                    radius: WHEEL_RADIUS,
                    ..Wheel::default()
                }
            })
            .collect();
        let truck = truck.min(wheels.len());
        frame.trailer_wheels = wheels.split_off(truck);
        frame.wheels = wheels;
        frame
    }
}

/// A telemetry source that plays a block log, a row per read, in a loop with a pause between rounds.
pub struct LogTelemetry {
    reader: LogReader,
    name: String,
    rows: Option<LogRows>,
    pause: usize,
    render_time: i64,
}

pub fn pause_blocks() -> usize {
    m::round_int(1.0 / DT) as usize
}

impl LogTelemetry {
    pub fn new(path: impl Into<PathBuf>, start: Option<&str>, end: Option<&str>) -> LogTelemetry {
        let reader = LogReader::new(path, start, end);
        let name = reader.path.file_name().unwrap_or_default().to_string_lossy().into_owned();
        LogTelemetry { reader, name, rows: None, pause: 0, render_time: 0 }
    }
}

impl TelemetrySource for LogTelemetry {
    fn read(&mut self) -> Option<Frame> {
        for _ in 0..2 {
            if self.pause > 0 {
                self.pause -= 1;
                self.render_time += 1;
                return Some(Frame { active: true, paused: true, render_time: self.render_time, ..Frame::default() });
            }
            if self.rows.is_none() {
                self.rows = self.reader.rows().ok();
            }
            match self.rows.as_mut().and_then(Iterator::next) {
                Some((_, _, frame)) => {
                    self.render_time = frame.render_time;
                    return Some(frame);
                }
                None => {
                    self.rows = None;
                    self.pause = pause_blocks();
                }
            }
        }
        None // a log without a single row in the stretch: nothing to play
    }

    fn name(&self) -> String {
        self.name.clone()
    }
}

/// The %.6g of Python's format, for frequencies.
fn format_6g(x: f64) -> String {
    if x == 0.0 {
        return "0".into();
    }
    // the exponent after rounding to 6 significant digits decides the notation
    let scientific = format!("{:.5e}", x);
    let (mantissa, exp) = scientific.split_once('e').unwrap();
    let exponent: i32 = exp.parse().unwrap();
    if !(-4..6).contains(&exponent) {
        let mantissa = mantissa.trim_end_matches('0').trim_end_matches('.');
        format!("{mantissa}e{}{:02}", if exponent < 0 { '-' } else { '+' }, exponent.abs())
    } else {
        let s = format!("{:.*}", (5 - exponent) as usize, x);
        if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_string() } else { s }
    }
}

/// The gains and motor curves a log was recorded with, and its version; defaults where it has none.
pub fn settings_of(log: &Path) -> (Gains, Equalizer, String) {
    let info: Map<String, Value> = std::fs::read_to_string(log.with_extension("json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    let mut gains = Gains::default();
    if let Some(Value::Object(stored)) = info.get("gains") {
        crate::settings::set_gain_fields(&mut gains, stored);
    }
    let mut curves = match info.get("eq_curves") {
        Some(Value::Object(curves)) => Some(curves.clone()),
        _ => None,
    };
    if curves.is_none() {
        if let Some(Value::Object(old)) = info.get("eq") {
            // older logs kept the curves as (ln Hz, ln gain + ln LEVEL) points
            let mut converted = Map::new();
            for (channel, points) in old {
                let Some(c) = EQ_CHANNELS.iter().position(|name| name == channel) else { continue };
                let mut curve = Map::new();
                for point in points.as_array().into_iter().flatten() {
                    let (Some(x), Some(y)) = (point.get(0).and_then(Value::as_f64), point.get(1).and_then(Value::as_f64))
                    else {
                        continue;
                    };
                    curve.insert(format_6g(m::exp(x)), Value::from(m::exp(y) / EQ_LEVEL[c]));
                }
                converted.insert(channel.clone(), Value::Object(curve));
            }
            curves = Some(converted);
        }
    }
    let version = info.get("version").and_then(Value::as_str).unwrap_or("?").to_string();
    (gains, Equalizer::new(&curves.unwrap_or_default()), version)
}

/// What one side of the comparison did: knocks, fade resets, how loud each channel ran.
#[derive(Debug, Default)]
pub struct Tally {
    pub knocks: usize,
    pub pulses: usize,
    pub strong: usize,
    pub fade_resets: usize,
    /// left, right, trigger
    pub peaks: [Vec<i64>; 3],
    knocking: bool,
    road: Option<f64>,
}

impl Tally {
    pub fn block(&mut self, peaks: [i64; 3], pulses: &[f64], road_fresh: f64, bumps_sounding: bool) {
        for (out, peak) in self.peaks.iter_mut().zip(peaks) {
            out.push(peak);
        }
        self.pulses += pulses.len();
        self.strong += pulses.iter().filter(|p| **p >= 0.5).count();
        if bumps_sounding && !self.knocking {
            self.knocks += 1;
        }
        self.knocking = bumps_sounding;
        if road_fresh > 0.95 && self.road.unwrap_or(1.0) < 0.5 {
            self.fade_resets += 1;
        }
        self.road = Some(road_fresh);
    }

    pub fn line(&self, name: &str) -> String {
        fn pct(values: &[i64], q: f64) -> i64 {
            let mut ordered = values.to_vec();
            ordered.sort_unstable();
            if ordered.is_empty() {
                return 0;
            }
            ordered[(ordered.len() - 1).min((q * ordered.len() as f64) as usize)]
        }
        let grips: Vec<i64> = self.peaks[0].iter().chain(&self.peaks[1]).copied().collect();
        let over = grips.iter().chain(&self.peaks[2]).filter(|p| **p as f64 > 0.9 * MAX_SAMPLE).count();
        let limited = over as f64 / (grips.len() + self.peaks[2].len()).max(1) as f64;
        let pulses = if name != "log" { format!("{:6} {:6}", self.pulses, self.strong) } else { "     -      -".into() };
        format!(
            "{name:6} {:6} {pulses} {:7}   {:4} {:4}   {:4} {:4}   {:5.2}%",
            self.knocks,
            self.fade_resets,
            pct(&grips, 0.5),
            pct(&grips, 0.99),
            pct(&self.peaks[2], 0.5),
            pct(&self.peaks[2], 0.99),
            100.0 * limited
        )
    }
}

/// Run the log through a fresh synth; (the log's tally, the replay's, blocks). With `knocks`,
/// every bump knock at least that strong is reported to `out`.
pub fn replay(path: &Path, gains: &Gains, eq: Equalizer, start: Option<&str>, end: Option<&str>, knocks: Option<f64>,
              seeds: PyRandom, out: &mut dyn FnMut(String)) -> std::io::Result<(Tally, Tally, usize)> {
    let mut synth = Synth::with_seeds(gains.effective(), eq, seeds);
    let (mut logged, mut replayed) = (Tally::default(), Tally::default());
    let mut seen: HashSet<u64> = HashSet::new();
    let mut blocks = 0;
    for (clock, row, frame) in LogReader::new(path, start, end).rows()? {
        blocks += 1;
        let block = synth.render(Some(&frame));
        let fresh: Vec<&str> = row.val("fresh").split('/').collect();
        let int = |name: &str| int_or(row.val(name), 0);
        logged.block(
            [int("left_grip"), int("right_grip"), int("trigger")],
            &[],
            if fresh.len() == 3 { float_or(fresh[1], 0.0) } else { 1.0 },
            row.val("sounding").split_whitespace().any(|s| s == "bumps"),
        );
        let mut pulses = Vec::new();
        for entry in &synth.bursts {
            // pulses, not the cab's rattle
            if entry.effect == Effect::Bumps && entry.burst.decay_s > 0.01 && seen.insert(entry.id()) {
                pulses.push(entry.burst.amplitude);
                if knocks.is_some_and(|k| entry.burst.amplitude >= k) {
                    out(format!(
                        "  {clock}  knock {:.2} at {:.0} Hz, {:.0} km/h",
                        entry.burst.amplitude,
                        entry.burst.freq,
                        float_or(row.val("speed_kmh"), 0.0)
                    ));
                }
            }
        }
        let live: HashSet<u64> = synth.bursts.iter().map(|e| e.id()).collect();
        seen.retain(|id| live.contains(id));
        let sounding = synth.bursts.iter().any(|e| e.effect == Effect::Bumps);
        let peak = |channel: &[f64]| m::round_int(channel.iter().fold(0.0f64, |p, x| m::max(p, x.abs())));
        replayed.block(
            [peak(&block.left_grip), peak(&block.right_grip), peak(&block.trigger)],
            &pulses,
            synth.fresh[1],
            sounding,
        );
    }
    Ok((logged, replayed, blocks))
}

/// The summary replay.py prints: the header line, the column titles, the log and the replay.
pub fn summary(log: &Path, version: &str, settings_from_log: bool, logged: &Tally, replayed: &Tally,
               blocks: usize) -> Vec<String> {
    let total = m::round_int(blocks as f64 * DT);
    let name = log.file_name().unwrap_or_default().to_string_lossy();
    vec![
        format!(
            "{name}: {blocks} blocks, {}:{:02}, recorded with {version}; settings from {}",
            total / 60,
            total % 60,
            if settings_from_log { "the log" } else { "config.json" }
        ),
        "       knocks pulses strong fade-up   grip p50/p99  trig p50/p99  limited".into(),
        logged.line("log"),
        replayed.line("replay"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn csv_reads_quotes_and_blank_lines() {
        let text = "a,b,c\r\n1,\"x, \"\"y\"\"\",\r\n\r\n\"multi\nline\",2,3\r\n";
        let mut csv = CsvReader::new(text.as_bytes());
        assert_eq!(csv.next_record().unwrap(), vec!["a", "b", "c"]);
        assert_eq!(csv.next_record().unwrap(), vec!["1", "x, \"y\"", ""]);
        assert_eq!(csv.next_record().unwrap(), Vec::<String>::new());
        assert_eq!(csv.next_record().unwrap(), vec!["multi\nline", "2", "3"]);
        assert_eq!(csv.next_record(), None);
    }

    #[test]
    fn six_significant_digits_as_python() {
        assert_eq!(format_6g(65.0), "65");
        assert_eq!(format_6g(64.99999999999997), "65");
        assert_eq!(format_6g(1234567.0), "1.23457e+06");
        assert_eq!(format_6g(0.000123456789), "0.000123457");
        assert_eq!(format_6g(31.5), "31.5");
    }

    #[test]
    fn clock_columns_as_seconds() {
        assert_eq!(seconds_of("19:17:50.125"), 19.0 * 3600.0 + 17.0 * 60.0 + 50.125);
    }

    /// The city drive's first 14 s through the debug log, a game frame about every other block.
    #[test]
    fn a_written_log_plays_back_by_stretch_and_in_a_loop() {
        use crate::blocklog::BlockLog;
        use crate::scenarios::{city, Drive};
        let dir = std::env::temp_dir().join(format!("ats_haptics_replay_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let drive = Drive::new(city());
        let mut synth = Synth::new(Gains::default(), Equalizer::default());
        let info = serde_json::json!({"version": "test", "gains": {"road": 0.25}});
        let mut log = BlockLog::new(&dir, Some(&info)).unwrap();
        let blocks = m::round_int(14.0 / DT) as usize;
        for n in 0..blocks {
            let mut frame = drive.frame(n as f64 * DT);
            frame.render_time = n as i64 * 8000 / 16667 * 16667;
            let block = synth.render(Some(&frame));
            log.add(&block, Some(&frame), &synth.levels.events.clone(), Some(&synth), "").unwrap();
        }
        let path = log.path.clone();
        log.close().unwrap();

        let rows: Vec<_> = LogReader::new(&path, None, None).rows().unwrap().collect();
        assert_eq!(rows.len(), blocks);
        let sent = drive.frame(400.0 * DT);
        let got = &rows[400].2;
        assert_eq!((got.gear, got.engine_on, got.trailer_attached), (sent.gear, sent.engine_on, sent.trailer_attached));
        assert_eq!(got.wheels.len() + got.trailer_wheels.len(), 10);

        let (start, end) = (rows[300].0.clone(), rows[500].0.clone());
        let stretch: Vec<_> = LogReader::new(&path, Some(&start), Some(&end)).rows().unwrap().collect();
        assert_eq!(stretch[0].0, start);
        assert_eq!(stretch[stretch.len() - 1].0, end);

        let (gains, _, version) = settings_of(&path);
        assert_eq!((gains.road, gains.gear, version.as_str()), (0.25, Gains::default().gear, "test"));

        let mut source = LogTelemetry::new(&path, None, None);
        let frames: Vec<Frame> = (0..blocks + pause_blocks() + 2).map(|_| source.read().unwrap()).collect();
        let pause = &frames[blocks..frames.len() - 2];
        assert!(pause.iter().all(|f| f.paused));
        let times: HashSet<i64> = pause.iter().map(|f| f.render_time).collect();
        assert_eq!(times.len(), pause.len()); // live again after it
        assert!(frames[frames.len() - 1].live());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
