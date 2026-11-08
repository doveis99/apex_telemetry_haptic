//! The 2 kHz output: each 8 ms block of the synth (1 kHz) sent as two reports of eight 2 kHz
//! samples, 4 ms apart, with bit 7 clear (docs/xhaptics-engine-notes.md sections 11 and 12).
//!
//! First what the pad does itself with bit 7 set: every 1 kHz sample joined to the one before
//! by a straight line (firmware 9.0.6.8, measured 2026-11-08). Everything felt so keeps the
//! response the motor curves and the defaults were tuned with.
//!
//! Then what the pad cannot do: a low-pass at 420 Hz takes out the images that 1 kHz samples
//! leave at 1000 - f, which the line only weakens. In the scenarios they sat 25-33 dB under
//! what the grips play, and the trigger's 100 Hz carrier left a steady 900 Hz line 30 dB down;
//! the pad's shell sends 0.5-1 kHz to the ear far better than it does the lows. Flat to 400 Hz
//! (+-0.05 dB), 33 dB or more off every image of what lies under 400 Hz, 0.7 ms late.
//!
//! The ticks (Synth::native_ticks) would come out of that low-pass rounded, so they go in after
//! it, as one 2 kHz sample with the push of the 1 kHz one: the triangle the line made of it
//! had twice its height in area. The same kick to the motor, a sharper edge. They pass the
//! motor's resonance cut (at 2 kHz) as they did in the synth, and when the ceiling holds one
//! back, the rest of its push follows in the next sample.

use crate::apex6::{Block, Route, SUBFRAMES};
use crate::effects::{ResonanceCut, Synth, CEILING_GRIP, CEILING_TRIGGER};

pub const RATE: f64 = 2000.0;
/// 2 kHz samples per 1 kHz block
const LINE: usize = 2 * SUBFRAMES;
/// scipy.signal.ellip(4, 0.05, 40, 420, fs=2000, output="sos"), scaled to 0 dB at 0 Hz.
const IMAGE_FILTER: [[f64; 6]; 2] = [
    [0.10987790770831055, 0.19381937118256315, 0.10987790770831057, 1.0, -0.32062983589710725, 0.12883839124666635],
    [1.0, 0.999332473802774, 1.0000000000000002, 1.0, -0.1227398160487654, 0.6575534224074202],
];
/// The low-pass is 1.3-1.6 samples late below 200 Hz: the ticks wait one so they stay on the
/// beat of the bursts they start.
const TICK_DELAY: usize = 1;
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
    cut: Option<ResonanceCut>,
    /// ticks at their 2 kHz place; the last ones spill into the next block
    ticks: [f64; LINE + TICK_DELAY + 1],
    /// what the ceiling held back of a tick's push, given in the next sample
    spill: f64,
}

impl Channel {
    fn new(ceiling: f64, cut: Option<ResonanceCut>) -> Channel {
        Channel { ceiling, last: 0.0, filter: ImageFilter::default(), cut, ticks: [0.0; LINE + TICK_DELAY + 1], spill: 0.0 }
    }

    fn run(&mut self, samples: &[f64; SUBFRAMES], ticks: &[f64; SUBFRAMES]) -> [f64; LINE] {
        for (n, &t) in ticks.iter().enumerate() {
            // where the line put the 1 kHz sample, at twice its height: the same area
            self.ticks[2 * n + 1 + TICK_DELAY] += 2.0 * t;
        }
        let mut out = [0.0; LINE];
        for (k, slot) in out.iter_mut().enumerate() {
            let x = samples[k / 2];
            let joined = if k % 2 == 1 { x } else { (if k == 0 { self.last } else { samples[k / 2 - 1] } + x) / 2.0 };
            let mut y = self.filter.next(joined);
            if joined == 0.0 && y.abs() < RUNG_OUT && self.filter.quiet() {
                y = 0.0;
                self.filter = ImageFilter::default();
            }
            let tick = match &mut self.cut {
                Some(cut) => cut.next(self.ticks[k]),
                None => self.ticks[k],
            };
            let pushed = tick + self.spill;
            let want = y + pushed;
            let held = want.clamp(-self.ceiling, self.ceiling);
            // the line's own overshoot is cut off, as encode() would; only a tick's push carries
            let excess = want - held;
            self.spill = if excess * pushed > 0.0 { excess.signum() * excess.abs().min(pushed.abs()) } else { 0.0 };
            *slot = held;
        }
        self.last = samples[SUBFRAMES - 1];
        let len = self.ticks.len();
        self.ticks.copy_within(LINE..len, 0);
        self.ticks[len - LINE..].fill(0.0);
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

impl Output2k {
    /// cuts: the motors' resonance cuts at 2 kHz (left grip, right grip, trigger), for the ticks.
    pub fn new(cuts: [Option<ResonanceCut>; 3]) -> Output2k {
        let [l, r, t] = cuts;
        Output2k {
            channels: [Channel::new(CEILINGS[0], l), Channel::new(CEILINGS[1], r), Channel::new(CEILINGS[2], t)],
            second: None,
        }
    }

    /// With the resonance cuts of the motor curves `synth` plays with.
    pub fn for_synth(synth: &Synth) -> Output2k {
        Output2k::new([synth.eq.cut_at(0, RATE), synth.eq.cut_at(0, RATE), synth.eq.cut_at(1, RATE)])
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
        let half = Block { upsample: false, ticks: None, ..block.clone() };
        let mut halves = [half.clone(), Block { side: None, ..half }];
        let none = [[0.0; SUBFRAMES]; 3];
        let ticks = block.ticks.as_deref().unwrap_or(&none);
        for (i, channel) in self.channels.iter_mut().enumerate() {
            let out = channel.run(samples(block, i), &ticks[i]);
            for (h, half) in halves.iter_mut().enumerate() {
                samples_mut(half, i).copy_from_slice(&out[h * SUBFRAMES..(h + 1) * SUBFRAMES]);
            }
        }
        for half in &mut halves {
            // the synth fell silent, but the low-pass rings on a little (or a late tick lands):
            // the trigger stays routed until it has rung out
            if half.route == Route::Mute && half.trigger.iter().any(|&x| x != 0.0) {
                half.route = Route::Both;
            }
        }
        halves
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::effects::TAU;

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

    #[test]
    fn felt_tones_keep_the_line_and_lose_their_image() {
        for freq in [50.0, 100.0, 190.0, 300.0] {
            let blocks = tone(freq, 40.0, 250);
            let mut output = Output2k::new([None, None, None]);
            let x = render(&blocks, &mut output);
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
        let mut output = Output2k::new([None, None, None]);
        let first = output.first(&block);
        let second = output.pending().unwrap();
        assert!(output.pending().is_none());
        assert!(!first.upsample && !second.upsample);
        assert_eq!((first.side, second.side), (Some(Route::Left), None));
        assert_eq!((first.route, second.route), (Route::Both, Route::Both));
    }

    #[test]
    fn a_tick_keeps_its_push_at_one_sample() {
        let mut block = Block::default();
        let mut ticks = [[0.0; SUBFRAMES]; 3];
        ticks[0][3] = 20.0;
        ticks[2][7] = 10.0; // the last sample: lands in the next block
        block.ticks = Some(Box::new(ticks));
        let mut output = Output2k::new([None, None, None]);
        let [a, b] = output.split(&block);
        let [c, _] = output.split(&Block::default());
        let grip: Vec<f64> = a.left_grip.iter().chain(&b.left_grip).copied().collect();
        let mut expected = [0.0; LINE];
        expected[2 * 3 + 1 + TICK_DELAY] = 40.0;
        assert_eq!(grip, expected.to_vec());
        assert!(b.trigger.iter().all(|&x| x == 0.0));
        assert_eq!(c.trigger[0], 20.0);
        // the synth had fallen silent there: the trigger is routed for the tick all the same
        assert_eq!(c.route, Route::Both);
    }

    #[test]
    fn the_ceiling_passes_a_ticks_push_on() {
        let mut block = Block::default();
        let mut ticks = [[0.0; SUBFRAMES]; 3];
        ticks[2][0] = 80.0; // 160 at 2 kHz, over the trigger's 102
        block.ticks = Some(Box::new(ticks));
        let mut output = Output2k::new([None, None, None]);
        let [a, _] = output.split(&block);
        assert_eq!(&a.trigger[..4], &[0.0, 0.0, CEILING_TRIGGER, 160.0 - CEILING_TRIGGER]);
    }

    /// Each block of a blinker test, rendered as the app sends it at 1 kHz and with native ticks.
    fn blinker(native: bool) -> Vec<Block> {
        use crate::effects::{Effect, Equalizer, Gains};
        use crate::pyrandom::PyRandom;
        let mut preview = crate::preview::Preview::with_synth(Effect::Blinker, &Gains::default(), &[], |gains| {
            Synth::with_seeds(gains, Equalizer::default(), PyRandom::new(1))
        });
        preview.set_native_ticks(native);
        std::iter::from_fn(|| preview.render()).collect()
    }

    #[test]
    fn native_ticks_leave_the_rest_alone() {
        let (plain, native) = (blinker(false), blinker(true));
        assert_eq!(plain.len(), native.len());
        let mut fired = 0;
        for (a, b) in plain.iter().zip(&native) {
            assert!(a.ticks.is_none());
            let ticks = b.ticks.as_deref().copied().unwrap_or([[0.0; SUBFRAMES]; 3]);
            for i in 0..3 {
                for n in 0..SUBFRAMES {
                    if ticks[i][n] == 0.0 {
                        assert_eq!(samples(a, i)[n], samples(b, i)[n]);
                    } else {
                        // with no resonance cut, together they are the 1 kHz sample
                        fired += 1;
                        assert!((samples(b, i)[n] + ticks[i][n] - samples(a, i)[n]).abs() < 1e-9);
                    }
                }
            }
            assert_eq!((a.route, a.side), (b.route, b.side));
        }
        assert!(fired > 0, "the blinker test has ticks");
    }

    #[test]
    fn ticks_push_the_motor_as_hard_at_2k() {
        // what the pad makes of the 1 kHz blocks (its line) against what the 2 kHz output sends
        let line: Vec<f64> = blinker(false)
            .iter()
            .flat_map(|b| b.left_grip)
            .scan(0.0, |last, x| {
                let joined = [(*last + x) / 2.0, x];
                *last = x;
                Some(joined)
            })
            .flatten()
            .collect();
        let mut output = Output2k::new([None, None, None]);
        let sent: Vec<f64> = blinker(true).iter().flat_map(|b| output.split(b)).flat_map(|h| h.left_grip).collect();
        // The felt part, around the motors' resonance, within 3%. Higher up a tick of one 2 kHz
        // sample gives more than the line's 1 ms triangle (+5.8% at 150 Hz, x2.5 at 600 Hz).
        for freq in [20.0, 50.0, 65.0, 80.0, 120.0] {
            let (a, b) = (level(&line, freq), level(&sent, freq));
            assert!((b / a - 1.0).abs() < 0.03, "{freq} Hz: {a} vs {b}");
        }
        // the click's edge: where the line had only the triangle's tail and the images, which the
        // low-pass takes, the tick now plays (blinker: x1.5 at 600 Hz, x2.9 at 800, x18 at 900)
        for freq in [600.0, 700.0, 800.0, 900.0] {
            assert!(level(&sent, freq) > 1.4 * level(&line, freq), "{freq} Hz");
        }
    }

    #[test]
    fn silence_comes_back_exact() {
        let mut output = Output2k::new([None, None, None]);
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
}
