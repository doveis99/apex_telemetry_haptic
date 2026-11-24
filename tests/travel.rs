//! A truck wheel's bump is measured over its axle's travel, as the game's own vibration measures
//! it (python/tests/test_travel.py is the same checks on the Python synth).

use ats_haptics::effects::{Effect, Equalizer, Gains, Synth, DT};
use ats_haptics::telemetry::{Frame, Wheel};

fn frame(depths: &[f64], block: i64) -> Frame {
    let wheels = depths
        .iter()
        .enumerate()
        .map(|(k, &deflection)| Wheel {
            deflection,
            substance: "road".into(),
            pos_x: if k % 2 == 0 { -1.0 } else { 1.0 },
            velocity: 7.0,
            radius: 0.5,
            ..Wheel::default()
        })
        .collect();
    Frame {
        active: true,
        paused: false,
        render_time: block * 8000,
        gear: 10,
        rpm: 1300.0,
        speed: 22.0,
        game_throttle: 0.4,
        user_throttle: 0.4,
        engine_on: true,
        accel: [0.2, 0.0, 0.5],
        wheels,
        ..Frame::default()
    }
}

/// How strong the knock is that one truck wheel stepping at `speed` m/s for a frame makes; 0 for none.
fn knock(wheel: usize, speed: f64, set: impl FnOnce(&mut Gains)) -> f64 {
    let mut gains = Gains::default();
    set(&mut gains);
    let mut synth = Synth::new(gains.effective(), Equalizer::default());
    let mut depths = [0.02; 6];
    synth.render(Some(&frame(&depths, 0)));
    depths[wheel] += speed * DT;
    synth.render(Some(&frame(&depths, 1)));
    depths[wheel] -= 0.0001; // it stays there: a step, not a spike
    synth.render(Some(&frame(&depths, 2)));
    synth.bursts.iter().filter(|b| b.effect == Effect::Bumps).map(|b| b.burst.level()).fold(0.0, f64::max)
}

#[test]
fn a_vanilla_truck_knocks_as_it_did_in_plain_metres_a_second() {
    assert_eq!((Gains::default().travel_front, Gains::default().travel_rear), (0.1, 0.088));
    let rear = knock(2, 0.6, |_| {});
    assert!(rear > 0.0);
    // each axle is set against its own vanilla travel: the same step, the same knock
    assert_eq!(knock(0, 0.6, |_| {}), rear);
    assert_eq!(knock(4, 0.6, |_| {}), rear);
    // a travel of nothing is read as the vanilla one
    assert_eq!(knock(2, 0.6, |g| g.travel_rear = 0.0), rear);
}

#[test]
fn the_same_step_counts_by_how_much_room_the_axle_has() {
    // twice the travel is half the movement: 0.6 m/s there is 0.3 m/s on a vanilla axle
    assert!((knock(0, 0.6, |g| g.travel_front = 0.2) - knock(0, 0.3, |_| {})).abs() < 1e-9);
    assert!((knock(2, 0.6, |g| g.travel_rear = 0.176) - knock(2, 0.3, |_| {})).abs() < 1e-9);
}

#[test]
fn a_physics_mods_longer_travel_softens_that_axle_only() {
    let (vanilla, modded) = (knock(0, 0.6, |_| {}), knock(0, 0.6, |g| g.travel_front = 0.18));
    assert!(0.0 < modded && modded < 0.5 * vanilla, "{modded} {vanilla}");
    assert_eq!(knock(2, 0.6, |g| g.travel_front = 0.18), knock(2, 0.6, |_| {}));
    // long enough, and the step is under the threshold
    assert_eq!(knock(0, 0.6, |g| g.travel_front = 0.3), 0.0);
    // a shorter travel makes more of the same step
    assert!(knock(2, 0.3, |g| g.travel_rear = 0.05) > knock(2, 0.3, |_| {}));
}
