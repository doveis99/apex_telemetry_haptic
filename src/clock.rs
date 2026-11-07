//! The local date and time, as the debug log and the event lines show it.

#[derive(Clone, Copy, Debug)]
pub struct LocalTime {
    pub year: u16,
    pub month: u16,
    pub day: u16,
    pub hour: u16,
    pub minute: u16,
    pub second: u16,
    pub millisecond: u16,
}

pub fn now() -> LocalTime {
    use windows_sys::Win32::System::SystemInformation::GetLocalTime;
    // SAFETY: GetLocalTime fills the struct it is given.
    let t = unsafe {
        let mut t = std::mem::zeroed();
        GetLocalTime(&mut t);
        t
    };
    LocalTime {
        year: t.wYear,
        month: t.wMonth,
        day: t.wDay,
        hour: t.wHour,
        minute: t.wMinute,
        second: t.wSecond,
        millisecond: t.wMilliseconds,
    }
}

impl LocalTime {
    /// HH:MM:SS
    pub fn hms(&self) -> String {
        format!("{:02}:{:02}:{:02}", self.hour, self.minute, self.second)
    }

    /// HH:MM:SS.mmm
    pub fn hms_millis(&self) -> String {
        format!("{}.{:03}", self.hms(), self.millisecond)
    }

    /// YYYYmmdd-HHMMSS
    pub fn stamp(&self) -> String {
        format!(
            "{:04}{:02}{:02}-{:02}{:02}{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }
}
