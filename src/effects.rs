//! Turn telemetry frames into 1 kHz waveforms for the APEX 6 Pro (port of effects.py).
//!
//! Outputs: left grip, right grip and one trigger channel, which plays on the left trigger,
//! the right or both. Each effect is routed to the grips, the trigger or both
//! (Gains::routes). Telemetry is read once per 8 ms block; amplitudes are ramped across
//! the block and oscillator phases carry over, so parameter changes do not click.
//!
//! Every number here is effects.py's; why each one is what it is (the recordings, drives
//! and feel tests behind it) is written next to it there and not repeated here. The
//! arithmetic follows the Python expression by expression, through pymath, so the output
//! matches the Python app's to the bit (tests/parity.rs).

// next() as effects.py names it: one sample of a signal, not an iterator
#![allow(clippy::should_implement_trait)]

use std::collections::HashMap;

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::apex6::{Block, Events, Route, FINE, MAX_SAMPLE, SAMPLE_LIMIT, SAMPLE_RATE, SUBFRAMES};
use crate::i18n::n_;
use crate::pymath as m;
use crate::pyrandom::PyRandom;
use crate::t;
use crate::telemetry::{Frame, Wheel};

const SR: f64 = SAMPLE_RATE as f64;
const RATE_2K: f64 = crate::rate2k::RATE;
/// how much shorter, and higher, a tick is at 2 kHz
const STRETCH: f64 = RATE_2K / SR;
pub const DT: f64 = SUBFRAMES as f64 / SAMPLE_RATE as f64; // 8 ms per telemetry update
pub const CEILING_GRIP: f64 = SAMPLE_LIMIT as f64;
pub const CEILING_TRIGGER: f64 = 102.0; // round(0.8 * SAMPLE_LIMIT)
pub const TAU: f64 = 2.0 * m::PI;
const CHANGE_TAU: f64 = 2.0;
const FADE_TAU: f64 = 8.0;
const MAX_FRAME_GAP: f64 = 0.2;
const RATE_AVG_TAU: f64 = 0.2;
const ROUGH_TAU: f64 = 0.076;
const ROUGH_FULL: f64 = 0.12;
const ROUGH_POWER: f64 = 2.0;
const ROUGH_NOVELTY: f64 = 1.5;
const DETAIL_TAU: f64 = 2.0;
const DETAIL_RANGE: (f64, f64) = (0.25, 4.0);
const LOCK_RATE: f64 = 12.0;
const LOCK_EDGE: f64 = 0.003;
const YAW_EVIDENCE: f64 = 2.0;
const RETARDER_HZ: (f64, f64) = (40.0, 65.0);
const JAKE_PEAK: f64 = 0.1;
const ENGINE_JITTER: f64 = 0.18;
const ENGINE_IMBALANCE: f64 = 0.3;
const ENGINE_UNEVEN_FLOOR: f64 = 0.4;
const ENGINE_V8_PATTERN: f64 = 0.5;
const ENGINE_HARSH: f64 = 0.25;
const ENGINE_STANDING: f64 = 3.0;
const ENGINE_STANDING_SPEED: f64 = 3.0;

fn change_k() -> f64 {
    1.0 - m::exp(-DT / CHANGE_TAU)
}

fn fade_k() -> f64 {
    m::exp(-DT / FADE_TAU)
}

fn jake_mean() -> f64 {
    JAKE_PEAK * m::E * (1.0 - (1.0 + 1.0 / JAKE_PEAK) * m::exp(-1.0 / JAKE_PEAK))
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Surface {
    pub level: f64,
    pub pitch: f64,
    pub stones: f64,
    pub crunch: f64,
    pub q: f64,
    pub swell: f64,
    pub swell_hz: f64,
    pub seams: f64,
    pub seam_hz: f64,
    pub seam_q: f64,
    pub seam_share: f64,
}

const fn surface(level: f64, pitch: f64) -> Surface {
    Surface {
        level,
        pitch,
        stones: 0.0,
        crunch: 0.0,
        q: 1.2,
        swell: 0.25,
        swell_hz: 4.0,
        seams: 0.0,
        seam_hz: 90.0,
        seam_q: 1.5,
        seam_share: 0.0,
    }
}

const ASPHALT: Surface = Surface { q: 0.8, swell: 0.12, ..surface(0.3, 100.0) };
pub const SURFACES: [(&str, Surface); 16] = [
    ("road_smooth", Surface { q: 0.8, swell: 0.1, ..surface(0.2, 120.0) }),
    ("road_coarse", Surface { stones: 6.0, crunch: 0.3, q: 0.8, swell: 0.15, swell_hz: 6.0, ..surface(0.45, 95.0) }),
    ("road_dirt", Surface { stones: 1.5, crunch: 0.35, q: 1.5, swell: 0.45, swell_hz: 1.5, ..surface(0.55, 70.0) }),
    ("ice", Surface { q: 0.8, swell: 0.05, ..surface(0.15, 160.0) }),
    ("snow", Surface { stones: 2.0, crunch: 0.3, q: 1.0, swell: 0.35, swell_hz: 2.5, ..surface(0.45, 110.0) }),
    ("rumble", ASPHALT),
    ("road", ASPHALT),
    ("asphalt", ASPHALT),
    ("concrete", Surface { q: 0.8, swell: 0.12, seams: 4.6, seam_hz: 85.0, seam_share: 0.4, ..surface(0.35, 90.0) }),
    ("static", Surface { q: 0.8, swell: 0.12, ..surface(0.35, 90.0) }),
    (
        "wood",
        Surface { q: 1.0, swell: 0.2, seams: 0.2, seam_hz: 80.0, seam_q: 1.0, seam_share: 0.6, ..surface(0.55, 75.0) },
    ),
    (
        "metal",
        Surface { q: 1.2, swell: 0.08, seams: 0.15, seam_hz: 150.0, seam_q: 6.0, seam_share: 0.7, ..surface(0.6, 130.0) },
    ),
    ("dirt", Surface { stones: 1.5, crunch: 0.4, q: 1.5, swell: 0.45, swell_hz: 1.5, ..surface(0.75, 60.0) }),
    ("grass", Surface { q: 3.0, swell: 0.45, swell_hz: 1.5, ..surface(0.7, 200.0) }),
    ("gravel", Surface { stones: 3.0, crunch: 0.6, q: 2.5, swell: 0.3, swell_hz: 5.0, ..surface(1.0, 85.0) }),
    ("rough", Surface { stones: 2.0, crunch: 0.4, q: 1.5, swell: 0.45, swell_hz: 2.0, ..surface(0.9, 70.0) }),
];
pub const DEFAULT_SURFACE: Surface = Surface { stones: 0.5, crunch: 0.1, ..surface(0.4, 90.0) };
const PITCH_STANDING: f64 = 0.6;
const SEAM_SPEED: f64 = 22.0;
const SEAM_JITTER: f64 = 0.05;
const RATTLE_HZ: f64 = 55.0;
const RATTLE_RATE: f64 = 40.0;
const RATTLE_ROUGH: (f64, f64) = (0.04, 0.15);
const RUMBLE_SPACING: f64 = 0.3;
const RUMBLE_HOLD: f64 = 0.25;
const RUMBLE_SHAPE: (f64, f64, f64) = (0.45, 0.17, 0.11);
const RUMBLE_LEVEL: f64 = 1.078;
const TRAILER_FEEL: f64 = 0.5;
const BUMP_HZ: (f64, f64) = (65.0, 40.0);
const BUMP_DECAY: (f64, f64) = (0.025, 0.06);
const TRAILER_BUMP_HZ: f64 = 10.0;
const BUMP_RATTLE_FROM: f64 = 0.3;
const BUMP_RATTLE_S: f64 = 0.3;
const BUMP_RATTLE_MAX: usize = 16;
const BUMP_RESTRIKE_S: f64 = 0.02;
const BUMP_GAP_S: f64 = 0.06;
const BUMP_RIDE_TAU: f64 = 20.0;
const BUMP_RIDE_SPEED: f64 = 3.0;
const BUMP_OVER_RIDE: f64 = 12.0;
const BUMP_RIDE_MAX: f64 = 0.5;
const CAB_SWAY_HZ: f64 = 45.0;
const CAB_SWAY: [(f64, f64); 3] = [(0.06, 0.0), (0.14, 0.25), (0.6, 1.0)];
const CAB_SWAY_TAU: f64 = 0.025;
const CAB_FAR_SIDE: f64 = 0.5;
const SPIKE_MIN: f64 = 0.004;
const SPIKE_BACK: f64 = 0.3;
const LEAP_SLOPE: f64 = 0.5;
const LEAP_FLOOR: f64 = 0.5;
const LEAP_HOLD: f64 = 0.15;
const SHIFT_HZ: f64 = 140.0;
/// (s after the gear change, strength at gear gain 1, decay s)
const SHIFT_HITS: [(f64, f64, f64); 3] = [(0.02, 0.19, 0.16), (0.14, 0.95, 0.005), (0.14, 0.43, 0.08)];
const SHIFT_COOLDOWN: f64 = 0.15;
pub const AMT: &str = "amt";
pub const CONVERTER: &str = "converter";
pub const GEARBOXES: [&str; 2] = [AMT, CONVERTER];
const CONVERTER_SHIFT_HZ: f64 = CAB_SWAY_HZ;
const CONVERTER_SHIFT: (f64, f64) = (0.2, 0.9);
const CONVERTER_SHIFT_ATTACK: f64 = 0.06;
const CONVERTER_SHIFT_DECAY: f64 = 0.15;
/// per selector, range then splitter: (s after the flip, Hz, strength at gain 1, decay s)
const SHIFTER_HITS: [[(f64, f64, f64, f64); 5]; 2] = [
    [(0.0, 140.0, 1.0, 0.005), (0.13, 170.0, 0.38, 0.005), (0.13, 170.0, 0.23, 0.08), (0.17, 170.0, 0.63, 0.005),
     (0.17, 170.0, 0.38, 0.08)],
    [(0.016, 60.0, 1.0, 0.003), (0.12, 170.0, 0.133, 0.005), (0.12, 170.0, 0.08, 0.06), (0.18, 170.0, 0.133, 0.005),
     (0.18, 170.0, 0.08, 0.06)],
];
const AIR_BRAKE_THUMP: f64 = 0.25;
const AIR_BRAKE_HISS_DECAY: f64 = 0.105;
const AIR_BRAKE_HISS_HZ: f64 = 350.0;
const AIR_BRAKE_HISS_Q: f64 = 1.2;
const PEDAL_FLOOR: f64 = 0.4;
const AIR_BRAKE_HOLD: f64 = 0.3;
const AIR_BRAKE_ON: f64 = 0.1;
const AIR_BRAKE_OFF: f64 = 0.03;
const BLINKER_HZ: f64 = 190.0;
const BLINKER_DECAY: f64 = 0.003;
const TICK_SHARE: f64 = 1.0;
const TICK_EDGE: f64 = 0.005;

/// A click's ring above 500 Hz, drawn only at 2 kHz (set_events_2k): a sine from zero at `hz`
/// with band noise around it (RING_TONAL, RING_Q), falling by e every `decay` s, carrying `db`
/// more energy in 500-1000 Hz than its tick would alone (one sample at twice the tick's 1 kHz
/// height).
#[derive(Clone, Copy, Debug)]
struct Ring {
    hz: f64,
    decay: f64,
    db: f64,
}

// In the sound mods' clips a cab click does not stop at its edge: its 500-1000 Hz band rings on
// for tens of milliseconds, where a tick at 2 kHz stops after its half millisecond. Around the
// band's loudest 2 ms (from 20 ms before to 150 ms after) it holds 6-9 dB more energy than within
// them (median: shift 8.0, range 6.2, splitter 6.9, relay 6.1, coupling 8.7); the 2 kHz output
// held -7 to +6.5 (shift -6.8, relay -8.9). At 1 kHz the band was out of reach; at 2 kHz each
// tick gets its ring, `db` fitted so the effect tests measure the clips' median within 0.5 dB
// (the splitter's is high as its clacks' ticks are 12 dB under its knob's, which is the loudest
// edge). The coupling's knocks come every 20-40 ms, so its ring is fitted knock by knock over
// 15 ms instead (75 recorded knocks: 3.2 dB median) and decays as one knock's does. The other
// decays are energy-weighted over the 100 ms after the band's peak (1 ms envelope). The Eaton shifts ring at 680-709 Hz in all 12 clips, 7-16 dB over the rest of the
// band; the range shift goes through the same gearbox (its two clips peak at 527 and 750 Hz,
// either side). The splitter's two clips ring at 861 and 885 Hz. The relays (16 clips of three
// packs) each ring at their own pitch, 511-996 Hz, so the blinker takes their median; the fifth
// wheel's knocks (11 clips) at 504-926 Hz, so each knock takes one of its own.
//
// A ring is not one pure tone, though: in the clips the band's tail 3-60 ms after its peak holds
// only 21-47% of its energy within 30 Hz of its strongest line (shift median 28%, relays 32-41%,
// range and splitter 27-36%), the rest spread about 140 Hz either side of a 640-800 Hz centre.
// Drawn as a sine, all of it sat on one line and played as a spring's twang (felt on the pad
// 2026-11-08, shift and blinker). So RING_TONAL of its energy stays on the line and the rest
// is noise through a band of RING_Q around it. The decays are the tails' own over those 3-60 ms
// (1 ms envelope, log fit): shift 27 ms (12 clips), range 30 and splitter 32 (two each), relay
// 15 (the trucks' relays; Sound Fixes Pack's 416 Hz one is a buzzer and was left out).
// Drawn so, the shift test's tails hold 36% on the line (blinker 41%), spread about 100 Hz and
// fall as the clips' do (shift -5 dB at 10-25 ms, -12 at 25-60); 500-1000 Hz within 1 dB of the sine.
// Levels by ear (examples/ring_level, wireless, 0 late reports, 2026-11-09 00:03, the clips
// played first on the PC): shift and relay kept their fitted db (0, 0 dB both rounds); range
// and splitter -6 / -12 -> -9 dB, the coupling -12 / -6 -> -9 dB; the stones +6 twice (STONE_HI).
const RING_TONAL: f64 = 0.15;
const RING_Q: f64 = 1.0;
const SHIFT_RING: Ring = Ring { hz: 690.0, decay: 0.027, db: 2.9 };
const RANGE_RING: Ring = Ring { hz: 690.0, decay: 0.030, db: -7.0 };
const SPLIT_RING: Ring = Ring { hz: 870.0, decay: 0.032, db: 8.1 };
const BLINKER_RING: Ring = Ring { hz: 730.0, decay: 0.015, db: 5.2 };
const COUPLE_RING_HZ: (f64, f64) = (504.0, 926.0);
const COUPLE_RING: Ring = Ring { hz: 700.0, decay: 0.013, db: -6.9 };

// At 2 kHz the stones click above 500 Hz too. In Sound Fixes Pack's five gravel loops the stones
// stand out in 500-1000 Hz 9-17 times a second and hold 20-57% of its energy, while 40-250 Hz
// stays plain noise (kurtosis 2-4) and peaks with only 0-48% of them: what tells gravel apart
// lies up there. Each click is flat from 400 to 1000 Hz within 6 dB, 8-12 dB down at 1.5 kHz,
// and falls by e in 4-5 ms. The stones' body rings at the surface's pitch inside the bed's
// 300 Hz low-pass, so each stone now also strikes a burst of noise through a band-pass over that
// band, as strong against its body as STONE_HI says. Fitted at 0.14 (the clips' share less the
// house -6 dB); picked by ear at +6 dB both rounds (2026-11-09, the top of the levels offered).
const STONE_HZ: f64 = 700.0;
const STONE_Q: f64 = 0.9;
const STONE_DECAY: f64 = 0.0045;
const STONE_HI: f64 = 0.28;
const COUPLE_S: f64 = 0.6;
const COUPLE_GAP: (f64, f64) = (0.02, 0.04);
const COUPLE_MAIN: (f64, f64) = (0.15, 0.45);
const COUPLE_HZ: (f64, f64) = (85.0, 150.0);
const COUPLE_LOUDEST: f64 = 1.22;
const COUPLE_KNOCKS: (f64, f64) = (0.25, 0.7);
const COUPLE_DECAY: f64 = 0.004;
const ENGINE_FIRING_HZ: f64 = 80.0;
const ENGINE_FIRING_DECAY: f64 = 0.012;
const ENGINE_START_PEAK: f64 = 1.18;
const ENGINE_CRANK: [(f64, i32); 6] = [(0.311, -14), (0.476, -10), (0.588, -14), (0.71, -14), (0.827, -12), (0.947, -16)];
const ENGINE_CATCH_AT: f64 = 1.04;
const ENGINE_CATCH: [(f64, i32); 16] = [
    (0.0, -15), (0.053, -10), (0.095, -10), (0.138, -6), (0.173, -8), (0.205, 0), (0.235, -4), (0.262, -5),
    (0.29, 0), (0.317, 0), (0.343, -4), (0.392, -7), (0.418, -9), (0.451, -8), (0.488, -12), (0.535, -17),
];
const ENGINE_STOP_PEAK: f64 = 0.73;
#[allow(clippy::approx_constant)] // 0.318 s is a recorded firing, not 1/pi
const ENGINE_STOP: [(f64, i32); 18] = [
    (0.003, -4), (0.033, -1), (0.068, -2), (0.103, -1), (0.138, -4), (0.173, 0), (0.209, -5), (0.243, -1),
    (0.279, -2), (0.318, -4), (0.357, -12), (0.384, -13), (0.42, -12), (0.486, -14), (0.515, -16), (0.627, -16),
    (0.652, -15), (0.677, -17),
];
const DUCK_FULL: f64 = 0.25;
const DUCK_LOOKAHEAD: f64 = 0.02;

fn duck_attack() -> f64 {
    1.0 - m::exp(-1.0 / (0.001 * SR))
}

fn duck_release() -> f64 {
    m::exp(-1.0 / (0.08 * SR))
}

fn db(db: i32) -> f64 {
    // 10 ** (db / 20)
    m::pow(10.0, db as f64 / 20.0)
}

/// The texture of a substance; None for no ground at all.
pub fn surface_of(name: &str) -> Option<Surface> {
    let name = name.to_lowercase();
    if name.is_empty() {
        return None;
    }
    Some(SURFACES.iter().find(|(key, _)| name.contains(key)).map(|(_, s)| *s).unwrap_or(DEFAULT_SURFACE))
}

/// One side's mix of surfaces: mean strength, the rest weighted by strength; seams are the
/// strongest seamed surface's, not averaged.
pub fn side_surface(wheels: &[&Wheel]) -> Surface {
    let found: Vec<Surface> = wheels.iter().filter_map(|w| surface_of(&w.substance)).collect();
    let total = m::sum(found.iter().map(|s| s.level));
    if total == 0.0 {
        return surface(0.0, DEFAULT_SURFACE.pitch);
    }
    let mut seamed = found[0];
    for s in &found[1..] {
        if s.level * s.seam_share > seamed.level * seamed.seam_share {
            seamed = *s;
        }
    }
    let blend = |field: fn(&Surface) -> f64| m::sum(found.iter().map(|s| s.level * field(s))) / total;
    Surface {
        level: total / wheels.len() as f64,
        seams: seamed.seams,
        seam_hz: seamed.seam_hz,
        seam_q: seamed.seam_q,
        pitch: blend(|s| s.pitch),
        stones: blend(|s| s.stones),
        crunch: blend(|s| s.crunch),
        q: blend(|s| s.q),
        swell: blend(|s| s.swell),
        swell_hz: blend(|s| s.swell_hz),
        seam_share: blend(|s| s.seam_share),
    }
}

/// The effects a player can switch off and route, in the window's order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Effect {
    Engine,
    Road,
    Bumps,
    Rumble,
    Wheelspin,
    Gear,
    Retarder,
    Impact,
    Trigger,
    Pedal,
    Squeal,
    Trailer,
    ParkBrake,
    AirBrake,
    Blinker,
    EngineStart,
    Shifter,
    GameEvents,
}

pub const TOGGLEABLE: [Effect; 18] = [
    Effect::Engine,
    Effect::Road,
    Effect::Bumps,
    Effect::Rumble,
    Effect::Wheelspin,
    Effect::Gear,
    Effect::Retarder,
    Effect::Impact,
    Effect::Trigger,
    Effect::Pedal,
    Effect::Squeal,
    Effect::Trailer,
    Effect::ParkBrake,
    Effect::AirBrake,
    Effect::Blinker,
    Effect::EngineStart,
    Effect::Shifter,
    Effect::GameEvents,
];

impl Effect {
    pub fn name(self) -> &'static str {
        match self {
            Effect::Engine => "engine",
            Effect::Road => "road",
            Effect::Bumps => "bumps",
            Effect::Rumble => "rumble",
            Effect::Wheelspin => "wheelspin",
            Effect::Gear => "gear",
            Effect::Retarder => "retarder",
            Effect::Impact => "impact",
            Effect::Trigger => "trigger",
            Effect::Pedal => "pedal",
            Effect::Squeal => "squeal",
            Effect::Trailer => "trailer",
            Effect::ParkBrake => "park_brake",
            Effect::AirBrake => "air_brake",
            Effect::Blinker => "blinker",
            Effect::EngineStart => "engine_start",
            Effect::Shifter => "shifter",
            Effect::GameEvents => "game_events",
        }
    }

    pub fn from_name(name: &str) -> Option<Effect> {
        TOGGLEABLE.iter().copied().find(|e| e.name() == name)
    }

    fn index(self) -> usize {
        self as usize
    }

    pub fn default_route(self) -> Channel {
        match self {
            Effect::Trigger | Effect::Pedal | Effect::Squeal => Channel::Trigger,
            _ => Channel::Grip,
        }
    }

    /// Bumps come from the road itself and the brakes vent on every release, so neither ducks the bed.
    fn duck_exempt(self) -> bool {
        matches!(self, Effect::Bumps | Effect::AirBrake)
    }
}

/// Where an effect plays: GRIP, TRIGGER or BOTH.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Channel {
    Grip,
    Trigger,
    Both,
}

impl Channel {
    pub fn name(self) -> &'static str {
        match self {
            Channel::Grip => "grip",
            Channel::Trigger => "trigger",
            Channel::Both => "both",
        }
    }

    pub fn from_name(name: &str) -> Option<Channel> {
        match name {
            "grip" => Some(Channel::Grip),
            "trigger" => Some(Channel::Trigger),
            "both" => Some(Channel::Both),
            _ => None,
        }
    }
}

// mix slots: left grip, right grip, then the trigger channel's three
const BOTH_TRIGGERS: usize = 2;
const BRAKE_TRIGGER: usize = 3;
const THROTTLE_TRIGGER: usize = 4;
const TRIGGER_SLOTS: [usize; 3] = [BOTH_TRIGGERS, BRAKE_TRIGGER, THROTTLE_TRIGGER];

pub fn default_routes() -> IndexMap<String, String> {
    TOGGLEABLE.iter().map(|e| (e.name().to_string(), e.default_route().name().to_string())).collect()
}

/// Effect strengths and options, as the window edits them and config.json keeps them.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Gains {
    pub master: f64,
    pub grip_level: f64,
    pub trigger_level: f64,
    pub engine: f64,
    pub road: f64,
    pub bumps: f64,
    pub rumble: f64,
    pub wheelspin: f64,
    pub gear: f64,
    pub retarder: f64,
    pub impact: f64,
    pub trigger: f64,
    pub throttle_pulse: f64,
    pub steady_fade: f64,
    pub cylinders: i64,
    pub gearbox: String,
    pub bump_threshold: f64,
    pub impact_threshold: f64,
    pub pedal: f64,
    pub squeal: f64,
    pub trailer: f64,
    pub park_brake: f64,
    pub engine_start: f64,
    pub shifter: f64,
    pub air_brake: f64,
    pub blinker: f64,
    pub game_events: f64,
    pub ducking: f64,
    /// effects switched off; their gains are kept so switching back restores them
    pub disabled: Vec<String>,
    /// effect -> grip, trigger or both; missing or unknown entries use the effect's default
    pub routes: IndexMap<String, String>,
}

impl Default for Gains {
    fn default() -> Gains {
        Gains {
            master: 1.0,
            grip_level: 1.0,
            trigger_level: 1.0,
            engine: 0.1,
            road: 0.5,
            bumps: 1.0,
            rumble: 0.8,
            wheelspin: 0.7,
            gear: 0.6,
            retarder: 0.3,
            impact: 1.0,
            trigger: 0.2,
            throttle_pulse: 0.8,
            steady_fade: 0.6,
            cylinders: 6,
            gearbox: AMT.to_string(),
            bump_threshold: 0.2,
            impact_threshold: 12.0,
            pedal: 0.6,
            squeal: 0.4,
            trailer: 0.9,
            park_brake: 0.7,
            engine_start: 0.8,
            shifter: 0.7,
            air_brake: 0.3,
            blinker: 0.5,
            game_events: 0.5,
            ducking: 0.7,
            disabled: Vec::new(),
            routes: default_routes(),
        }
    }
}

/// The float fields by name, in the dataclass's order.
pub const FLOAT_FIELDS: [&str; 26] = [
    "master", "grip_level", "trigger_level", "engine", "road", "bumps", "rumble", "wheelspin", "gear", "retarder",
    "impact", "trigger", "throttle_pulse", "steady_fade", "bump_threshold", "impact_threshold", "pedal", "squeal",
    "trailer", "park_brake", "engine_start", "shifter", "air_brake", "blinker", "game_events", "ducking",
];

impl Gains {
    pub fn float_mut(&mut self, name: &str) -> Option<&mut f64> {
        Some(match name {
            "master" => &mut self.master,
            "grip_level" => &mut self.grip_level,
            "trigger_level" => &mut self.trigger_level,
            "engine" => &mut self.engine,
            "road" => &mut self.road,
            "bumps" => &mut self.bumps,
            "rumble" => &mut self.rumble,
            "wheelspin" => &mut self.wheelspin,
            "gear" => &mut self.gear,
            "retarder" => &mut self.retarder,
            "impact" => &mut self.impact,
            "trigger" => &mut self.trigger,
            "throttle_pulse" => &mut self.throttle_pulse,
            "steady_fade" => &mut self.steady_fade,
            "bump_threshold" => &mut self.bump_threshold,
            "impact_threshold" => &mut self.impact_threshold,
            "pedal" => &mut self.pedal,
            "squeal" => &mut self.squeal,
            "trailer" => &mut self.trailer,
            "park_brake" => &mut self.park_brake,
            "engine_start" => &mut self.engine_start,
            "shifter" => &mut self.shifter,
            "air_brake" => &mut self.air_brake,
            "blinker" => &mut self.blinker,
            "game_events" => &mut self.game_events,
            "ducking" => &mut self.ducking,
            _ => return None,
        })
    }

    pub fn float(&self, name: &str) -> Option<f64> {
        self.clone().float_mut(name).map(|v| *v)
    }

    /// The strength of a toggleable effect.
    pub fn gain(&self, effect: Effect) -> f64 {
        match effect {
            Effect::Engine => self.engine,
            Effect::Road => self.road,
            Effect::Bumps => self.bumps,
            Effect::Rumble => self.rumble,
            Effect::Wheelspin => self.wheelspin,
            Effect::Gear => self.gear,
            Effect::Retarder => self.retarder,
            Effect::Impact => self.impact,
            Effect::Trigger => self.trigger,
            Effect::Pedal => self.pedal,
            Effect::Squeal => self.squeal,
            Effect::Trailer => self.trailer,
            Effect::ParkBrake => self.park_brake,
            Effect::AirBrake => self.air_brake,
            Effect::Blinker => self.blinker,
            Effect::EngineStart => self.engine_start,
            Effect::Shifter => self.shifter,
            Effect::GameEvents => self.game_events,
        }
    }

    pub fn route_of(&self, effect: Effect) -> Channel {
        self.routes.get(effect.name()).and_then(|r| Channel::from_name(r)).unwrap_or(effect.default_route())
    }

    /// The gains the synth should use: disabled effects at zero.
    pub fn effective(&self) -> Gains {
        let mut result = self.clone();
        for name in &self.disabled {
            if Effect::from_name(name).is_some() {
                if let Some(value) = result.float_mut(name) {
                    *value = 0.0;
                }
            }
        }
        result
    }
}

pub const GAME_EVENT_LABELS: [(&str, &str); 6] = [
    ("delivered", "배송 완료"),
    ("fined", "벌금"),
    ("tollgate", "톨게이트"),
    ("ferry", "페리"),
    ("train", "기차"),
    ("refuel", "주유"),
];

/// Every block looks up the eq of each effect sounding, so it is kept by frequency.
const PAIR_CACHE: usize = 512;
const FLAT: (f64, f64) = (1.0, 1.0);

fn biquad_magnitude(b: [f64; 3], a: [f64; 2], freq: f64) -> f64 {
    m::biquad_magnitude(b, a, TAU, freq, SR)
}

/// A motor's resonance taken back out of its drive: a peaking cut (RBJ biquad).
#[derive(Clone, Debug)]
pub struct ResonanceCut {
    pub center: f64,
    pub depth_db: f64,
    pub q: f64,
    rate: f64,
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl ResonanceCut {
    pub fn new(center: f64, depth_db: f64, q: f64) -> ResonanceCut {
        ResonanceCut::at_rate(center, depth_db, q, SR)
    }

    /// The same cut for samples at `rate` Hz (the 2 kHz output's ticks).
    pub fn at_rate(center: f64, depth_db: f64, q: f64, rate: f64) -> ResonanceCut {
        let a = m::pow(10.0, -depth_db / 40.0);
        let w = TAU * center / rate;
        let alpha = m::sin(w) / (2.0 * q);
        let a0 = 1.0 + alpha / a;
        ResonanceCut {
            center,
            depth_db,
            q,
            rate,
            b0: (1.0 + alpha * a) / a0,
            b1: -2.0 * m::cos(w) / a0,
            b2: (1.0 - alpha * a) / a0,
            a1: -2.0 * m::cos(w) / a0,
            a2: (1.0 - alpha / a) / a0,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    pub fn magnitude(&self, freq: f64) -> f64 {
        m::biquad_magnitude([self.b0, self.b1, self.b2], [self.a1, self.a2], TAU, freq, self.rate)
    }

    pub fn next(&mut self, x: f64) -> f64 {
        let mut y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2;
        if x == 0.0 && y.abs() < 1e-6 && self.y1.abs() < 1e-6 {
            y = 0.0; // rung out: back to exact silence, which mutes the trigger route
        }
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
}

const BED_LOWPASS_HZ: f64 = 300.0;

/// A 2nd-order low-pass (RBJ biquad), for the bed.
#[derive(Clone, Debug)]
pub struct LowPass {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl Default for LowPass {
    fn default() -> LowPass {
        LowPass::new(BED_LOWPASS_HZ, 1.0 / 2.0f64.sqrt())
    }
}

impl LowPass {
    pub fn new(corner: f64, q: f64) -> LowPass {
        let w = TAU * corner / SR;
        let alpha = m::sin(w) / (2.0 * q);
        let cos = m::cos(w);
        let a0 = 1.0 + alpha;
        let b0 = (1.0 - cos) / 2.0 / a0;
        LowPass {
            b0,
            b1: (1.0 - cos) / a0,
            b2: b0,
            a1: -2.0 * cos / a0,
            a2: (1.0 - alpha) / a0,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    pub fn magnitude(&self, freq: f64) -> f64 {
        biquad_magnitude([self.b0, self.b1, self.b2], [self.a1, self.a2], freq)
    }

    pub fn next(&mut self, x: f64) -> f64 {
        let mut y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2;
        if x == 0.0 && self.x1 == 0.0 && y.abs() < 1e-6 && self.y1.abs() < 1e-6 {
            y = 0.0; // rung out: back to exact silence, which mutes the trigger route
        }
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
}

/// Python's float(x) of a JSON value; None where it would raise.
pub fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => py_float_str(s),
        _ => None,
    }
}

/// Python's float(str): surrounding whitespace and digit-separating underscores allowed.
pub fn py_float_str(s: &str) -> Option<f64> {
    let s = s.trim();
    if s.starts_with('_') || s.ends_with('_') || s.contains("__") {
        return None;
    }
    s.replace('_', "").parse::<f64>().ok()
}

/// How much more drive each frequency needs to feel as strong as the reference, per output.
#[derive(Clone, Debug)]
pub struct Equalizer {
    /// as given, for the debug log to keep
    pub curves: serde_json::Map<String, Value>,
    points: [Vec<(f64, f64)>; 2], // grip, trigger: (ln Hz, ln gain + ln LEVEL)
    pub flat: bool,
    pub resonances: [Option<(f64, f64, f64)>; 2],
    shapes: [Option<ResonanceCut>; 2],
    pairs: HashMap<u64, (f64, f64)>,
}

pub const EQ_CHANNELS: [&str; 2] = ["grip", "trigger"];
const EQ_LIMITS: (f64, f64) = (0.25, 4.0);
const RESONANCE_MIN_DB: f64 = 3.0;
const RESONANCE_Q: (f64, f64) = (1.0, 8.0);
const EQ_LEVEL: [f64; 2] = [0.4, 1.0];

impl Default for Equalizer {
    fn default() -> Equalizer {
        Equalizer::new(&serde_json::Map::new())
    }
}

impl Equalizer {
    /// Matched by feel on the developer's APEX (2026-10-25), then smoothed.
    pub fn default_curves() -> serde_json::Map<String, Value> {
        serde_json::from_str(
            r#"{"grip": {"30": 3.89, "40": 3.55, "50": 2.94, "65": 1.0, "80": 2.27, "100": 2.98, "130": 3.47,
                         "160": 3.3, "200": 3.16, "250": 3.27},
               "trigger": {"50": 4.0, "65": 3.63, "80": 2.7, "100": 1.0, "130": 2.27, "160": 2.1, "200": 2.15,
                           "250": 2.12}}"#,
        )
        .unwrap()
    }

    /// The player's own calibration, and the default for a motor they have not measured.
    pub fn for_player(own: &serde_json::Map<String, Value>) -> Equalizer {
        let mut curves = Equalizer::default_curves();
        for (channel, curve) in own {
            curves.insert(channel.clone(), curve.clone());
        }
        Equalizer::new(&curves)
    }

    pub fn new(curves: &serde_json::Map<String, Value>) -> Equalizer {
        let mut points: [Vec<(f64, f64)>; 2] = [Vec::new(), Vec::new()];
        for (c, channel) in EQ_CHANNELS.iter().enumerate() {
            let level = m::log(EQ_LEVEL[c]);
            if let Some(Value::Object(stored)) = curves.get(*channel) {
                for (freq, gain) in stored {
                    let (Some(freq), Some(gain)) = (py_float_str(freq), py_float(gain)) else { continue };
                    if freq > 0.0 && gain > 0.0 {
                        let clamped = m::min(m::max(gain, EQ_LIMITS.0), EQ_LIMITS.1);
                        points[c].push((m::log(freq), m::log(clamped) + level));
                    }
                }
            }
            points[c].sort_by(|a, b| a.partial_cmp(b).unwrap());
        }
        let flat = points.iter().all(|p| p.is_empty());
        let resonances = [resonance(&points[0]), resonance(&points[1])];
        let shapes = resonances.map(|found| found.map(|(c, d, q)| ResonanceCut::new(c, d, q)));
        Equalizer { curves: curves.clone(), points, flat, resonances, shapes, pairs: HashMap::new() }
    }

    /// A fresh filter for one output of this channel; None when its curve shows no resonance.
    pub fn cut(&self, channel: usize) -> Option<ResonanceCut> {
        self.resonances[channel].map(|(c, d, q)| ResonanceCut::new(c, d, q))
    }

    /// cut() for samples at `rate` Hz.
    pub fn cut_at(&self, channel: usize, rate: f64) -> Option<ResonanceCut> {
        self.resonances[channel].map(|(c, d, q)| ResonanceCut::at_rate(c, d, q, rate))
    }

    /// The curve: how much a steady tone at freq is driven, all told.
    pub fn gain(&self, channel: usize, freq: f64) -> f64 {
        let points = &self.points[channel];
        if points.is_empty() || freq <= 0.0 {
            return 1.0;
        }
        let x = m::log(freq);
        if x <= points[0].0 {
            return m::exp(points[0].1);
        }
        if x >= points[points.len() - 1].0 {
            return m::exp(points[points.len() - 1].1);
        }
        let i = points.partition_point(|p| p.0 <= x);
        let ((x0, y0), (x1, y1)) = (points[i - 1], points[i]);
        m::exp(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    }

    /// (grip, trigger) gain for a vibration at freq, ahead of its channel's ResonanceCut.
    pub fn pair(&mut self, freq: f64) -> (f64, f64) {
        if self.flat {
            return FLAT;
        }
        if let Some(found) = self.pairs.get(&freq.to_bits()) {
            return *found;
        }
        if self.pairs.len() >= PAIR_CACHE {
            self.pairs.clear();
        }
        let one = |c: usize| {
            self.gain(c, freq) / self.shapes[c].as_ref().map_or(1.0, |shape| shape.magnitude(freq))
        };
        let found = (one(0), one(1));
        self.pairs.insert(freq.to_bits(), found);
        found
    }
}

/// (Hz, dB, Q) of the resonance a curve shows: its lowest point against the median of the
/// points from half an octave above, as wide as the points within an octave of it say.
fn resonance(points: &[(f64, f64)]) -> Option<(f64, f64, f64)> {
    if points.len() < 4 {
        return None;
    }
    let mut lowest = points[0];
    for p in &points[1..] {
        if p.1 < lowest.1 {
            lowest = *p;
        }
    }
    let (x0, y0) = lowest;
    let mut above: Vec<f64> = points.iter().filter(|(x, _)| *x >= x0 + m::log(2.0) / 2.0).map(|(_, y)| *y).collect();
    if above.len() < 2 {
        return None;
    }
    above.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = above.len();
    let floor = (above[(n - 1) / 2] + above[n / 2]) / 2.0;
    let depth = (floor - y0) * 20.0 / m::log(10.0);
    if depth < RESONANCE_MIN_DB {
        return None;
    }
    let center = m::exp(x0);
    let near: Vec<(f64, f64)> = points
        .iter()
        .filter(|(x, _)| {
            let d = (x - x0).abs();
            0.0 < d && d <= m::log(2.0)
        })
        .map(|&(x, y)| (m::exp(x), m::max(0.0, (floor - y) * 20.0 / m::log(10.0))))
        .collect();
    let (low, high) = RESONANCE_Q;
    let misfit = |q: f64| {
        let shape = ResonanceCut::new(center, depth, q);
        m::sum(near.iter().map(|&(freq, lift)| m::pow(-20.0 * m::log10(shape.magnitude(freq)) - lift, 2.0)))
    };
    let steps = m::round_int((high - low) / 0.1);
    let mut best = (low, misfit(low));
    for n in 1..=steps {
        let q = low + n as f64 * 0.1;
        let score = misfit(q);
        if score < best.1 {
            best = (q, score);
        }
    }
    Some((center, depth, best.0))
}

#[derive(Clone, Debug, Default)]
pub struct Oscillator {
    pub phase: f64,
}

impl Oscillator {
    pub fn next(&mut self, freq: f64) -> f64 {
        let value = m::sin(TAU * self.phase);
        self.phase = m::rem(self.phase + freq / SR, 1.0);
        value
    }
}

/// One firing cycle of the engine brake's knock: peak 1 at JAKE_PEAK, zero mean.
fn jake_pulse(phase: f64, mean: f64) -> f64 {
    let u = phase / JAKE_PEAK;
    (u * m::exp(1.0 - u) - mean) / (1.0 - mean)
}

/// 2-pole band-pass (RBJ biquad, 0 dB at the centre); it can be retuned between blocks.
#[derive(Clone, Debug)]
pub struct Resonator {
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
    pub center: f64,
    pub q: f64,
    /// samples per second it is tuned for
    rate: f64,
    b0: f64,
    b2: f64,
    a1: f64,
    a2: f64,
}

impl Resonator {
    pub fn new(center: f64, q: f64) -> Resonator {
        let mut r = Resonator {
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
            center: 0.0,
            q: 0.0,
            rate: SR,
            b0: 0.0,
            b2: 0.0,
            a1: 0.0,
            a2: 0.0,
        };
        r.tune(center, q);
        r
    }

    /// The same band for samples at `rate` Hz, ringing on from where it was.
    pub fn set_rate(&mut self, rate: f64) {
        if rate != self.rate {
            self.rate = rate;
            let (center, q) = (self.center, self.q);
            self.center = 0.0;
            self.tune(center, q);
        }
    }

    /// A band at `rate` Hz as given there, not as it would play at 1 kHz (for bands above 500 Hz).
    pub fn native(center: f64, q: f64, rate: f64) -> Resonator {
        let mut r = Resonator::new(center, q);
        r.rate = rate;
        r.coefficients(center, q);
        r
    }

    /// center and q as at 1 kHz; at another rate the band keeps the edges it has there.
    pub fn tune(&mut self, center: f64, q: f64) {
        if center == self.center && q == self.q {
            return;
        }
        self.center = center;
        self.q = q;
        let (center, q) = if self.rate == SR { (center, q) } else { same_band(center, q, self.rate) };
        self.coefficients(center, q);
    }

    fn coefficients(&mut self, center: f64, q: f64) {
        let w = TAU * center / self.rate;
        let alpha = m::sin(w) / (2.0 * q);
        let a0 = 1.0 + alpha;
        self.b0 = alpha / a0;
        self.b2 = -alpha / a0;
        self.a1 = -2.0 * m::cos(w) / a0;
        self.a2 = (1.0 - alpha) / a0;
    }

    fn reset(&mut self) {
        (self.x1, self.x2, self.y1, self.y2) = (0.0, 0.0, 0.0, 0.0);
    }

    pub fn next(&mut self, x: f64) -> f64 {
        let y = self.b0 * x + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }

    /// Largest swing this tuning rings with after a unit impulse.
    pub fn impulse_peak(&self) -> f64 {
        let (mut x1, mut x2, mut y1, mut y2) = (0.0, 0.0, 0.0, 0.0);
        let mut peak = 0.0;
        for n in 0..((self.rate / self.center) as i64 + 2) {
            let x = if n == 0 { 1.0 } else { 0.0 };
            let y = self.b0 * x + self.b2 * x2 - self.a1 * y1 - self.a2 * y2;
            x2 = x1;
            x1 = x;
            y2 = y1;
            y1 = y;
            peak = m::max(peak, f64::abs(y));
        }
        let _ = x1;
        peak
    }
}

/// (centre, Q) at `rate` of the band a (centre, Q) plays at 1 kHz. Near 500 Hz the 1 kHz band
/// is squeezed (350 Hz at Q 1.2 spans 292-396 Hz there, 247-473 at 2 kHz with the same Q), and
/// the effects were tuned as they played. The same centre, where the 1 kHz band peaks, and the
/// same skirt below it, where the motors are felt: within 0.7 dB of the 1 kHz band up to 300 Hz
/// for every band the events use. Above, only the 1 kHz null at 500 Hz is gone (350 Hz, Q 1.2:
/// 282-427 Hz).
fn same_band(center: f64, q: f64, rate: f64) -> (f64, f64) {
    // well under the band, either rises as pi f / rate / (tan(pi centre / rate) Q)
    (center, q * (m::PI * center / SR).tan() * SR / (rate * (m::PI * center / rate).tan()))
}

/// White noise through a band-pass.
#[derive(Clone)]
pub struct BandNoise {
    pub filter: Resonator,
    rng: PyRandom,
}

impl BandNoise {
    pub fn new(center: f64, q: f64, seed: u64) -> BandNoise {
        BandNoise { filter: Resonator::new(center, q), rng: PyRandom::new(seed) }
    }

    pub fn next(&mut self) -> f64 {
        let x = self.rng.uniform(-1.0, 1.0);
        self.filter.next(x) * 2.5
    }

    /// One sample at `rate` Hz. Faster, the same noise spreads over a wider band and less of
    /// it falls in the filter's: it is raised to keep its level within the band.
    pub fn next_at(&mut self, rate: f64) -> f64 {
        self.filter.set_rate(rate);
        if rate == SR {
            return self.next();
        }
        let x = self.rng.uniform(-1.0, 1.0);
        self.filter.next(x) * 2.5 * (rate / SR).sqrt()
    }
}

/// Random clicks, each ringing the band-pass with a peak up to 1.
#[derive(Clone)]
struct Grains {
    filter: Resonator,
    rng: PyRandom,
    rate: f64,
    gain: f64,
    /// the click struck on the last sample, before the band-pass (0 when none)
    last: f64,
}

const GRAINS_Q: f64 = 1.5;

impl Grains {
    fn new(seed: u64, center: f64) -> Grains {
        let filter = Resonator::new(center, GRAINS_Q);
        let gain = 1.0 / filter.impulse_peak();
        Grains { filter, rng: PyRandom::new(seed), rate: 0.0, gain, last: 0.0 }
    }

    fn tune(&mut self, center: f64, rate: f64) {
        if center != self.filter.center {
            self.filter.tune(center, GRAINS_Q);
            self.gain = 1.0 / self.filter.impulse_peak();
        }
        self.rate = rate;
    }

    fn next(&mut self) -> f64 {
        let mut click = 0.0;
        if self.rate != 0.0 && self.rng.random() < self.rate / SR {
            let r = self.rng.random();
            let sign = self.rng.choice(&[-1.0, 1.0]);
            click = (0.2 + 0.8 * m::pow(r, 3.0)) * self.gain * sign;
        }
        self.last = click / self.gain;
        self.filter.next(click)
    }
}

/// Seams a set distance apart under a rolling tyre, each a knock ringing the band-pass.
#[derive(Clone)]
struct Seams {
    filter: Resonator,
    rng: PyRandom,
    spacing: f64,
    step: f64,
    travel: f64,
    next_at: f64,
    gain: f64,
}

impl Seams {
    fn new(seed: u64) -> Seams {
        Seams {
            filter: Resonator::new(90.0, 1.5),
            rng: PyRandom::new(seed),
            spacing: 0.0,
            step: 0.0,
            travel: 0.0,
            next_at: 1.0,
            gain: 0.0,
        }
    }

    fn tune(&mut self, spacing: f64, center: f64, q: f64, speed: f64) {
        if (center != self.filter.center || q != self.filter.q) || spacing != self.spacing {
            self.filter.tune(center, q);
            self.gain = if spacing != 0.0 { 1.0 / self.power(spacing).sqrt() } else { 0.0 };
            if spacing != self.spacing {
                self.travel = 0.0;
                self.next_at = spacing;
            }
        }
        self.spacing = spacing;
        self.step = speed / SR;
    }

    /// Mean square of unit knocks spacing apart at SEAM_SPEED, played through.
    fn power(&self, spacing: f64) -> f64 {
        let mut ring = Resonator::new(self.filter.center, self.filter.q);
        let period = spacing * SR / SEAM_SPEED;
        let (mut total, mut count, mut until) = (0.0, 0i64, period);
        let settle = SAMPLE_RATE as i64 / 2;
        let end = settle + (SAMPLE_RATE as i64).max(m::round_int(4.0 * period));
        for n in 0..end {
            let mut knock = 0.0;
            if n as f64 >= until {
                until += period;
                knock = 1.0;
            }
            let y = ring.next(knock);
            if n >= settle {
                total += y * y;
                count += 1;
            }
        }
        total / count as f64
    }

    fn next(&mut self) -> f64 {
        let mut knock = 0.0;
        if self.spacing != 0.0 {
            self.travel += self.step;
            if self.travel >= self.next_at {
                self.travel -= self.next_at;
                self.next_at = self.spacing * (1.0 + SEAM_JITTER * self.rng.uniform(-1.0, 1.0));
                knock = self.gain * (0.85 + 0.3 * self.rng.random());
            }
        }
        self.filter.next(knock)
    }
}

/// Slow random gain around 1: white noise through two one-pole low-passes.
#[derive(Clone)]
struct Swell {
    rng: PyRandom,
    y1: f64,
    y2: f64,
    depth: f64,
    hz: f64,
    a: f64,
    scale: f64,
    rms: f64,
}

impl Swell {
    fn new(seed: u64) -> Swell {
        let mut s = Swell { rng: PyRandom::new(seed), y1: 0.0, y2: 0.0, depth: 0.0, hz: 0.0, a: 0.0, scale: 0.0, rms: 0.0 };
        s.tune(0.25, 4.0);
        s
    }

    fn tune(&mut self, depth: f64, hz: f64) {
        if depth == self.depth && hz == self.hz {
            return;
        }
        self.depth = depth;
        self.hz = hz;
        let a = m::exp(-TAU * hz / SR);
        self.a = a;
        // deviation of uniform noise (variance 1/3) after the two poles
        self.scale = depth / (m::pow(1.0 - a, 4.0) * (1.0 + a * a) / m::pow(1.0 - a * a, 3.0) / 3.0).sqrt();
        self.rms = (1.0 + m::pow(depth, 2.0)).sqrt();
    }

    fn next(&mut self) -> f64 {
        let noise = self.rng.uniform(-1.0, 1.0);
        self.y1 = self.a * self.y1 + (1.0 - self.a) * noise;
        self.y2 = self.a * self.y2 + (1.0 - self.a) * self.y1;
        m::max(0.0, 1.0 + self.scale * self.y2)
    }
}

/// One side's road texture: noise around the surface's pitch, stones clicking, seams at a
/// steady beat, the cab rattling on rough ground, all swelling as the surface does.
#[derive(Clone)]
struct Texture {
    noise: BandNoise,
    stones: Grains,
    rattle: Grains,
    seams: Seams,
    swell: Swell,
    crunch: f64,
    noise_gain: f64,
    hiss: f64,
    seam_gain: f64,
    pitch: f64,
    /// the stone struck on the last sample, as strong as its body rings (0 when none)
    stone_hit: f64,
}

const STONE_GAIN: f64 = 5.0;
const RATTLE_GAIN: f64 = 3.0;
const NOISE_RMS: f64 = 0.6;

impl Texture {
    fn new(seed: u64) -> Texture {
        Texture {
            noise: BandNoise::new(DEFAULT_SURFACE.pitch, 1.2, seed),
            stones: Grains::new(seed + 100, 100.0),
            rattle: Grains::new(seed + 200, RATTLE_HZ),
            seams: Seams::new(seed + 400),
            swell: Swell::new(seed + 300),
            crunch: 0.0,
            noise_gain: 1.0,
            hiss: 1.0,
            seam_gain: 0.0,
            pitch: DEFAULT_SURFACE.pitch,
            stone_hit: 0.0,
        }
    }

    fn tune(&mut self, surface: &Surface, speed: f64, roughness: f64) {
        let pace = m::min(1.0, speed / 25.0);
        let pitch = surface.pitch * (PITCH_STANDING + (1.0 - PITCH_STANDING) * pace);
        self.pitch = pitch;
        self.noise.filter.tune(pitch, surface.q);
        // keep the strength of the 80 Hz, Q 1.2 band every surface used to share
        self.noise_gain = (80.0 / pitch * surface.q / 1.2).sqrt();
        self.stones.tune(pitch * 1.25, surface.stones * speed);
        self.crunch = surface.crunch;
        self.hiss = (1.0 - surface.seam_share).sqrt();
        self.seam_gain = NOISE_RMS * surface.seam_share.sqrt();
        self.seams.tune(surface.seams, surface.seam_hz, surface.seam_q, speed);
        self.swell.tune(surface.swell, surface.swell_hz);
        let (low, full) = RATTLE_ROUGH;
        self.rattle.rate = RATTLE_RATE * m::min(1.0, m::max(0.0, (roughness - low) / (full - low)));
    }

    fn next(&mut self) -> f64 {
        let mut value = (1.0 - self.crunch) * self.hiss * self.noise_gain * self.noise.next();
        let mut stone = 0.0;
        if self.crunch != 0.0 {
            value += self.crunch * STONE_GAIN * self.stones.next();
            stone = self.crunch * STONE_GAIN * self.stones.last;
        }
        if self.seam_gain != 0.0 {
            value += self.seam_gain * self.seams.next();
        }
        value += RATTLE_GAIN * self.rattle.next(); // also while the last knock rings out
        let swell = self.swell.next();
        self.stone_hit = stone * swell / self.swell.rms;
        value * swell / self.swell.rms
    }
}

/// A stone's click above 500 Hz at 2 kHz: noise through a band-pass over 400-1000 Hz, falling
/// by e in STONE_DECAY. Its strength is the noise's RMS as it is struck.
#[derive(Clone)]
struct StoneClick {
    filter: Resonator,
    rng: PyRandom,
    level: f64,
    fade: f64,
    /// makes the band-passed uniform noise come out at unit RMS
    scale: f64,
}

impl StoneClick {
    fn new(seed: u64) -> StoneClick {
        let filter = Resonator::native(STONE_HZ, STONE_Q, RATE_2K);
        // white noise's power through the band-pass: its impulse response's energy
        let mut probe = filter.clone();
        let power: f64 = (0..4000).map(|n| probe.next(if n == 0 { 1.0 } else { 0.0 }).powi(2)).sum();
        let scale = 1.0 / (power / 3.0).sqrt(); // uniform(-1, 1) has a variance of 1/3
        StoneClick { filter, rng: PyRandom::new(seed), level: 0.0, fade: m::exp(-1.0 / (STONE_DECAY * RATE_2K)), scale }
    }

    fn strike(&mut self, strength: f64) {
        self.level = m::max(self.level, strength.abs());
    }

    /// One 2 kHz sample; exactly 0 once rung out.
    fn next(&mut self) -> f64 {
        if self.level < 1e-6 {
            self.level = 0.0;
            if self.filter.y1.abs() < 1e-9 && self.filter.y2.abs() < 1e-9 {
                self.filter.reset();
                return 0.0;
            }
        }
        let x = self.level * self.scale * self.rng.uniform(-1.0, 1.0);
        self.level *= self.fade;
        self.filter.next(x)
    }
}

/// White noise through a band-pass at 2 kHz, at a sine's mean square (1/2).
#[derive(Clone)]
pub struct RingNoise {
    filter: Resonator,
    rng: PyRandom,
    scale: f64,
}

impl RingNoise {
    fn new(center: f64, q: f64, seed: u64) -> RingNoise {
        let filter = Resonator::native(center, q, RATE_2K);
        // white noise's power through the band-pass: its impulse response's energy
        let mut probe = filter.clone();
        let power: f64 = (0..4000).map(|n| probe.next(if n == 0 { 1.0 } else { 0.0 }).powi(2)).sum();
        // uniform(-1, 1) has a variance of 1/3; a sine's mean square is 1/2
        RingNoise { filter, rng: PyRandom::new(seed), scale: (0.5 / (power / 3.0)).sqrt() }
    }

    fn next(&mut self) -> f64 {
        let x = self.scale * self.rng.uniform(-1.0, 1.0);
        self.filter.next(x)
    }
}

#[derive(Clone)]
enum Carrier {
    Sine,
    Noise(Box<BandNoise>),
    /// A ring's spread part, drawn at 2 kHz only (RingNoise).
    Ring(Box<RingNoise>),
    /// One sample at the given strength: the click the pad makes of it.
    Tick { fired: bool },
}

/// Decaying pulse for discrete events, optionally delayed: a sine, band noise or a tick.
#[derive(Clone)]
pub struct Burst {
    pub freq: f64,
    pub amplitude: f64,
    pub decay_s: f64,
    pub attack_s: f64,
    pub ducks: bool,
    trigger_slot: usize,
    pub age: f64,
    phase: f64,
    carrier: Carrier,
    /// sounded yet (the Synth makes up at 2 kHz for the line the pad drew up to its first sample)
    begun: bool,
}

impl Burst {
    pub fn new(freq: f64, amplitude: f64, decay_s: f64) -> Burst {
        Burst {
            freq,
            amplitude,
            decay_s,
            attack_s: 0.0,
            ducks: true,
            trigger_slot: BOTH_TRIGGERS,
            age: 0.0,
            phase: 0.0,
            carrier: Carrier::Sine,
            begun: false,
        }
    }

    /// Decaying band-limited noise: air hiss, cranking (NoiseBurst).
    fn noise(center: f64, amplitude: f64, decay_s: f64, q: f64, seed: u64) -> Burst {
        Burst { carrier: Carrier::Noise(Box::new(BandNoise::new(center, q, seed))), ..Burst::new(center, amplitude, decay_s) }
    }

    /// A ring's spread part: noise through a band at `center`, at 2 kHz (RingNoise), drawn
    /// with a sine's mean square so `amplitude` carries the same energy a sine's would.
    fn ring_noise(center: f64, amplitude: f64, decay_s: f64, q: f64, seed: u64) -> Burst {
        Burst { carrier: Carrier::Ring(Box::new(RingNoise::new(center, q, seed))), ..Burst::new(center, amplitude, decay_s) }
    }

    /// One sample at the given strength; frequency 0 so the motor eq leaves it as it is, and it never ducks.
    fn tick(amplitude: f64, delay_s: f64) -> Burst {
        Burst { ducks: false, carrier: Carrier::Tick { fired: false }, ..Burst::new(0.0, amplitude, 1.0 / SR) }
            .delay(delay_s)
    }

    fn delay(mut self, delay_s: f64) -> Burst {
        self.age = -delay_s;
        self
    }

    /// 0.25 starts at the crest instead of zero: a hard edge, not a swell.
    fn phase(mut self, phase: f64) -> Burst {
        self.phase = phase;
        self
    }

    fn attack(mut self, attack_s: f64) -> Burst {
        self.attack_s = attack_s;
        self
    }

    fn no_duck(mut self) -> Burst {
        self.ducks = false;
        self
    }

    fn slot(mut self, slot: usize) -> Burst {
        self.trigger_slot = slot;
        self
    }

    fn envelope(&self) -> f64 {
        let envelope = self.amplitude * m::exp(-self.age / self.decay_s);
        if self.age < self.attack_s {
            envelope * self.age / self.attack_s
        } else {
            envelope
        }
    }

    /// Current envelope, before next() advances it; full just ahead of a delayed start.
    pub fn level(&self) -> f64 {
        if self.age >= 0.0 {
            return self.envelope();
        }
        if self.age >= -DUCK_LOOKAHEAD {
            self.amplitude
        } else {
            0.0
        }
    }

    /// Restart the envelope at a new peak, optionally retuned; the carrier keeps running.
    pub fn retrigger(&mut self, amplitude: f64, freq: Option<f64>, decay_s: Option<f64>) {
        self.amplitude = amplitude;
        self.age = 0.0;
        if let Some(f) = freq.filter(|f| *f != 0.0) {
            self.freq = f;
        }
        if let Some(d) = decay_s.filter(|d| *d != 0.0) {
            self.decay_s = d;
        }
    }

    pub fn next(&mut self) -> f64 {
        self.next_at(SR)
    }

    /// One sample at `rate` Hz: the same burst, drawn finer. A tick's one sample is as much
    /// higher as it is shorter, so it pushes the motor as hard.
    pub fn next_at(&mut self, rate: f64) -> f64 {
        if let Carrier::Tick { fired } = &mut self.carrier {
            if self.age < 0.0 {
                self.age += 1.0 / rate;
                return 0.0;
            }
            let value = if *fired { 0.0 } else { self.amplitude * (rate / SR) };
            *fired = true;
            self.age += 1.0 / rate;
            return value;
        }
        if self.age < 0.0 {
            self.age += 1.0 / rate;
            return 0.0;
        }
        let carrier = match &mut self.carrier {
            Carrier::Sine => {
                let value = m::sin(TAU * self.phase);
                self.phase = m::rem(self.phase + self.freq / rate, 1.0);
                value
            }
            Carrier::Noise(noise) => noise.next_at(rate),
            Carrier::Ring(noise) => noise.next(),
            Carrier::Tick { .. } => unreachable!(),
        };
        let value = carrier * self.envelope();
        self.begun = true;
        self.age += 1.0 / rate;
        value
    }

    /// The next sample is its first.
    pub fn starts(&self) -> bool {
        !self.begun && self.age >= 0.0
    }

    pub fn done(&self) -> bool {
        match self.carrier {
            Carrier::Tick { fired } => fired,
            _ => self.age > self.attack_s + self.decay_s * 5.0,
        }
    }

    pub fn is_tick(&self) -> bool {
        matches!(self.carrier, Carrier::Tick { .. })
    }
}

/// Brake squeal as recorded: separate squeals of a few tenths of a second that swell in and
/// stop short, with gaps between and each a little off the last in pitch.
#[derive(Clone)]
struct Squeal {
    rng: PyRandom,
    osc: Oscillator,
    age: f64,
    length: f64,
    gap: f64,
    pitch: f64,
}

const SQUEAL_HZ: f64 = 230.0;
const SQUEAL_ATTACK: f64 = 0.08;
const SQUEAL_RELEASE: f64 = 0.04;

impl Squeal {
    fn new(seed: u64) -> Squeal {
        Squeal { rng: PyRandom::new(seed), osc: Oscillator::default(), age: 0.0, length: 0.0, gap: 0.0, pitch: 1.0 }
    }

    /// The next sample starts a squeal, rather than wherever the last one left off.
    fn restart(&mut self) {
        self.age = self.length + self.gap;
    }

    fn next(&mut self) -> f64 {
        if self.age >= self.length + self.gap {
            self.age = 0.0;
            self.length = self.rng.uniform(0.15, 0.5);
            self.gap = self.rng.uniform(0.03, 0.25);
            self.pitch = self.rng.uniform(0.92, 1.08);
        }
        let age = self.age;
        self.age += 1.0 / SR;
        let envelope = if age < self.length {
            m::min(m::min(1.0, age / SQUEAL_ATTACK), (self.length - age) / SQUEAL_RELEASE)
        } else {
            0.0
        };
        envelope * self.osc.next(SQUEAL_HZ * self.pitch)
    }
}

/// Last block's effect levels, for the monitor line.
#[derive(Clone, Debug, Default)]
pub struct Levels {
    pub engine: f64,
    pub road_left: f64,
    pub road_right: f64,
    pub trigger: f64,
    pub trigger_mode: &'static str,
    pub surface: String,
    pub max_accel: f64,
    pub events: Vec<String>,
}

/// Which grip a burst plays on: "L", "R", or "C" for both.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    L,
    R,
    C,
}

pub struct BurstEntry {
    pub effect: Effect,
    pub side: Side,
    pub burst: Burst,
    id: u64,
}

impl BurstEntry {
    /// Unique for the synth's lifetime: tells a burst seen before from a new one.
    pub fn id(&self) -> u64 {
        self.id
    }
}

#[derive(Clone, Copy, Default)]
struct Amps {
    engine: f64,
    road_l: f64,
    road_r: f64,
    rumble_l: f64,
    rumble_r: f64,
    spin: f64,
    retarder: f64,
    jake: f64,
    trigger: f64,
    squeal: f64,
    harsh: f64,
    sway_l: f64,
    sway_r: f64,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum TriggerMode {
    None,
    Brake,
    Throttle,
}

// keys of the steady-fade references (Synth::novelty)
const REF_LOAD: usize = 0;
const REF_RPM: usize = 1;
const REF_SPEED: usize = 2;
const REF_ROAD: [usize; 2] = [3, 4];
const REF_THROTTLE: usize = 5;
const REF_BRAKE: usize = 6;
// the steady beds: engine, road, trigger
const BED_ENGINE: usize = 0;
const BED_ROAD: usize = 1;
const BED_TRIGGER: usize = 2;

pub struct Synth {
    pub g: Gains,
    pub eq: Equalizer,
    cuts: [Option<ResonanceCut>; 3], // left, right, trigger
    bed_filters: [LowPass; 3],
    bed_shape: LowPass,
    bed_pairs: HashMap<u64, (f64, f64)>,
    engine_osc: Oscillator,
    cylinder_offsets: [f64; 12],
    cylinder: usize,
    firing_gain: f64,
    trigger_osc: Oscillator,
    retarder_osc: Oscillator,
    squeal: Squeal,
    texture: [Texture; 2],
    rumble_osc: [Oscillator; 2],
    rumble_half: [Oscillator; 2],
    spin_noise: BandNoise,
    spin_chatter: Oscillator,
    pub bursts: Vec<BurstEntry>,
    next_burst_id: u64,
    trigger_slot: usize,
    trigger_side: Route,
    prev_amp: Amps,
    prev_flags: HashMap<String, bool>,
    prev_game_events: Option<std::collections::BTreeSet<String>>,
    blinker_idle: f64,
    brake_held: f64,
    brake_peak: f64,
    was_live: bool,
    prev_render_time: Option<i64>,
    prev_deflection: Vec<f64>,
    deflection_rate_avg: Vec<f64>,
    held_frame: Option<(Frame, Vec<f64>, f64)>,
    repeat_dt: f64,
    leap_held: Vec<f64>,
    bump_bursts: Vec<Option<(Side, u64)>>,
    bump_rattles: Vec<u64>,
    ride: [f64; 2],
    ride_wheels: [usize; 2],
    rng: PyRandom,
    /// where NoiseBursts draw their seeds, as the Python app's global random module
    seeds: PyRandom,
    pub roughness: [f64; 2],
    pub rough_avg: [f64; 2],
    rumble_hold: [[f64; 2]; 2],
    spin: f64,
    yaw_sign: f64,
    yaw_evidence: f64,
    prev_gear: Option<i64>,
    gear_cooldown: f64,
    prev_accel: Option<[f64; 3]>,
    impact_cooldown: f64,
    cab_sway: (f64, f64),
    sway: [f64; 2],
    sway_osc: Oscillator,
    lock_phase: f64,
    was_locked: bool,
    steady_ref: [Option<f64>; 7],
    /// engine, road, trigger: 1 just after a bed's input moved, falling while it holds
    pub fresh: [f64; 3],
    /// left grip, right grip, trigger; 0 = no ducking
    pub duck: [f64; 3],
    pub levels: Levels,
    /// Render for the 2 kHz output (set_events_2k). Off, the output is the Python app's to the bit.
    events_2k: bool,
    /// the motors' resonance cuts at 2 kHz for the events (left, right, trigger), one for the
    /// bursts and one for the ticks, which go through the soft limit apart
    cuts_2k: [[Option<ResonanceCut>; 2]; 3],
    /// each channel's last sample ahead of the soft limit, where the pad's line starts
    line_from: [f64; 3],
    /// what a burst's start gives back in the next 2 kHz sample, each channel
    onset_carry: [f64; 3],
    /// the stones' clicks above 500 Hz at 2 kHz, left and right
    stones_2k: [StoneClick; 2],
    /// the coupling rings' pitches; only drawn from at 2 kHz, so the 1 kHz output stays as it was
    ring_rng: PyRandom,
    /// how loud the rings and the stones' clicks above 500 Hz play against their fitted level
    /// (1.0); only the ring-level listening check changes it (src/ring_level.rs)
    hi_trim: f64,
}

impl Synth {
    pub fn new(gains: Gains, eq: Equalizer) -> Synth {
        Synth::with_seeds(gains, eq, PyRandom::from_entropy())
    }

    /// A synth whose noise bursts draw their seeds from `seeds` (the Python app's global random).
    pub fn with_seeds(gains: Gains, eq: Equalizer, seeds: PyRandom) -> Synth {
        let cuts = [eq.cut(0), eq.cut(0), eq.cut(1)];
        Synth {
            g: gains,
            eq,
            cuts,
            bed_filters: [LowPass::default(), LowPass::default(), LowPass::default()],
            bed_shape: LowPass::default(),
            bed_pairs: HashMap::new(),
            engine_osc: Oscillator::default(),
            cylinder_offsets: std::array::from_fn(|n| PyRandom::new(11 + n as u64).uniform(-1.0, 1.0)),
            cylinder: 0,
            firing_gain: 1.0,
            trigger_osc: Oscillator::default(),
            retarder_osc: Oscillator::default(),
            squeal: Squeal::new(4),
            texture: [Texture::new(1), Texture::new(2)],
            rumble_osc: Default::default(),
            rumble_half: Default::default(),
            spin_noise: BandNoise::new(45.0, 1.5, 3),
            spin_chatter: Oscillator::default(),
            bursts: Vec::new(),
            next_burst_id: 0,
            trigger_slot: BOTH_TRIGGERS,
            trigger_side: Route::Both,
            prev_amp: Amps::default(),
            prev_flags: HashMap::new(),
            prev_game_events: None,
            blinker_idle: 0.0,
            brake_held: 0.0,
            brake_peak: 0.0,
            was_live: false,
            prev_render_time: None,
            prev_deflection: Vec::new(),
            deflection_rate_avg: Vec::new(),
            held_frame: None,
            repeat_dt: 0.0,
            leap_held: Vec::new(),
            bump_bursts: Vec::new(),
            bump_rattles: Vec::new(),
            ride: [0.0; 2],
            ride_wheels: [0; 2],
            rng: PyRandom::new(7),
            seeds,
            roughness: [0.0; 2],
            rough_avg: [0.0; 2],
            rumble_hold: [[0.0; 2]; 2],
            spin: 0.0,
            yaw_sign: 1.0,
            yaw_evidence: 0.0,
            prev_gear: None,
            gear_cooldown: 0.0,
            prev_accel: None,
            impact_cooldown: 0.0,
            cab_sway: (0.0, 0.0),
            sway: [0.0; 2],
            sway_osc: Oscillator::default(),
            lock_phase: 0.0,
            was_locked: false,
            steady_ref: [None; 7],
            fresh: [1.0; 3],
            duck: [0.0; 3],
            levels: Levels { trigger_mode: "-", ..Levels::default() },
            events_2k: false,
            cuts_2k: Default::default(),
            line_from: [0.0; 3],
            onset_carry: [0.0; 3],
            stones_2k: [StoneClick::new(21), StoneClick::new(22)],
            ring_rng: PyRandom::new(23),
            hi_trim: 1.0,
        }
    }

    /// Render the events (every burst and tick) at 2 kHz for the 2 kHz output, from the next
    /// block on. The continuous effects stay at 1 kHz in the samples, where src/rate2k.rs
    /// joins them as the pad does and filters their images; the events go in Block::events,
    /// drawn at 2 kHz, to be laid over that: a knock's crest-first edge in half a millisecond
    /// instead of the line's whole one, a 3 ms click in six samples instead of three, the
    /// hisses' noise without a fold at 500 Hz and the 420 Hz filter taking their top half.
    /// The felt part stays as it was.
    pub fn set_events_2k(&mut self, on: bool) {
        if on && !self.events_2k {
            self.cuts_2k = [0, 0, 1].map(|c| [(); 2].map(|_| self.eq.cut_at(c, RATE_2K)));
            self.onset_carry = [0.0; 3];
            self.stones_2k = [StoneClick::new(21), StoneClick::new(22)];
        }
        self.events_2k = on;
    }

    pub fn events_2k(&self) -> bool {
        self.events_2k
    }

    /// Play the rings and the stones' clicks above 500 Hz `db` from their fitted level, or not at
    /// all for None: the levels the ring-level listening check (src/ring_level.rs) compares.
    pub fn set_hi_trim(&mut self, db: Option<f64>) {
        self.hi_trim = db.map_or(0.0, |db| m::pow(10.0, db / 20.0));
    }

    fn push(&mut self, effect: Effect, side: Side, burst: Burst) -> u64 {
        let id = self.next_burst_id;
        self.next_burst_id += 1;
        self.bursts.push(BurstEntry { effect, side, burst, id });
        id
    }

    fn both(&mut self, effect: Effect, burst: Burst) {
        self.push(effect, Side::C, burst);
    }

    /// At 2 kHz, the ring above 500 Hz that follows a tick of `tick` (its 1 kHz height), on `side`.
    /// Its energy in 500-1000 Hz is ring.db over the tick's: the tick is one 2 kHz sample twice
    /// as high, so half its 4 tick^2 lies there; the ring's is amplitude^2 decay 2000 / 4. It is
    /// drawn as flat as the tick, the motor eq taken back out at its pitch.
    fn ring(&mut self, effect: Effect, side: Side, tick: f64, delay: f64, ring: Ring) {
        if !self.events_2k || tick == 0.0 || self.hi_trim == 0.0 {
            return;
        }
        let share = m::pow(10.0, ring.db / 10.0);
        let amplitude = self.hi_trim * tick * (2.0 * share / (500.0 * ring.decay)).sqrt() / self.eq.pair(ring.hz).0;
        let tone = Burst::new(ring.hz, amplitude * RING_TONAL.sqrt(), ring.decay);
        self.push(effect, side, tone.delay(delay).no_duck());
        let seed = self.ring_rng.randrange(1 << 30) as u64;
        let spread = Burst::ring_noise(ring.hz, amplitude * (1.0 - RING_TONAL).sqrt(), ring.decay, RING_Q, seed);
        self.push(effect, side, spread.delay(delay).no_duck());
    }

    /// A NoiseBurst: its noise is seeded from the shared seed source as it is made.
    fn noise_burst(&mut self, center: f64, amplitude: f64, decay_s: f64, delay_s: f64, attack_s: f64, q: f64) -> Burst {
        let seed = self.seeds.randrange(1 << 30) as u64;
        Burst::noise(center, amplitude, decay_s, q, seed).delay(delay_s).attack(attack_s)
    }

    fn live_burst(&mut self, id: u64) -> Option<&mut Burst> {
        // a burst leaves the list once done, so one still in it is not done
        self.bursts.iter_mut().find(|e| e.id == id).map(|e| &mut e.burst)
    }

    // -- event detection, once per block ------------------------------------

    /// Forget the edge detectors' last values as telemetry comes back after a gap.
    fn rebaseline(&mut self) {
        self.prev_render_time = None;
        self.brake_held = 0.0;
        self.brake_peak = 0.0;
        self.prev_deflection.clear();
        self.held_frame = None;
        self.repeat_dt = 0.0;
        self.leap_held.clear();
        self.prev_accel = None;
        self.cab_sway = (0.0, 0.0);
        self.prev_gear = None;
        self.prev_flags.clear(); // the game events stay: they are one-shot flags, not states
        self.steady_ref = [None; 7];
        self.fresh = [1.0; 3];
    }

    /// Game time in s since the last new frame; None when this frame was read before.
    fn frame_dt(&mut self, frame: &Frame) -> Option<f64> {
        let previous = self.prev_render_time.replace(frame.render_time);
        if previous == Some(frame.render_time) {
            return None;
        }
        let dt = match previous {
            None => 0.0,
            Some(p) => (frame.render_time - p) as f64 / 1e6,
        };
        Some(if 0.0 < dt && dt <= MAX_FRAME_GAP { dt } else { 0.0 })
    }

    /// Bumps and roughness of the frame before this one, once this one shows its spikes.
    fn wheel_events(&mut self, frame: &Frame, dt: f64) {
        let deflections: Vec<f64> = frame.wheels.iter().chain(&frame.trailer_wheels).map(|w| w.deflection).collect();
        if dt != 0.0 {
            if let Some((_, held, _)) = &self.held_frame {
                if *held == deflections {
                    self.repeat_dt += dt; // the game repeating a state: its time belongs to the next real frame
                    return;
                }
            }
        }
        let mut dt = dt;
        if dt != 0.0 {
            dt += self.repeat_dt;
            if dt > MAX_FRAME_GAP {
                dt = 0.0; // repeated that long, it was a hitch
            }
        }
        self.repeat_dt = 0.0;
        let Some((held, mut deflections, dt)) = self.held_frame.replace((frame.clone(), deflections, dt)) else {
            return;
        };
        let (_, after, after_dt) = self.held_frame.as_ref().unwrap();
        if dt != 0.0
            && *after_dt != 0.0
            && after.len() == deflections.len()
            && deflections.len() == self.prev_deflection.len()
        {
            for i in 0..deflections.len() {
                let (before, now, then) = (self.prev_deflection[i], deflections[i], after[i]);
                let jump = now - before;
                if jump.abs() >= SPIKE_MIN && (then - before).abs() < SPIKE_BACK * jump.abs() {
                    deflections[i] = before + (then - before) * dt / (dt + after_dt);
                }
            }
        }
        self.drop_leaps(&held, &mut deflections, dt);
        self.suspension(&held, deflections, dt);
    }

    /// Hold a wheel where it was while it reads further off than rolling could take it.
    fn drop_leaps(&mut self, frame: &Frame, deflections: &mut [f64], dt: f64) {
        if self.leap_held.len() != deflections.len() {
            self.leap_held = vec![0.0; deflections.len()];
        }
        if dt == 0.0 || self.prev_deflection.len() != deflections.len() {
            return;
        }
        let reach = (LEAP_SLOPE * frame.speed.abs() + LEAP_FLOOR) * dt;
        for i in 0..deflections.len() {
            let (before, now) = (self.prev_deflection[i], deflections[i]);
            if (now - before).abs() <= reach {
                self.leap_held[i] = 0.0;
            } else if self.leap_held[i] + dt <= LEAP_HOLD {
                self.leap_held[i] += dt;
                deflections[i] = before;
            } else {
                // it stays there: where the wheel is from now on, not a movement
                self.leap_held[i] = 0.0;
                self.prev_deflection[i] = now;
            }
        }
    }

    fn suspension(&mut self, frame: &Frame, deflections: Vec<f64>, dt: f64) {
        let wheels: Vec<&Wheel> = frame.wheels.iter().chain(&frame.trailer_wheels).collect();
        for (group, count) in [frame.wheels.len(), frame.trailer_wheels.len()].into_iter().enumerate() {
            if self.ride_wheels[group] != count {
                // another truck or trailer, or none: learn its ride afresh
                self.ride_wheels[group] = count;
                self.ride[group] = 0.0;
            }
        }
        if self.prev_deflection.len() != wheels.len() {
            self.prev_deflection = deflections;
            self.deflection_rate_avg = vec![0.0; wheels.len()];
            self.bump_bursts = vec![None; wheels.len()];
            return;
        }
        if dt == 0.0 {
            // no rate across a gap: the travel since then is not one movement
            self.prev_deflection = deflections;
            return;
        }
        let avg_k = 1.0 - m::exp(-dt / RATE_AVG_TAU);
        let mut rough = [0.0; 2];
        let mut counts = [0usize; 2];
        let bump_threshold = self.g.bump_threshold;
        let thresholds = self.ride.map(|ride| m::max(bump_threshold, m::min(BUMP_RIDE_MAX, BUMP_OVER_RIDE * ride)));
        let mut ride = [0.0; 2];
        let mut ride_counts = [0usize; 2];
        for (i, wheel) in wheels.iter().enumerate() {
            let rate = (deflections[i] - self.prev_deflection[i]) / dt; // m/s
            self.prev_deflection[i] = deflections[i];
            let avg = self.deflection_rate_avg[i];
            let avg = avg + (rate - avg) * avg_k;
            self.deflection_rate_avg[i] = avg;
            let high = (rate - avg).abs();
            let side = if wheel.pos_x < 0.0 { 0 } else { 1 };
            let trailer = i >= frame.wheels.len();
            if !trailer {
                // road texture is what the truck's own tyres feel
                rough[side] += high;
                counts[side] += 1;
            }
            if !wheel.on_ground {
                continue;
            }
            let group = trailer as usize;
            ride[group] += high;
            ride_counts[group] += 1;
            if high > thresholds[group] {
                // a step in suspension travel: pothole, kerb, joint
                let size = m::min(1.0, (high - bump_threshold) / 0.8);
                let strength = size * self.g.bumps * if trailer { TRAILER_FEEL } else { 1.0 };
                let (small_hz, big_hz) = BUMP_HZ;
                let freq = small_hz + (big_hz - small_hz) * size + if trailer { TRAILER_BUMP_HZ } else { 0.0 };
                let decay = BUMP_DECAY.0 + (BUMP_DECAY.1 - BUMP_DECAY.0) * size;
                let rattles = size >= BUMP_RATTLE_FROM && !trailer;
                if let Some((where_, id)) = self.bump_bursts[i] {
                    if let Some(current) = self.live_burst(id) {
                        // One pulse per wheel; only the step's next frame may still strike it harder.
                        let mut struck = false;
                        if strength > current.level() && (current.age < BUMP_RESTRIKE_S || current.age >= BUMP_GAP_S) {
                            current.retrigger(strength, Some(freq), Some(decay));
                            struck = true;
                        }
                        if struck && rattles {
                            self.rattle(where_, strength, size);
                        }
                        continue;
                    }
                }
                let where_ = if wheel.pos_x.abs() < 0.15 {
                    Side::C
                } else if side == 0 {
                    Side::L
                } else {
                    Side::R
                };
                let id = self.push(Effect::Bumps, where_, Burst::new(freq, strength, decay).phase(0.25));
                self.bump_bursts[i] = Some((where_, id));
                if rattles {
                    self.rattle(where_, strength, size);
                }
            }
        }
        if frame.speed.abs() >= BUMP_RIDE_SPEED {
            let ride_k = 1.0 - m::exp(-dt / BUMP_RIDE_TAU);
            for group in 0..2 {
                if ride_counts[group] != 0 {
                    self.ride[group] += (ride[group] / ride_counts[group] as f64 - self.ride[group]) * ride_k;
                }
            }
        }
        let rough_k = 1.0 - m::exp(-dt / ROUGH_TAU);
        for side in 0..2 {
            let value = if counts[side] != 0 { rough[side] / counts[side] as f64 } else { 0.0 };
            self.roughness[side] += (value - self.roughness[side]) * rough_k;
            self.rough_avg[side] += (self.roughness[side] - self.rough_avg[side]) * (1.0 - m::exp(-dt / DETAIL_TAU));
        }
    }

    /// A few small knocks over the next BUMP_RATTLE_S, fading: the cab settling after a hit.
    fn rattle(&mut self, where_: Side, strength: f64, size: f64) {
        let live: Vec<u64> = self.bursts.iter().map(|e| e.id).collect();
        self.bump_rattles.retain(|id| live.contains(id));
        for _ in 0..(2.0 + 4.0 * size) as i64 {
            if self.bump_rattles.len() >= BUMP_RATTLE_MAX {
                return;
            }
            let delay = self.rng.uniform(0.04, BUMP_RATTLE_S);
            let amplitude = strength * 0.3 * self.rng.uniform(0.4, 1.0) * (1.0 - delay / (BUMP_RATTLE_S + 0.05));
            let freq = self.rng.uniform(55.0, 85.0);
            let id = self.push(Effect::Bumps, where_, Burst::new(freq, amplitude, 0.008).delay(delay).phase(0.25));
            self.bump_rattles.push(id);
        }
    }

    fn gear_event(&mut self, frame: &Frame) {
        self.gear_cooldown = m::max(0.0, self.gear_cooldown - DT);
        let Some(prev_gear) = self.prev_gear else {
            // first live frame: already in this gear, nothing shifted
            self.prev_gear = Some(frame.gear);
            return;
        };
        if frame.gear != prev_gear && frame.gear != 0 && self.gear_cooldown == 0.0 {
            let gear = self.g.gear;
            if self.g.gearbox == CONVERTER {
                // a powershift: pushed as hard as the engine pulls
                let (coasting, floored) = CONVERTER_SHIFT;
                let strength = coasting + (floored - coasting) * m::min(1.0, m::max(0.0, frame.game_throttle));
                self.both(
                    Effect::Gear,
                    Burst::new(CONVERTER_SHIFT_HZ, strength * gear, CONVERTER_SHIFT_DECAY)
                        .attack(CONVERTER_SHIFT_ATTACK)
                        .no_duck(),
                );
            } else {
                // the recorded shift on its own timeline
                for (delay, strength, decay) in SHIFT_HITS {
                    self.both(Effect::Gear, Burst::new(SHIFT_HZ, strength * gear, decay).delay(delay).phase(0.25));
                    if decay <= TICK_EDGE {
                        self.both(Effect::Gear, Burst::tick(TICK_SHARE * strength * gear, delay));
                        self.ring(Effect::Gear, Side::C, TICK_SHARE * strength * gear, delay, SHIFT_RING);
                    }
                }
            }
            self.gear_cooldown = SHIFT_COOLDOWN;
            self.levels.events.push(t!("변속 {gear}단", gear = frame.gear));
        }
        if frame.gear != 0 {
            self.prev_gear = Some(frame.gear);
        }
    }

    fn impact_event(&mut self, frame: &Frame, dt: f64) {
        self.impact_cooldown = m::max(0.0, self.impact_cooldown - dt);
        let [ax, _, az] = frame.accel;
        let horizontal = m::hypot(ax, az);
        self.levels.max_accel = m::max(self.levels.max_accel, horizontal);
        if dt != 0.0 && self.impact_cooldown == 0.0 {
            if let Some([px, _, pz]) = self.prev_accel {
                let jump = m::hypot(ax - px, az - pz);
                let threshold = self.g.impact_threshold;
                if jump > threshold {
                    let strength = m::min(1.0, (jump - threshold) / 30.0 + 0.4) * self.g.impact;
                    self.both(Effect::Impact, Burst::new(50.0, strength, 0.06));
                    self.impact_cooldown = 0.3;
                    self.levels.events.push(t!("충돌 {accel} m/s²", accel = format!("{jump:.0}")));
                }
            }
        }
        self.prev_accel = Some(frame.accel);
    }

    /// How much rougher the road under that side is now than over the last few seconds, rooted.
    fn detail(&self, side: usize) -> f64 {
        if self.rough_avg[side] < 1e-4 {
            return 1.0;
        }
        let (low, high) = DETAIL_RANGE;
        m::min(high, m::max(low, self.roughness[side] / self.rough_avg[side])).sqrt()
    }

    /// How the cab sways on its mounts now, each side.
    fn cab_event(&mut self, frame: &Frame) {
        let tilt = m::sum(frame.cabin_accel.iter().map(|a| a * a)).sqrt();
        let strength = ramp(tilt, &CAB_SWAY) * self.g.bumps;
        let lean = if tilt != 0.0 { frame.cabin_accel[2] / tilt } else { 0.0 }; // the roll's share, positive pushing left
        let far = 1.0 - lean.abs() * (1.0 - CAB_FAR_SIDE);
        self.cab_sway = if lean >= 0.0 { (strength * 1.0, strength * far) } else { (strength * far, strength * 1.0) };
    }

    /// True when value differs from the last frame's (never on the first frame).
    fn changed(&mut self, key: &str, value: bool) -> bool {
        let old = self.prev_flags.insert(key.to_string(), value).unwrap_or(value);
        old != value
    }

    fn state_events(&mut self, frame: &Frame) {
        let g = self.g.clone();
        for (key, value, slot) in [("throttle", frame.user_throttle, THROTTLE_TRIGGER), ("brake", frame.user_brake, BRAKE_TRIGGER)] {
            let floored = value >= 0.97;
            if self.changed(&format!("{key}_floor"), floored) && floored && g.pedal != 0.0 {
                self.both(Effect::Pedal, Burst::new(130.0, PEDAL_FLOOR * g.pedal, 0.012).phase(0.25).slot(slot));
                self.levels.events.push(t!("페달 바닥"));
            }
        }

        if self.changed("park_brake", frame.park_brake) && g.park_brake != 0.0 {
            let s = g.park_brake;
            if frame.park_brake {
                // the knob clunks, then the spring brakes vent in a hiss
                self.both(Effect::ParkBrake, Burst::new(110.0, 1.0 * s, 0.02).phase(0.25));
                let hiss = self.noise_burst(250.0, 0.5 * s, 0.35, 0.03, 0.15, 0.8);
                self.both(Effect::ParkBrake, hiss);
            } else {
                // the knob thunks, and a short pop of air about 0.3 s later
                self.both(Effect::ParkBrake, Burst::new(130.0, 0.83 * s, 0.035).phase(0.25));
                let pop = self.noise_burst(250.0, 0.45 * s, 0.05, 0.28, 0.0, 0.8);
                self.both(Effect::ParkBrake, pop);
            }
            self.levels.events.push(if frame.park_brake { t!("파킹 브레이크 체결") } else { t!("파킹 브레이크 해제") });
        }

        if self.changed("trailer", frame.trailer_attached) && g.trailer != 0.0 {
            let s = g.trailer;
            if frame.trailer_attached {
                self.couple(s);
                self.levels.events.push(t!("트레일러 연결"));
            } else {
                self.both(Effect::Trailer, Burst::new(50.0, 0.9 * s, 0.05));
                let hiss = self.noise_burst(250.0, 0.4 * s, 0.15, 0.15, 0.0, 0.8);
                self.both(Effect::Trailer, hiss);
                self.levels.events.push(t!("트레일러 분리"));
            }
        }

        if frame.user_brake > AIR_BRAKE_ON {
            self.brake_held += DT;
            self.brake_peak = m::max(self.brake_peak, frame.user_brake);
        } else if frame.user_brake < AIR_BRAKE_OFF {
            if self.brake_held >= AIR_BRAKE_HOLD && g.air_brake != 0.0 && frame.air_pressure > 30.0 {
                let s = g.air_brake * (0.4 + 0.6 * self.brake_peak);
                self.both(Effect::AirBrake, Burst::new(50.0, AIR_BRAKE_THUMP * s, 0.03).phase(0.25));
                let hiss = self.noise_burst(AIR_BRAKE_HISS_HZ, 0.5 * s, AIR_BRAKE_HISS_DECAY, 0.01, 0.04, AIR_BRAKE_HISS_Q);
                self.both(Effect::AirBrake, hiss);
            }
            self.brake_held = 0.0;
            self.brake_peak = 0.0;
        }

        // The lamp flag toggles with every flash; the relay clicks on both edges.
        self.blinker_idle += DT;
        for (key, on, side) in [("blinker_l", frame.blinker_left, Side::L), ("blinker_r", frame.blinker_right, Side::R)] {
            if self.changed(key, on) && g.blinker != 0.0 {
                let strength = if on { 1.0 } else { 0.7 } * g.blinker;
                self.push(Effect::Blinker, side, Burst::new(BLINKER_HZ, strength, BLINKER_DECAY).phase(0.25));
                self.push(Effect::Blinker, side, Burst::tick(TICK_SHARE * strength, 0.0));
                self.ring(Effect::Blinker, side, TICK_SHARE * strength, 0.0, BLINKER_RING);
                if self.blinker_idle > 1.5 {
                    self.levels.events.push(if key == "blinker_l" { t!("방향지시등 좌") } else { t!("방향지시등 우") });
                }
                self.blinker_idle = 0.0;
            }
        }

        if self.changed("engine_on", frame.engine_on) && g.engine_start != 0.0 {
            let s = g.engine_start;
            if frame.engine_on {
                let peak = ENGINE_START_PEAK * s;
                let starter = self.noise_burst(110.0, 0.1 * s, 0.8, 0.05, 0.05, 0.8);
                self.both(Effect::EngineStart, starter);
                let engaging = Burst::new(115.0, peak * m::pow(10.0, -7.0 / 20.0), 0.01).delay(0.08).phase(0.25);
                self.both(Effect::EngineStart, engaging);
                for (at, level) in ENGINE_CRANK {
                    // compression knocks, low like the exhaust's
                    self.both(Effect::EngineStart, Burst::new(70.0, peak * db(level), 0.015).delay(at).phase(0.25));
                }
                self.firings(&ENGINE_CATCH, peak, ENGINE_CATCH_AT);
                self.levels.events.push(t!("시동"));
            } else {
                self.firings(&ENGINE_STOP, ENGINE_STOP_PEAK * s, 0.0);
                self.levels.events.push(t!("시동 끔"));
            }
        }

        for (i, toggle) in frame.shifter_toggle.into_iter().enumerate() {
            if self.changed(&format!("shifter_{i}"), toggle) && g.shifter != 0.0 {
                for (delay, freq, strength, decay) in SHIFTER_HITS[i.min(1)] {
                    self.both(Effect::Shifter, Burst::new(freq, strength * g.shifter, decay).delay(delay).phase(0.25));
                    if decay <= TICK_EDGE {
                        // the knob, and each clack's edge
                        self.both(Effect::Shifter, Burst::tick(TICK_SHARE * strength * g.shifter, delay));
                        if delay > 0.1 {
                            // the gearbox's clacks ring; the knob in the hand does not
                            let ring = if i == 0 { RANGE_RING } else { SPLIT_RING };
                            self.ring(Effect::Shifter, Side::C, TICK_SHARE * strength * g.shifter, delay, ring);
                        }
                    }
                }
                self.levels.events.push(if i == 0 { t!("레인지 셀렉터") } else { t!("스플리터") });
            }
        }

        let previous = self.prev_game_events.replace(frame.game_events.clone()).unwrap_or_default();
        for name in frame.game_events.difference(&previous) {
            if g.game_events != 0.0 {
                let s = g.game_events;
                // two taps, each over in a few cycles so the pair stays two
                self.both(Effect::GameEvents, Burst::new(80.0, 1.0 * s, 0.015).phase(0.25));
                self.both(Effect::GameEvents, Burst::new(80.0, 1.0 * s, 0.015).delay(0.12).phase(0.25));
            }
            let label = GAME_EVENT_LABELS.iter().find(|(key, _)| key == name).map(|(_, label)| n_(label));
            self.levels.events.push(label.map(|l| t!(l)).unwrap_or_else(|| name.clone()));
        }
    }

    /// The fifth wheel's clatter as a trailer couples, different every time.
    fn couple(&mut self, strength: f64) {
        let mut knocks = Vec::new();
        let mut at = 0.0;
        while at < COUPLE_S {
            knocks.push(at);
            at += self.rng.uniform(COUPLE_GAP.0, COUPLE_GAP.1);
        }
        let main = self.rng.uniform(COUPLE_MAIN.0, COUPLE_MAIN.1);
        let mut loudest = knocks[0];
        for &k in &knocks[1..] {
            if (k - main).abs() < (loudest - main).abs() {
                loudest = k;
            }
        }
        for at in knocks {
            let level = if at == loudest {
                COUPLE_LOUDEST
            } else {
                COUPLE_LOUDEST * self.rng.uniform(COUPLE_KNOCKS.0, COUPLE_KNOCKS.1)
            };
            let freq = self.rng.uniform(COUPLE_HZ.0, COUPLE_HZ.1);
            self.both(Effect::Trailer, Burst::new(freq, level * strength, COUPLE_DECAY).delay(at).phase(0.25));
            self.both(Effect::Trailer, Burst::tick(TICK_SHARE * level * strength, at));
            if self.events_2k {
                let hz = self.ring_rng.uniform(COUPLE_RING_HZ.0, COUPLE_RING_HZ.1);
                self.ring(Effect::Trailer, Side::C, TICK_SHARE * level * strength, at, Ring { hz, ..COUPLE_RING });
            }
        }
    }

    /// The engine's firings as it starts or stops: (s, dB against peak) each.
    fn firings(&mut self, firings: &[(f64, i32)], peak: f64, start: f64) {
        for &(at, level) in firings {
            self.both(
                Effect::EngineStart,
                Burst::new(ENGINE_FIRING_HZ, peak * db(level), ENGINE_FIRING_DECAY).delay(start + at).phase(0.25),
            );
        }
    }

    /// 1 while value moves by dead + scale or more within about CHANGE_TAU; 0 for moves inside dead.
    fn novelty(&mut self, key: usize, value: f64, scale: f64, dead: f64) -> f64 {
        let reference = *self.steady_ref[key].get_or_insert(value);
        self.steady_ref[key] = Some(reference + (value - reference) * change_k());
        m::min(1.0, m::max(0.0, (value - reference).abs() - dead) / scale)
    }

    /// Full as the bed's input moves (novelty 1), settling toward the floor while it holds.
    fn steady_gain(&mut self, bed: usize, novelty: f64) -> f64 {
        let held = m::max(novelty, self.fresh[bed] * fade_k());
        self.fresh[bed] = held;
        let floor = 1.0 - m::min(1.0, m::max(0.0, self.g.steady_fade));
        floor + (1.0 - floor) * held
    }

    /// Weigh one frame's turn against its wheels and flip yaw_sign once they clearly disagree.
    fn learn_yaw_sign(&mut self, frame: &Frame) {
        if frame.yaw_rate.abs() < 0.05 || frame.speed.abs() < 1.0 {
            return;
        }
        let rims: Vec<(f64, f64)> = frame
            .wheels
            .iter()
            .filter(|w| w.on_ground)
            .map(|w| (w.pos_x, w.velocity.abs() * TAU * w.radius))
            .collect();
        let left: Vec<f64> = rims.iter().filter(|(x, _)| *x < -0.3).map(|(_, rim)| *rim).collect();
        let right: Vec<f64> = rims.iter().filter(|(x, _)| *x > 0.3).map(|(_, rim)| *rim).collect();
        if left.is_empty() || right.is_empty() {
            return;
        }
        // reversing, the inside runs faster
        let spread = m::sum(right.iter().copied()) / right.len() as f64 - m::sum(left.iter().copied()) / left.len() as f64;
        self.yaw_evidence += frame.yaw_rate * spread * m::copysign(1.0, frame.speed);
        self.yaw_evidence = m::max(-5.0 * YAW_EVIDENCE, m::min(5.0 * YAW_EVIDENCE, self.yaw_evidence));
        let sign = m::copysign(1.0, self.yaw_evidence);
        if self.yaw_evidence.abs() > YAW_EVIDENCE && sign != self.yaw_sign {
            self.yaw_sign = sign;
            self.levels.events.push(t!("회전 각속도 부호 뒤집음 (바퀴 속도 기준)"));
        }
    }

    /// Share of grounded driven wheels turning clearly faster than their own spot of road moves.
    fn wheel_spin(&self, frame: &Frame) -> f64 {
        let speed = frame.speed.abs();
        let grounded: Vec<&Wheel> = frame.wheels.iter().filter(|w| w.on_ground && w.powered).collect();
        if grounded.is_empty() {
            return 0.0;
        }
        let yaw = frame.yaw_rate * self.yaw_sign;
        let mut spinning = 0;
        for wheel in &grounded {
            // the patch's velocity: forward speed plus the turn's share (x right, z back)
            let ground = m::hypot(frame.speed + yaw * wheel.pos_x, yaw * wheel.pos_z);
            let excess = wheel.velocity.abs() * TAU * wheel.radius - ground;
            // 25% clears the outer wheels of a tight turn; 1 m/s the creep off the line
            if excess > m::max(1.0, 0.25 * speed) {
                spinning += 1;
            }
        }
        spinning as f64 / grounded.len() as f64
    }

    fn wheel_lock(&self, frame: &Frame) -> f64 {
        let speed = frame.speed.abs();
        if speed < 3.0 {
            return 0.0;
        }
        let grounded: Vec<&Wheel> = frame.wheels.iter().filter(|w| w.on_ground).collect();
        if grounded.is_empty() {
            return 0.0;
        }
        let locked = grounded
            .iter()
            .filter(|w| (speed - w.velocity.abs() * TAU * w.radius) / speed > 0.3)
            .count();
        locked as f64 / grounded.len() as f64
    }

    // -- block rendering ------------------------------------------------------

    pub fn render(&mut self, frame: Option<&Frame>) -> Block {
        self.levels.events = Vec::new();
        let live_frame = frame.filter(|f| f.live());
        let live = live_frame.is_some();

        let (mut engine_amp, mut road_l, mut road_r, mut rumble_l, mut rumble_r) = (0.0, 0.0, 0.0, 0.0, 0.0);
        let (mut spin_amp, mut retarder_amp, mut jake_amp) = (0.0, 0.0, 0.0);
        let (mut trigger_amp, mut squeal_amp, mut engine_harsh, mut engine_uneven) = (0.0, 0.0, 0.0, 0.0);
        let mut engine_freq = 40.0;
        let mut rumble_freq = 40.0;
        let mut retarder_freq = RETARDER_HZ.0;
        let mut trigger_mode = TriggerMode::None;
        let mut lock = 0.0;
        if live && !self.was_live {
            self.rebaseline();
        }
        self.was_live = live;
        if let Some(frame) = live_frame {
            if let Some(dt) = self.frame_dt(frame) {
                // the same frame read again holds no new movement
                self.wheel_events(frame, dt);
                self.impact_event(frame, dt);
                self.cab_event(frame);
            }
            self.gear_event(frame);
            self.state_events(frame);
            let g = self.g.clone();

            // Brake squeal: light braking while rolling to a stop.
            let speed = frame.speed.abs();
            let braking = m::max(frame.user_brake, frame.game_brake);
            if braking > 0.08 && 0.3 < speed && speed < 4.0 {
                squeal_amp = g.squeal * 0.35 * (1.0 - speed / 4.0).sqrt();
            }

            let load = m::max(frame.game_throttle, 0.0);
            let rpm_frac = m::min(1.0, frame.rpm / frame.rpm_max);
            // cruise control trims the load by a few percent all the time
            let load_moved = self.novelty(REF_LOAD, load, 0.15, 0.05);
            let rpm_moved = self.novelty(REF_RPM, rpm_frac, 0.06, 0.02);
            let engine_moved = m::max(load_moved, rpm_moved);
            if frame.engine_on && frame.rpm > 100.0 {
                // Firing frequency of a 4-stroke engine: rpm/60 * cylinders/2.
                engine_freq = m::max(25.0, m::min(140.0, frame.rpm / 60.0 * g.cylinders as f64 / 2.0));
                let standing = m::max(0.0, 1.0 - speed / ENGINE_STANDING_SPEED) * (1.0 - load);
                engine_amp = g.engine
                    * (0.25 + 0.55 * load + 0.2 * rpm_frac)
                    * (1.0 + (ENGINE_STANDING - 1.0) * standing)
                    * self.steady_gain(BED_ENGINE, engine_moved);
                engine_harsh = ENGINE_HARSH * load;
                engine_uneven = m::max(ENGINE_UNEVEN_FLOOR, 1.0 - 2.0 * load - rpm_frac);
            }

            let speed_factor = m::pow(m::min(1.0, frame.speed.abs() / 25.0), 0.7);
            let sides: [Vec<&Wheel>; 2] = [
                frame.wheels.iter().filter(|w| w.pos_x < 0.0 && w.on_ground).collect(),
                frame.wheels.iter().filter(|w| w.pos_x >= 0.0 && w.on_ground).collect(),
            ];
            // A new surface, rougher ground or a change of pace brings the road back.
            let mut road_moved = self.novelty(REF_SPEED, speed, 2.0, 1.0);
            let mut textures = [0.0; 2];
            for (side, wheels) in sides.iter().enumerate() {
                let surface = side_surface(wheels);
                let rough = self.roughness[side];
                self.texture[side].tune(&surface, speed, rough);
                textures[side] = m::min(
                    1.0,
                    surface.level * speed_factor + m::min(1.0, m::pow(rough / ROUGH_FULL, ROUGH_POWER)),
                );
                let ground = m::min(1.0, surface.level * speed_factor + ROUGH_NOVELTY * rough);
                road_moved = m::max(road_moved, self.novelty(REF_ROAD[side], ground, 0.15, 0.05));
            }
            let road_gain = g.road * self.steady_gain(BED_ROAD, road_moved);
            road_l = road_gain * textures[0] * self.detail(0);
            road_r = road_gain * textures[1] * self.detail(1);
            rumble_freq = m::max(25.0, m::min(120.0, frame.speed.abs() / RUMBLE_SPACING));
            let trailer_sides: [Vec<&Wheel>; 2] = [
                frame.trailer_wheels.iter().filter(|w| w.pos_x < 0.0 && w.on_ground).collect(),
                frame.trailer_wheels.iter().filter(|w| w.pos_x >= 0.0 && w.on_ground).collect(),
            ];
            let mut rumble = [0.0; 2];
            for (group, (groups, feel)) in [(&sides, 1.0), (&trailer_sides, TRAILER_FEEL)].into_iter().enumerate() {
                for (side, wheels) in groups.iter().enumerate() {
                    let hold = &mut self.rumble_hold[group][side];
                    if wheels.iter().any(|w| w.substance.to_lowercase().contains("rumble")) {
                        *hold = RUMBLE_HOLD;
                    } else {
                        *hold = m::max(0.0, *hold - DT);
                    }
                    if *hold > 0.0 {
                        rumble[side] = m::max(rumble[side], feel);
                    }
                }
            }
            let strip = if frame.speed.abs() > 1.0 {
                g.rumble * m::pow(m::min(1.0, frame.speed.abs() / 15.0), 0.5)
            } else {
                0.0
            };
            rumble_l = strip * rumble[0];
            rumble_r = strip * rumble[1];

            // Wheelspin: driven wheels outrunning the truck under power.
            self.learn_yaw_sign(frame);
            let powered = frame.engine_on && m::max(frame.user_throttle, frame.game_throttle) > 0.05;
            let spin = if powered { self.wheel_spin(frame) } else { 0.0 };
            self.spin = m::max(spin, self.spin - DT / 0.15); // let go over 150 ms, not on one frame
            if self.spin > 0.0 {
                spin_amp = g.wheelspin * m::min(1.0, 0.4 + self.spin);
            }
            if let Some(first) = frame.wheels.first() {
                self.levels.surface = first.substance.clone();
            }

            let rolling = m::min(1.0, speed / 5.0);
            if frame.retarder != 0 && frame.retarder_steps != 0 {
                retarder_amp = g.retarder * frame.retarder as f64 / frame.retarder_steps as f64 * rolling;
                let (low, high) = RETARDER_HZ;
                retarder_freq = low + (high - low) * m::min(1.0, speed / 25.0);
            }
            if frame.motor_brake && frame.engine_on && frame.rpm > 100.0 && load < 0.05 {
                jake_amp = g.retarder * (0.3 + 0.3 * rpm_frac) * rolling;
            }

            // One trigger channel: follow whichever pedal the player is pressing.
            lock = if frame.user_brake > 0.05 { self.wheel_lock(frame) } else { 0.0 };
            // the finger on the trigger is never quite still
            let throttle_moved = self.novelty(REF_THROTTLE, frame.user_throttle, 0.1, 0.03);
            let brake_moved = self.novelty(REF_BRAKE, frame.user_brake, 0.1, 0.03);
            if frame.user_brake > 0.05 && frame.user_brake >= frame.user_throttle {
                trigger_mode = TriggerMode::Brake;
                if lock > 0.0 {
                    trigger_amp = g.trigger * m::min(1.0, 0.6 + lock);
                } else {
                    trigger_amp = g.trigger
                        * (0.1 + 0.5 * frame.user_brake)
                        * speed_factor
                        * self.steady_gain(BED_TRIGGER, brake_moved);
                }
            } else if frame.user_throttle > 0.05 && frame.engine_on {
                trigger_mode = TriggerMode::Throttle;
                trigger_amp =
                    g.trigger * (0.2 + 0.6 * frame.user_throttle) * self.steady_gain(BED_TRIGGER, throttle_moved);
            }
        }

        if lock > 0.0 && !self.was_locked {
            self.lock_phase = 0.0; // the warning starts with a pulse, not up to half a cycle late
        }
        self.was_locked = lock > 0.0;
        if trigger_mode != TriggerMode::None {
            // after letting go, its tail stays on that pedal's trigger
            self.trigger_slot = if trigger_mode == TriggerMode::Brake { BRAKE_TRIGGER } else { THROTTLE_TRIGGER };
        }
        let sway_k = 1.0 - m::exp(-DT / CAB_SWAY_TAU);
        let wants = if live { [self.cab_sway.0, self.cab_sway.1] } else { [0.0, 0.0] };
        for (side, want) in wants.into_iter().enumerate() {
            self.sway[side] += (want - self.sway[side]) * sway_k;
            if want == 0.0 && self.sway[side] < 1e-4 {
                self.sway[side] = 0.0;
            }
        }
        let [sway_l, sway_r] = self.sway;
        let g = self.g.clone();
        let routes: [Channel; 18] = TOGGLEABLE.map(|e| g.route_of(e));
        // each vibration scaled for how strongly each motor gives back its frequency
        let eq_engine = self.bed_pair(engine_freq);
        let eq_retarder = self.bed_pair(retarder_freq);
        let eq_rumble = self.bed_pair(rumble_freq);
        let eq_rumble_half = self.bed_pair(rumble_freq / 2.0);
        let eq_road = [self.bed_pair(self.texture[0].pitch), self.bed_pair(self.texture[1].pitch)];
        let eq_spin = self.bed_pair(self.spin_noise.filter.center);
        let eq_squeal = self.eq.pair(SQUEAL_HZ);
        let eq_sway = self.eq.pair(CAB_SWAY_HZ);
        let eq_trigger =
            self.bed_pair(if trigger_mode == TriggerMode::Brake && lock > 0.0 { 110.0 } else { 100.0 });
        let freqs: Vec<f64> = self.bursts.iter().map(|e| e.burst.freq).collect();
        let eq_bursts: Vec<(f64, f64)> = freqs.into_iter().map(|f| self.eq.pair(f)).collect();
        let jake_mean = jake_mean();
        let duck_attack = duck_attack();
        let duck_release = duck_release();
        let mut block = Block::default();
        let mut heard = [false; 5]; // the TRIGGER_SLOTS something played in
        let events_2k = self.events_2k;
        let mut events: Events = [[0.0; FINE]; 3]; // left grip, right grip, trigger
        let p = self.prev_amp;
        for n in 0..SUBFRAMES {
            let k = (n + 1) as f64 / SUBFRAMES as f64;
            let e = p.engine + (engine_amp - p.engine) * k;
            let rl = p.road_l + (road_l - p.road_l) * k;
            let rr = p.road_r + (road_r - p.road_r) * k;
            let ul = p.rumble_l + (rumble_l - p.rumble_l) * k;
            let ur = p.rumble_r + (rumble_r - p.rumble_r) * k;
            let sp = p.spin + (spin_amp - p.spin) * k;
            let rt = p.retarder + (retarder_amp - p.retarder) * k;
            let jb = p.jake + (jake_amp - p.jake) * k;
            let eh = p.harsh + (engine_harsh - p.harsh) * k;
            let tr = p.trigger + (trigger_amp - p.trigger) * k;
            let sq = p.squeal + (squeal_amp - p.squeal) * k;
            let wl = p.sway_l + (sway_l - p.sway_l) * k;
            let wr = p.sway_r + (sway_r - p.sway_r) * k;

            let mut out = [0.0; 5]; // left grip, right grip, then the TRIGGER_SLOTS: events
            let mut bed = [0.0; 5]; // continuous effects, ducked under the events
            let firing = self.engine_osc.phase;
            let fire_gain = self.firing_gain;
            let w = TAU * firing;
            let osc = self.engine_osc.next(engine_freq);
            let engine = e * fire_gain * (osc + 0.3 * m::sin(2.0 * w) + eh * m::sin(3.0 * w));
            if self.engine_osc.phase < firing {
                // a cycle done: the next cylinder fires
                self.next_firing(engine_uneven);
            }
            mix(&mut bed, routes[Effect::Engine.index()], engine, engine, engine, eq_engine, BOTH_TRIGGERS);
            let retarder = rt * self.retarder_osc.next(retarder_freq);
            mix(&mut bed, routes[Effect::Retarder.index()], retarder, retarder, retarder, eq_retarder, BOTH_TRIGGERS);
            if jb != 0.0 {
                // on the engine's own firing, knock peak on its crest
                let jake = jb * jake_pulse(m::rem(firing - 0.25 + JAKE_PEAK, 1.0), jake_mean);
                mix(&mut bed, routes[Effect::Retarder.index()], jake, jake, jake, eq_engine, BOTH_TRIGGERS);
            }
            // Independent noises: 0.7 keeps their sum near one side's strength.
            let noise_l = rl * self.texture[0].next();
            let noise_r = rr * self.texture[1].next();
            if events_2k {
                for (side, amp) in [(0, rl), (1, rr)] {
                    let hit = self.texture[side].stone_hit;
                    if hit != 0.0 {
                        self.stones_2k[side].strike(self.hi_trim * STONE_HI * amp * hit);
                    }
                }
            }
            let ((grip_l, trig_l), (grip_r, trig_r)) = (eq_road[0], eq_road[1]);
            mix(
                &mut bed,
                routes[Effect::Road.index()],
                noise_l * grip_l,
                noise_r * grip_r,
                0.7 * (noise_l * trig_l + noise_r * trig_r),
                FLAT,
                BOTH_TRIGGERS,
            );
            if ul != 0.0 || ur != 0.0 {
                // the half-rate part takes the eq of its own frequency
                let (strip_l, half_l) = self.strip(0, rumble_freq);
                let (strip_r, half_r) = self.strip(1, rumble_freq);
                let (strip_l, half_l, strip_r, half_r) = (ul * strip_l, ul * half_l, ur * strip_r, ur * half_r);
                let route = routes[Effect::Rumble.index()];
                mix(&mut bed, route, strip_l, strip_r, 0.7 * (strip_l + strip_r), eq_rumble, BOTH_TRIGGERS);
                mix(&mut bed, route, half_l, half_r, 0.7 * (half_l + half_r), eq_rumble_half, BOTH_TRIGGERS);
            }
            if sp != 0.0 {
                // Low grinding noise, chattering as the tyres hop between grip and slip.
                let noise = self.spin_noise.next();
                let chatter = self.spin_chatter.next(13.0);
                let grind = 2.0 * sp * noise * (0.55 + 0.45 * chatter);
                mix(&mut bed, routes[Effect::Wheelspin.index()], grind, grind, grind, eq_spin, BOTH_TRIGGERS);
            }

            let t = if trigger_mode == TriggerMode::Brake && lock > 0.0 {
                let chop = self.lock_chop();
                tr * chop * self.trigger_osc.next(110.0)
            } else if trigger_mode == TriggerMode::Throttle {
                // 100 Hz carrier, pulsed with the engine's own firings
                let depth = m::min(1.0, m::max(0.0, g.throttle_pulse));
                let pulse = (1.0 - depth * 0.5 * (1.0 - m::sin(w))) * (1.0 + depth * (fire_gain - 1.0));
                tr * pulse * self.trigger_osc.next(100.0)
            } else {
                tr * self.trigger_osc.next(100.0)
            };
            mix(&mut bed, routes[Effect::Trigger.index()], t, t, t, eq_trigger, self.trigger_slot);
            if sq != 0.0 {
                let squeal = sq * self.squeal.next();
                mix(&mut out, routes[Effect::Squeal.index()], squeal, squeal, squeal, eq_squeal, BRAKE_TRIGGER);
            } else {
                self.squeal.restart();
            }
            if wl != 0.0 || wr != 0.0 {
                // one cab, one tone: the same swing on both grips, leaning as the cab does
                let c = self.sway_osc.next(CAB_SWAY_HZ);
                mix(&mut out, routes[Effect::Bumps.index()], wl * c, wr * c, 0.7 * (wl + wr) * c, eq_sway, BOTH_TRIGGERS);
            }
            let mut drive = [0.0; 5];
            // events_2k: the bursts at 2 kHz, two to this sample, the ticks apart, and the first
            // samples of the bursts that start on them
            let (mut fine, mut taps, mut starts) = ([[0.0; 5]; 2], [[0.0; 5]; 2], [[0.0; 5]; 2]);
            for (entry, &burst_eq) in self.bursts.iter_mut().zip(&eq_bursts) {
                let route = routes[entry.effect.index()];
                let slot = entry.burst.trigger_slot;
                if !entry.effect.duck_exempt() && entry.burst.ducks {
                    let d = entry.burst.level();
                    let (l, r) = side_pair(entry.side, d);
                    mix(&mut drive, route, l, r, d, FLAT, slot);
                }
                if events_2k {
                    let tick = entry.burst.is_tick();
                    for h in 0..2 {
                        let first = !tick && entry.burst.starts();
                        let b = entry.burst.next_at(RATE_2K);
                        let (l, r) = side_pair(entry.side, b);
                        mix(if tick { &mut taps[h] } else { &mut fine[h] }, route, l, r, b, burst_eq, slot);
                        if first {
                            mix(&mut starts[h], route, l, r, b, burst_eq, slot);
                        }
                    }
                } else {
                    let b = entry.burst.next();
                    let (l, r) = side_pair(entry.side, b);
                    mix(&mut out, route, l, r, b, burst_eq, slot);
                }
            }
            if events_2k {
                for half in &mut fine {
                    let (l, r) = (self.stones_2k[0].next(), self.stones_2k[1].next());
                    if l != 0.0 || r != 0.0 {
                        mix(half, routes[Effect::Road.index()], l, r, 0.7 * (l + r), FLAT, BOTH_TRIGGERS);
                    }
                }
            }
            for slot in TRIGGER_SLOTS {
                if out[slot] != 0.0 || bed[slot] != 0.0 || fine.iter().chain(&taps).any(|half| half[slot] != 0.0) {
                    heard[slot] = true;
                }
            }
            let ([f0, f1], [t0, t1], [s0, s1]) = (&mut fine, &mut taps, &mut starts);
            for mix in [&mut out, &mut bed, &mut drive, f0, f1, t0, t1, s0, s1] {
                // one trigger channel from here on
                mix[BOTH_TRIGGERS] += mix[BRAKE_TRIGGER] + mix[THROTTLE_TRIGGER];
            }
            for i in 0..3 {
                let target = m::min(1.0, drive[i] / DUCK_FULL) * g.ducking;
                if target > self.duck[i] {
                    self.duck[i] += (target - self.duck[i]) * duck_attack;
                } else {
                    self.duck[i] = m::max(target, self.duck[i] * duck_release);
                }
                out[i] += self.bed_filters[i].next(bed[i]) * (1.0 - self.duck[i]);
                if let Some(cut) = &mut self.cuts[i] {
                    out[i] = cut.next(out[i]);
                }
            }
            block.left_grip[n] = self.out(out[0], g.grip_level, CEILING_GRIP);
            block.right_grip[n] = self.out(out[1], g.grip_level, CEILING_GRIP);
            block.trigger[n] = self.out(out[2], g.trigger_level, CEILING_TRIGGER);
            if events_2k {
                for (i, (level, ceiling)) in
                    [(g.grip_level, CEILING_GRIP), (g.grip_level, CEILING_GRIP), (g.trigger_level, CEILING_TRIGGER)]
                        .into_iter()
                        .enumerate()
                {
                    // where the pad's line runs at the two 2 kHz samples: halfway from the last
                    // sample, then this one
                    let under = [(self.line_from[i] + out[i]) / 2.0, out[i]];
                    for h in 0..2 {
                        let [burst, tick] = [fine[h][i], taps[h][i]];
                        let [cut_burst, cut_tick] = &mut self.cuts_2k[i];
                        let burst = cut_burst.as_mut().map_or(burst, |cut| cut.next(burst));
                        // a tick is one sample as much higher as it is shorter: it goes through
                        // the soft limit at the height it had at 1 kHz, which it pushes with
                        let tick = cut_tick.as_mut().map_or(tick, |cut| cut.next(tick)) / STRETCH;
                        let carried = std::mem::take(&mut self.onset_carry[i]);
                        if burst != 0.0 || tick != 0.0 || carried != 0.0 {
                            // what the events add through the soft limit, on top of what plays under them
                            let (u, with) = (under[h], under[h] + burst);
                            let limit = |x: f64| self.out(x, level, ceiling);
                            // The pad's line rose to a burst's first 1 kHz sample over the
                            // millisecond before it, so a burst that starts on its crest pushed
                            // half a sample more than its own shape. At 2 kHz that rise takes half
                            // as long: the first two samples give the rest back, of the sample as
                            // limited, which is what the line was drawn through (given ahead of
                            // the limit, the gear's clack still lost a quarter of its push under
                            // 50 Hz). Spread over two, it adds little over 500 Hz.
                            let first = starts[h][i];
                            let onset = if first == 0.0 {
                                0.0
                            } else {
                                (limit(with) - limit(with - first)) * (STRETCH - 1.0) / 4.0
                            };
                            events[i][2 * n + h] =
                                limit(with) - limit(u) + onset + carried + STRETCH * (limit(with + tick) - limit(with));
                            self.onset_carry[i] = onset;
                        }
                    }
                }
            }
            self.line_from = [out[0], out[1], out[2]];
        }
        if events.iter().flatten().any(|&x| x != 0.0) {
            block.events = Some(Box::new(events));
        }
        self.prev_amp = Amps {
            engine: engine_amp,
            road_l,
            road_r,
            rumble_l,
            rumble_r,
            spin: spin_amp,
            retarder: retarder_amp,
            jake: jake_amp,
            trigger: trigger_amp,
            squeal: squeal_amp,
            harsh: engine_harsh,
            sway_l,
            sway_r,
        };
        self.bursts.retain(|e| !e.burst.done());

        let lv = &mut self.levels;
        lv.engine = engine_amp;
        lv.road_left = road_l;
        lv.road_right = road_r;
        lv.trigger = trigger_amp;
        lv.trigger_mode = match trigger_mode {
            TriggerMode::None => "-",
            TriggerMode::Brake => "brake",
            TriggerMode::Throttle => "throttle",
        };
        if block.trigger.iter().all(|&x| x == 0.0) && block.events.as_ref().is_none_or(|e| e[2].iter().all(|&x| x == 0.0)) {
            block.route = Route::Mute;
            return block;
        }
        // the 0x57 route stays BOTH, as Flydigi's engine sends it: the side is picked by Block::side
        block.route = Route::Both;
        block.side = Some(self.trigger_side(&heard));
        block
    }

    /// Where this block's trigger channel plays: one pedal's trigger when only its effects
    /// played on the channel, else both; with nothing in the mix, where it played last.
    fn trigger_side(&mut self, heard: &[bool; 5]) -> Route {
        let (both, brake, throttle) = (heard[BOTH_TRIGGERS], heard[BRAKE_TRIGGER], heard[THROTTLE_TRIGGER]);
        if brake && !both && !throttle {
            self.trigger_side = Route::Left;
        } else if throttle && !both && !brake {
            self.trigger_side = Route::Right;
        } else if both || brake || throttle {
            self.trigger_side = Route::Both;
        }
        self.trigger_side
    }

    /// One sample of a side's strip: the grooves with their harmonics, and the half-rate part.
    fn strip(&mut self, side: usize, freq: f64) -> (f64, f64) {
        let w = TAU * self.rumble_osc[side].phase;
        let (half, second, third) = RUMBLE_SHAPE;
        let grooves = self.rumble_osc[side].next(freq) + second * m::sin(2.0 * w) + third * m::sin(3.0 * w);
        (RUMBLE_LEVEL * grooves, RUMBLE_LEVEL * half * self.rumble_half[side].next(freq / 2.0))
    }

    /// Wheel-lock gate: on for the first half of each LOCK_RATE cycle, with LOCK_EDGE ramps.
    fn lock_chop(&mut self) -> f64 {
        let p = self.lock_phase;
        self.lock_phase = m::rem(p + LOCK_RATE / SR, 1.0);
        if p >= 0.5 {
            return 0.0;
        }
        let edge = LOCK_EDGE * LOCK_RATE;
        m::min(m::min(1.0, p / edge), (0.5 - p) / edge)
    }

    /// Strength of the next cylinder's firing: its own share of the unevenness, which grows
    /// towards idle (a V8's stays put), and a little difference from one cycle to the next.
    fn next_firing(&mut self, uneven: f64) {
        let cylinders = self.g.cylinders.clamp(1, self.cylinder_offsets.len() as i64) as usize;
        self.cylinder = (self.cylinder + 1) % cylinders;
        let used = &self.cylinder_offsets[..cylinders];
        let mean = m::sum(used.iter().copied()) / cylinders as f64;
        let offset = used[self.cylinder] - mean;
        let imbalance = if cylinders == 8 {
            let spread = (m::sum(used.iter().map(|o| m::pow(o - mean, 2.0))) / cylinders as f64).sqrt();
            // as strong overall as an even engine, so the engine level stays where it was
            (1.0 + ENGINE_V8_PATTERN * offset / spread) / (1.0 + m::pow(ENGINE_V8_PATTERN, 2.0)).sqrt() - 1.0
        } else {
            ENGINE_IMBALANCE * uneven * offset
        };
        self.firing_gain = (1.0 + imbalance) * (1.0 + ENGINE_JITTER * self.rng.uniform(-1.0, 1.0));
    }

    /// (grip, trigger) gain for a continuous effect at freq: its eq, and what the bed's low-pass takes there.
    fn bed_pair(&mut self, freq: f64) -> (f64, f64) {
        if let Some(found) = self.bed_pairs.get(&freq.to_bits()) {
            return *found;
        }
        if self.bed_pairs.len() >= PAIR_CACHE {
            self.bed_pairs.clear();
        }
        let lost = self.bed_shape.magnitude(freq);
        let (grip, trigger) = self.eq.pair(freq);
        let found = (grip / lost, trigger / lost);
        self.bed_pairs.insert(freq.to_bits(), found);
        found
    }

    /// Soft limit so stacked effects saturate smoothly instead of clipping.
    fn out(&self, x: f64, channel_level: f64, ceiling: f64) -> f64 {
        m::tanh(x * self.g.master * channel_level * MAX_SAMPLE / ceiling) * ceiling
    }
}

fn side_pair(side: Side, value: f64) -> (f64, f64) {
    (if side == Side::R { 0.0 } else { value }, if side == Side::L { 0.0 } else { value })
}

/// Add one effect's sample to the channels its route selects, scaled by its (grip, trigger)
/// eq; on the trigger, in the slot of the pedal it belongs to.
fn mix(out: &mut [f64; 5], route: Channel, left: f64, right: f64, trigger: f64, eq: (f64, f64), slot: usize) {
    if route != Channel::Trigger {
        out[0] += left * eq.0;
        out[1] += right * eq.0;
    }
    if route != Channel::Grip {
        out[slot] += trigger * eq.1;
    }
}

/// Straight lines through (x, y) points: the first y below them, the last above.
fn ramp(x: f64, points: &[(f64, f64)]) -> f64 {
    if x <= points[0].0 {
        return points[0].1;
    }
    for pair in points.windows(2) {
        let ((x0, y0), (x1, y1)) = (pair[0], pair[1]);
        if x <= x1 {
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
        }
    }
    points[points.len() - 1].1
}
