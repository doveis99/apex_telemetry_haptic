//! The ring-level check, by ear (examples/ring_level.rs runs it on the pad).
//!
//! At 2 kHz the cab clicks ring above 500 Hz and the gravel's stones click there too
//! (effects.rs Ring, STONE_HI), their levels fitted in the signal to the sound mods' clips: as
//! if the pad turned 700 Hz into sound as it turns 160 Hz. It does not (rate-2k's recordings:
//! 700-900 Hz came out -12 to +20 dB from 160 Hz at the same drive), so the levels are judged
//! by ear. Each effect test (▶) plays as the app plays it with the 2 kHz output on (Preview,
//! Output2k, Apex6::stream: the same path, the same timing) at every level of LEVELS, put on
//! numbered slots in random order; before them, the truck's own sound for that test plays on
//! the PC (tools/make_ring_references.py). The player picks the slot closest to it. Each
//! effect comes up `rounds` times, shuffled again, so a pick that holds can be told from chance.

use crate::apex6::{Apex6, Block, Clock, Link, PadError, StreamStats, FRAME_PERIOD};
use crate::effects::{Effect, Equalizer, Gains};
use crate::preview::Preview;
use crate::pyrandom::PyRandom;
use crate::rate2k::Output2k;

/// dB from the fitted level; None leaves the rings and stone clicks out.
pub const LEVELS: [Option<f64>; 5] = [None, Some(-12.0), Some(-6.0), Some(0.0), Some(6.0)];
pub const ROUNDS: usize = 2;

pub struct Case {
    pub effect: Effect,
    pub surfaces: &'static [&'static str],
    pub label: &'static str,
    /// the reference's file name in "sound sample/ring-reference"
    pub reference: &'static str,
}

pub const CASES: [Case; 5] = [
    Case { effect: Effect::Gear, surfaces: &[], label: "변속 딸깍 (690 Hz 링)", reference: "gear.wav" },
    Case { effect: Effect::Shifter, surfaces: &[], label: "레인지·스플리터 딸깍 (690·870 Hz 링)", reference: "shifter.wav" },
    Case { effect: Effect::Blinker, surfaces: &[], label: "깜빡이 릴레이 (730 Hz 링)", reference: "blinker.wav" },
    Case { effect: Effect::Trailer, surfaces: &[], label: "트레일러 연결 (500~930 Hz 링)", reference: "trailer.wav" },
    Case { effect: Effect::Road, surfaces: &["gravel"], label: "자갈길 돌 튐 (500~1000 Hz)", reference: "road.wav" },
];

pub fn level_label(level: Option<f64>) -> String {
    match level {
        None => "끔".to_string(),
        Some(0.0) => "+0 dB".to_string(),
        Some(db) => format!("{db:+} dB"),
    }
}

fn shuffle<T>(items: &mut [T], rng: &mut PyRandom) {
    for i in (1..items.len()).rev() {
        let j = rng.randrange(i as u32 + 1) as usize;
        items.swap(i, j);
    }
}

/// (case index, levels by slot) per trial: every case `rounds` times, slots and trials shuffled.
pub fn plan(rounds: usize, rng: &mut PyRandom) -> Vec<(usize, Vec<Option<f64>>)> {
    let mut plan = Vec::new();
    for _ in 0..rounds {
        for case in 0..CASES.len() {
            let mut slots = LEVELS.to_vec();
            shuffle(&mut slots, rng);
            plan.push((case, slots));
        }
    }
    shuffle(&mut plan, rng);
    plan
}

/// The blocks the pad gets for one case at one level: its effect test through the 2 kHz
/// output, as the app's Player sends it.
pub fn renderer(case: &Case, level: Option<f64>, gains: &Gains, eq: Equalizer) -> impl FnMut(usize) -> Option<Block> {
    let mut preview = Preview::new(case.effect, gains, eq, case.surfaces);
    preview.set_events_2k(true);
    preview.set_hi_trim(level);
    let mut output = Output2k::new();
    move |_| {
        if let Some(second) = output.pending() {
            return Some(second);
        }
        preview.render().map(|block| output.first(&block))
    }
}

/// Play one case at one level on the pad.
pub fn play<L: Link, C: Clock>(pad: &mut Apex6<L, C>, case: &Case, level: Option<f64>, gains: &Gains,
                               eq: Equalizer) -> Result<StreamStats, PadError> {
    pad.stream(renderer(case, level, gains, eq), None, FRAME_PERIOD)
}

/// Every stream's numbers together, for the report: late reports mean the pad ran short.
#[derive(Clone, Debug, Default)]
pub struct Totals {
    pub streams: u64,
    pub frames: u64,
    pub late_frames: u64,
    pub max_lateness_ms: f64,
    pub max_write_ms: f64,
    pub total_write_ms: f64,
}

impl Totals {
    pub fn add(&mut self, stats: &StreamStats) {
        self.streams += 1;
        self.frames += stats.frames;
        self.late_frames += stats.late_frames;
        self.max_lateness_ms = self.max_lateness_ms.max(stats.max_lateness_ms);
        self.max_write_ms = self.max_write_ms.max(stats.max_write_ms);
        self.total_write_ms += stats.total_write_ms;
    }

    pub fn summary(&self) -> String {
        let share = if self.frames > 0 { 100.0 * self.late_frames as f64 / self.frames as f64 } else { 0.0 };
        let avg = if self.frames > 0 { self.total_write_ms / self.frames as f64 } else { 0.0 };
        format!(
            "재생 {}번, 리포트 {}개, 늦음 {}개 ({share:.2}%), 가장 늦은 {:.2} ms, 쓰기 평균 {avg:.2} ms / 최대 {:.2} ms",
            self.streams, self.frames, self.late_frames, self.max_lateness_ms, self.max_write_ms
        )
    }
}

/// Lines per case from (case index, level picked) of each trial.
pub fn verdict(results: &[(usize, Option<f64>)]) -> Vec<String> {
    let mut lines = Vec::new();
    for (index, case) in CASES.iter().enumerate() {
        let picks: Vec<Option<f64>> = results.iter().filter(|(c, _)| *c == index).map(|(_, l)| *l).collect();
        if picks.is_empty() {
            continue;
        }
        let note = if picks.len() < 2 {
            "한 번뿐이라 판단 보류".to_string()
        } else if picks.iter().all(|p| *p == picks[0]) {
            "매번 같음 -> ".to_string() + &level_label(picks[0])
        } else {
            let spots: Vec<usize> = picks.iter().map(|p| LEVELS.iter().position(|l| l == p).unwrap()).collect();
            let spread = spots.iter().max().unwrap() - spots.iter().min().unwrap();
            if spread == 1 { "이웃한 단계 사이 -> 그 사이 어딘가".to_string() } else { "매번 다름 -> 이 범위에선 차이가 잘 안 들림".to_string() }
        };
        let text: Vec<String> = picks.iter().map(|p| level_label(*p)).collect();
        lines.push(format!("{} [{}]: {}  ({note})", case.label, case.effect.name(), text.join(", ")));
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plan_has_every_case_rounds_times_with_every_level_on_a_slot() {
        let plan = plan(2, &mut PyRandom::new(5));
        let mut cases: Vec<usize> = plan.iter().map(|(c, _)| *c).collect();
        cases.sort();
        assert_eq!(cases, vec![0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);
        for (_, slots) in &plan {
            assert_eq!(slots.len(), LEVELS.len());
            assert!(LEVELS.iter().all(|l| slots.contains(l)));
        }
        assert!(plan.iter().any(|(_, slots)| slots != &LEVELS.to_vec()));
    }

    #[test]
    fn verdict_tells_a_held_pick_from_a_wandering_one() {
        let lines = verdict(&[(0, Some(-6.0)), (0, Some(-6.0)), (1, None), (1, Some(6.0)), (2, Some(-12.0)),
                              (2, Some(-6.0)), (3, Some(0.0))]);
        assert!(lines[0].contains("매번 같음 -> -6 dB"), "{}", lines[0]);
        assert!(lines[1].contains("차이가 잘 안 들림"));
        assert!(lines[2].contains("그 사이"));
        assert!(lines[3].contains("보류") && lines[3].contains("+0 dB"));
        assert_eq!(lines.len(), 4);
    }

    fn above_500(case: &Case, level: Option<f64>) -> (usize, f64) {
        let mut render = renderer(case, level, &Gains::default(), Equalizer::for_player(&Default::default()));
        let mut samples = Vec::new();
        let mut first = 0;
        while let Some(block) = render(first) {
            assert!(!block.upsample, "every block goes out as 2 kHz");
            samples.extend_from_slice(&block.left_grip);
            first += crate::apex6::SUBFRAMES;
        }
        // first difference: a crude high-pass, enough to tell the levels apart
        let energy = samples.windows(2).map(|w| (w[1] - w[0]).powi(2)).sum::<f64>();
        (samples.len(), energy)
    }

    #[test]
    fn levels_play_the_whole_test_at_2k_and_order_the_band_above_500_hz() {
        let gear = &CASES[0];
        let energies: Vec<f64> = LEVELS
            .iter()
            .map(|level| {
                let (length, energy) = above_500(gear, *level);
                assert_eq!(length, (crate::preview::seconds_of(Effect::Gear) * 2000.0).round() as usize);
                energy
            })
            .collect();
        assert!(energies.windows(2).all(|w| w[0] < w[1]), "{energies:?}");
    }

    #[test]
    fn each_reference_is_named_for_its_effect_as_make_ring_references_writes_it() {
        for case in &CASES {
            assert_eq!(case.reference, format!("{}.wav", case.effect.name()));
        }
        let tool = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tools/make_ring_references.py")).unwrap();
        for case in &CASES {
            assert!(tool.contains(&format!("\"{}\": (", case.effect.name())), "{} has no recipe", case.effect.name());
        }
    }

    #[test]
    fn totals_add_up_and_say_how_many_were_late() {
        let mut totals = Totals::default();
        totals.add(&StreamStats { frames: 100, late_frames: 1, max_lateness_ms: 5.0, max_write_ms: 1.0, total_write_ms: 50.0 });
        totals.add(&StreamStats { frames: 300, late_frames: 3, max_lateness_ms: 2.0, max_write_ms: 2.5, total_write_ms: 150.0 });
        let text = totals.summary();
        assert!(text.contains("재생 2번") && text.contains("리포트 400개") && text.contains("늦음 4개 (1.00%)"), "{text}");
        assert!(text.contains("가장 늦은 5.00 ms") && text.contains("평균 0.50 ms / 최대 2.50 ms"), "{text}");
    }
}
