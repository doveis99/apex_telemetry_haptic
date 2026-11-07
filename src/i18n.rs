//! UI language (port of i18n.py): the Korean text in the code is the message key, looked up
//! in per-language tables. A missing translation falls back to English, then to the Korean
//! key. The tables are python/locales/*.py exported by tools/export_locales.py and embedded.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock, RwLock};

/// code, name in that language; the order of the language menu
pub const LANGUAGES: [(&str, &str); 10] = [
    ("ko", "한국어"),
    ("en", "English"),
    ("ja", "日本語"),
    ("zh", "简体中文"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("es", "Español"),
    ("pt", "Português (Brasil)"),
    ("pl", "Polski"),
    ("ru", "Русский"),
];

const TABLES: [(&str, &str); 9] = [
    ("en", include_str!("../resources/locales/en.json")),
    ("ja", include_str!("../resources/locales/ja.json")),
    ("zh", include_str!("../resources/locales/zh.json")),
    ("de", include_str!("../resources/locales/de.json")),
    ("fr", include_str!("../resources/locales/fr.json")),
    ("es", include_str!("../resources/locales/es.json")),
    ("pt", include_str!("../resources/locales/pt.json")),
    ("pl", include_str!("../resources/locales/pl.json")),
    ("ru", include_str!("../resources/locales/ru.json")),
];

/// Windows primary language id -> code
const WINDOWS_LANGUAGES: [(u16, &str); 10] = [
    (0x12, "ko"),
    (0x09, "en"),
    (0x11, "ja"),
    (0x04, "zh"),
    (0x07, "de"),
    (0x0C, "fr"),
    (0x0A, "es"),
    (0x16, "pt"),
    (0x15, "pl"),
    (0x19, "ru"),
];

type Table = HashMap<String, String>;

fn table(code: &str) -> Option<Table> {
    TABLES.iter().find(|(c, _)| *c == code).map(|(_, json)| serde_json::from_str(json).expect("embedded table"))
}

fn english() -> &'static Table {
    static EN: OnceLock<Table> = OnceLock::new();
    EN.get_or_init(|| table("en").unwrap_or_default())
}

struct Current {
    code: &'static str,
    table: Option<Arc<Table>>,
}

fn current() -> &'static RwLock<Current> {
    static CURRENT: OnceLock<RwLock<Current>> = OnceLock::new();
    CURRENT.get_or_init(|| RwLock::new(Current { code: "ko", table: None }))
}

pub fn language() -> &'static str {
    current().read().unwrap().code
}

/// Switch the language; an unknown code gives English.
pub fn set_language(code: &str) {
    let code = LANGUAGES.iter().map(|(c, _)| *c).find(|c| *c == code).unwrap_or("en");
    let table = if code == "ko" { None } else { table(code).map(Arc::new) };
    *current().write().unwrap() = Current { code, table };
}

/// The Windows display language if there is a table for it, else English.
pub fn system_language() -> &'static str {
    // SAFETY: takes no arguments.
    let primary = unsafe { windows_sys::Win32::Globalization::GetUserDefaultUILanguage() } & 0x3FF;
    WINDOWS_LANGUAGES.iter().find(|(id, _)| *id == primary).map_or("en", |(_, code)| code)
}

/// The UI font for the current language.
pub fn ui_font() -> &'static str {
    match language() {
        "ko" => "Malgun Gothic",
        "ja" => "Yu Gothic UI",
        "zh" => "Microsoft YaHei UI",
        _ => "Segoe UI",
    }
}

/// The message in the current language, its {name} fields filled.
pub fn t(text: &str, values: &[(&str, String)]) -> String {
    let message = {
        let current = current().read().unwrap();
        match &current.table {
            None => text.to_string(),
            Some(table) => table.get(text).or_else(|| english().get(text)).cloned().unwrap_or_else(|| text.to_string()),
        }
    };
    let mut message = message;
    for (name, value) in values {
        message = message.replace(&format!("{{{name}}}"), value);
    }
    message
}

/// Mark a message for translation without translating it (i18n.N_).
pub const fn n_(text: &'static str) -> &'static str {
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The Korean keys the Rust code asks for: t!("…") and n_("…") literals in src/.
    fn keys_in_code() -> Vec<(String, String)> {
        let mut keys = Vec::new();
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut stack = vec![root];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(dir).unwrap().filter_map(Result::ok) {
                let path = entry.path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                if path.extension().is_none_or(|x| x != "rs") || path.ends_with("i18n.rs") {
                    continue;
                }
                let text = std::fs::read_to_string(&path).unwrap();
                for opener in ["t!(\"", "n_(\"", "tr(\"", "crate::i18n::t(\""] {
                    for (at, _) in text.match_indices(opener) {
                        let rest = &text[at + opener.len()..];
                        let mut key = String::new();
                        let mut chars = rest.chars();
                        while let Some(c) = chars.next() {
                            match c {
                                '"' => break,
                                '\\' => match chars.next() {
                                    Some('n') => key.push('\n'),
                                    Some('\n') => {
                                        // a line continuation: the next line's leading spaces go too
                                        let rest: String = chars.clone().collect();
                                        let trimmed = rest.trim_start();
                                        let skip = rest.len() - trimmed.len();
                                        for _ in 0..rest[..skip].chars().count() {
                                            chars.next();
                                        }
                                    }
                                    Some(other) => key.push(other),
                                    None => {}
                                },
                                c => key.push(c),
                            }
                        }
                        if key.chars().any(|c| ('\u{ac00}'..='\u{d7a3}').contains(&c)) {
                            keys.push((key, path.file_name().unwrap().to_string_lossy().into_owned()));
                        }
                    }
                }
            }
        }
        keys
    }

    #[test]
    fn every_message_in_the_code_has_an_english_translation() {
        let mut keys = keys_in_code();
        assert!(keys.len() > 30, "the scan found only {} messages", keys.len());
        // labels kept in tables, which the scan does not see
        keys.extend(crate::effects::GAME_EVENT_LABELS.iter().map(|(_, label)| (label.to_string(), "effects.rs".into())));
        let missing: Vec<_> = keys.into_iter().filter(|(key, _)| !english().contains_key(key)).collect();
        assert!(missing.is_empty(), "no English for {} messages: {missing:#?}", missing.len());
    }

    /// The keys the window's own texts ask for: @tr("…") in ui/*.slint.
    fn keys_in_slint() -> Vec<(String, String)> {
        let mut keys = Vec::new();
        let ui = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("ui");
        for entry in std::fs::read_dir(ui).unwrap().filter_map(Result::ok) {
            let text = std::fs::read_to_string(entry.path()).unwrap();
            for (at, _) in text.match_indices("@tr(\"") {
                let mut key = String::new();
                let mut chars = text[at + 5..].chars();
                while let Some(c) = chars.next() {
                    match c {
                        '"' => break,
                        '\\' => match chars.next() {
                            Some('n') => key.push('\n'),
                            Some(other) => key.push(other),
                            None => {}
                        },
                        c => key.push(c),
                    }
                }
                keys.push((key, entry.file_name().to_string_lossy().into_owned()));
            }
        }
        keys
    }

    #[test]
    fn every_text_in_the_window_has_an_english_translation() {
        let keys = keys_in_slint();
        assert!(keys.len() > 80, "the scan found only {} texts", keys.len());
        let missing: Vec<_> = keys.into_iter().filter(|(key, _)| !english().contains_key(key)).collect();
        assert!(missing.is_empty(), "no English for {} window texts: {missing:#?}", missing.len());
    }

    #[test]
    fn languages_switch_and_fall_back() {
        assert_eq!(t("정지됨", &[]), "정지됨");
        set_language("en");
        assert_eq!(t("변속 {gear}단", &[("gear", "3".into())]), english()["변속 {gear}단"].replace("{gear}", "3"));
        assert_eq!(t("no such message", &[]), "no such message");
        set_language("xx");
        assert_eq!(language(), "en");
        set_language("ko");
        assert_eq!(language(), "ko");
    }
}
