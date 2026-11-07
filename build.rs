//! Compile the window (ui/app.slint) with the translations bundled: resources/lang/<code>/LC_MESSAGES/
//! ats_haptics.po, written by tools/export_locales.py from the Python app's tables. Without a
//! default context the Korean source text alone is the key, as in i18n.py.
//!
//! Then give each program its own icon (resources/icons, drawn by tools/make_icons.py) and a
//! description Explorer and Task Manager show, so the window stands apart from the two console
//! programs beside it.

use std::path::Path;

/// (binary, icon, FileDescription)
const PROGRAMS: [(&str, &str, &str); 3] = [
    ("ATSHaptics", "ATSHaptics.ico", "ATS Haptics"),
    ("ats_haptics", "ats_haptics.ico", "ATS Haptics command line"),
    ("replay", "replay.ico", "ATS Haptics debug log replay"),
];

fn main() {
    let config = slint_build::CompilerConfiguration::new()
        .with_bundled_translations("resources/lang")
        .with_default_translation_context(slint_build::DefaultTranslationContext::None);
    slint_build::compile_with_config("ui/app.slint", config).expect("ui/app.slint");
    println!("cargo:rerun-if-changed=resources/lang");

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        resources();
    }
}

/// One .rc per program: icon 1 (the one Explorer shows and platform::app_icon loads) and the
/// version block, with the version from Cargo.toml.
fn resources() {
    let out = std::env::var("OUT_DIR").unwrap();
    let icons = Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("resources").join("icons");
    let version = std::env::var("CARGO_PKG_VERSION").unwrap();
    let mut numbers = version.split(['.', '-']).map(|part| part.parse::<u16>().unwrap_or(0));
    let (major, minor, patch) = (numbers.next().unwrap_or(0), numbers.next().unwrap_or(0), numbers.next().unwrap_or(0));
    for (binary, icon, description) in PROGRAMS {
        let icon = icons.join(icon).to_string_lossy().replace('\\', "/");
        let rc = format!(
            r#"1 ICON "{icon}"

1 VERSIONINFO
FILEVERSION {major},{minor},{patch},0
PRODUCTVERSION {major},{minor},{patch},0
FILEFLAGSMASK 0x3f
FILEFLAGS 0
FILEOS 0x40004
FILETYPE 1
FILESUBTYPE 0
BEGIN
    BLOCK "StringFileInfo"
    BEGIN
        BLOCK "040904b0"
        BEGIN
            VALUE "FileDescription", "{description}"
            VALUE "FileVersion", "{version}"
            VALUE "InternalName", "{binary}"
            VALUE "OriginalFilename", "{binary}.exe"
            VALUE "ProductName", "ATS Haptics"
            VALUE "ProductVersion", "{version}"
        END
    END
    BLOCK "VarFileInfo"
    BEGIN
        VALUE "Translation", 0x409, 1200
    END
END
"#
        );
        let path = Path::new(&out).join(format!("{binary}.rc"));
        std::fs::write(&path, rc).unwrap();
        embed_resource::compile_for(&path, [binary], embed_resource::NONE).manifest_required().unwrap();
    }
    println!("cargo:rerun-if-changed=resources/icons");
}
