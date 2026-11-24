//! Find ATS/ETS2 installs and make sure a compatible telemetry plugin is present (port of
//! plugin_install.py).
//!
//! The haptics read the shared memory of a RenCloud scs-sdk-plugin (revision 12) build.
//! TruckDash ships such a build under its own mapping name; SimHub's plugin uses another
//! layout revision that has not been verified. When no compatible plugin is found, the
//! bundled scs-telemetry.dll (embedded in the exe) can be copied into the game's plugins
//! folder. The bundled one is this project's build of RenCloud's V.1.12.1 (MIT) against SCS
//! SDK 1.16, with fixes and the same memory layout: source and changes in plugin/, built by
//! tools/build_plugin.ps1.

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
/// What this project's build of the plugin carries (PLUGIN_BUILD_NAME in its source), followed
/// by its build number. Looked for first: it uses RenCloud's mapping name too.
const OWN_BUILD: &str = "SCS Haptics build ";
/// The Win64 build of plugin/ (tools/build_plugin.ps1 puts it in resources/scs-sdk-plugin).
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
        self.plugins.values().any(|p| COMPATIBLE.contains(&p.as_str()) || p.starts_with(OWN_BUILD))
    }

    /// The game has the plugin file this program installs, in an older build than the one it
    /// carries: RenCloud's own release, or an earlier build of this project's.
    pub fn updatable(&self) -> bool {
        let Some(installed) = self.plugins.get(PLUGIN_FILE) else { return false };
        match (build_number(installed), own_build(BUNDLED_PLUGIN).as_deref().and_then(build_number)) {
            (Some(installed), Some(bundled)) => installed < bundled,
            (None, Some(_)) => installed == "RenCloud",
            _ => false,
        }
    }

    pub fn describe(&self) -> String {
        if self.plugins.is_empty() {
            return t!("텔레메트리 플러그인 없음");
        }
        let names: std::collections::BTreeSet<&str> = self.plugins.values().map(String::as_str).collect();
        let names = names.into_iter().collect::<Vec<_>>().join(", ");
        if self.updatable() {
            t!("{names} (새 빌드 있음)", names = names)
        } else if self.compatible() {
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

/// 1 for "SCS Haptics build 1"; None for another provider's name.
fn build_number(provider: &str) -> Option<u32> {
    provider.strip_prefix(OWN_BUILD)?.parse().ok()
}

/// "SCS Haptics build 1" when the DLL is this project's build of the plugin.
fn own_build(data: &[u8]) -> Option<String> {
    let marker = OWN_BUILD.as_bytes();
    let at = data.windows(marker.len()).position(|w| w == marker)? + marker.len();
    let digits: String = data[at..].iter().take_while(|b| b.is_ascii_digit()).map(|&b| b as char).collect();
    (!digits.is_empty()).then(|| format!("{OWN_BUILD}{digits}"))
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
        if let Some(build) = own_build(&data) {
            found.insert(entry.file_name().to_string_lossy().into_owned(), build);
            continue;
        }
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
    // the plugin an earlier install moved aside, which no game holds any more
    let aside = dir.join(format!("{PLUGIN_FILE}.old"));
    let _ = std::fs::remove_file(&aside);
    if let Err(error) = std::fs::write(&target, BUNDLED_PLUGIN) {
        // A running game holds its plugin: Windows will not write into a loaded DLL, but lets
        // it be renamed. The game keeps the one it loaded and takes the new one next time.
        if !target.is_file() || std::fs::rename(&target, &aside).is_err() {
            return Err(error);
        }
        if let Err(error) = std::fs::write(&target, BUNDLED_PLUGIN) {
            let _ = std::fs::rename(&aside, &target);
            return Err(error);
        }
    }
    game.plugins = scan_plugins(&dir);
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_bundled_plugin_is_this_projects_build() {
        let dir = std::env::temp_dir().join(format!("ats_haptics_plugin_{}", std::process::id()));
        let mut game = GameInstall { key: "ATS".into(), name: "ATS".into(), path: dir.clone(), plugins: BTreeMap::new() };
        let target = install(&mut game).unwrap();
        assert!(target.ends_with(PLUGIN_FILE));
        let provider = game.plugins.get(PLUGIN_FILE).cloned().unwrap_or_default();
        assert!(provider.starts_with(OWN_BUILD) && provider.len() > OWN_BUILD.len(), "{provider}");
        assert!(game.compatible());
        // it serves RenCloud's mapping name, which is what tells RenCloud's own release
        let name: Vec<u8> = r"Local\SCSTelemetry".encode_utf16().flat_map(u16::to_le_bytes).collect();
        assert!(contains(BUNDLED_PLUGIN, &name));
        assert_eq!(own_build(&name), None);
        // what was just installed is the build this program carries: nothing newer to offer
        assert!(!game.updatable());
        assert!(!game.describe().contains('('), "{}", game.describe());
        // RenCloud's own release, or an earlier build of ours, is one this program can replace
        for older in ["RenCloud", "SCS Haptics build 0"] {
            game.plugins.insert(PLUGIN_FILE.into(), older.into());
            assert!(game.updatable() && game.compatible(), "{older}");
            assert!(game.describe().starts_with(older) && game.describe().len() > older.len(), "{}", game.describe());
        }
        // a build newer than this program's, another provider, another file: left alone
        game.plugins.insert(PLUGIN_FILE.into(), "SCS Haptics build 9999".into());
        assert!(!game.updatable());
        game.plugins.insert(PLUGIN_FILE.into(), "TruckDash".into());
        assert!(!game.updatable());
        game.plugins = BTreeMap::from([("other.dll".to_string(), "RenCloud".to_string())]);
        assert!(!game.updatable());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_plugin_the_running_game_holds_is_moved_aside() {
        use std::os::windows::fs::OpenOptionsExt;
        let dir = std::env::temp_dir().join(format!("ats_haptics_plugin_held_{}", std::process::id()));
        let mut game = GameInstall { key: "ATS".into(), name: "ATS".into(), path: dir.clone(), plugins: BTreeMap::new() };
        std::fs::create_dir_all(game.plugins_dir()).unwrap();
        let target = game.plugins_dir().join(PLUGIN_FILE);
        std::fs::write(&target, b"an older plugin").unwrap();
        // as a loaded DLL is held: others may read and rename it, not write it
        let (share_read, share_delete) = (1, 4);
        let held = std::fs::OpenOptions::new().read(true).share_mode(share_read | share_delete).open(&target).unwrap();
        assert!(std::fs::write(&target, b"x").is_err());
        assert_eq!(install(&mut game).unwrap(), target);
        assert_eq!(std::fs::read(&target).unwrap(), BUNDLED_PLUGIN);
        let aside = game.plugins_dir().join(format!("{PLUGIN_FILE}.old"));
        assert!(aside.is_file());
        assert_eq!(game.plugins.len(), 1, "the one moved aside is no plugin the game loads");
        // the game has quit: the next install clears it away
        drop(held);
        install(&mut game).unwrap();
        assert!(!aside.exists());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn library_paths_come_out_of_the_vdf() {
        let vdf = "\"libraryfolders\"\n{\n\t\"0\"\n\t{\n\t\t\"path\"\t\t\"C:\\\\Program Files (x86)\\\\Steam\"\n\t}\n\t\"1\"\n\t{\n\t\t\"path\"\t\t\"D:\\\\Games\"\n\t}\n}";
        assert_eq!(quoted_values(vdf, "path"), vec!["C:\\\\Program Files (x86)\\\\Steam", "D:\\\\Games"]);
    }
}
