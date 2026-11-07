//! The window builds and draws in every language: each screen through ATSHaptics.exe
//! --snapshot, offscreen, with %APPDATA% pointed at an empty folder so the player's config and
//! error log are left alone.

use std::path::Path;

const SCREENS: [&str; 6] = ["main", "tip", "plugins", "calibration", "session", "result"];

#[test]
fn every_screen_draws_in_every_language() {
    let exe = env!("CARGO_BIN_EXE_ATSHaptics");
    let dir = std::env::temp_dir().join(format!("ats_haptics_window_{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let mut failures = Vec::new();
    for (code, _) in ats_haptics::i18n::LANGUAGES {
        for screen in SCREENS {
            let out = dir.join(format!("{code}_{screen}.png"));
            let status = std::process::Command::new(exe)
                .args(["--snapshot"])
                .arg(&out)
                .args(["--language", code, "--screen", screen])
                .env("APPDATA", &dir)
                .status()
                .unwrap();
            let drawn = std::fs::read(&out).map(|png| png.len() > 10_000 && png.starts_with(b"\x89PNG")).unwrap_or(false);
            if !status.success() || !drawn {
                failures.push(format!("{code} {screen}: {status}"));
            }
        }
    }
    let errors = std::fs::read_to_string(Path::new(&dir).join("ATSHaptics").join("logs").join("error.log")).unwrap_or_default();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(failures.is_empty() && errors.is_empty(), "{failures:#?}\n{errors}");
}
