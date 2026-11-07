//! Detect whether ATS or ETS2 is running (port of gamewatch.py): a Toolhelp process
//! snapshot every POLL_S on a background thread, off the 8 ms output loop.

use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::Arc;
use std::time::Duration;

pub const GAME_PROCESSES: [(&str, &str); 2] = [("amtrucks.exe", "ATS"), ("eurotrucks2.exe", "ETS2")];
/// one snapshot costs ~10 ms, so keep it off the 8 ms audio loop
pub const POLL_S: f64 = 2.0;

/// "ATS" or "ETS2" if one of the games is running.
pub fn running_game() -> Option<&'static str> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    // SAFETY: the snapshot handle is checked and closed; the entry is sized as the API asks.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return None;
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut found = None;
        let mut ok = Process32FirstW(snapshot, &mut entry) != 0;
        while ok {
            let len = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(entry.szExeFile.len());
            let name = String::from_utf16_lossy(&entry.szExeFile[..len]).to_lowercase();
            if let Some((_, game)) = GAME_PROCESSES.iter().find(|(exe, _)| *exe == name) {
                found = Some(*game);
                break;
            }
            ok = Process32NextW(snapshot, &mut entry) != 0;
        }
        CloseHandle(snapshot);
        found
    }
}

fn code(game: Option<&str>) -> u8 {
    match game {
        Some("ATS") => 1,
        Some("ETS2") => 2,
        _ => 0,
    }
}

/// Keeps `game()` current every POLL_S seconds on a background thread.
pub struct GameWatcher {
    game: Arc<AtomicU8>,
    stop: Option<mpsc::Sender<()>>,
}

impl Default for GameWatcher {
    fn default() -> Self {
        GameWatcher::new()
    }
}

impl GameWatcher {
    pub fn new() -> GameWatcher {
        let game = Arc::new(AtomicU8::new(code(running_game())));
        let (stop, stopped) = mpsc::channel::<()>();
        let shared = Arc::clone(&game);
        std::thread::spawn(move || {
            while let Err(RecvTimeoutError::Timeout) = stopped.recv_timeout(Duration::from_secs_f64(POLL_S)) {
                shared.store(code(running_game()), Ordering::Relaxed);
            }
        });
        GameWatcher { game, stop: Some(stop) }
    }

    pub fn game(&self) -> Option<&'static str> {
        match self.game.load(Ordering::Relaxed) {
            1 => Some("ATS"),
            2 => Some("ETS2"),
            _ => None,
        }
    }

    pub fn close(&mut self) {
        self.stop = None; // the thread sees the channel close and ends
    }
}

impl Drop for GameWatcher {
    fn drop(&mut self) {
        self.close();
    }
}
