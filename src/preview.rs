//! Play one effect alone for a few seconds: the ▶ test button beside each effect (port of
//! preview.py).
//!
//! Each effect gets a short scripted moment that sets it off, rendered by the same synth as
//! driving, so it plays at its current strength and route and with the motors' calibration.
//! An effect that is switched off plays too. Effects with a side go left first, then right.
//! The steady fade is left out. The road can also play one surface alone, or every surface
//! in turn. While the output runs, the Runner plays a preview in place of the drive; while it
//! is stopped, Player takes the controller only for as long as one plays.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use crate::apex6::{Apex6, Block, PadError, FRAME_PERIOD, MAX_SAMPLE, SAMPLE_RATE, SUBFRAMES};
use crate::effects::{surface_of, Effect, Equalizer, Gains, Synth, DT, TAU, TOGGLEABLE};
use crate::pymath as m;
use crate::rate2k::Output2k;
use crate::telemetry::{Frame, Wheel};

pub const FADE_S: f64 = 0.1;
const CRUISE: f64 = 22.0; // m/s, 80 km/h
const TRUCK_WHEELS: [f64; 6] = [-1.0, 1.0, -0.9, 0.9, -0.9, 0.9]; // the steer axle, then the two drive axles
const ROUGH: f64 = 0.0006;
const LOOSE: f64 = 5.0;
/// the road test's surfaces, smooth to rough as the road slider's tip lists them
pub const ROAD_SURFACES: [&str; 9] = ["ice", "road", "concrete", "snow", "wood", "metal", "grass", "dirt", "gravel"];
pub const SURFACE_S: f64 = 3.0; // one surface alone
pub const SURFACE_STEP_S: f64 = 5.0; // each surface when they play in turn
/// (s, side, depth m, width s): a joint under the left front wheel, one under the right, then a pothole each
const HITS: [(f64, f64, f64, f64); 4] = [(0.3, -1.0, 0.012, 0.02), (0.9, 1.0, 0.012, 0.02), (1.6, -1.0, 0.06, 0.04),
                                        (2.4, 1.0, 0.06, 0.04)];

/// Cruising in tenth gear on asphalt.
fn frame(t: f64, speed: f64, throttle: f64, rough: f64) -> Frame {
    let mut frame = Frame {
        active: true,
        paused: false,
        render_time: (t * 1e6) as i64,
        rpm: 1300.0,
        speed,
        gear: 10,
        engine_on: true,
        user_throttle: throttle,
        game_throttle: throttle,
        ..Frame::default()
    };
    frame.wheels = TRUCK_WHEELS
        .iter()
        .enumerate()
        .map(|(i, &x)| Wheel {
            deflection: 0.02 + rough * m::sin(t * TAU * 6.0 + i as f64),
            pos_x: x,
            velocity: speed / (TAU * 0.5),
            radius: 0.5,
            ..Wheel::default()
        })
        .collect();
    frame
}

fn cruise(t: f64) -> Frame {
    frame(t, CRUISE, 0.3, ROUGH)
}

/// A frame with the brake set, which the game applies as well.
fn braking(mut frame: Frame, brake: f64) -> Frame {
    frame.user_brake = brake;
    frame.game_brake = brake;
    frame
}

fn parked(t: f64) -> Frame {
    let mut frame = frame(t, 0.0, 0.0, ROUGH);
    frame.gear = 0;
    frame.rpm = 650.0;
    frame
}

fn engine(t: f64) -> Frame {
    // idle with its lope, then a rev under load and back down
    let rise = if t < 2.4 { m::min(1.0, m::max(0.0, (t - 1.2) / 0.8)) } else { m::max(0.0, 1.0 - (t - 2.4) / 0.6) };
    let mut f = parked(t);
    f.rpm = 650.0 + 1050.0 * rise;
    let load = if (1.2..2.4).contains(&t) { 0.8 } else { 0.0 };
    f.user_throttle = load;
    f.game_throttle = load;
    f
}

fn road(t: f64) -> Frame {
    let gravel = t >= 1.6;
    let mut f = frame(t, CRUISE, 0.3, if gravel { LOOSE * ROUGH } else { ROUGH });
    for wheel in &mut f.wheels {
        wheel.substance = if gravel { "gravel" } else { "road" }.into();
    }
    f
}

/// Cruising on one surface; loose ground moves the suspension more, as gravel does above.
fn on_surface(t: f64, substance: &str) -> Frame {
    let loose = surface_of(substance).is_some_and(|s| s.stones >= 1.0);
    let mut f = frame(t, CRUISE, 0.3, if loose { LOOSE * ROUGH } else { ROUGH });
    for wheel in &mut f.wheels {
        wheel.substance = substance.into();
    }
    f
}

fn bumps(t: f64) -> Frame {
    let mut f = frame(t, CRUISE, 0.3, 0.0);
    for wheel in f.wheels[..2].iter_mut() {
        // the steer axle: the axles behind would knock again, later
        wheel.deflection -= m::sum(
            HITS.iter()
                .filter(|(_, side, _, _)| side * wheel.pos_x > 0.0)
                .map(|&(at, _, depth, width)| depth * m::exp(-m::pow((t - at) / width, 2.0))),
        );
    }
    f
}

fn rumble(t: f64) -> Frame {
    let mut f = cruise(t);
    let side = if (0.2..1.2).contains(&t) { -1.0 } else if (1.6..2.6).contains(&t) { 1.0 } else { 0.0 };
    for wheel in &mut f.wheels {
        if side * wheel.pos_x > 0.0 {
            wheel.substance = "rumble_stripe".into();
        }
    }
    f
}

fn wheelspin(t: f64) -> Frame {
    let spinning = (0.2..1.8).contains(&t);
    let mut f = frame(t, 3.0, if spinning { 0.9 } else { 0.0 }, ROUGH);
    f.gear = 1;
    f.rpm = if spinning { 1500.0 } else { 800.0 };
    if spinning {
        for wheel in f.wheels[2..].iter_mut() {
            wheel.velocity = 9.0 / (TAU * wheel.radius);
        }
    }
    f
}

fn gear(t: f64) -> Frame {
    // pulling up through the gears, which a torque converter's push follows
    let mut f = frame(t, CRUISE, 0.7, ROUGH);
    f.gear = 8 + [0.3, 0.9, 1.5].iter().filter(|at| t >= **at).count() as i64;
    f
}

fn retarder(t: f64) -> Frame {
    // the stages one by one, then the engine brake
    let mut f = frame(t, 17.0, 0.0, ROUGH);
    f.rpm = 1500.0;
    f.retarder_steps = 4;
    f.motor_brake = (2.6..3.8).contains(&t);
    if (0.2..2.4).contains(&t) {
        f.retarder = 4.min(1 + ((t - 0.2) / 0.5) as i64);
    }
    f
}

fn impact(t: f64, gains: &Gains) -> Frame {
    let jump = gains.impact_threshold + 15.0; // past whatever the threshold is set to
    let mut f = cruise(t);
    f.accel = [0.0, 0.0, if (0.3..0.4).contains(&t) { jump } else { 0.0 }];
    f
}

fn trigger(t: f64) -> Frame {
    if t < 1.9 {
        // pressing the throttle in, slowly to the floor, and letting go
        let throttle = if t < 1.6 { m::min(1.0, m::max(0.0, (t - 0.2) / 1.4)) } else { 0.0 };
        return frame(t, CRUISE, throttle, ROUGH);
    }
    // braking, the drive axles locking for a second
    let mut f = braking(frame(t, 15.0, 0.0, ROUGH), 0.6);
    if (2.1..3.1).contains(&t) {
        for wheel in f.wheels[2..].iter_mut() {
            wheel.velocity *= 0.2;
        }
    }
    f
}

fn pedal(t: f64) -> Frame {
    let throttle = if (0.3..0.9).contains(&t) { 1.0 } else { 0.0 };
    braking(frame(t, CRUISE, throttle, ROUGH), if (1.3..1.9).contains(&t) { 1.0 } else { 0.0 })
}

fn squeal(t: f64) -> Frame {
    // rolling to a stop on a light brake
    let speed = m::max(0.3, 3.8 - 3.5 * m::max(0.0, t - 0.2) / 2.0);
    let mut f = braking(frame(t, speed, 0.0, ROUGH), 0.3);
    f.gear = 1;
    f.rpm = 700.0;
    f
}

fn blinker(t: f64) -> Frame {
    let lamp = ((t / 0.4) as i64) % 2 == 1; // each side flashes twice: on, off, on, off
    let mut f = cruise(t);
    f.blinker_left = lamp && t < 1.8;
    f.blinker_right = lamp && (2.0..3.4).contains(&t);
    f
}

pub(crate) fn script(effect: Effect, t: f64, gains: &Gains) -> Frame {
    match effect {
        Effect::Engine => engine(t),
        Effect::Road => road(t),
        Effect::Bumps => bumps(t),
        Effect::Rumble => rumble(t),
        Effect::Wheelspin => wheelspin(t),
        Effect::Gear => gear(t),
        Effect::Retarder => retarder(t),
        Effect::Impact => impact(t, gains),
        Effect::Trigger => trigger(t),
        Effect::Pedal => pedal(t),
        Effect::Squeal => squeal(t),
        Effect::Trailer => Frame { trailer_attached: (0.3..1.5).contains(&t), ..parked(t) },
        Effect::ParkBrake => Frame { park_brake: (0.3..1.8).contains(&t), ..parked(t) },
        Effect::AirBrake => {
            let brake = if (0.2..0.9).contains(&t) { 0.5 } else if (1.3..2.0).contains(&t) { 1.0 } else { 0.0 };
            braking(frame(t, 10.0, 0.0, ROUGH), brake)
        }
        Effect::Blinker => blinker(t),
        Effect::EngineStart => {
            let on = (0.3..2.2).contains(&t);
            Frame { engine_on: on, rpm: if on { 650.0 } else { 0.0 }, ..parked(t) }
        }
        Effect::Shifter => Frame { shifter_toggle: [(0.3..1.5).contains(&t), t >= 0.9], ..cruise(t) },
        Effect::GameEvents => {
            let events: BTreeSet<String> =
                if (0.3..0.8).contains(&t) { BTreeSet::from(["tollgate".to_string()]) } else { BTreeSet::new() };
            Frame { game_events: events, ..parked(t) }
        }
    }
}

/// Seconds an effect's moment takes, its tail included.
pub fn seconds_of(effect: Effect) -> f64 {
    match effect {
        Effect::Engine | Effect::Road | Effect::Bumps => 3.2,
        Effect::Rumble => 3.0,
        Effect::Wheelspin | Effect::Pedal | Effect::Shifter => 2.2,
        Effect::Gear => 2.0,
        Effect::Retarder => 4.0,
        Effect::Impact | Effect::GameEvents => 1.0,
        Effect::Trigger => 3.3,
        Effect::Squeal | Effect::Trailer => 2.4,
        Effect::ParkBrake => 2.6,
        Effect::AirBrake => 2.8,
        Effect::Blinker => 3.6,
        Effect::EngineStart => 3.4,
    }
}

/// One effect alone through its scripted moment, a block at a time.
pub struct Preview {
    pub effect: Effect,
    /// the road on these substances, one after another, in place of the effect's own moment
    pub surfaces: Vec<String>,
    step: f64,
    pub seconds: f64,
    gains: Gains,
    synth: Synth,
    blocks: usize,
}

impl Preview {
    pub fn new(effect: Effect, gains: &Gains, eq: Equalizer, surfaces: &[&str]) -> Preview {
        Preview::with_synth(effect, gains, surfaces, |gains| Synth::new(gains, eq))
    }

    /// With the synth made by `make` from the preview's gains (the tests seed its noise).
    pub fn with_synth(effect: Effect, gains: &Gains, surfaces: &[&str], make: impl FnOnce(Gains) -> Synth) -> Preview {
        let step = if surfaces.len() == 1 { SURFACE_S } else { SURFACE_STEP_S };
        let seconds = if surfaces.is_empty() { seconds_of(effect) } else { step * surfaces.len() as f64 };
        let mut gains = gains.clone();
        gains.disabled = TOGGLEABLE.iter().filter(|e| **e != effect).map(|e| e.name().to_string()).collect();
        gains.steady_fade = 0.0;
        let synth = make(gains.effective());
        Preview { effect, surfaces: surfaces.iter().map(|s| s.to_string()).collect(), step, seconds, gains, synth, blocks: 0 }
    }

    /// Render for the 2 kHz output (Synth::set_events_2k).
    pub fn set_events_2k(&mut self, on: bool) {
        self.synth.set_events_2k(on);
    }

    /// The rings and stone clicks above 500 Hz this many dB from their level, None for off
    /// (Synth::set_hi_trim).
    pub fn set_hi_trim(&mut self, db: Option<f64>) {
        self.synth.set_hi_trim(db);
    }

    fn surface_at(&self, t: f64) -> &str {
        &self.surfaces[((t / self.step) as usize).min(self.surfaces.len() - 1)]
    }

    /// The substance under the wheels now; "" when not driving over chosen surfaces.
    pub fn surface(&self) -> String {
        if self.surfaces.is_empty() {
            String::new()
        } else {
            self.surface_at(self.blocks as f64 * DT).to_string()
        }
    }

    pub fn render(&mut self) -> Option<Block> {
        let t = self.blocks as f64 * DT;
        if t >= self.seconds {
            return None;
        }
        self.blocks += 1;
        let frame = if self.surfaces.is_empty() {
            script(self.effect, t, &self.gains)
        } else {
            let substance = self.surface_at(t).to_string();
            on_surface(t, &substance)
        };
        let mut block = self.synth.render(Some(&frame));
        if self.seconds - t >= FADE_S + DT {
            return Some(block);
        }
        // the last stretch fades out instead of stopping on a step (a click)
        for n in 0..SUBFRAMES {
            let fade = m::max(0.0, m::min(1.0, (self.seconds - t - (n + 1) as f64 / SAMPLE_RATE as f64) / FADE_S));
            block.trigger[n] *= fade;
            block.left_grip[n] *= fade;
            block.right_grip[n] *= fade;
            if let Some(events) = &mut block.events {
                for channel in events.iter_mut() {
                    channel[2 * n] *= fade;
                    channel[2 * n + 1] *= fade;
                }
            }
        }
        Some(block)
    }
}

#[derive(Default)]
struct PlayerState {
    pending: Option<Preview>,
    current: Option<Preview>,
    running: bool,
    /// left grip, right grip, trigger; 0..1, held until read
    peaks: [f64; 3],
    error: Option<String>,
    /// the effect last started, and the surface the road test is on now
    name: Option<Effect>,
    surface: String,
    /// play at 2 kHz (src/rate2k.rs); taken up at the next 8 ms block
    output_2k: bool,
    rate2k: Option<Output2k>,
}

/// Plays previews while the output is stopped, holding the controller only while one plays.
/// A preview started while another plays takes over at the next block.
#[derive(Clone, Default)]
pub struct Player {
    state: Arc<Mutex<PlayerState>>,
    thread: Arc<Mutex<Option<std::thread::JoinHandle<()>>>>,
}

impl Player {
    pub fn busy(&self) -> bool {
        self.state.lock().unwrap().running
    }

    /// (effect, surface) of what plays now, for the status line.
    pub fn playing(&self) -> Option<(Effect, String)> {
        let state = self.state.lock().unwrap();
        state.name.map(|name| (name, state.surface.clone()))
    }

    pub fn take_error(&self) -> Option<String> {
        self.state.lock().unwrap().error.take()
    }

    pub fn take_peaks(&self) -> [f64; 3] {
        std::mem::take(&mut self.state.lock().unwrap().peaks)
    }

    /// Play `preview`, at 2 kHz with `output_2k`.
    pub fn play(&self, preview: Preview, output_2k: bool) {
        {
            let mut state = self.state.lock().unwrap();
            state.output_2k = output_2k;
            state.name = Some(preview.effect);
            state.pending = Some(preview);
            if state.running {
                return;
            }
            state.running = true;
        }
        let state = Arc::clone(&self.state);
        let handle = std::thread::spawn(move || Player::work(state));
        *self.thread.lock().unwrap() = Some(handle);
    }

    /// Switch a test that plays now to 2 kHz or back, at its next 8 ms block.
    pub fn set_output_2k(&self, on: bool) {
        self.state.lock().unwrap().output_2k = on;
    }

    /// End the preview and wait for the controller to be let go.
    pub fn stop(&self) {
        {
            let mut state = self.state.lock().unwrap();
            state.pending = None;
            state.current = None;
        }
        if let Some(handle) = self.thread.lock().unwrap().take() {
            let _ = handle.join();
        }
    }

    fn next_block(state: &Mutex<PlayerState>) -> Option<Block> {
        let mut state = state.lock().unwrap();
        if let Some(second) = state.rate2k.as_mut().and_then(Output2k::pending) {
            return Some(second);
        }
        if let Some(pending) = state.pending.take() {
            state.current = Some(pending);
        }
        let two_k = state.output_2k;
        if let Some(current) = &mut state.current {
            current.set_events_2k(two_k);
        }
        let block = state.current.as_mut().and_then(Preview::render);
        let Some(block) = block else {
            state.current = None;
            return None;
        };
        state.surface = state.current.as_ref().map(Preview::surface).unwrap_or_default();
        for (held, peak) in state.peaks.iter_mut().zip(block.peaks()) {
            *held = m::max(*held, peak / MAX_SAMPLE);
        }
        if two_k != state.rate2k.is_some() {
            state.rate2k = match (two_k, &state.current) {
                (true, Some(_)) => Some(Output2k::new()),
                _ => None,
            };
        }
        Some(match &mut state.rate2k {
            Some(output) => output.first(&block),
            None => block,
        })
    }

    fn work(state: Arc<Mutex<PlayerState>>) {
        loop {
            let error = match Apex6::open() {
                Ok(mut pad) => match pad.stream(|_| Player::next_block(&state), None, FRAME_PERIOD) {
                    Ok(_) => None,
                    Err(PadError::Unavailable(e) | PadError::Fatal(e)) => Some(e),
                },
                Err(PadError::Unavailable(e) | PadError::Fatal(e)) => Some(e),
            };
            let mut state = state.lock().unwrap();
            state.rate2k = None;
            // one started while the controller was let go plays on
            if error.is_some() || state.pending.is_none() {
                state.error = error;
                state.pending = None;
                state.current = None;
                state.running = false;
                state.name = None;
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_effect_plays_and_ends_on_its_own() {
        for effect in TOGGLEABLE {
            let mut preview = Preview::new(effect, &Gains::default(), Equalizer::default(), &[]);
            let mut blocks = 0;
            let mut loudest = 0.0f64;
            while let Some(block) = preview.render() {
                blocks += 1;
                for x in block.left_grip.iter().chain(&block.right_grip).chain(&block.trigger) {
                    loudest = loudest.max(x.abs());
                }
            }
            let expected = (0..).take_while(|n| (*n as f64 * DT) < seconds_of(effect)).count();
            assert_eq!(blocks, expected, "{effect:?}");
            assert!(loudest > 1.0, "{effect:?} stayed silent");
        }
    }

    #[test]
    fn surfaces_play_one_after_another() {
        let mut preview = Preview::new(Effect::Road, &Gains::default(), Equalizer::default(), &["ice", "gravel"]);
        assert_eq!(preview.seconds, 2.0 * SURFACE_STEP_S);
        assert_eq!(preview.surface(), "ice");
        for _ in 0..(SURFACE_STEP_S / DT) as usize + 1 {
            preview.render();
        }
        assert_eq!(preview.surface(), "gravel");
    }
}
