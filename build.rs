//! Compile the window (ui/app.slint) with the translations bundled: resources/lang/<code>/LC_MESSAGES/
//! ats_haptics.po, written by tools/export_locales.py from the Python app's tables. Without a
//! default context the Korean source text alone is the key, as in i18n.py.

fn main() {
    let config = slint_build::CompilerConfiguration::new()
        .with_bundled_translations("resources/lang")
        .with_default_translation_context(slint_build::DefaultTranslationContext::None);
    slint_build::compile_with_config("ui/app.slint", config).expect("ui/app.slint");
    println!("cargo:rerun-if-changed=resources/lang");
}
