//! Check GitHub for a newer release and install it over the running copy (port of updater.py).
//!
//! Only an installed release updates itself: the release zip is downloaded with Windows'
//! own HTTP stack (WinHTTP), checked against the SHA-256 digest GitHub publishes for the
//! asset and unpacked to %TEMP%. A small PowerShell script then waits until every
//! ATSHaptics.exe from the install folder has exited, backs the install up, copies the new
//! files over it and starts the new version; a failed copy restores the backup. A build run
//! from anywhere else, or from a folder that is not writable, only points at the release page.

use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::t;

pub const REPO: &str = "doveis99/apex_telemetry_haptic";
pub const PAGE_URL: &str = "https://github.com/doveis99/apex_telemetry_haptic";
const API_URL: &str = "https://api.github.com/repos/doveis99/apex_telemetry_haptic/releases/latest";
pub const EXE_NAME: &str = "ATSHaptics.exe";
const TIMEOUT_MS: i32 = 10_000;

/// The same script as updater.py's, so an update works across both apps' layouts.
const UPDATE_SCRIPT: &str = r#"
param([string]$Source, [string]$Target, [string]$Log)
$exe = Join-Path $Target "ATSHaptics.exe"
"waiting for $exe to exit" | Out-File $Log
for ($i = 0; $i -lt 240; $i++) {
    $running = Get-Process ATSHaptics -ErrorAction SilentlyContinue | Where-Object { $_.Path -ieq $exe }
    if (-not $running) { break }
    Start-Sleep -Milliseconds 500
}
if ($running) {
    # Its files are locked: copying now would leave half of each version.
    "still running after 120 s; update not applied" | Out-File $Log -Append
    exit 1
}
function Copy-Install([string]$From, [string]$To) {
    # _internal is mirrored so files dropped by the new version disappear; the
    # top level is only overwritten, never purged. Returns the worse robocopy
    # exit code (8 and above is a failure).
    robocopy (Join-Path $From "_internal") (Join-Path $To "_internal") /MIR /R:10 /W:1 /NP /NFL /NDL | Out-File $Log -Append
    $internal = $LASTEXITCODE
    robocopy $From $To /E /XD _internal /R:10 /W:1 /NP /NFL /NDL | Out-File $Log -Append
    "robocopy exit codes: $internal, $LASTEXITCODE (below 8 is success)" | Out-File $Log -Append
    return [Math]::Max($internal, $LASTEXITCODE)
}
# Keep the installed version until the new one is fully in place, so a failed
# copy can put it back instead of leaving a mix that will not start.
$backup = Join-Path (Split-Path $Source) "previous"
"backing up $Target to $backup" | Out-File $Log -Append
if ((Copy-Install $Target $backup) -ge 8) {
    "backup failed; update not applied" | Out-File $Log -Append
    Start-Process $exe
    exit 1
}
"installing $Source" | Out-File $Log -Append
if ((Copy-Install $Source $Target) -ge 8) {
    "install failed; restoring the previous version" | Out-File $Log -Append
    if ((Copy-Install $backup $Target) -ge 8) { "restore failed too: copy $backup back by hand" | Out-File $Log -Append }
}
Start-Process $exe
"#;

#[derive(Clone, Debug)]
pub struct Release {
    pub tag: String,
    pub page: String,
    pub notes: String,
    pub asset_url: Option<String>,
    pub asset_size: u64,
    pub sha256: Option<String>,
}

impl Release {
    pub fn version(&self) -> String {
        self.tag.trim_start_matches(['v', 'V']).to_string()
    }
}

pub fn parse_version(text: &str) -> Vec<u64> {
    text.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect()
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

struct Handle(*mut std::ffi::c_void);

impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: a WinHTTP handle opened below, closed once.
            unsafe {
                windows_sys::Win32::Networking::WinHttp::WinHttpCloseHandle(self.0);
            }
        }
    }
}

/// GET a URL over HTTPS, redirects followed, the body handed over in chunks. Err for a
/// network failure or a status other than 200.
pub fn http_get(url: &str, mut on_chunk: impl FnMut(&[u8]) -> Result<(), String>) -> Result<(), String> {
    use windows_sys::Win32::Networking::WinHttp::*;
    let failed = |what: &str| format!("{what}: {}", std::io::Error::last_os_error());
    let url_w = wide(url);
    // SAFETY: every buffer outlives the call it is given to; handles close on drop.
    unsafe {
        let mut parts: URL_COMPONENTS = std::mem::zeroed();
        parts.dwStructSize = std::mem::size_of::<URL_COMPONENTS>() as u32;
        parts.dwHostNameLength = u32::MAX;
        parts.dwUrlPathLength = u32::MAX;
        parts.dwExtraInfoLength = u32::MAX;
        if WinHttpCrackUrl(url_w.as_ptr(), 0, 0, &mut parts) == 0 {
            return Err(failed("URL"));
        }
        let host: Vec<u16> = std::slice::from_raw_parts(parts.lpszHostName, parts.dwHostNameLength as usize)
            .iter()
            .copied()
            .chain(Some(0))
            .collect();
        let path: Vec<u16> = std::slice::from_raw_parts(parts.lpszUrlPath,
                                                        (parts.dwUrlPathLength + parts.dwExtraInfoLength) as usize)
            .iter()
            .copied()
            .chain(Some(0))
            .collect();
        let agent = wide(&format!("ATSHaptics/{}", env!("CARGO_PKG_VERSION")));
        let session = Handle(WinHttpOpen(agent.as_ptr(), WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, std::ptr::null(),
                                         std::ptr::null(), 0));
        if session.0.is_null() {
            return Err(failed("WinHttpOpen"));
        }
        WinHttpSetTimeouts(session.0, TIMEOUT_MS, TIMEOUT_MS, TIMEOUT_MS, TIMEOUT_MS);
        let connect = Handle(WinHttpConnect(session.0, host.as_ptr(), parts.nPort, 0));
        if connect.0.is_null() {
            return Err(failed("connect"));
        }
        let verb = wide("GET");
        let secure = if parts.nScheme == WINHTTP_INTERNET_SCHEME_HTTPS { WINHTTP_FLAG_SECURE } else { 0 };
        let request = Handle(WinHttpOpenRequest(connect.0, verb.as_ptr(), path.as_ptr(), std::ptr::null(),
                                                std::ptr::null(), std::ptr::null(), secure));
        if request.0.is_null() {
            return Err(failed("request"));
        }
        let headers = wide("Accept: application/vnd.github+json\r\n");
        WinHttpAddRequestHeaders(request.0, headers.as_ptr(), u32::MAX, WINHTTP_ADDREQ_FLAG_ADD);
        if WinHttpSendRequest(request.0, std::ptr::null(), 0, std::ptr::null(), 0, 0, 0) == 0
            || WinHttpReceiveResponse(request.0, std::ptr::null_mut()) == 0
        {
            return Err(failed("request"));
        }
        let mut status: u32 = 0;
        let mut size = 4u32;
        WinHttpQueryHeaders(request.0, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, std::ptr::null(),
                            (&mut status as *mut u32).cast(), &mut size, std::ptr::null_mut());
        if status != 200 {
            return Err(format!("HTTP {status}"));
        }
        let mut buffer = vec![0u8; 256 * 1024];
        loop {
            let mut read = 0u32;
            if WinHttpReadData(request.0, buffer.as_mut_ptr().cast(), buffer.len() as u32, &mut read) == 0 {
                return Err(failed("read"));
            }
            if read == 0 {
                return Ok(());
            }
            on_chunk(&buffer[..read as usize])?;
        }
    }
}

pub fn latest_release() -> Result<Release, String> {
    let mut body = Vec::new();
    http_get(API_URL, |chunk| {
        body.extend_from_slice(chunk);
        Ok(())
    })?;
    let data: Value = serde_json::from_slice(&body).map_err(|e| e.to_string())?;
    let text = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let asset = data["assets"].as_array().and_then(|assets| {
        assets.iter().find(|a| {
            let name = a["name"].as_str().unwrap_or_default();
            name.starts_with("ATSHaptics-") && name.ends_with(".zip")
        })
    });
    let digest = asset.map(|a| text(&a["digest"])).unwrap_or_default();
    Ok(Release {
        tag: text(&data["tag_name"]),
        page: text(&data["html_url"]),
        notes: text(&data["body"]).trim().to_string(),
        asset_url: asset.map(|a| text(&a["browser_download_url"])),
        asset_size: asset.and_then(|a| a["size"].as_u64()).unwrap_or(0),
        sha256: digest.strip_prefix("sha256:").map(str::to_lowercase),
    })
}

/// The latest release if it is newer than this build.
pub fn newer_release() -> Result<Option<Release>, String> {
    let release = latest_release()?;
    Ok((parse_version(&release.tag) > parse_version(env!("CARGO_PKG_VERSION"))).then_some(release))
}

pub fn install_dir() -> PathBuf {
    std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf)).unwrap_or_default()
}

fn writable(dir: &Path) -> bool {
    let probe = dir.join(".ats_haptics_write_test");
    let ok = std::fs::write(&probe, b"").is_ok();
    let _ = std::fs::remove_file(probe);
    ok
}

/// An installed release (its folder holds _internal, as every release zip has) that can write to its folder.
pub fn can_self_update(release: &Release) -> bool {
    let dir = install_dir();
    release.asset_url.is_some() && release.sha256.is_some() && dir.join("_internal").is_dir() && writable(&dir)
}

fn find(dir: &Path, name: &str) -> Option<PathBuf> {
    for entry in std::fs::read_dir(dir).ok()?.filter_map(Result::ok) {
        let path = entry.path();
        if path.is_dir() {
            if let Some(found) = find(&path, name) {
                return Some(found);
            }
        } else if path.file_name().is_some_and(|n| n.eq_ignore_ascii_case(name)) {
            return Some(path);
        }
    }
    None
}

/// Fetch, verify and unpack the release zip; the folder holding the new exe.
pub fn download(release: &Release, mut progress: impl FnMut(f64)) -> Result<PathBuf, String> {
    let work = std::env::temp_dir().join(format!("{WORK_PREFIX}{}", release.version()));
    let _ = std::fs::remove_dir_all(&work);
    std::fs::create_dir_all(&work).map_err(|e| e.to_string())?;
    let archive = work.join("release.zip");
    let mut out = std::io::BufWriter::new(std::fs::File::create(&archive).map_err(|e| e.to_string())?);
    let mut digest = Sha256::new();
    let mut done = 0u64;
    http_get(release.asset_url.as_deref().unwrap_or_default(), |chunk| {
        std::io::Write::write_all(&mut out, chunk).map_err(|e| e.to_string())?;
        digest.update(chunk);
        done += chunk.len() as u64;
        if release.asset_size > 0 {
            progress(done as f64 / release.asset_size as f64);
        }
        Ok(())
    })?;
    std::io::Write::flush(&mut out).map_err(|e| e.to_string())?;
    drop(out);
    let hex: String = digest.finalize().iter().map(|b| format!("{b:02x}")).collect();
    if Some(hex) != release.sha256 {
        return Err(t!("다운로드한 파일의 해시가 릴리스와 다릅니다"));
    }
    let unpacked = work.join("unpacked");
    let quoted = |p: &Path| format!("'{}'", p.to_string_lossy().replace('\'', "''"));
    let status = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command"])
        .arg(format!("Expand-Archive -LiteralPath {} -DestinationPath {} -Force", quoted(&archive), quoted(&unpacked)))
        .creation_flags(CREATE_NO_WINDOW)
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        // PowerShell missing, blocked by policy, or the disk full: not the zip's doing
        let code = status.code().map_or("?".to_string(), |code| code.to_string());
        return Err(t!("릴리스 zip을 풀지 못했습니다 (PowerShell {code})", code = code));
    }
    match find(&unpacked, EXE_NAME) {
        Some(exe) if exe.parent().is_some_and(|d| d.join("_internal").is_dir()) => Ok(exe.parent().unwrap().to_path_buf()),
        _ => Err(t!("릴리스 zip에 {exe}이 없습니다", exe = EXE_NAME)),
    }
}

const WORK_PREFIX: &str = "ATSHaptics-update-";

/// A download folder (its name) this build has no more use for: of this version or an older one.
/// One of a newer version is an update that has not gone through yet; its log says why.
fn spent(folder: &str, running: &str) -> bool {
    folder.strip_prefix(WORK_PREFIX).is_some_and(|version| {
        let version = parse_version(version);
        !version.is_empty() && version <= parse_version(running)
    })
}

/// Remove what past updates left in %TEMP%: the zip, the unpacked release and the copy of the
/// version before it, about three times the program's size each time. Called a while after
/// the start, when the script that installed this version has long ended.
pub fn clean_up_downloads() {
    let Ok(entries) = std::fs::read_dir(std::env::temp_dir()) else { return };
    for entry in entries.filter_map(Result::ok) {
        if spent(&entry.file_name().to_string_lossy(), env!("CARGO_PKG_VERSION")) && entry.path().is_dir() {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}

use std::os::windows::process::CommandExt;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Start the copy script; it waits for this program to exit. Returns its log path.
pub fn apply_on_exit(source: &Path) -> std::io::Result<PathBuf> {
    let parent = source.parent().unwrap_or(source);
    let script = parent.join("apply-update.ps1");
    // UTF-8 with a BOM, as updater.py writes it, so Windows PowerShell reads it as UTF-8
    let mut text = vec![0xEF, 0xBB, 0xBF];
    text.extend_from_slice(UPDATE_SCRIPT.replace('\n', "\r\n").as_bytes());
    std::fs::write(&script, text)?;
    let log = parent.join("apply-update.log");
    // CREATE_NO_WINDOW, not DETACHED_PROCESS: powershell.exe without any console exits at once
    std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(&script)
        .arg("-Source")
        .arg(source)
        .arg("-Target")
        .arg(install_dir())
        .arg("-Log")
        .arg(&log)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()?;
    Ok(log)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_by_number() {
        assert!(parse_version("v0.20.0") > parse_version("0.19.0"));
        assert!(parse_version("0.19.10") > parse_version("0.19.9"));
        assert_eq!(parse_version("v1.2.3"), vec![1, 2, 3]);
    }

    #[test]
    fn only_downloads_of_this_version_or_older_are_spent() {
        assert!(spent("ATSHaptics-update-0.21.0", "0.21.0"));
        assert!(spent("ATSHaptics-update-0.19.10", "0.21.0"));
        assert!(!spent("ATSHaptics-update-0.22.0", "0.21.0"), "an update still to go through");
        assert!(!spent("ATSHaptics-update-", "0.21.0"));
        assert!(!spent("ATSHaptics-plugin", "0.21.0"));
        assert!(!spent("something-else-0.1.0", "0.21.0"));
    }
}

#[cfg(test)]
mod network {
    /// cargo test --lib updater::network -- --ignored --nocapture
    #[test]
    #[ignore = "reaches api.github.com"]
    fn the_latest_release_comes_back() {
        let release = super::latest_release().unwrap();
        eprintln!("{} {} asset={:?} sha256={:?} notes={} chars", release.tag, release.page, release.asset_url,
                  release.sha256, release.notes.len());
        assert!(!release.tag.is_empty());
    }
}

#[cfg(test)]
mod download {
    /// cargo test --lib updater::download -- --ignored --nocapture
    #[test]
    #[ignore = "downloads the latest release"]
    fn the_latest_release_downloads_verified() {
        let release = super::latest_release().unwrap();
        let mut last = 0.0;
        let folder = super::download(&release, |done| last = done).unwrap();
        eprintln!("unpacked to {} (progress reached {last:.2})", folder.display());
        assert!(folder.join(super::EXE_NAME).is_file());
        let _ = std::fs::remove_dir_all(folder.ancestors().find(|p| p.ends_with(format!("ATSHaptics-update-{}", release.version()))).unwrap());
    }
}
