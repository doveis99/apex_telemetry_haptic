//! The Steam launch option and the self-test (port of app.py).
//!
//!   ATSHaptics.exe --launch %command%    open the window minimized, then run the game and wait
//!   ATSHaptics.exe --selftest            check the build without a window; report to %TEMP%

use std::os::windows::process::CommandExt;
use std::path::PathBuf;

pub const WINDOW_FLAGS: [&str; 3] = ["--autostart", "--exit-with-game", "--minimized"];
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED_PROCESS: u32 = 0x0000_0008;

fn window_command() -> Vec<String> {
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("ATSHaptics.exe"));
    std::iter::once(exe.to_string_lossy().into_owned()).chain(WINDOW_FLAGS.iter().map(|s| s.to_string())).collect()
}

/// subprocess.list2cmdline: the command line Windows' C runtime splits back into these arguments.
pub fn list2cmdline(args: &[String]) -> String {
    let mut line = String::new();
    for (i, arg) in args.iter().enumerate() {
        if i > 0 {
            line.push(' ');
        }
        let quote = arg.is_empty() || arg.contains([' ', '\t']);
        if quote {
            line.push('"');
        }
        let mut backslashes = 0;
        for c in arg.chars() {
            match c {
                '\\' => backslashes += 1,
                '"' => {
                    line.push_str(&"\\".repeat(backslashes * 2 + 1));
                    line.push('"');
                    backslashes = 0;
                }
                _ => {
                    line.push_str(&"\\".repeat(backslashes));
                    backslashes = 0;
                    line.push(c);
                }
            }
        }
        line.push_str(&"\\".repeat(if quote { backslashes * 2 } else { backslashes }));
        if quote {
            line.push('"');
        }
    }
    line
}

/// Start the command through WMI so it is not a child of this process: Steam injects its
/// overlay into the launch-option process and everything it starts, and with Steam Input on
/// the overlay hides the controller from them. A WMI-created process sits outside that tree.
fn start_outside_steam(command: &[String]) -> bool {
    let line = list2cmdline(command).replace('\'', "''");
    let folder = PathBuf::from(&command[0]).parent().map(|p| p.to_string_lossy().replace('\'', "''")).unwrap_or_default();
    let script = format!(
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments \
         @{{CommandLine='{line}'; CurrentDirectory='{folder}'}}; exit $r.ReturnValue"
    );
    std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .creation_flags(CREATE_NO_WINDOW)
        .status()
        .is_ok_and(|s| s.success())
}

/// Start the window detached, then run the game so Steam keeps tracking it.
pub fn launch(game_command: &[String]) -> i32 {
    let command = window_command();
    if !start_outside_steam(&command) {
        let _ = std::process::Command::new(&command[0]).args(&command[1..]).creation_flags(DETACHED_PROCESS).spawn();
    }
    if game_command.is_empty() {
        return 0;
    }
    std::process::Command::new(&game_command[0])
        .args(&game_command[1..])
        .status()
        .map(|s| s.code().unwrap_or(1))
        .unwrap_or(1)
}

/// Check the build without opening a window; report to %TEMP%\ATSHaptics-selftest.txt.
pub fn selftest() -> i32 {
    let mut lines = vec![
        format!("version {}", crate::runner::version()),
        format!("bundled plugin: {}", !crate::plugin_install::BUNDLED_PLUGIN.is_empty()),
    ];
    let status = match hidapi::HidApi::new() {
        Ok(api) => {
            let count = api
                .device_list()
                .filter(|d| {
                    d.vendor_id() == crate::apex6::VENDOR_ID
                        && d.product_id() == crate::apex6::PRODUCT_ID
                        && d.usage_page() == crate::apex6::USAGE_PAGE
                })
                .count();
            lines.push(format!("apex 6 interfaces: {count}"));
            for game in crate::plugin_install::find_games() {
                lines.push(format!("{}: {} -> {}", game.key, game.path.display(), game.describe()));
            }
            0
        }
        Err(error) => {
            lines.push(format!("hidapi: {error}"));
            1
        }
    };
    let report = std::env::temp_dir().join("ATSHaptics-selftest.txt");
    let _ = std::fs::write(report, lines.join("\n"));
    status
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_lines_quote_as_python_does() {
        let args: Vec<String> = ["C:\\Program Files\\x.exe", "-a", "say \"hi\"", "end\\"].iter().map(|s| s.to_string()).collect();
        assert_eq!(list2cmdline(&args), "\"C:\\Program Files\\x.exe\" -a \"say \\\"hi\\\"\" end\\");
    }
}
