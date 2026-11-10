//! The 2 kHz output: each 8 ms block of the synth sent as two reports of eight 2 kHz samples,
//! 4 ms apart, with bit 7 clear (docs/xhaptics-engine-notes.md sections 11 to 13).
//!
//! The continuous effects come at 1 kHz. First what the pad does itself with bit 7 set: every
//! 1 kHz sample joined to the one before by a straight line (firmware 9.0.6.8, measured
//! 2026-11-08). Everything felt so keeps the response the motor curves and the defaults were
//! tuned with.
//!
//! Then what the pad cannot do: a low-pass at 420 Hz takes out the images that 1 kHz samples
//! leave at 1000 - f, which the line only weakens. In the scenarios they sat 25-33 dB under
//! what the grips play, and the trigger's 100 Hz carrier left a steady 900 Hz line 30 dB down;
//! the pad's shell sends 0.5-1 kHz to the ear far better than it does the lows. Flat to 400 Hz
//! (+-0.05 dB), 33 dB or more off every image of what lies under 400 Hz, 0.7 ms late.
//!
//! The events (Synth::set_events_2k: every burst and tick) come drawn at 2 kHz already, through
//! the motors' resonance cut and the soft limit, as what each adds on top of the line. The
//! low-pass would round them, so they go in after it, one sample late as it is. When the
//! ceiling holds one back, the rest of its push follows in the next sample.

use crate::apex6::{Block, Route, FINE, SUBFRAMES};
use crate::effects::{CEILING_GRIP, CEILING_TRIGGER};

pub const RATE: f64 = 2000.0;
/// scipy.signal.ellip(4, 0.05, 40, 420, fs=2000, output="sos"), scaled to 0 dB at 0 Hz.
const IMAGE_FILTER: [[f64; 6]; 2] = [
    [0.10987790770831055, 0.19381937118256315, 0.10987790770831057, 1.0, -0.32062983589710725, 0.12883839124666635],
    [1.0, 0.999332473802774, 1.0000000000000002, 1.0, -0.1227398160487654, 0.6575534224074202],
];
/// The low-pass is 1.3-1.6 samples late below 200 Hz: the events wait one so they stay on the
/// beat of the line they were limited against.
const EVENT_DELAY: usize = 1;
const CEILINGS: [f64; 3] = [CEILING_GRIP, CEILING_GRIP, CEILING_TRIGGER];
/// Under this a channel has rung out and goes back to exact silence, which mutes the trigger
/// route and lets the stream see an idle pad (as LowPass and ResonanceCut do). Far under the
/// half step a sample rounds away at, and reached ~25 ms after a full-strength tone ends.
const RUNG_OUT: f64 = 1e-3;

/// The image low-pass, two biquads in transposed direct form II.
#[derive(Clone, Debug, Default)]
struct ImageFilter {
    z: [[f64; 2]; 2],
}

impl ImageFilter {
    fn next(&mut self, x: f64) -> f64 {
        let mut v = x;
        for (s, z) in IMAGE_FILTER.iter().zip(&mut self.z) {
            let y = s[0] * v + z[0];
            z[0] = s[1] * v - s[4] * y + z[1];
            z[1] = s[2] * v - s[5] * y;
            v = y;
        }
        v
    }

    fn quiet(&self) -> bool {
        self.z.iter().flatten().all(|z| z.abs() < RUNG_OUT)
    }
}

#[derive(Clone, Debug)]
struct Channel {
    ceiling: f64,
    /// the last 1 kHz sample, where the next block's first line starts
    last: f64,
    filter: ImageFilter,
    /// events at their place; the last ones spill into the next block
    events: [f64; FINE + EVENT_DELAY],
    /// what the ceiling held back of an event's push, given in the next sample
    spill: f64,
}

impl Channel {
    fn new(ceiling: f64) -> Channel {
        Channel { ceiling, last: 0.0, filter: ImageFilter::default(), events: [0.0; FINE + EVENT_DELAY], spill: 0.0 }
    }

    fn run(&mut self, samples: &[f64; SUBFRAMES], events: &[f64; FINE]) -> [f64; FINE] {
        for (k, &e) in events.iter().enumerate() {
            self.events[k + EVENT_DELAY] += e;
        }
        let mut out = [0.0; FINE];
        for (k, slot) in out.iter_mut().enumerate() {
            let x = samples[k / 2];
            let joined = if k % 2 == 1 { x } else { (if k == 0 { self.last } else { samples[k / 2 - 1] } + x) / 2.0 };
            let mut y = self.filter.next(joined);
            if joined == 0.0 && y.abs() < RUNG_OUT && self.filter.quiet() {
                y = 0.0;
                self.filter = ImageFilter::default();
            }
            let pushed = self.events[k] + self.spill;
            let want = y + pushed;
            let held = want.clamp(-self.ceiling, self.ceiling);
            // the line's own overshoot is cut off, as encode() would; only an event's push carries
            let excess = want - held;
            self.spill = if excess * pushed > 0.0 { excess.signum() * excess.abs().min(pushed.abs()) } else { 0.0 };
            *slot = held;
        }
        self.last = samples[SUBFRAMES - 1];
        let len = self.events.len();
        self.events.copy_within(FINE..len, 0);
        self.events[len - FINE..].fill(0.0);
        out
    }
}

fn samples(block: &Block, channel: usize) -> &[f64; SUBFRAMES] {
    match channel {
        0 => &block.left_grip,
        1 => &block.right_grip,
        _ => &block.trigger,
    }
}

fn samples_mut(block: &mut Block, channel: usize) -> &mut [f64; SUBFRAMES] {
    match channel {
        0 => &mut block.left_grip,
        1 => &mut block.right_grip,
        _ => &mut block.trigger,
    }
}

/// Turns 1 kHz blocks into 2 kHz ones, keeping each block's second half until it is due.
#[derive(Clone, Debug)]
pub struct Output2k {
    channels: [Channel; 3],
    second: Option<Block>,
}

impl Default for Output2k {
    fn default() -> Output2k {
        Output2k::new()
    }
}

impl Output2k {
    pub fn new() -> Output2k {
        Output2k { channels: CEILINGS.map(Channel::new), second: None }
    }

    /// The second half of the last block, once, when it is next to go out.
    pub fn pending(&mut self) -> Option<Block> {
        self.second.take()
    }

    /// The first half of a new 1 kHz block; its second half waits in pending().
    pub fn first(&mut self, block: &Block) -> Block {
        let [first, second] = self.split(block);
        self.second = Some(second);
        first
    }

    /// One 1 kHz block as two 2 kHz ones. The trigger frame goes with the first.
    pub fn split(&mut self, block: &Block) -> [Block; 2] {
        let half = Block { upsample: false, events: None, ..block.clone() };
        let mut halves = [half.clone(), Block { side: None, ..half }];
        let none = [[0.0; FINE]; 3];
        let events = block.events.as_deref().unwrap_or(&none);
        for (i, channel) in self.channels.iter_mut().enumerate() {
            let out = channel.run(samples(block, i), &events[i]);
            for (h, half) in halves.iter_mut().enumerate() {
                samples_mut(half, i).copy_from_slice(&out[h * SUBFRAMES..(h + 1) * SUBFRAMES]);
            }
        }
        for half in &mut halves {
            // the synth fell silent, but the low-pass rings on a little (or a late event lands):
            // the trigger stays routed until it has rung out
            if half.route == Route::Mute && half.trigger.iter().any(|&x| x != 0.0) {
                half.route = Route::Both;
            }
        }
        halves
    }
}

/// A 2 kHz report lasts this long: the pad needs one every 4 ms.
const REPORT_S: f64 = crate::apex6::FRAME_PERIOD / 2.0;
const PACE_WINDOW_S: f64 = 0.5;
/// a gap this long is the stream stopping or stalling, not the link running slow
const PACE_STALL_S: f64 = 0.05;
const PACE_SHARE: f64 = 0.95;
const PACE_WINDOWS: u32 = 3;

/// Tells when the link does not take a report every 4 ms, so the 2 kHz output can give way to
/// the 1 kHz one instead of playing with gaps.
///
/// A link may hold every write a little longer than that: wired, each took 5.1 ms on
/// 2026-11-08 (docs/xhaptics-engine-notes.md section 17; the same cable had taken them in
/// 1.3 ms that morning, the dongle did all along). The stream then falls behind, drops what
/// it cannot catch up (Apex6::stream's late frames, 23% there) and the pad runs out of
/// samples many times a second, all the time. It shows as fewer reports going out than the
/// time needed: about 200 a second there where 250 were due, against all of them on a link
/// that keeps up, whose reports leave on absolute deadlines however each one's own start wavers.
///
/// Counted over windows of PACE_WINDOW_S; PACE_WINDOWS short ones running give the verdict.
/// A window the stream stopped in is left out, and the ones either side of it still count
/// together, so effect tests of a second or two add up across tests.
#[derive(Clone, Debug, Default)]
pub struct Pace {
    /// when the window began and the last report went, s
    window: Option<(f64, f64)>,
    reports: u32,
    short: u32,
}

impl Pace {
    /// A 2 kHz report goes out at `now` (s on any steady clock). Some(share of the reports the
    /// pad needed that went out) once the link has shown it does not keep up; the count then
    /// starts over.
    pub fn report(&mut self, now: f64) -> Option<f64> {
        let Some((start, last)) = self.window else {
            self.window = Some((now, now));
            self.reports = 0;
            return None;
        };
        if now - last > PACE_STALL_S || now < last {
            self.window = Some((now, now));
            self.reports = 0;
            return None;
        }
        self.reports += 1;
        self.window = Some((start, now));
        if now - start < PACE_WINDOW_S {
            return None;
        }
        let share = self.reports as f64 * REPORT_S / (now - start);
        self.window = Some((now, now));
        self.reports = 0;
        if share >= PACE_SHARE {
            self.short = 0;
            return None;
        }
        self.short += 1;
        if self.short < PACE_WINDOWS {
            return None;
        }
        self.short = 0;
        Some(share)
    }

    /// report() on the process's own clock.
    pub fn report_now(&mut self) -> Option<f64> {
        static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
        self.report(START.get_or_init(std::time::Instant::now).elapsed().as_secs_f64())
    }
}

/// The event lines for a 2 kHz output that gave way: what happened, then why, with the share
/// Pace found. Two short ones, as the window's event list cuts a long line off.
pub fn gave_way_lines(share: f64) -> [String; 2] {
    [
        crate::t!("2 kHz 출력을 끄고 1 kHz로 돌아감"),
        crate::t!("4 ms마다 리포트를 못 받음 (보낸 것 {percent})", percent = format!("{:.0}%", share * 100.0)),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::effects::{Effect, Equalizer, Gains, Synth, TAU};
    use crate::preview::Preview;
    use crate::pyrandom::PyRandom;

    fn tone(freq: f64, amp: f64, blocks: usize) -> Vec<Block> {
        (0..blocks)
            .map(|b| {
                let s: [f64; SUBFRAMES] =
                    std::array::from_fn(|n| amp * (TAU * freq * (b * SUBFRAMES + n) as f64 / 1000.0).sin());
                Block { left_grip: s, right_grip: s, trigger: s, route: Route::Both, ..Block::default() }
            })
            .collect()
    }

    fn render(blocks: &[Block], output: &mut Output2k) -> Vec<f64> {
        blocks.iter().flat_map(|b| output.split(b)).flat_map(|h| h.left_grip).collect()
    }

    /// Amplitude of the component at freq (Hz) in 2 kHz samples, by correlation.
    fn level(x: &[f64], freq: f64) -> f64 {
        let (mut c, mut s) = (0.0, 0.0);
        for (k, v) in x.iter().enumerate() {
            let w = TAU * freq * k as f64 / RATE;
            c += v * w.cos();
            s += v * w.sin();
        }
        2.0 * (c * c + s * s).sqrt() / x.len() as f64
    }

    /// Mean square of level() over (lo, hi) Hz, every 5 Hz.
    fn band(x: &[f64], lo: f64, hi: f64) -> f64 {
        let freqs: Vec<f64> = (0..).map(|n| lo + 5.0 * n as f64).take_while(|f| *f <= hi).collect();
        freqs.iter().map(|&f| level(x, f).powi(2)).sum::<f64>() / freqs.len() as f64
    }

    #[test]
    fn felt_tones_keep_the_line_and_lose_their_image() {
        for freq in [50.0, 100.0, 190.0, 300.0] {
            let blocks = tone(freq, 40.0, 250);
            let x = render(&blocks, &mut Output2k::new());
            let settled = &x[400..]; // past the start
            let line = (std::f64::consts::PI * freq / RATE).cos().powi(2); // what the pad's own line passes
            let kept = level(settled, freq) / (40.0 * line);
            assert!((kept - 1.0).abs() < 0.01, "{freq} Hz kept {kept}");
            let image = level(settled, 1000.0 - freq) / 40.0;
            assert!(image < 0.02, "{freq} Hz image at {} Hz: {image}", 1000.0 - freq); // under -34 dB
        }
    }

    #[test]
    fn halves_go_out_unjoined_with_the_trigger_frame_first() {
        let mut block = tone(100.0, 30.0, 1).remove(0);
        block.side = Some(Route::Left);
        let mut output = Output2k::new();
        let first = output.first(&block);
        let second = output.pending().unwrap();
        assert!(output.pending().is_none());
        assert!(!first.upsample && !second.upsample);
        assert_eq!((first.side, second.side), (Some(Route::Left), None));
        assert_eq!((first.route, second.route), (Route::Both, Route::Both));
    }

    #[test]
    fn events_go_in_one_sample_late() {
        let mut block = Block::default();
        let mut events = [[0.0; FINE]; 3];
        events[0][5] = 20.0;
        events[2][FINE - 1] = 10.0; // the last sample: lands in the next block
        block.events = Some(Box::new(events));
        let mut output = Output2k::new();
        let [a, b] = output.split(&block);
        let [c, _] = output.split(&Block::default());
        let grip: Vec<f64> = a.left_grip.iter().chain(&b.left_grip).copied().collect();
        let mut expected = [0.0; FINE];
        expected[5 + EVENT_DELAY] = 20.0;
        assert_eq!(grip, expected.to_vec());
        assert!(b.trigger.iter().all(|&x| x == 0.0));
        assert_eq!(c.trigger[0], 10.0);
        // the synth had fallen silent there: the trigger is routed for the event all the same
        assert_eq!(c.route, Route::Both);
    }

    #[test]
    fn the_ceiling_passes_an_events_push_on() {
        let mut block = Block::default();
        let mut events = [[0.0; FINE]; 3];
        events[2][0] = 160.0; // over the trigger's 102
        block.events = Some(Box::new(events));
        let [a, _] = Output2k::new().split(&block);
        assert_eq!(&a.trigger[..4], &[0.0, CEILING_TRIGGER, 160.0 - CEILING_TRIGGER, 0.0]);
    }

    /// Every block of an effect test, rendered for the 1 kHz output or the 2 kHz one.
    fn test_blocks(effect: Effect, events_2k: bool, seed: u64) -> Vec<Block> {
        let mut preview = Preview::with_synth(effect, &Gains::default(), &[], |gains| {
            Synth::with_seeds(gains, Equalizer::for_player(&Default::default()), PyRandom::new(seed))
        });
        preview.set_events_2k(events_2k);
        std::iter::from_fn(|| preview.render()).collect()
    }

    /// What the pad plays of 1 kHz blocks: its line through them, at 2 kHz.
    fn pad_line(blocks: &[Block]) -> Vec<f64> {
        blocks
            .iter()
            .flat_map(|b| b.left_grip)
            .scan(0.0, |last, x| {
                let joined = [(*last + x) / 2.0, x];
                *last = x;
                Some(joined)
            })
            .flatten()
            .collect()
    }

    fn sent_2k(blocks: &[Block]) -> Vec<f64> {
        let mut output = Output2k::new();
        blocks.iter().flat_map(|b| output.split(b)).flat_map(|h| h.left_grip).collect()
    }

    /// The channel an effect test plays on: the left grip, or the trigger for the pedal.
    fn played(blocks: &[Block]) -> Vec<Block> {
        let trigger = blocks.iter().all(|b| b.left_grip.iter().all(|&x| x == 0.0) && b.events.as_ref().is_none_or(|e| e[0].iter().all(|&x| x == 0.0)));
        blocks
            .iter()
            .map(|b| {
                let mut b = b.clone();
                if trigger {
                    b.left_grip = b.trigger;
                    if let Some(e) = &mut b.events {
                        e[0] = e[2];
                    }
                }
                b
            })
            .collect()
    }

    /// Strength over (lo, hi) Hz at 2 kHz against what the pad played of the 1 kHz blocks, bar
    /// its line's droop (cos^2, -3.5% at 120 Hz), which the events no longer pass.
    fn felt_ratio(plain: &[Block], fine: &[Block], lo: f64, hi: f64) -> f64 {
        let (line, sent) = (pad_line(&played(plain)), sent_2k(&played(fine)));
        let (mut a, mut b) = (0.0, 0.0);
        for f in (0..).map(|n| lo + 5.0 * n as f64).take_while(|f| *f <= hi) {
            let droop = (std::f64::consts::PI * f / RATE).cos().powi(2);
            a += (level(&line, f) / droop).powi(2);
            b += level(&sent, f).powi(2);
        }
        (b / a).sqrt()
    }

    /// The effects made of bursts and ticks only, no noise.
    const STRUCK: [Effect; 8] = [Effect::Blinker, Effect::Gear, Effect::Shifter, Effect::Trailer, Effect::Impact,
                                 Effect::GameEvents, Effect::Bumps, Effect::Pedal];
    const FELT: [(f64, f64); 4] = [(20.0, 45.0), (45.0, 90.0), (90.0, 150.0), (150.0, 250.0)];
    /// 1.2 dB up, 1.5 down: within it in every felt band (measured 2026-11-08 at the default
    /// gains: gear 45-90 Hz -1.05 dB, in the resonance cut's notch with the clack in the soft
    /// limit; game events 150-250 Hz +1.0; the rest within 0.7). The rings drawn as spread noise
    /// with the clips' shorter decays (effects.rs RING_TONAL) push the shift's clack 0.3 dB
    /// further into the limit: gear 45-90 Hz -1.38 dB.
    const WITHIN: (f64, f64) = (0.841, 1.148);

    #[test]
    fn events_push_the_motor_as_hard_at_2k() {
        for effect in STRUCK {
            let (a, b) = (test_blocks(effect, false, 1), test_blocks(effect, true, 1));
            for (lo, hi) in FELT {
                let ratio = felt_ratio(&a, &b, lo, hi);
                assert!(WITHIN.0 < ratio && ratio < WITHIN.1, "{effect:?} {lo}-{hi} Hz: {ratio}");
            }
            // under the resonance much of the clicks' push is where the pad's line rose to their
            // first sample, which the 2 kHz events make up for (drawn plainly: about -30%)
            let ratio = felt_ratio(&a, &b, 20.0, 45.0);
            assert!((ratio - 1.0).abs() < 0.03, "{effect:?} under 45 Hz: {ratio}");
        }
    }

    #[test]
    fn hisses_keep_their_felt_strength_at_2k() {
        // noise drawn twice as often is another draw: the mean of a few. Under 45 Hz they hold
        // under 1% of their felt part and the draw decides it (park brake 0.5-1.6x by seed).
        for effect in [Effect::AirBrake, Effect::ParkBrake, Effect::EngineStart] {
            for (lo, hi) in &FELT[1..] {
                let ratio = (1..=6).map(|seed| felt_ratio(&test_blocks(effect, false, seed), &test_blocks(effect, true, seed), *lo, *hi)).sum::<f64>() / 6.0;
                assert!(WITHIN.0 < ratio && ratio < WITHIN.1, "{effect:?} {lo}-{hi} Hz: {ratio}");
            }
        }
    }

    #[test]
    fn events_leave_the_bed_as_it_was() {
        // a drive with every continuous effect, and the turn signals over it
        let gains = Gains { ducking: 0.0, bumps: 0.0, ..Gains::default() };
        let quiet = Gains { blinker: 0.0, ..gains.clone() };
        let eq = || Equalizer::for_player(&Default::default());
        let (mut plain, mut fine) = (Synth::with_seeds(quiet, eq(), PyRandom::new(1)), Synth::with_seeds(gains, eq(), PyRandom::new(1)));
        fine.set_events_2k(true);
        let mut fired = 0;
        for b in 0..450 {
            let frame = crate::preview::script(Effect::Blinker, b as f64 * crate::effects::DT, &Gains::default());
            let (a, x) = (plain.render(Some(&frame)), fine.render(Some(&frame)));
            assert_eq!((a.left_grip, a.right_grip, a.trigger), (x.left_grip, x.right_grip, x.trigger), "block {b}");
            fired += x.events.is_some() as usize;
        }
        assert!(fired > 0, "the turn signals clicked");
    }

    #[test]
    fn edges_are_drawn_at_2k() {
        // the clack of a gear shift: a 140 Hz crest that falls 63% in 5 ms
        let (old, sent) = (sent_2k(&test_blocks(Effect::Gear, false, 1)), sent_2k(&test_blocks(Effect::Gear, true, 1)));
        // the line and the 420 Hz low-pass took everything over 450 Hz but the tick; now the
        // bursts' own edges reach it
        let (a, b) = (band(&old, 500.0, 950.0), band(&sent, 500.0, 950.0));
        assert!(b > 4.0 * a, "500-950 Hz: {a} vs {b}");
        // and the crest arrives in one 2 kHz sample, not over the line and the low-pass's rise
        let rise = |x: &[f64]| {
            let start = x.iter().position(|v| v.abs() > 0.5).unwrap();
            let peak = (start..start + 8).max_by(|&i, &j| x[i].abs().total_cmp(&x[j].abs())).unwrap();
            peak - start
        };
        assert!(rise(&sent) <= 1 && rise(&old) >= 2, "rise {} vs {}", rise(&sent), rise(&old));
    }

    #[test]
    fn the_air_brake_hisses_over_its_whole_band() {
        // the 350 Hz hiss lost its upper half to the 420 Hz low-pass; drawn at 2 kHz it keeps it
        let (mut a, mut b) = (0.0, 0.0);
        for seed in 1..=4 {
            a += band(&sent_2k(&test_blocks(Effect::AirBrake, false, seed)), 450.0, 650.0);
            b += band(&sent_2k(&test_blocks(Effect::AirBrake, true, seed)), 450.0, 650.0);
        }
        assert!(b > 10.0 * a, "450-650 Hz: {a} vs {b}");
    }

    /// The road test on one surface, rendered for the 1 kHz output or the 2 kHz one.
    fn surface_blocks(surface: &str, events_2k: bool) -> Vec<Block> {
        let mut preview = Preview::with_synth(Effect::Road, &Gains::default(), &[surface], |gains| {
            Synth::with_seeds(gains, Equalizer::for_player(&Default::default()), PyRandom::new(1))
        });
        preview.set_events_2k(events_2k);
        std::iter::from_fn(|| preview.render()).collect()
    }

    /// Where in (lo, hi) Hz the most is, every 5 Hz.
    fn loudest(x: &[f64], lo: f64, hi: f64) -> f64 {
        let freqs = (0..).map(|n| lo + 5.0 * n as f64).take_while(|f| *f <= hi);
        freqs.max_by(|a, b| level(x, *a).total_cmp(&level(x, *b))).unwrap()
    }

    #[test]
    fn clicks_ring_on_above_500_hz_at_their_recorded_pitch() {
        // the gear shift's clack rings at the Eaton gearbox's 690 Hz, the turn signal's relay at
        // its 730 Hz; at 1 kHz there was no such ring
        for (effect, pitch) in [(Effect::Gear, 690.0), (Effect::Blinker, 730.0)] {
            let (old, sent) = (sent_2k(&test_blocks(effect, false, 1)), sent_2k(&test_blocks(effect, true, 1)));
            let at = loudest(&sent, 500.0, 950.0);
            assert!((at - pitch).abs() <= 10.0, "{effect:?} loudest at {at} Hz");
            let (a, b) = (band(&old, pitch - 30.0, pitch + 30.0), band(&sent, pitch - 30.0, pitch + 30.0));
            assert!(b > 4.0 * a, "{effect:?} around {pitch} Hz: {a} vs {b}");
        }
    }

    #[test]
    fn stones_click_above_500_hz_on_loose_ground_only() {
        // gravel's stones give the band most of what it had nowhere at 1 kHz
        let (old, sent) = (sent_2k(&surface_blocks("gravel", false)), sent_2k(&surface_blocks("gravel", true)));
        let (a, b) = (band(&old, 500.0, 950.0), band(&sent, 500.0, 950.0));
        assert!(b > 20.0 * a, "gravel 500-950 Hz: {a} vs {b}");
        // and the felt part stays as it was
        for (lo, hi) in FELT {
            let ratio = (band(&sent, lo, hi) / band(&old, lo, hi)).sqrt();
            assert!((ratio - 1.0).abs() < 0.02, "gravel {lo}-{hi} Hz: {ratio}");
        }
        // asphalt has no stones: nothing is drawn at 2 kHz, the output is the 1 kHz one's
        let blocks = surface_blocks("road", true);
        assert!(blocks.iter().all(|b| b.events.is_none()));
        assert_eq!(sent_2k(&blocks), sent_2k(&surface_blocks("road", false)));
    }

    #[test]
    fn silence_comes_back_exact() {
        let mut output = Output2k::new();
        let blocks: Vec<Block> = tone(150.0, 60.0, 10).into_iter().chain(std::iter::repeat_n(Block::default(), 40)).collect();
        let halves: Vec<Block> = blocks.iter().flat_map(|b| output.split(b)).collect();
        let last = halves.last().unwrap();
        assert!(last.is_silent());
        assert_eq!(last.route, Route::Mute);
        // and it took no more than 32 ms of ringing, under a step after the first 16 ms
        let ringing: Vec<&Block> = halves[20..].iter().take_while(|h| !h.is_silent()).collect();
        assert!(ringing.len() <= 8, "{} halves", ringing.len());
        assert!(ringing.iter().skip(4).flat_map(|h| h.left_grip).all(|x| x.abs() < 0.5));
    }

    /// Reports `every` s apart from `from` for `seconds`; the verdicts and when the last went.
    fn paced(pace: &mut Pace, from: f64, every: f64, seconds: f64) -> (Vec<f64>, f64) {
        let mut verdicts = Vec::new();
        let mut now = from;
        while now - from < seconds {
            verdicts.extend(pace.report(now));
            now += every;
        }
        (verdicts, now - every)
    }

    #[test]
    fn a_link_that_keeps_up_never_gives_way() {
        let mut pace = Pace::default();
        assert!(paced(&mut pace, 3.0, REPORT_S, 60.0).0.is_empty());
        // each report up to 2 ms late or early of its deadline, the deadlines themselves kept
        let mut pace = Pace::default();
        let mut rng = PyRandom::new(9);
        for n in 0..15_000 {
            let now = n as f64 * REPORT_S + rng.uniform(0.0, 0.002);
            assert_eq!(pace.report(now), None, "report {n}");
        }
    }

    #[test]
    fn a_link_that_holds_each_write_gives_way_within_two_seconds() {
        // 5.1 ms a write, as the wired pad took them on 2026-11-08
        let mut pace = Pace::default();
        let (verdicts, _) = paced(&mut pace, 0.0, 0.0051, 1.6);
        assert_eq!(verdicts.len(), 1, "{verdicts:?}");
        assert!((verdicts[0] - 4.0 / 5.1).abs() < 0.01, "{verdicts:?}");
        // 4.5 ms: 11% short, still not kept up with
        assert_eq!(paced(&mut Pace::default(), 0.0, 0.0045, 1.6).0.len(), 1);
    }

    #[test]
    fn short_tests_add_up_and_a_stall_is_not_a_slow_link() {
        // effect tests of 0.7 s each on a slow link, the controller let go between them
        let mut pace = Pace::default();
        let mut verdicts = Vec::new();
        let mut from = 0.0;
        for _ in 0..3 {
            let (found, last) = paced(&mut pace, from, 0.0051, 0.7);
            verdicts.extend(found);
            from = last + 2.0;
        }
        assert_eq!(verdicts.len(), 1, "one short window a test, three tests");
        // a good link that stalls for 300 ms now and then (the PC, not the pad)
        let mut pace = Pace::default();
        let mut from = 0.0;
        for _ in 0..20 {
            let (found, last) = paced(&mut pace, from, REPORT_S, 0.8);
            assert!(found.is_empty());
            from = last + 0.3;
        }
        // and one good window between slow ones starts the count again
        let mut pace = Pace::default();
        let (a, last) = paced(&mut pace, 0.0, 0.0051, 1.1);
        let (b, last) = paced(&mut pace, last + REPORT_S, REPORT_S, 0.6);
        let (c, _) = paced(&mut pace, last + 0.0051, 0.0051, 1.1);
        assert!(a.is_empty() && b.is_empty() && c.is_empty(), "{a:?} {b:?} {c:?}");
    }
}
