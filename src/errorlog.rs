//! Unexpected errors, appended to %APPDATA%\ATSHaptics\logs\error.log (port of errorlog.py).
//!
//! The window has no console: without this a panic vanishes and the status line only shows
//! its message. Past MAX_BYTES the file starts over, keeping the previous one as error.log.1.

use std::io::Write;

const MAX_BYTES: u64 = 1_000_000;

pub fn error_log() -> std::path::PathBuf {
    crate::blocklog::log_dir().join("error.log")
}

/// Append an error with where it happened. Never fails: it runs inside error handling.
pub fn log_error(message: &str, place: &str) {
    let path = error_log();
    let _ = (|| -> std::io::Result<()> {
        std::fs::create_dir_all(path.parent().unwrap())?;
        if std::fs::metadata(&path).is_ok_and(|m| m.len() > MAX_BYTES) {
            std::fs::rename(&path, path.with_extension("log.1"))?;
        }
        let now = crate::clock::now();
        let stamp = format!("{:04}-{:02}-{:02} {}", now.year, now.month, now.day, now.hms());
        let mut out = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
        writeln!(out, "--- {stamp} ATSHaptics {} · {place}", crate::runner::version())?;
        writeln!(out, "{message}")
    })();
}

/// Log every panic with its backtrace, whichever thread it happens on.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let thread = std::thread::current().name().unwrap_or("worker").to_string();
        let backtrace = std::backtrace::Backtrace::force_capture();
        log_error(&format!("{info}\n{backtrace}"), &thread);
        previous(info);
    }));
}

/// A panic's message, for the status line.
pub fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<&str>()
        .map(|s| s.to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "panic".into())
}
