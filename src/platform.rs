//! What the window asks of Windows itself: message boxes and file pickers (the native ones,
//! as tkinter's are), opening a link or a folder, a dark title bar and the window's icon, and
//! the named mutex and event that keep one window running and let a Steam launch start its
//! session.

// A window handle here is only ever handed to Windows as the owner or target of a call, never
// dereferenced by this code.
#![allow(clippy::not_unsafe_ptr_arg_deref)]

use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::HWND;

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

/// Open a link in the browser or a folder in Explorer.
pub fn open(target: &str) {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let (verb, target) = (wide("open"), wide(target));
    // SAFETY: null-terminated strings; ShellExecuteW copies them.
    unsafe {
        ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), target.as_ptr(), std::ptr::null(), std::ptr::null(),
                      SW_SHOWNORMAL);
    }
}

pub fn open_folder(path: &Path) {
    let _ = std::fs::create_dir_all(path);
    open(&path.to_string_lossy());
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Info,
    Question,
    Error,
}

fn message_box(owner: HWND, title: &str, text: &str, buttons: u32, icon: Icon) -> i32 {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_ICONINFORMATION, MB_ICONQUESTION};
    let icon = match icon {
        Icon::Info => MB_ICONINFORMATION,
        Icon::Question => MB_ICONQUESTION,
        Icon::Error => MB_ICONERROR,
    };
    let (title, text) = (wide(title), wide(text));
    // SAFETY: null-terminated strings; the owner may be null.
    unsafe { MessageBoxW(owner, text.as_ptr(), title.as_ptr(), buttons | icon) }
}

pub fn show(owner: HWND, title: &str, text: &str, icon: Icon) {
    use windows_sys::Win32::UI::WindowsAndMessaging::MB_OK;
    message_box(owner, title, text, MB_OK, icon);
}

pub fn ask_yes_no(owner: HWND, title: &str, text: &str) -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::{IDYES, MB_YESNO};
    message_box(owner, title, text, MB_YESNO, Icon::Question) == IDYES
}

pub fn ask_ok_cancel(owner: HWND, title: &str, text: &str) -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::{IDOK, MB_OKCANCEL};
    message_box(owner, title, text, MB_OKCANCEL, Icon::Question) == IDOK
}

/// Some(true) yes, Some(false) no, None cancel.
pub fn ask_yes_no_cancel(owner: HWND, title: &str, text: &str) -> Option<bool> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{IDNO, IDYES, MB_YESNOCANCEL};
    match message_box(owner, title, text, MB_YESNOCANCEL, Icon::Question) {
        IDYES => Some(true),
        IDNO => Some(false),
        _ => None,
    }
}

/// The open-file dialog; filters as (label, pattern) pairs.
pub fn pick_file(owner: HWND, title: &str, initial_dir: Option<&Path>, filters: &[(&str, &str)]) -> Option<PathBuf> {
    use windows_sys::Win32::UI::Controls::Dialogs::{
        GetOpenFileNameW, OFN_EXPLORER, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR, OPENFILENAMEW,
    };
    let mut filter: Vec<u16> = Vec::new();
    for (label, pattern) in filters {
        filter.extend(label.encode_utf16().chain(Some(0)));
        filter.extend(pattern.encode_utf16().chain(Some(0)));
    }
    filter.push(0);
    let title = wide(title);
    let initial = initial_dir.map(|d| wide(&d.to_string_lossy()));
    let mut file = vec![0u16; 1024];
    // SAFETY: every pointer stays valid for the call; the struct is sized as the API asks.
    unsafe {
        let mut ofn: OPENFILENAMEW = std::mem::zeroed();
        ofn.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
        ofn.hwndOwner = owner;
        ofn.lpstrFilter = filter.as_ptr();
        ofn.lpstrFile = file.as_mut_ptr();
        ofn.nMaxFile = file.len() as u32;
        ofn.lpstrTitle = title.as_ptr();
        ofn.lpstrInitialDir = initial.as_ref().map_or(std::ptr::null(), |d| d.as_ptr());
        ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_NOCHANGEDIR;
        if GetOpenFileNameW(&mut ofn) == 0 {
            return None;
        }
    }
    let end = file.iter().position(|&c| c == 0).unwrap_or(file.len());
    Some(PathBuf::from(String::from_utf16_lossy(&file[..end])))
}

/// The folder picker.
pub fn pick_folder(owner: HWND, title: &str) -> Option<PathBuf> {
    use windows_sys::Win32::System::Com::{CoInitializeEx, CoTaskMemFree, COINIT_APARTMENTTHREADED};
    use windows_sys::Win32::UI::Shell::{
        SHBrowseForFolderW, SHGetPathFromIDListW, BIF_NEWDIALOGSTYLE, BIF_RETURNONLYFSDIRS, BROWSEINFOW,
    };
    let title = wide(title);
    let mut path = vec![0u16; 1024];
    // SAFETY: COM is initialised for the dialog; the item list is freed once read.
    unsafe {
        CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32);
        let mut info: BROWSEINFOW = std::mem::zeroed();
        info.hwndOwner = owner;
        info.lpszTitle = title.as_ptr();
        info.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
        let list = SHBrowseForFolderW(&info);
        if list.is_null() {
            return None;
        }
        let ok = SHGetPathFromIDListW(list, path.as_mut_ptr()) != 0;
        CoTaskMemFree(list as *const _);
        if !ok {
            return None;
        }
    }
    let end = path.iter().position(|&c| c == 0).unwrap_or(path.len());
    Some(PathBuf::from(String::from_utf16_lossy(&path[..end])))
}

/// Ask Windows 10/11 for a dark caption; harmless where unsupported.
pub fn dark_title_bar(hwnd: HWND) {
    use windows_sys::Win32::Graphics::Dwm::DwmSetWindowAttribute;
    let on: i32 = 1;
    for attribute in [20u32, 19] {
        // SAFETY: a 4-byte BOOL for DWMWA_USE_IMMERSIVE_DARK_MODE.
        if unsafe { DwmSetWindowAttribute(hwnd, attribute, (&on as *const i32).cast(), 4) } == 0 {
            break;
        }
    }
}

/// Give a window the program's own icon (resource 1, which build.rs embeds) in its caption and
/// on the taskbar, at the sizes the window's DPI asks for; winit leaves both empty.
pub fn app_icon(hwnd: HWND) {
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::HiDpi::{GetDpiForWindow, GetSystemMetricsForDpi};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        LoadImageW, SendMessageW, ICON_BIG, ICON_SMALL, IMAGE_ICON, LR_DEFAULTCOLOR, SM_CXICON, SM_CXSMICON,
        WM_SETICON,
    };
    // SAFETY: a window of this process; the icon resource is this executable's, and an icon
    // LoadImageW makes without LR_SHARED stays alive for the window to keep using.
    unsafe {
        let module = GetModuleHandleW(std::ptr::null());
        let dpi = GetDpiForWindow(hwnd).max(96);
        for (kind, metric) in [(ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)] {
            let side = GetSystemMetricsForDpi(metric, dpi);
            let icon = LoadImageW(module, 1 as _, IMAGE_ICON, side, side, LR_DEFAULTCOLOR);
            if !icon.is_null() {
                SendMessageW(hwnd, WM_SETICON, kind as usize, icon as isize);
            }
        }
    }
}

pub const INSTANCE_MUTEX: &str = "Local\\ApexAtsHapticsGui";
pub const LAUNCH_EVENT: &str = "Local\\ApexAtsHapticsGuiLaunch";

/// Hold the named mutex for this process's life; false if another window already holds it.
pub fn claim_single_instance() -> bool {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::CreateMutexW;
    let name = wide(INSTANCE_MUTEX);
    // SAFETY: the handle is deliberately kept open until the process exits.
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        !(handle.is_null() || GetLastError() == ERROR_ALREADY_EXISTS)
    }
}

/// The auto-reset event a Steam launch sets when the window is already open.
pub struct LaunchEvent(windows_sys::Win32::Foundation::HANDLE);

// SAFETY: an event handle may be waited on from any thread.
unsafe impl Send for LaunchEvent {}

impl LaunchEvent {
    pub fn open() -> Option<LaunchEvent> {
        use windows_sys::Win32::System::Threading::CreateEventW;
        let name = wide(LAUNCH_EVENT);
        // SAFETY: an unnamed security descriptor; the handle is closed on drop.
        let handle = unsafe { CreateEventW(std::ptr::null(), 0, 0, name.as_ptr()) };
        (!handle.is_null()).then_some(LaunchEvent(handle))
    }

    /// True once per launch request (the wait resets the event).
    pub fn requested(&self) -> bool {
        use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
        use windows_sys::Win32::System::Threading::WaitForSingleObject;
        // SAFETY: a valid event handle.
        unsafe { WaitForSingleObject(self.0, 0) == WAIT_OBJECT_0 }
    }

    /// From a second instance: have the open window start its game session.
    pub fn request() {
        use windows_sys::Win32::System::Threading::SetEvent;
        if let Some(event) = LaunchEvent::open() {
            // SAFETY: a valid event handle.
            unsafe {
                SetEvent(event.0);
            }
        }
    }
}

impl Drop for LaunchEvent {
    fn drop(&mut self) {
        // SAFETY: opened in LaunchEvent::open and closed once.
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
