//! The ring-level check on the pad (src/ring_level.rs), from the pad's buttons:
//!
//!   cargo run --release --example ring_level            two rounds
//!   cargo run --release --example ring_level -- 3       three
//!
//! Each trial plays the truck's own sound for one effect test on the PC (Y again), then slot 1
//! on the pad; D-pad ← → step through the slots, X plays the slot again, A picks it once every
//! slot has been heard, B stops. The tests play with the player's own gains and eq, through the
//! app's 2 kHz output. The report goes to the app's logs folder as ring-level-*.txt.

use std::path::{Path, PathBuf};
use std::time::Duration;

use ats_haptics::apex6::Apex6;
use ats_haptics::calibrate::PadButtons;
use ats_haptics::effects::Equalizer;
use ats_haptics::pyrandom::PyRandom;
use ats_haptics::ring_level::{level_label, play, plan, verdict, Totals, CASES, LEVELS};
use ats_haptics::settings::Settings;

const LEFT: u16 = 0x0004;
const RIGHT: u16 = 0x0008;
const A: u16 = 0x1000;
const B: u16 = 0x2000;
const X: u16 = 0x4000;
const Y: u16 = 0x8000;
const REFERENCE_GAP: Duration = Duration::from_millis(400);

fn reference_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("sound sample").join("ring-reference")
}

/// Play a WAV through the PC's default output and wait for it to end.
fn play_reference(path: &Path) {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Media::Audio::{PlaySoundW, SND_FILENAME, SND_SYNC};
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: a NUL-terminated path; SND_SYNC returns once it has played.
    unsafe { PlaySoundW(wide.as_ptr(), std::ptr::null_mut(), SND_FILENAME | SND_SYNC) };
}

/// The next button that goes down among `wanted`.
fn wait(buttons: &mut PadButtons, wanted: u16) -> u16 {
    loop {
        let pressed = buttons.pressed() & wanted;
        if pressed != 0 {
            return 1 << pressed.trailing_zeros();
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn main() {
    let rounds: usize = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(ats_haptics::ring_level::ROUNDS).max(1);
    let settings = Settings::default();
    let gains = settings.load_gains();
    let eq = Equalizer::for_player(&settings.load_eq());
    let mut buttons = PadButtons::default();
    let mut pad = match Apex6::open() {
        Ok(pad) => pad,
        Err(e) => {
            eprintln!("컨트롤러를 열 수 없습니다: {e:?}");
            std::process::exit(1);
        }
    };
    if !buttons.connected {
        eprintln!("XInput 패드가 안 보입니다. 패드를 Xbox 모드로 연결하세요.");
        std::process::exit(1);
    }
    let info = pad.info.clone().unwrap();
    let firmware: Vec<String> = info.firmware().iter().map(|(name, v)| format!("{name} {v}")).collect();
    let references: Vec<Option<PathBuf>> =
        CASES.iter().map(|c| Some(reference_dir().join(c.reference)).filter(|p| p.is_file())).collect();
    let levels: Vec<String> = LEVELS.iter().map(|l| level_label(*l)).collect();

    println!("펌웨어: {}", firmware.join(", "));
    if info.connection_mode != 0 {
        println!("  ! 무선으로 보입니다. 무선은 소리가 잠깐씩 끊겨 딸깍거릴 수 있습니다. 가능하면 유선으로.");
    }
    println!("\n앱의 '2 kHz 출력'과 같은 경로로 효과 테스트(▶)를 링·돌 소리 크기만 바꿔 번호 칸에 무작위로 놓았습니다.");
    println!("  단계: {} (0 dB가 지금 값). 어느 칸이 어느 단계인지는 끝나고 알려 줍니다.", levels.join(", "));
    if references.iter().any(Option::is_some) {
        println!("  회차마다 먼저 기준음(사운드 모드의 실제 트럭 소리, 같은 박자)이 PC로 나오고 이어서 칸 1이 패드로 나옵니다.");
        println!("  PC 볼륨은 게임에서 그 소리가 들리는 정도로. 크기가 아니라 성격(밝기·울림)이 가장 비슷한 칸을 고르세요.");
    } else {
        println!("  기준음이 없습니다 ({}). 만들려면: python tools/make_ring_references.py --dlls <libvorbis 폴더>",
                 reference_dir().display());
    }
    println!("  튀거나 '띵' 하고 따로 놀면 너무 큰 것, 둔하고 먹먹하면 작은 것. '끔'도 답이 될 수 있습니다.");
    println!("  십자키 ← → 칸 옮기며 듣기 / X 다시 듣기 / Y 기준음 / A 이 칸으로 고르기 (모든 칸을 한 번씩 들은 뒤) / B 그만");

    let plan = plan(rounds, &mut PyRandom::from_entropy());
    let mut results: Vec<(usize, Option<f64>)> = Vec::new();
    let mut totals = Totals::default();
    let (mut replays, mut reference_plays) = (0, 0);
    println!("\n{}번 합니다. A를 누르면 시작합니다", plan.len());
    if wait(&mut buttons, A | B) == B {
        println!("그만둡니다.");
        return;
    }
    'trials: for (number, (case, slots)) in plan.iter().enumerate() {
        let case_info = &CASES[*case];
        println!("[{}/{}] {}: 칸 {}개", number + 1, plan.len(), case_info.label, slots.len());
        let reference = |reference_plays: &mut u32| match &references[*case] {
            Some(path) => {
                println!("  기준음 (PC)");
                play_reference(path);
                *reference_plays += 1;
                true
            }
            None => false,
        };
        if reference(&mut reference_plays) {
            std::thread::sleep(REFERENCE_GAP);
        }
        let mut slot = 0usize;
        let mut heard = vec![false; slots.len()];
        heard[0] = true;
        loop {
            println!("  칸 {}", slot + 1);
            match play(&mut pad, case_info, slots[slot], &gains, eq.clone()) {
                Ok(stats) => totals.add(&stats),
                Err(e) => {
                    eprintln!("재생 실패: {e:?}");
                    break 'trials;
                }
            }
            loop {
                match wait(&mut buttons, LEFT | RIGHT | X | Y | A | B) {
                    B => {
                        break 'trials;
                    }
                    Y => {
                        if !reference(&mut reference_plays) {
                            println!("  이 효과는 기준음이 없습니다");
                        }
                    }
                    A if heard.iter().all(|h| *h) => {
                        results.push((*case, slots[slot]));
                        println!("  칸 {} 고름", slot + 1);
                        continue 'trials;
                    }
                    A => {
                        let left: Vec<String> =
                            heard.iter().enumerate().filter(|(_, h)| !**h).map(|(n, _)| (n + 1).to_string()).collect();
                        println!("  아직 안 들은 칸: {}", left.join(", "));
                    }
                    X => {
                        replays += 1;
                        break;
                    }
                    button => {
                        slot = if button == RIGHT { (slot + 1) % slots.len() } else { (slot + slots.len() - 1) % slots.len() };
                        heard[slot] = true;
                        break;
                    }
                }
            }
        }
    }
    drop(pad);
    if results.is_empty() {
        println!("그만둡니다.");
        return;
    }
    let with_reference: Vec<&str> =
        CASES.iter().zip(&references).filter(|(_, r)| r.is_some()).map(|(c, _)| c.effect.name()).collect();
    let mut lines = vec![
        format!("링 크기 듣기 점검 (Rust) {}, ATS 햅틱 {}", ats_haptics::clock::now().stamp(), env!("CARGO_PKG_VERSION")),
        format!("구성 요소 펌웨어: {}", firmware.join(", ")),
        format!("기기: {}, connection_mode={}, features=0x{:02X}", info.name(), info.connection_mode, info.features),
        format!(
            "0x01 응답: {}",
            info.raw.iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(" ")
        ),
        format!("앱의 2 kHz 출력 경로, 양쪽 그립, 내 설정(게인·eq), 단계 {}", levels.join(", ")),
        format!("답: 패드 버튼, {}/{}회, 다시 듣기 {replays}번", results.len(), plan.len()),
        format!(
            "기준음: {} ({}), 들은 횟수 {reference_plays}번",
            if with_reference.is_empty() { "없음".to_string() } else { with_reference.join(", ") },
            "ring-reference"
        ),
        format!("스트림: {}", totals.summary()),
        String::new(),
    ];
    lines.extend(verdict(&results));
    let report = lines.join("\n");
    println!("\n{report}");
    let dir = ats_haptics::blocklog::log_dir();
    let path = dir.join(format!("ring-level-{}.txt", ats_haptics::clock::now().stamp()));
    match std::fs::create_dir_all(&dir).and_then(|_| std::fs::write(&path, report + "\n")) {
        Ok(()) => println!("\n저장: {}", path.display()),
        Err(e) => eprintln!("\n저장 실패: {e}"),
    }
}
