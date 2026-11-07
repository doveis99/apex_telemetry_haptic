//! Find ATS/ETS2 installs and make sure a compatible telemetry plugin is present (port of
//! plugin_install.py).
//!
//! The haptics read the shared memory of a RenCloud scs-sdk-plugin (revision 12) build.
//! TruckDash ships such a build under its own mapping name; SimHub's plugin uses another
//! layout revision that has not been verified. When no compatible plugin is found, the
//! bundled RenCloud scs-telemetry.dll (MIT, V.1.12.1, embedded in the exe) can be copied
//! into the game's plugins folder.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::t;

pub const STEAM_APPS: [(&str, u32, &str); 2] =
    [("ATS", 270880, "American Truck Simulator"), ("ETS2", 227300, "Euro Truck Simulator 2")];
pub const PLUGIN_FILE: &str = "scs-telemetry.dll";
/// shared-memory name inside the DLL (UTF-16) -> provider
const PROVIDERS: [(&str, &str); 3] =
    [("SCSTelemetry", "RenCloud"), ("TruckDashTelemetry", "TruckDash"), ("SHSCSTelemetry", "SimHub")];
const COMPATIBLE: [&str; 2] = ["RenCloud", "TruckDash"];
/// The unmodified Win64 build from RenCloud's release V.1.12.1 (resources/scs-sdk-plugin).
pub const BUNDLED_PLUGIN: &[u8] = include_bytes!("../resources/scs-sdk-plugin/scs-telemetry.dll");

#[derive(Clone, Debug)]
pub struct GameInstall {
    /// "ATS" or "ETS2"
    pub key: String,
    pub name: String,
    pub path: PathBuf,
    /// dll file name -> provider
    pub plugins: BTreeMap<String, String>,
}

impl GameInstall {
    pub fn plugins_dir(&self) -> PathBuf {
        self.path.join("bin").join("win_x64").join("plugins")
    }

    pub fn compatible(&self) -> bool {
        self.plugins.values().any(|p| COMPATIBLE.contains(&p.as_str()))
    }

    pub fn describe(&self) -> String {
        if self.plugins.is_empty() {
            return t!("텔레메트리 플러그인 없음");
        }
        let names: std::collections::BTreeSet<&str> = self.plugins.values().map(String::as_str).collect();
        let names = names.into_iter().collect::<Vec<_>>().join(", ");
        if self.compatible() {
            names
        } else {
            t!("{names} (호환 미확인)", names = names)
        }
    }
}

fn registry_string(hive: windows_sys::Win32::System::Registry::HKEY, key: &str, value: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{RegGetValueW, RRF_RT_REG_SZ};
    let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    let (key, value) = (wide(key), wide(value));
    let mut size: u32 = 0;
    // SAFETY: the first call asks for the size, the second fills a buffer of that size.
    unsafe {
        if RegGetValueW(hive, key.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), std::ptr::null_mut(),
                        &mut size) != 0 {
            return None;
        }
        let mut buf = vec![0u16; (size as usize).div_ceil(2)];
        if RegGetValueW(hive, key.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(),
                        buf.as_mut_ptr().cast(), &mut size) != 0 {
            return None;
        }
        let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        Some(String::from_utf16_lossy(&buf[..end]))
    }
}

fn steam_root() -> Option<PathBuf> {
    use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    registry_string(HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
        .or_else(|| registry_string(HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"))
        .map(PathBuf::from)
}

/// Every `"name"   "value"` pair's value in a Valve KeyValues text, in order.
fn quoted_values<'a>(text: &'a str, name: &str) -> Vec<&'a str> {
    let needle = format!("\"{name}\"");
    let mut values = Vec::new();
    let mut rest = text;
    while let Some(at) = rest.find(&needle) {
        rest = &rest[at + needle.len()..];
        let trimmed = rest.trim_start_matches([' ', '\t']);
        if let Some(value) = trimmed.strip_prefix('"') {
            if let Some(end) = value.find('"') {
                values.push(&value[..end]);
            }
        }
    }
    values
}

fn library_folders(steam: &Path) -> Vec<PathBuf> {
    let mut folders = vec![steam.to_path_buf()];
    let Ok(bytes) = std::fs::read(steam.join("steamapps").join("libraryfolders.vdf")) else { return folders };
    let text = String::from_utf8_lossy(&bytes);
    for raw in quoted_values(&text, "path") {
        let path = PathBuf::from(raw.replace("\\\\", "\\"));
        if !folders.contains(&path) {
            folders.push(path);
        }
    }
    folders
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

pub fn scan_plugins(plugins_dir: &Path) -> BTreeMap<String, String> {
    let mut found = BTreeMap::new();
    let Ok(entries) = std::fs::read_dir(plugins_dir) else { return found };
    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if !path.extension().is_some_and(|x| x.eq_ignore_ascii_case("dll")) {
            continue;
        }
        let Ok(data) = std::fs::read(&path) else { continue };
        for (mapping, provider) in PROVIDERS {
            let needle: Vec<u8> = format!("Local\\{mapping}").encode_utf16().flat_map(u16::to_le_bytes).collect();
            if contains(&data, &needle) {
                found.insert(entry.file_name().to_string_lossy().into_owned(), provider.to_string());
                break;
            }
        }
    }
    found
}

fn exe_of(key: &str) -> &'static str {
    if key == "ATS" {
        "amtrucks.exe"
    } else {
        "eurotrucks2.exe"
    }
}

/// Accept a game folder picked by hand (non-Steam or unusual layouts).
pub fn game_from_folder(folder: &Path) -> Option<GameInstall> {
    for (key, _, name) in STEAM_APPS {
        if folder.join("bin").join("win_x64").join(exe_of(key)).is_file() {
            let mut game = GameInstall { key: key.into(), name: name.into(), path: folder.into(), plugins: BTreeMap::new() };
            game.plugins = scan_plugins(&game.plugins_dir());
            return Some(game);
        }
    }
    None
}

pub fn find_games() -> Vec<GameInstall> {
    let Some(steam) = steam_root() else { return Vec::new() };
    let mut games: Vec<GameInstall> = Vec::new();
    for library in library_folders(&steam) {
        for (key, app_id, name) in STEAM_APPS {
            let manifest = library.join("steamapps").join(format!("appmanifest_{app_id}.acf"));
            let Ok(bytes) = std::fs::read(&manifest) else { continue };
            let text = String::from_utf8_lossy(&bytes);
            let dir = quoted_values(&text, "installdir").first().map(|s| s.to_string()).unwrap_or_else(|| name.into());
            let path = library.join("steamapps").join("common").join(dir);
            if path.is_dir() && games.iter().all(|g| g.key != key) {
                let mut game = GameInstall { key: key.into(), name: name.into(), path, plugins: BTreeMap::new() };
                game.plugins = scan_plugins(&game.plugins_dir());
                games.push(game);
            }
        }
    }
    games
}

/// Copy the bundled plugin into the game. A folder that needs administrator rights gives a
/// PermissionDenied error.
pub fn install(game: &mut GameInstall) -> std::io::Result<PathBuf> {
    let dir = game.plugins_dir();
    std::fs::create_dir_all(&dir)?;
    let target = dir.join(PLUGIN_FILE);
    std::fs::write(&target, BUNDLED_PLUGIN)?;
    game.plugins = scan_plugins(&dir);
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_bundled_plugin_is_a_rencloud_build() {
        let dir = std::env::temp_dir().join(format!("ats_haptics_plugin_{}", std::process::id()));
        let mut game = GameInstall { key: "ATS".into(), name: "ATS".into(), path: dir.clone(), plugins: BTreeMap::new() };
        let target = install(&mut game).unwrap();
        assert!(target.ends_with(PLUGIN_FILE));
        assert_eq!(game.plugins.get(PLUGIN_FILE).map(String::as_str), Some("RenCloud"));
        assert!(game.compatible());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn library_paths_come_out_of_the_vdf() {
        let vdf = "\"libraryfolders\"\n{\n\t\"0\"\n\t{\n\t\t\"path\"\t\t\"C:\\\\Program Files (x86)\\\\Steam\"\n\t}\n\t\"1\"\n\t{\n\t\t\"path\"\t\t\"D:\\\\Games\"\n\t}\n}";
        assert_eq!(quoted_values(vdf, "path"), vec!["C:\\\\Program Files (x86)\\\\Steam", "D:\\\\Games"]);
    }
}
