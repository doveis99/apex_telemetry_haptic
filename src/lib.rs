//! ATS / ETS2 telemetry to APEX 6 Pro voice-coil haptics: the Rust port of the Python app.
//!
//! The synth matches the Python one sample for sample (tests/parity.rs runs both on the same
//! drives); pymath and pyrandom are what make that possible.

pub mod apex6;
pub mod blocklog;
pub mod calibrate;
pub mod clock;
pub mod effects;
pub mod errorlog;
pub mod gamewatch;
pub mod i18n;
pub mod launch;
pub mod platform;
pub mod plugin_install;
pub mod preview;
pub mod pymath;
pub mod pyrandom;
pub mod replay;
pub mod runner;
pub mod scenarios;
pub mod settings;
pub mod telemetry;
pub mod updater;
