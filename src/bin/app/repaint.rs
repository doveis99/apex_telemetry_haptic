//! Draw the whole window again when Windows asks for it.
//!
//! Slint's software renderer draws only what changed since its last frame and trusts the rest
//! to be on screen still. Windows throws a window's picture away while it is minimized (and
//! when the display changes mode, as under a fullscreen game), then asks for all of it with a
//! WM_PAINT that carries an update region. The winit backend knows of a minimize only by the
//! window's size going to zero, and forgets it with the first frame drawn while minimized,
//! which the poll's changing texts bring at once. A window opened minimized by the Steam
//! launch option and restored during the game then stayed empty, but for the spots where the
//! mouse made something change.
//!
//! Slint's own redraw requests arrive as WM_PAINT without an update region (winit asks with
//! RDW_INTERNALPAINT), which tells the two apart: only Windows' requests redraw everything.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::GetUpdateRect;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallWindowProcW, DefWindowProcW, GetWindowLongPtrW, SetWindowLongPtrW, GWLP_WNDPROC, WM_NCDESTROY, WM_PAINT,
    WNDPROC,
};

/// A window whose `repaint` property (ui/widgets.slint's Repaint) redraws all of it when counted up.
pub trait Repaint {
    fn repaint(&self);
}

macro_rules! repaint {
    ($($window:ty),*) => {$(
        impl Repaint for $window {
            fn repaint(&self) {
                self.set_repaint(self.get_repaint().wrapping_add(1));
            }
        }
    )*};
}
repaint!(crate::AppWindow, crate::PluginDialog, crate::CalibrationDialog);

struct Hook {
    /// winit's window procedure, which ours passes every message on to
    previous: WNDPROC,
    repaint: Rc<dyn Fn()>,
}

thread_local! {
    /// by window handle; the windows and their procedure live on the event loop's thread
    static HOOKS: RefCell<HashMap<usize, Hook>> = RefCell::new(HashMap::new());
}

/// Call `repaint` before each WM_PAINT in which Windows itself asks for part of this window.
pub fn watch(hwnd: HWND, repaint: impl Fn() + 'static) {
    let known = HOOKS.with(|hooks| hooks.borrow().contains_key(&(hwnd as usize)));
    if known || hwnd.is_null() {
        return;
    }
    // SAFETY: a window of this thread; the value read is its window procedure, and ours takes
    // its place only once the entry it looks up is there.
    unsafe {
        let previous: WNDPROC = std::mem::transmute(GetWindowLongPtrW(hwnd, GWLP_WNDPROC));
        HOOKS.with(|hooks| hooks.borrow_mut().insert(hwnd as usize, Hook { previous, repaint: Rc::new(repaint) }));
        SetWindowLongPtrW(hwnd, GWLP_WNDPROC, window_proc as *const () as isize);
    }
}

unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    // the entry is copied out: `repaint` runs Slint code, which may come back in here
    let hook = HOOKS.with(|hooks| {
        let mut hooks = hooks.borrow_mut();
        if message == WM_NCDESTROY {
            hooks.remove(&(hwnd as usize)).map(|hook| (hook.previous, None))
        } else {
            hooks.get(&(hwnd as usize)).map(|hook| (hook.previous, Some(Rc::clone(&hook.repaint))))
        }
    });
    let Some((previous, repaint)) = hook else {
        return DefWindowProcW(hwnd, message, wparam, lparam);
    };
    if message == WM_PAINT && GetUpdateRect(hwnd, std::ptr::null_mut(), 0) != 0 {
        if let Some(repaint) = repaint {
            repaint();
        }
    }
    CallWindowProcW(previous, hwnd, message, wparam, lparam)
}
