//! How long one report takes to reach the pad: silence streamed as the app streams it, at
//! 1 kHz (a report every 8 ms) and then at 2 kHz (every 4 ms), nothing felt.
//!
//!   cargo run --release --example write_timing            3 s of each
//!   cargo run --release --example write_timing -- 10      10 s of each
//!   cargo run --release --example write_timing -- 3 5 6   2 kHz blocks sent every 5 and 6 ms too
//!
//! At 2 kHz a write that takes over 4 ms leaves the pad short of samples: it plays with gaps.

use ats_haptics::apex6::{Apex6, Block, FRAME_PERIOD};

fn main() {
    let seconds: f64 = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(3.0);
    let mut pad = match Apex6::open() {
        Ok(pad) => pad,
        Err(e) => {
            eprintln!("컨트롤러를 열 수 없습니다: {e:?}");
            std::process::exit(1);
        }
    };
    let info = pad.info.clone().unwrap();
    let firmware: Vec<String> = info.firmware().iter().map(|(name, v)| format!("{name} {v}")).collect();
    println!("{}, connection_mode={}, 펌웨어: {}", info.name(), info.connection_mode, firmware.join(", "));
    let extra: Vec<f64> = std::env::args().skip(2).filter_map(|a| a.parse().ok()).collect();
    let mut runs = vec![("1k (8 ms)".to_string(), true, FRAME_PERIOD), ("2k (4 ms)".to_string(), false, FRAME_PERIOD)];
    runs.extend(extra.iter().map(|ms| (format!("2k blocks every {ms} ms"), false, 2.0 * ms / 1000.0)));
    for (name, upsample, period) in runs {
        let block = Block { upsample, ..Block::default() };
        let blocks = (seconds / block.period(period)).round() as usize;
        match pad.stream(|first| (first / 8 < blocks).then(|| block.clone()), None, period) {
            Ok(stats) => {
                let share = 100.0 * stats.late_frames as f64 / stats.frames.max(1) as f64;
                println!("{name}: {}  (늦음 {share:.1}%)", stats.summary());
            }
            Err(e) => eprintln!("{name}: {e:?}"),
        }
    }
}
