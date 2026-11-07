//! ATS / ETS2 telemetry to APEX 6 Pro voice-coil haptics: the Rust port of the Python app.
//!
//! The synth matches the Python one sample for sample (tests/parity.rs runs both on the same
//! drives); pymath and pyrandom are what make that possible.

pub mod apex6;
pub mod effects;
pub mod gamewatch;
pub mod i18n;
pub mod pymath;
pub mod pyrandom;
pub mod runner;
pub mod settings;
pub mod telemetry;
