//! Writes an effect test (▶) as the pad gets it, for measuring on the PC.
//!
//!   cargo run --release --example render_effect -- blinker out.f32          1 kHz samples
//!   cargo run --release --example render_effect -- blinker out.f32 2k       the 2 kHz output
//!   cargo run --release --example render_effect -- road out.f32 2k gravel   the road test on one surface
//!
//! The file holds little-endian f32 triples (left grip, right grip, trigger) at the output's rate,
//! on the pad's 0..127 scale before rounding.

use std::io::Write;

use ats_haptics::effects::{Effect, Equalizer, Gains};
use ats_haptics::preview::Preview;
use ats_haptics::rate2k::Output2k;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 2 {
        eprintln!("usage: render_effect <effect> <out.f32> [2k] [surface]");
        std::process::exit(2);
    }
    let effect = Effect::from_name(&args[0]).unwrap_or_else(|| panic!("no effect {:?}", args[0]));
    let two_k = args.iter().skip(2).any(|a| a == "2k");
    let surfaces: Vec<&str> = args.iter().skip(2).filter(|a| *a != "2k").map(String::as_str).collect();
    let mut preview = Preview::new(effect, &Gains::default(), Equalizer::for_player(&Default::default()), &surfaces);
    preview.set_events_2k(two_k);
    let mut output = two_k.then(Output2k::new);
    let mut bytes = Vec::new();
    let mut put = |l: &[f64], r: &[f64], t: &[f64]| {
        for n in 0..l.len() {
            for v in [l[n], r[n], t[n]] {
                bytes.extend_from_slice(&(v as f32).to_le_bytes());
            }
        }
    };
    while let Some(block) = preview.render() {
        match &mut output {
            Some(output) => {
                for half in output.split(&block) {
                    put(&half.left_grip, &half.right_grip, &half.trigger);
                }
            }
            None => put(&block.left_grip, &block.right_grip, &block.trigger),
        }
    }
    std::fs::File::create(&args[1]).and_then(|mut f| f.write_all(&bytes)).expect("write");
}
