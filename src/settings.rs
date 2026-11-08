//! Effect gains and app options in %APPDATA%\ATSHaptics\config.json (port of settings.py).
//!
//! The same file the Python app reads and writes: gain fields at the top level plus an
//! "app" object, the motors' "eq" calibration and "presets". Each save rewrites only its
//! own part so they never clobber each other; a value of the wrong type keeps its default,
//! converted the way Python's float()/int()/str()/bool() would.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::effects::{py_float, Gains, FLOAT_FIELDS};

pub fn config_path() -> PathBuf {
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("ATSHaptics").join("config.json")
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct AppOptions {
    pub auto_start_on_game: bool,
    /// i18n code; empty follows the Windows display language
    pub language: String,
    /// write every output block to a CSV (blocklog)
    pub debug_log: bool,
    /// the preset in use: its button is lit and 저장 writes into it; "" none
    pub preset: String,
    /// send 2 kHz samples, a report every 4 ms (rate2k.rs); off: 1 kHz every 8 ms
    pub output_2k: bool,
}

fn read(path: &Path) -> Map<String, Value> {
    // missing, unreadable, not UTF-8 or not JSON: start from defaults
    let Ok(text) = std::fs::read_to_string(path) else { return Map::new() };
    match serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')) {
        Ok(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

fn write(path: &Path, data: &Map<String, Value>) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // Write beside it and swap in, so a crash mid-write never leaves a truncated file.
    let mut temp = path.as_os_str().to_owned();
    temp.push(".tmp");
    let temp = PathBuf::from(temp);
    let text = serde_json::to_string_pretty(data).map_err(std::io::Error::other)?;
    // CRLF, as the Python app's write_text leaves it on Windows
    std::fs::write(&temp, crate::blocklog::text_mode(&text))?;
    std::fs::rename(&temp, path)
}

/// Python's str() of a JSON value.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) if n.is_f64() => {
            let x = n.as_f64().unwrap();
            if x.fract() == 0.0 && x.abs() < 1e16 {
                format!("{x:.1}")
            } else {
                format!("{x:?}")
            }
        }
        other => other.to_string(),
    }
}

/// Python's int() of a JSON value; None where it would raise.
fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|x| x.is_finite()).map(|x| x.trunc() as i64)),
        Value::Bool(b) => Some(*b as i64),
        Value::String(s) => {
            let s = s.trim();
            if s.starts_with('_') || s.ends_with('_') || s.contains("__") {
                return None;
            }
            s.replace('_', "").parse::<i64>().ok()
        }
        _ => None,
    }
}

/// Python's bool() of a JSON value.
fn py_bool(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Copy the stored values that fit; a hand-edited one of the wrong type keeps its default.
pub fn set_gain_fields(gains: &mut Gains, stored: &Map<String, Value>) {
    for name in FLOAT_FIELDS {
        if let Some(value) = stored.get(name).and_then(py_float) {
            *gains.float_mut(name).unwrap() = value;
        }
    }
    if let Some(value) = stored.get("cylinders").and_then(py_int) {
        gains.cylinders = value;
    }
    if let Some(value) = stored.get("gearbox") {
        gains.gearbox = py_str(value);
    }
    if let Some(Value::Array(items)) = stored.get("disabled") {
        gains.disabled = items.iter().map(py_str).collect();
    }
    if let Some(Value::Object(routes)) = stored.get("routes") {
        // keep defaults for keys an older config lacks
        for (name, route) in routes {
            gains.routes.insert(name.clone(), py_str(route));
        }
    }
}

fn set_app_fields(options: &mut AppOptions, stored: &Map<String, Value>) {
    if let Some(v) = stored.get("auto_start_on_game") {
        options.auto_start_on_game = py_bool(v);
    }
    if let Some(v) = stored.get("language") {
        options.language = py_str(v);
    }
    if let Some(v) = stored.get("debug_log") {
        options.debug_log = py_bool(v);
    }
    if let Some(v) = stored.get("preset") {
        options.preset = py_str(v);
    }
    if let Some(v) = stored.get("output_2k") {
        options.output_2k = py_bool(v);
    }
}

fn gains_value(gains: &Gains) -> Map<String, Value> {
    match serde_json::to_value(gains) {
        Ok(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

pub struct Settings {
    pub path: PathBuf,
}

impl Default for Settings {
    fn default() -> Settings {
        Settings { path: config_path() }
    }
}

impl Settings {
    pub fn at(path: impl Into<PathBuf>) -> Settings {
        Settings { path: path.into() }
    }

    pub fn load_gains(&self) -> Gains {
        let mut gains = Gains::default();
        set_gain_fields(&mut gains, &read(&self.path));
        gains
    }

    pub fn save_gains(&self, gains: &Gains) -> std::io::Result<()> {
        let mut data = read(&self.path);
        for (key, value) in gains_value(gains) {
            data.insert(key, value);
        }
        write(&self.path, &data)
    }

    /// The player's own motor calibration, {channel: {Hz: gain}}; a motor not in it gets the default.
    pub fn load_eq(&self) -> Map<String, Value> {
        match read(&self.path).remove("eq") {
            Some(Value::Object(map)) => map,
            _ => Map::new(),
        }
    }

    pub fn save_eq(&self, curves: &Map<String, Value>) -> std::io::Result<()> {
        let mut data = read(&self.path);
        data.insert("eq".into(), Value::Object(curves.clone()));
        write(&self.path, &data)
    }

    pub fn load_app_options(&self) -> AppOptions {
        let mut options = AppOptions::default();
        if let Some(Value::Object(stored)) = read(&self.path).get("app") {
            set_app_fields(&mut options, stored);
        }
        options
    }

    pub fn save_app_options(&self, options: &AppOptions) -> std::io::Result<()> {
        let mut data = read(&self.path);
        data.insert("app".into(), serde_json::to_value(options).unwrap_or(Value::Null));
        write(&self.path, &data)
    }

    /// The player's named tunings; an entry that is not an object is left out.
    pub fn load_presets(&self) -> Vec<(String, Gains)> {
        let mut presets = Vec::new();
        if let Some(Value::Object(stored)) = read(&self.path).get("presets") {
            for (name, values) in stored {
                if let Value::Object(values) = values {
                    let mut gains = Gains::default();
                    set_gain_fields(&mut gains, values);
                    presets.push((name.clone(), gains));
                }
            }
        }
        presets
    }

    /// Store these gains under the name, replacing a preset of that name.
    pub fn save_preset(&self, name: &str, gains: &Gains) -> std::io::Result<()> {
        let mut data = read(&self.path);
        let entry = Value::Object(gains_value(gains));
        // in place, so the key keeps where it was in the file
        match data.get_mut("presets") {
            Some(Value::Object(presets)) => {
                presets.insert(name.to_string(), entry);
            }
            _ => {
                let mut presets = Map::new();
                presets.insert(name.to_string(), entry);
                data.insert("presets".into(), Value::Object(presets));
            }
        }
        write(&self.path, &data)
    }

    pub fn delete_preset(&self, name: &str) -> std::io::Result<()> {
        let mut data = read(&self.path);
        if let Some(Value::Object(presets)) = data.get_mut("presets") {
            if presets.shift_remove(name).is_some() {
                return write(&self.path, &data);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::effects::py_float_str;

    fn temp_config(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("ats_haptics_test_{}_{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.join("config.json")
    }

    #[test]
    fn a_missing_file_gives_defaults() {
        let settings = Settings::at(temp_config("missing"));
        assert_eq!(settings.load_gains(), Gains::default());
        assert_eq!(settings.load_app_options(), AppOptions::default());
        assert!(settings.load_eq().is_empty());
    }

    #[test]
    fn values_of_the_wrong_type_keep_their_defaults() {
        let path = temp_config("types");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            r#"{"road": "0.25", "engine": null, "bumps": [1], "cylinders": 8.9, "gear": true,
                "routes": {"road": "both"}, "disabled": ["engine", 3], "app": {"debug_log": 1, "language": "en"}}"#,
        )
        .unwrap();
        let settings = Settings::at(&path);
        let gains = settings.load_gains();
        assert_eq!(gains.road, 0.25);
        assert_eq!(gains.engine, Gains::default().engine);
        assert_eq!(gains.bumps, Gains::default().bumps);
        assert_eq!(gains.cylinders, 8);
        assert_eq!(gains.gear, 1.0);
        assert_eq!(gains.routes["road"], "both");
        assert_eq!(gains.routes["engine"], "grip"); // kept from the defaults
        assert_eq!(gains.disabled, vec!["engine".to_string(), "3".to_string()]);
        let app = settings.load_app_options();
        assert!(app.debug_log);
        assert_eq!(app.language, "en");
    }

    #[test]
    fn each_save_keeps_the_other_parts() {
        let settings = Settings::at(temp_config("parts"));
        let mut gains = Gains::default();
        gains.road = 0.33;
        settings.save_gains(&gains).unwrap();
        settings.save_app_options(&AppOptions { language: "ja".into(), ..AppOptions::default() }).unwrap();
        settings.save_preset("car", &gains).unwrap();
        let curves: Map<String, Value> = serde_json::from_str(r#"{"grip": {"65": 1.0}}"#).unwrap();
        settings.save_eq(&curves).unwrap();
        assert_eq!(settings.load_gains().road, 0.33);
        assert_eq!(settings.load_app_options().language, "ja");
        assert_eq!(settings.load_presets()[0].0, "car");
        assert_eq!(settings.load_eq(), curves);
        settings.delete_preset("car").unwrap();
        assert!(settings.load_presets().is_empty());
        assert_eq!(settings.load_gains().road, 0.33);
    }

    #[test]
    fn floats_parse_as_python_does() {
        assert_eq!(py_float_str(" 1_000.5 "), Some(1000.5));
        assert_eq!(py_float_str("_1"), None);
        assert_eq!(py_int(&Value::from(-2.7)), Some(-2));
    }
}
