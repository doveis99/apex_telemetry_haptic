//! Run a debug log through today's synth and set what the log recorded against the replay
//! (port of replay.py's command line).
//!
//!   replay LOG.csv                       the drive through the synth with the log's settings
//!   replay LOG.csv --from 19:17:50 --to 19:18:10
//!   replay LOG.csv --settings current    with config.json's settings and motor curves
//!   replay LOG.csv --knocks 0.5          also list every bump knock of at least 0.5
//!   ats_haptics --replay LOG.csv         feel it on the pad

use std::path::PathBuf;

use ats_haptics::effects::Equalizer;
use ats_haptics::pyrandom::PyRandom;
use ats_haptics::replay::{replay, settings_of, summary};
use ats_haptics::settings::Settings;

const USAGE: &str = "usage: replay LOG [--from HH:MM:SS] [--to HH:MM:SS] [--settings log|current] [--knocks STRENGTH]";

fn fail(message: &str) -> ! {
    eprintln!("{USAGE}\n\nerror: {message}");
    std::process::exit(2);
}

fn main() {
    let mut log: Option<PathBuf> = None;
    let (mut start, mut end, mut knocks) = (None, None, None);
    let mut from_log = true;
    let mut items = std::env::args().skip(1);
    while let Some(item) = items.next() {
        let (flag, inline) = match item.split_once('=') {
            Some((flag, value)) if flag.starts_with("--") => (flag.to_string(), Some(value.to_string())),
            _ => (item.clone(), None),
        };
        let mut value = || inline.clone().or_else(|| items.next()).unwrap_or_else(|| fail(&format!("{flag} needs a value")));
        match flag.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                return;
            }
            "--from" => start = Some(value()),
            "--to" => end = Some(value()),
            "--knocks" => {
                let text = value();
                knocks = Some(text.parse::<f64>().unwrap_or_else(|_| fail(&format!("--knocks: not a number: {text}"))));
            }
            "--settings" => match value().as_str() {
                "log" => from_log = true,
                "current" => from_log = false,
                other => fail(&format!("--settings: choose log or current, not {other}")),
            },
            _ if !flag.starts_with("--") && log.is_none() => log = Some(PathBuf::from(item)),
            _ => fail(&format!("unrecognized argument: {item}")),
        }
    }
    let Some(log) = log else { fail("the log is required") };
    let (mut gains, mut eq, version) = settings_of(&log);
    if !from_log {
        let settings = Settings::default();
        gains = settings.load_gains();
        eq = Equalizer::for_player(&settings.load_eq());
    }
    let result = replay(&log, &gains, eq, start.as_deref(), end.as_deref(), knocks, PyRandom::from_entropy(),
                        &mut |line| println!("{line}"));
    match result {
        Ok((logged, replayed, blocks)) => {
            for line in summary(&log, &version, from_log, &logged, &replayed, blocks) {
                println!("{line}");
            }
        }
        Err(error) => {
            eprintln!("{}: {error}", log.display());
            std::process::exit(1);
        }
    }
}
