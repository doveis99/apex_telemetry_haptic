//! How punctually the stream goes out, without a pad: the scripted drive through the synth
//! and Apex6::stream into a link that discards its reports, as docs/latency-notes.md
//! measured the Python app (median block gap 8.01 ms, synth 137 us median).
//!
//!   cargo run --release --example stream_timing -- 20          seconds to run (default 10)
//!   cargo run --release --example stream_timing -- 20 2k       the 2 kHz output, a report every 4 ms
//!   cargo run --release --example stream_timing -- 20 2k load  with every logical core kept busy

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use ats_haptics::apex6::{Apex6, Link, RealClock, FRAME_PERIOD};
use ats_haptics::effects::{Equalizer, Gains, Synth};
use ats_haptics::rate2k::Output2k;
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
    let args: Vec<String> = std::env::args().skip(1).collect();
    let seconds: f64 = args.first().and_then(|s| s.parse().ok()).unwrap_or(10.0);
    let two_k = args.iter().any(|a| a == "2k");
    let load = args.iter().any(|a| a == "load");

    let busy = Arc::new(AtomicBool::new(load));
    let spinners: Vec<_> = (0..if load { std::thread::available_parallelism().map_or(8, |n| n.get()) } else { 0 })
        .map(|_| {
            let busy = Arc::clone(&busy);
            std::thread::spawn(move || {
                let mut x = 0u64;
                while busy.load(Ordering::Relaxed) {
                    x = std::hint::black_box(x.wrapping_mul(6364136223846793005).wrapping_add(1));
                }
            })
        })
        .collect();

    let mut synth = Synth::new(Gains::default(), Equalizer::for_player(&Default::default()));
    synth.set_events_2k(two_k);
    let mut output = two_k.then(Output2k::new);
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
                if let Some(second) = output.as_mut().and_then(Output2k::pending) {
                    return Some(second);
                }
                let frame = source.read();
                let started = Instant::now();
                let block = synth.render(frame.as_ref());
                let block = match &mut output {
                    Some(output) => output.first(&block),
                    None => block,
                };
                render_us.push(started.elapsed().as_secs_f64() * 1e6);
                Some(block)
            },
            Some(seconds),
            FRAME_PERIOD,
        )
        .unwrap();
    busy.store(false, Ordering::Relaxed);
    for spinner in spinners {
        let _ = spinner.join();
    }
    let period_ms = if two_k { FRAME_PERIOD * 500.0 } else { FRAME_PERIOD * 1000.0 };
    render_us.sort_by(|a, b| a.partial_cmp(b).unwrap());
    gaps_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let over = |ms: f64| gaps_ms.iter().filter(|g| **g > period_ms + ms).count();
    println!("{} output{}", if two_k { "2 kHz" } else { "1 kHz" }, if load { ", every core busy" } else { "" });
    println!("{}", stats.summary());
    println!(
        "render (synth{}): median {:.1} us, p99 {:.1} us, max {:.1} us",
        if two_k { " + 2 kHz stage" } else { "" },
        percentile(&render_us, 0.5),
        percentile(&render_us, 0.99),
        render_us[render_us.len() - 1]
    );
    println!(
        "report gap: median {:.3} ms, p99 {:.3} ms, max {:.3} ms; over the {period_ms} ms period by 0.5 ms {}, by 1 ms {}, of {}",
        percentile(&gaps_ms, 0.5),
        percentile(&gaps_ms, 0.99),
        gaps_ms[gaps_ms.len() - 1],
        over(0.5),
        over(1.0),
        gaps_ms.len()
    );
    pad.set_enabled(false);
}
