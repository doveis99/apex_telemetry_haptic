//! ATSHaptics.exe: the live tuning window (port of app.py and gui.py's main).
//!
//!   ATSHaptics.exe                       the window
//!   ATSHaptics.exe --launch %command%    Steam launch option: open the window minimized, then
//!                                        run the game and wait
//!   ATSHaptics.exe --dry-run | --autostart | --exit-with-game | --minimized
//!   ATSHaptics.exe --selftest            check the build; report to %TEMP%\ATSHaptics-selftest.txt
//!   ATSHaptics.exe --snapshot OUT.png [--language CODE] [--screen main|plugins|calibration]
//!                                        draw a window offscreen into a PNG, for checking the layout
//!
//! Only one window runs at a time; a second launch exits quietly, and if it came from the
//! Steam launch option the open window starts the game session instead. Unexpected errors
//! land in logs\error.log.

#![windows_subsystem = "windows"]

slint::include_modules!();

mod calibration;
mod plugins;
mod snapshot;
mod specs;
mod window;
mod worker;

use std::time::Duration;

use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use windows_sys::Win32::Foundation::HWND;

use ats_haptics::i18n;
use ats_haptics::platform::{self, LaunchEvent};
use ats_haptics::settings::Settings;

/// The native window behind a Slint window, once it is shown; null before.
pub fn hwnd(window: &slint::Window) -> HWND {
    match window.window_handle().window_handle().map(|h| h.as_raw()) {
        Ok(RawWindowHandle::Win32(handle)) => handle.hwnd.get() as HWND,
        _ => std::ptr::null_mut(),
    }
}

/// Bring a window to the front.
pub fn raise(window: &slint::Window) {
    let hwnd = hwnd(window);
    if !hwnd.is_null() {
        // SAFETY: a window handle of this process.
        unsafe {
            windows_sys::Win32::UI::WindowsAndMessaging::SetForegroundWindow(hwnd);
        }
    }
}

/// Run `f` with the native window once it exists (and, with `visible`, is on screen): winit
/// creates it a few turns of the event loop after show() and shows it after its first frame,
/// so this looks again every 20 ms, for up to two seconds.
pub fn when_created<C: slint::ComponentHandle + 'static>(component: &C, visible: bool, f: impl FnOnce(HWND) + 'static) {
    fn attempt<C: slint::ComponentHandle + 'static>(weak: slint::Weak<C>, visible: bool, f: Box<dyn FnOnce(HWND)>,
                                                     left: u32) {
        let Some(component) = weak.upgrade() else { return };
        let handle = hwnd(component.window());
        // SAFETY: IsWindowVisible only reads the state of a window handle.
        let ready = !handle.is_null()
            && (!visible || unsafe { windows_sys::Win32::UI::WindowsAndMessaging::IsWindowVisible(handle) } != 0);
        if ready {
            f(handle);
        } else if left > 0 {
            slint::Timer::single_shot(Duration::from_millis(20), move || attempt(weak, visible, f, left - 1));
        }
    }
    let weak = component.as_weak();
    slint::Timer::single_shot(Duration::from_millis(0), move || attempt(weak, visible, Box::new(f), 100));
}

/// A dark caption once the native window exists.
pub fn dark_title_bar_later<C: slint::ComponentHandle + 'static>(component: &C) {
    when_created(component, false, platform::dark_title_bar);
}

/// Switch both the Rust messages and the window's own (@tr) texts.
pub fn select_language(code: &str) {
    i18n::set_language(code);
    let _ = slint::select_bundled_translation(if code == "ko" { "" } else { code });
}

fn main() {
    ats_haptics::errorlog::install_panic_hook();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--launch") => std::process::exit(ats_haptics::launch::launch(&args[1..])),
        Some("--selftest") => std::process::exit(ats_haptics::launch::selftest()),
        Some("--snapshot") => std::process::exit(snapshot::run(&args[1..])),
        _ => {}
    }
    let flag = |name: &str| args.iter().any(|a| a == name);
    let (dry_run, autostart, exit_with_game, minimized) =
        (flag("--dry-run"), flag("--autostart"), flag("--exit-with-game"), flag("--minimized"));
    if !platform::claim_single_instance() {
        if autostart {
            LaunchEvent::request(); // a Steam launch: the window already open runs the session
        }
        return;
    }
    let options = Settings::default().load_app_options();
    let language = if options.language.is_empty() { i18n::system_language().to_string() } else { options.language };
    i18n::set_language(&language);
    let app = window::App::new(dry_run, exit_with_game);
    select_language(&language); // the window's own texts, now that it exists
    {
        let mut a = app.borrow_mut();
        if autostart {
            a.start();
        }
        let _ = a.ui.show();
        a.start_polling(true);
    }
    let weak = std::rc::Rc::downgrade(&app);
    let ui = app.borrow().ui.clone_strong();
    dark_title_bar_later(&ui);
    if minimized {
        when_created(&ui, true, |handle| {
            // minimized without taking the focus from the game being launched
            // SAFETY: a window of this process.
            unsafe {
                windows_sys::Win32::UI::WindowsAndMessaging::ShowWindow(
                    handle,
                    windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWMINNOACTIVE,
                );
            }
        });
    }
    drop(ui);
    if !minimized {
        slint::Timer::single_shot(Duration::from_millis(300), move || window::with(&weak, |a| a.check_plugins()));
    }
    let _ = slint::run_event_loop_until_quit();
    drop(app);
}
