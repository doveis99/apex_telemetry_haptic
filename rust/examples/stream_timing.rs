//! How punctually the 8 ms stream goes out, without a pad: the scripted drive through the
//! synth and Apex6::stream into a link that discards its reports, as docs/latency-notes.md
//! measured the Python app (median block gap 8.01 ms, synth 137 us median).
//!
//!   cargo run --release --example stream_timing -- 20      seconds to run (default 10)

use std::time::Instant;

use ats_haptics::apex6::{Apex6, Link, RealClock, FRAME_PERIOD};
use ats_haptics::effects::{Equalizer, Gains, Synth};
use ats_haptics::telemetry::{SimulatedTelemetry, TelemetrySource};

struct Discard;

impl Link for Discard {
    fn write(&mut self, report: &[u8]) -> Result<usize, String> {
        Ok(report.len())
    }

    fn read(&mut self, _buf: &mut [u8], _timeout_ms: i32) -> Result<usize, String> {
        Ok(0)
    }
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    sorted[((sorted.len() - 1) as f64 * q).round() as usize]
}

fn main() {
    let seconds: f64 = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(10.0);
    let mut synth = Synth::new(Gains::default(), Equalizer::for_player(&Default::default()));
    let mut source = SimulatedTelemetry::new(Some(60.0));
    let mut pad = Apex6::with_link(Discard, RealClock::default());
    let mut render_us = Vec::new();
    let mut gaps_ms = Vec::new();
    let mut last: Option<Instant> = None;
    let stats = pad
        .stream(
            |_| {
                let now = Instant::now();
                if let Some(last) = last {
                    gaps_ms.push((now - last).as_secs_f64() * 1000.0);
                }
                last = Some(now);
                let frame = source.read();
                let started = Instant::now();
                let block = synth.render(frame.as_ref());
                render_us.push(started.elapsed().as_secs_f64() * 1e6);
                Some(block)
            },
            Some(seconds),
            FRAME_PERIOD,
        )
        .unwrap();
    render_us.sort_by(|a, b| a.partial_cmp(b).unwrap());
    gaps_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let late = gaps_ms.iter().filter(|g| **g > FRAME_PERIOD * 1000.0 + 1.0).count();
    println!("{}", stats.summary());
    println!(
        "synth render: median {:.1} us, p99 {:.1} us, max {:.1} us",
        percentile(&render_us, 0.5),
        percentile(&render_us, 0.99),
        render_us[render_us.len() - 1]
    );
    println!(
        "block gap: median {:.3} ms, p99 {:.3} ms, max {:.3} ms, {} of {} over 9 ms",
        percentile(&gaps_ms, 0.5),
        percentile(&gaps_ms, 0.99),
        gaps_ms[gaps_ms.len() - 1],
        late,
        gaps_ms.len()
    );
    pad.set_enabled(false);
}
