//! Equal-strength calibration of the motors, for effects::Equalizer (port of calibrate.py).
//!
//! The voice coils resonate (grips near 65 Hz, triggers near 100 Hz), so the same drive feels
//! weaker off resonance. Each frequency is only ever compared with its neighbour, working
//! outward from the motor's reference; the two play in random order and the player says
//! which was stronger; an up-down staircase on those answers closes in on the gain at which
//! the two feel the same. The window runs it in a dialog and takes the answers from its
//! buttons, the keyboard or the pad's own buttons.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::apex6::{Apex6, Block, Link, Route, FRAME_PERIOD, MAX_SAMPLE, SAMPLE_RATE, SUBFRAMES};
use crate::pymath as m;
use crate::pyrandom::PyRandom;

pub const CHANNELS: [&str; 2] = ["grip", "trigger"];
pub fn reference(channel: &str) -> f64 {
    if channel == "grip" {
        65.0
    } else {
        100.0
    }
}
pub fn freqs(channel: &str) -> &'static [f64] {
    if channel == "grip" {
        &[30.0, 40.0, 50.0, 80.0, 100.0, 130.0, 160.0, 200.0, 250.0]
    } else {
        &[50.0, 65.0, 80.0, 130.0, 160.0, 200.0, 250.0]
    }
}
pub const GAP_MS: usize = 300; // silence between the two tones
pub const AMP: f64 = 25.0; // reference strength in percent
pub const SECONDS: f64 = 0.8; // each tone
const LIMITS: (f64, f64) = (0.25, 4.0); // Equalizer.LIMITS

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Answer {
    First,
    Second,
    Replay,
    Cancel,
}

/// Python's f"{x:g}" for the frequencies used as curve keys.
fn key(freq: f64) -> String {
    crate::telemetry::format_g(freq)
}

/// (frequency, neighbour) in the order they are matched: up from the reference, then down.
pub fn neighbours(channel: &str) -> Vec<(f64, f64)> {
    let mut all: Vec<f64> = freqs(channel).to_vec();
    all.push(reference(channel));
    all.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let r = all.iter().position(|f| *f == reference(channel)).unwrap();
    (r + 1..all.len()).map(|i| (all[i], all[i - 1])).chain((0..r).rev().map(|i| (all[i], all[i + 1]))).collect()
}

/// The test tone's level against its neighbour, in dB, from answers to "which was stronger".
#[derive(Clone, Debug)]
pub struct Staircase {
    low_db: f64,
    high_db: f64,
    pub db: f64,
    pub trials: usize,
    reversals: Vec<f64>,
    direction: i32,
    pinned: usize,
}

const START_DB: f64 = 3.0;
const FINE_DB: f64 = 1.5;
const REVERSALS: usize = 5;
const MAX_TRIALS: usize = 16;

impl Staircase {
    pub fn new(low_db: f64, high_db: f64) -> Staircase {
        Staircase {
            low_db,
            high_db,
            db: m::min(m::max(0.0, low_db), high_db),
            trials: 0,
            reversals: Vec::new(),
            direction: 0,
            pinned: 0,
        }
    }

    pub fn done(&self) -> bool {
        self.reversals.len() >= REVERSALS || self.trials >= MAX_TRIALS || self.pinned >= 2
    }

    pub fn result_db(&self) -> f64 {
        if self.pinned >= 2 || self.reversals.len() < 2 {
            return self.db;
        }
        m::sum(self.reversals[1..].iter().copied()) / (self.reversals.len() - 1) as f64
    }

    pub fn answer(&mut self, test_stronger: bool) {
        self.trials += 1;
        let direction = if test_stronger { -1 } else { 1 };
        if self.direction != 0 && direction != self.direction {
            self.reversals.push(self.db);
        }
        self.direction = direction;
        let step = if self.reversals.is_empty() { START_DB } else { FINE_DB };
        let wanted = self.db + direction as f64 * step;
        self.db = m::min(m::max(wanted, self.low_db), self.high_db);
        self.pinned = if wanted != self.db { self.pinned + 1 } else { 0 };
    }
}

/// Where a calibration stands: which pair is being compared, the staircase, the gains found.
pub struct Session {
    pub channels: Vec<String>,
    pub amp: f64,
    pub seconds: f64,
    rng: PyRandom,
    pub steps: Vec<(String, f64, f64)>,
    pub index: usize,
    pub curves: BTreeMap<String, BTreeMap<String, f64>>,
    /// channels with every frequency matched
    pub finished: Vec<String>,
    pub staircase: Staircase,
    test_first: bool,
}

impl Session {
    pub fn new(channels: &[&str]) -> Session {
        Session::with_rng(channels, PyRandom::from_entropy())
    }

    pub fn with_rng(channels: &[&str], rng: PyRandom) -> Session {
        let steps = channels
            .iter()
            .flat_map(|c| neighbours(c).into_iter().map(move |(f, n)| (c.to_string(), f, n)))
            .collect();
        let curves = channels.iter().map(|c| (c.to_string(), BTreeMap::from([(key(reference(c)), 1.0)]))).collect();
        let mut session = Session {
            channels: channels.iter().map(|c| c.to_string()).collect(),
            amp: AMP,
            seconds: SECONDS,
            rng,
            steps,
            index: 0,
            curves,
            finished: Vec::new(),
            staircase: Staircase::new(0.0, 0.0),
            test_first: true,
        };
        session.start_step();
        session
    }

    pub fn current(&self) -> Option<&(String, f64, f64)> {
        self.steps.get(self.index)
    }

    pub fn gain(&self, channel: &str, freq: f64) -> f64 {
        self.curves[channel][&key(freq)]
    }

    fn start_step(&mut self) {
        let Some((channel, _, neighbour)) = self.current().cloned() else { return };
        let base = self.gain(&channel, neighbour);
        let (low, high) = LIMITS;
        let high = m::min(high, 100.0 / self.amp); // the output cannot go past full scale
        self.staircase = Staircase::new(20.0 * m::log10(low / base), 20.0 * m::log10(high / base));
        self.test_first = self.rng.random() < 0.5;
    }

    /// The two tones of this trial in play order, as (frequency, gain).
    pub fn tones(&self) -> [(f64, f64); 2] {
        let (channel, freq, neighbour) = self.current().unwrap();
        let base = self.gain(channel, *neighbour);
        let test = (*freq, base * m::pow(10.0, self.staircase.db / 20.0));
        let other = (*neighbour, base);
        if self.test_first {
            [test, other]
        } else {
            [other, test]
        }
    }

    /// Take First or Second; returns the channel this answer has just finished, if any.
    pub fn answer(&mut self, answer: Answer) -> Option<String> {
        if self.current().is_none() || !matches!(answer, Answer::First | Answer::Second) {
            return None;
        }
        let first = answer == Answer::First;
        self.staircase.answer(first == self.test_first);
        if !self.staircase.done() {
            self.test_first = self.rng.random() < 0.5;
            return None;
        }
        let (channel, freq, neighbour) = self.current().cloned().unwrap();
        let (low, high) = LIMITS;
        let gain = self.gain(&channel, neighbour) * m::pow(10.0, self.staircase.result_db() / 20.0);
        // round(x, 3): the exact value rounded half to even, as {:.3} does
        let rounded: f64 = format!("{:.3}", m::min(m::max(gain, low), high)).parse().unwrap();
        self.curves.get_mut(&channel).unwrap().insert(key(freq), rounded);
        self.index += 1;
        self.start_step();
        if self.current().is_none_or(|(c, _, _)| *c != channel) {
            self.finished.push(channel.clone());
            return Some(channel);
        }
        None
    }

    /// The finished channels' curves, as config.json keeps them.
    pub fn finished_curves(&self) -> Map<String, Value> {
        self.finished
            .iter()
            .map(|c| {
                let curve: Map<String, Value> = self.curves[c].iter().map(|(f, g)| (f.clone(), Value::from(*g))).collect();
                (c.clone(), Value::Object(curve))
            })
            .collect()
    }
}

/// tone.py's tone_renderer for the grips or the triggers: a sine with 15 ms fades.
pub fn tone_renderer(triggers: bool, freq: f64, amp_percent: f64, seconds: f64) -> impl FnMut(usize) -> Option<Block> {
    let total = (seconds * SAMPLE_RATE as f64) as usize;
    let fade = ((0.015 * SAMPLE_RATE as f64) as usize).max(1);
    let peak = MAX_SAMPLE * m::max(0.0, m::min(100.0, amp_percent)) / 100.0;
    let mut phase = 0.0;
    move |first| {
        if first >= total {
            return None;
        }
        let mut samples = [0.0; SUBFRAMES];
        for (i, n) in (first..first + SUBFRAMES).enumerate() {
            if n >= total {
                continue;
            }
            let envelope = m::min(m::min(1.0, n as f64 / fade as f64), (total - n) as f64 / fade as f64);
            samples[i] = m::sin(2.0 * m::PI * phase) * peak * envelope;
            phase = m::rem(phase + freq / SAMPLE_RATE as f64, 1.0);
        }
        Some(if triggers {
            Block { trigger: samples, route: Route::Both, ..Block::default() }
        } else {
            Block { left_grip: samples, right_grip: samples, ..Block::default() }
        })
    }
}

/// The trial's two tones with a pause between; an answer heard on the way stops them and is returned.
pub fn play<L: Link, C: crate::apex6::Clock>(pad: &mut Apex6<L, C>, session: &Session,
                                              listen: &mut dyn FnMut() -> Option<Answer>)
    -> Result<Option<Answer>, crate::apex6::PadError> {
    let triggers = session.current().unwrap().0 == "trigger";
    let [(f1, g1), (f2, g2)] = session.tones();
    let mut heard = None;
    let mut renders: [Box<dyn FnMut(usize) -> Option<Block>>; 3] = [
        Box::new(tone_renderer(triggers, f1, m::min(100.0, session.amp * g1), session.seconds)),
        Box::new(|first| (first < GAP_MS).then(Block::default)),
        Box::new(tone_renderer(triggers, f2, m::min(100.0, session.amp * g2), session.seconds)),
    ];
    for render in renders.iter_mut() {
        pad.stream(
            |first| {
                if heard.is_none() {
                    heard = listen();
                }
                if heard.is_some() {
                    None
                } else {
                    render(first)
                }
            },
            None,
            FRAME_PERIOD,
        )?;
        if heard.is_some() {
            return Ok(heard);
        }
    }
    Ok(None)
}

/// Poll for the next answer; Cancel once cancelled() says so.
pub fn wait(listen: &mut dyn FnMut() -> Option<Answer>, cancelled: &dyn Fn() -> bool) -> Answer {
    while !cancelled() {
        if let Some(answer) = listen() {
            return answer;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    Answer::Cancel
}

/// Store the finished channels, keeping a motor measured before and not redone now.
pub fn save(session: &Session, settings: &crate::settings::Settings) -> std::io::Result<()> {
    let mut curves = settings.load_eq();
    for (channel, curve) in session.finished_curves() {
        curves.insert(channel, curve);
    }
    settings.save_eq(&curves)
}

/// New presses on any XInput pad, which the APEX is in its usual Xbox mode: D-pad left or LB
/// the first, D-pad right or RB the second, X replay, B stop. XInput works whichever window
/// has the focus. A button already held when this starts counts once it goes down again.
pub struct PadButtons {
    held: u16,
    pub connected: bool,
}

const MAP: [(u16, Answer); 6] = [
    (0x0004, Answer::First),
    (0x0100, Answer::First),
    (0x0008, Answer::Second),
    (0x0200, Answer::Second),
    (0x4000, Answer::Replay),
    (0x2000, Answer::Cancel),
];

impl Default for PadButtons {
    fn default() -> Self {
        let mut pad = PadButtons { held: 0, connected: false };
        pad.held = pad.buttons();
        pad
    }
}

impl PadButtons {
    fn buttons(&mut self) -> u16 {
        use windows_sys::Win32::UI::Input::XboxController::{XInputGetState, XINPUT_STATE};
        let mut buttons = 0;
        let mut connected = false;
        for index in 0..4 {
            // SAFETY: XInputGetState fills the state it is given.
            let mut state: XINPUT_STATE = unsafe { std::mem::zeroed() };
            if unsafe { XInputGetState(index, &mut state) } == 0 {
                connected = true;
                buttons |= state.Gamepad.wButtons;
            }
        }
        self.connected = connected;
        buttons
    }

    /// Buttons gone down since the last call.
    pub fn pressed(&mut self) -> u16 {
        let buttons = self.buttons();
        let pressed = buttons & !self.held;
        self.held = buttons;
        pressed
    }

    pub fn poll(&mut self) -> Option<Answer> {
        let pressed = self.pressed();
        MAP.iter().find(|(mask, _)| pressed & mask != 0).map(|(_, answer)| *answer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn neighbours_go_up_from_the_reference_then_down() {
        assert_eq!(
            neighbours("grip"),
            vec![(80.0, 65.0), (100.0, 80.0), (130.0, 100.0), (160.0, 130.0), (200.0, 160.0), (250.0, 200.0),
                 (50.0, 65.0), (40.0, 50.0), (30.0, 40.0)]
        );
    }

    #[test]
    fn a_player_who_always_hears_the_test_stronger_drives_it_down() {
        let mut session = Session::with_rng(&["trigger"], PyRandom::new(3));
        while session.current().is_some() {
            let test_first = session.tones()[0].0 == session.current().unwrap().1;
            session.answer(if test_first { Answer::First } else { Answer::Second });
        }
        assert_eq!(session.finished, vec!["trigger".to_string()]);
        let curve = &session.curves["trigger"];
        assert_eq!(curve["100"], 1.0);
        assert!(curve.iter().filter(|(f, _)| *f != "100").all(|(_, g)| *g < 1.0), "{curve:?}");
    }

    #[test]
    fn tones_fade_in_and_out() {
        let mut render = tone_renderer(false, 65.0, 50.0, 0.1);
        let first = render(0).unwrap();
        assert_eq!(first.left_grip[0], 0.0);
        let mut blocks = 1;
        while render(blocks * SUBFRAMES).is_some() {
            blocks += 1;
        }
        assert_eq!(blocks, 13); // 100 samples in blocks of 8
    }
}
