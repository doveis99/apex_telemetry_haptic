//! Messages are keyed by their Korean source text, as in i18n.py. For now the port speaks
//! only the source language; the translation tables (locales/*.py) come over with the window.

/// Fill a message's {name} fields.
pub fn t(text: &str, values: &[(&str, String)]) -> String {
    let mut message = text.to_string();
    for (name, value) in values {
        message = message.replace(&format!("{{{name}}}"), value);
    }
    message
}

/// Mark a message for translation without translating it (i18n.N_).
pub fn n_(text: &'static str) -> &'static str {
    text
}

/// t!("변속 {gear}단", gear = frame.gear)
#[macro_export]
macro_rules! t {
    ($text:expr $(,)?) => {
        $crate::i18n::t($text, &[])
    };
    ($text:expr, $($name:ident = $value:expr),+ $(,)?) => {
        $crate::i18n::t($text, &[$((stringify!($name), format!("{}", $value))),+])
    };
}
