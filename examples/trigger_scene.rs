//! The firmware's own trigger effects (0x53 trigger record mode 4), one per trigger:
//!
//!   cargo run --release --example trigger_scene -- ram     what the records hold, no hands needed
//!   cargo run --release --example trigger_scene            the feel check, from the pad
//!   cargo run --release --example trigger_scene -- rate    what the loop's param does: one trigger
//!                                                          through its values, then two side by side
//!
//! A mode-4 record is `01 0s 04 type strength param start` (s: 0 left, 1 right; type 0 a loop,
//! 1 one shot; param the loop's rate; start the trigger pull it plays from, 0-255), after
//! ApexSenseBridge 1.1.0, which plays them when a game drives both triggers differently. The
//! first byte stays 01: 00 is the area Space Station saves to flash.
//!
//! The feel check is not blind. Each step sets the records, says what to pull and asks with
//! the face buttons (Back stops); what the triggers read meanwhile and the motor RAM after
//! each step go to the report, trigger-scene-*.txt in the app's logs folder.

use std::time::{Duration, Instant};

use ats_haptics::apex6::{
    build_command, motor_disable, Apex6, Block, Link, Report, Route, CMD_MOTOR_ROUTE, FRAME_PERIOD, MAGIC,
    MAX_SAMPLE, REPORT_SIZE, SAMPLE_RATE, SUBFRAMES,
};

const CMD_RAM_READ: u8 = 0xA3;
const RAM_MOTOR: u8 = 0x06;
const RAM_CHUNK: u8 = 16;
const LEFT: u8 = 0;
const RIGHT: u8 = 1;

const BACK: u16 = 0x0020;
const A: u16 = 0x1000;
const B: u16 = 0x2000;
const X: u16 = 0x4000;
const Y: u16 = 0x8000;

#[derive(Clone, Copy)]
struct Scene {
    kind: u8,
    strength: u8,
    param: u8,
    start: u8,
}

const fn looped(strength: u8, param: u8, start: u8) -> Scene {
    Scene { kind: 0, strength, param, start }
}

const fn one_shot(strength: u8, start: u8) -> Scene {
    Scene { kind: 1, strength, param: 0, start }
}

fn scene_frame(side: u8, scene: Scene) -> Report {
    build_command(CMD_MOTOR_ROUTE, &[0x01, side, 0x04, scene.kind, scene.strength, scene.param, scene.start])
}

fn off_frame(side: u8) -> Report {
    build_command(CMD_MOTOR_ROUTE, &[0x01, side, 0x00])
}

fn send(pad: &mut Apex6, report: Report) -> Result<(), String> {
    pad.link_mut().write(&report).map(|_| ())?;
    std::thread::sleep(Duration::from_millis(12)); // the pad takes no report for ~5 ms after a 0x53
    Ok(())
}

fn set_scenes(pad: &mut Apex6, left: Option<Scene>, right: Option<Scene>) -> Result<(), String> {
    for (side, scene) in [(LEFT, left), (RIGHT, right)] {
        send(pad, scene.map_or(off_frame(side), |s| scene_frame(side, s)))?;
    }
    Ok(())
}

/// build_command's frame with the checksum right after the payload, as Space Station sends it.
fn inline_command(command: u8, payload: &[u8]) -> Report {
    let mut report = [0u8; REPORT_SIZE];
    report[1..3].copy_from_slice(&MAGIC);
    report[3] = command;
    report[4] = payload.len() as u8 + 2;
    report[5..5 + payload.len()].copy_from_slice(payload);
    report[5 + payload.len()] = report[3..5 + payload.len()].iter().fold(0u8, |s, &b| s.wrapping_add(b));
    report
}

/// (status, total length, data) of the 0xA3 reply for that chunk.
fn ram_chunk(data: &[u8], ram_id: u8, index: u8) -> Option<(u8, usize, Vec<u8>)> {
    let frame = if data.len() >= REPORT_SIZE && data[0] == 0 && data[1..3] == MAGIC {
        &data[1..REPORT_SIZE]
    } else if data.len() >= REPORT_SIZE - 1 && data[..2] == MAGIC {
        &data[..REPORT_SIZE - 1]
    } else {
        return None;
    };
    if frame[2] != CMD_RAM_READ {
        return None;
    }
    let sum = |end: usize| frame[2..end - 1].iter().fold(0u8, |s, &b| s.wrapping_add(b));
    let end = (5..=frame.len()).rev().find(|&end| sum(end) == frame[end - 1])?;
    let payload = &frame[3..end - 1];
    if payload.len() < 8 || payload[0] != 0 || payload[2] != ram_id || payload[6] != index {
        return None;
    }
    let data = &payload[8..(8 + payload[7] as usize).min(payload.len())];
    Some((payload[1], payload[4] as usize | (payload[5] as usize) << 8, data.to_vec()))
}

fn read_ram(pad: &mut Apex6, ram_id: u8) -> Result<Vec<u8>, String> {
    let mut data: Vec<u8> = Vec::new();
    let mut total: Option<usize> = None;
    let mut index = 0u8;
    while total.is_none_or(|t| data.len() < t) {
        let request = [0x00, ram_id, index, RAM_CHUNK];
        let mut chunk = None;
        for attempt in 0..4 {
            let report =
                if attempt % 2 == 0 { build_command(CMD_RAM_READ, &request) } else { inline_command(CMD_RAM_READ, &request) };
            pad.link_mut().write(&report)?;
            let deadline = Instant::now() + Duration::from_millis(600);
            let mut buf = [0u8; 64];
            while chunk.is_none() && Instant::now() < deadline {
                let n = pad.link_mut().read(&mut buf, 50)?;
                chunk = if n > 0 { ram_chunk(&buf[..n], ram_id, index) } else { None };
            }
            if chunk.is_some() {
                break;
            }
        }
        let (status, length, part) = chunk.ok_or(format!("0xA3 RAM {ram_id} chunk {index}: 응답 없음"))?;
        if status != 0 {
            return Err(format!("0xA3 RAM {ram_id} chunk {index}: status {status}"));
        }
        total.get_or_insert(length);
        if part.is_empty() {
            break;
        }
        data.extend(part);
        index += 1;
    }
    data.truncate(total.unwrap_or(0));
    Ok(data)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(" ")
}

/// The realtime area's records: trigger left/right (11 bytes each), grip left/right (2 each).
fn records(pad: &mut Apex6) -> String {
    match read_ram(pad, RAM_MOTOR) {
        Ok(ram) if ram.len() >= 0x3B => format!(
            "트리거 왼 [{}] 오른 [{}] / 그립 왼 [{}] 오른 [{}]",
            hex(&ram[0x21..0x2C]),
            hex(&ram[0x2C..0x37]),
            hex(&ram[0x37..0x39]),
            hex(&ram[0x39..0x3B])
        ),
        Ok(ram) => format!("RAM이 짧음 ({}바이트): {}", ram.len(), hex(&ram)),
        Err(e) => format!("RAM 읽기 실패: {e}"),
    }
}

fn clear(pad: &mut Apex6) {
    let _ = set_scenes(pad, None, None);
    for report in motor_disable() {
        let _ = send(pad, report);
    }
}

fn silent(pad: &mut Apex6, trigger_bit: bool, blocks: usize) -> Result<(), String> {
    let block = Block { route: if trigger_bit { Route::Both } else { Route::Mute }, ..Block::default() };
    for _ in 0..blocks {
        pad.write_block(&block).map_err(|e| format!("{e:?}"))?;
        std::thread::sleep(Duration::from_secs_f64(FRAME_PERIOD));
    }
    Ok(())
}

fn ram_check(pad: &mut Apex6) -> Result<Vec<String>, String> {
    let mut lines = Vec::new();
    let mut note = |pad: &mut Apex6, what: &str| {
        let line = format!("{what}\n    {}", records(pad));
        println!("{line}");
        lines.push(line);
    };
    note(pad, "처음");
    clear(pad);
    note(pad, "끈 뒤");
    send(pad, scene_frame(LEFT, looped(100, 60, 0x40)))?;
    note(pad, "왼쪽 01 00 04 00 64 3C 40 (반복, 세기 100, 60, 시작 0x40)");
    send(pad, scene_frame(RIGHT, one_shot(200, 0x80)))?;
    note(pad, "오른쪽 01 01 04 01 C8 00 80 (한 번, 세기 200, 시작 0x80)");
    silent(pad, false, 12)?;
    note(pad, "0x57 12개 (그립 비트만, 샘플 0)");
    silent(pad, true, 12)?;
    note(pad, "0x57 12개 (트리거 비트 포함, 샘플 0)");
    send(pad, off_frame(RIGHT))?;
    silent(pad, true, 12)?;
    note(pad, "오른쪽 끄고 0x57 12개 (트리거 비트 포함)");
    println!("0x57 없이 11초 기다립니다...");
    std::thread::sleep(Duration::from_secs(11));
    note(pad, "0x57 없이 11초 뒤");
    clear(pad);
    note(pad, "끈 뒤");
    Ok(lines)
}

/// The pad as XInput shows it: buttons gone down, and how far each trigger was pulled since
/// the last question.
#[derive(Default)]
struct Input {
    held: u16,
    pulls: [[u8; 2]; 2], // [left, right] x [least, most]
    fresh: bool,
}

impl Input {
    fn state() -> Option<(u16, u8, u8)> {
        use windows_sys::Win32::UI::Input::XboxController::{XInputGetState, XINPUT_STATE};
        let mut found = None;
        for index in 0..4 {
            // SAFETY: XInputGetState fills the state it is given.
            let mut state: XINPUT_STATE = unsafe { std::mem::zeroed() };
            if unsafe { XInputGetState(index, &mut state) } == 0 {
                let (buttons, left, right) = found.unwrap_or((0, 0, 0));
                let pad = state.Gamepad;
                found = Some((buttons | pad.wButtons, pad.bLeftTrigger.max(left), pad.bRightTrigger.max(right)));
            }
        }
        found
    }

    fn reset(&mut self) {
        self.fresh = true;
    }

    /// The first of `wanted` that went down since the last call.
    fn poll(&mut self, wanted: u16) -> Option<u16> {
        let (buttons, left, right) = Input::state()?;
        for (side, pull) in [left, right].into_iter().enumerate() {
            let [least, most] = self.pulls[side];
            self.pulls[side] = if self.fresh { [pull, pull] } else { [least.min(pull), most.max(pull)] };
        }
        self.fresh = false;
        let pressed = buttons & !self.held & wanted;
        self.held = buttons;
        (pressed != 0).then(|| 1 << pressed.trailing_zeros())
    }

    fn pulled(&self) -> String {
        let [[l0, l1], [r0, r1]] = self.pulls;
        format!("LT {l0}-{l1}, RT {r0}-{r1}")
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Stream {
    None,
    Grips,   // 0x57 with the grip bits only, a 65 Hz tone on both grips
    Trigger, // 0x57 with the trigger bit, a 110 Hz tone on the trigger channel
}

type Choices = &'static [(u16, &'static str, &'static str)];

const WHERE: Choices = &[(X, "X", "왼쪽만"), (B, "B", "오른쪽만"), (Y, "Y", "양쪽"), (A, "A", "안 울림")];
const DIFFER: Choices = &[(Y, "Y", "뚜렷이 다름"), (X, "X", "조금 다름"), (A, "A", "같음")];
const STRONGER: Choices = &[(X, "X", "왼쪽이 셈"), (B, "B", "오른쪽이 셈"), (A, "A", "같음")];
const YES_NO: Choices = &[(Y, "Y", "나옴"), (A, "A", "안 나옴")];
const SHOT: Choices =
    &[(X, "X", "당길 때 한 번 툭"), (Y, "Y", "계속 떨림"), (A, "A", "아무것도 없음"), (B, "B", "다른 느낌")];
const CHARACTER: Choices =
    &[(X, "X", "끊어지는 박자"), (Y, "Y", "연속 떨림"), (B, "B", "다른 느낌"), (A, "A", "안 울림")];
const FASTER: Choices =
    &[(X, "X", "왼쪽이 빠름"), (B, "B", "오른쪽이 빠름"), (A, "A", "같음"), (Y, "Y", "느낌이 아예 다름")];
const DEPTH: Choices = &[(Y, "Y", "깊게 당겨야 울림"), (X, "X", "얕아도 울림"), (A, "A", "안 울림")];

struct Step {
    title: &'static str,
    left: Option<Scene>,
    right: Option<Scene>,
    stream: Stream,
    questions: &'static [(&'static str, Choices)],
}

impl Step {
    fn copy(&self) -> Step {
        Step { title: self.title, left: self.left, right: self.right, stream: self.stream, questions: self.questions }
    }
}

const STEPS: &[Step] = &[
    Step {
        title: "왼쪽에만 효과 (반복, 세기 100, 60)",
        left: Some(looped(100, 60, 0x20)),
        right: None,
        stream: Stream::None,
        questions: &[("트리거를 하나씩, 그리고 같이 당겨 보세요. 어느 트리거가 울리나요?", WHERE)],
    },
    Step {
        title: "오른쪽에만 효과 (반복, 세기 100, 60)",
        left: None,
        right: Some(looped(100, 60, 0x20)),
        stream: Stream::None,
        questions: &[("트리거를 하나씩, 그리고 같이 당겨 보세요. 어느 트리거가 울리나요?", WHERE)],
    },
    Step {
        title: "양쪽에 다른 빠르기 (왼쪽 30, 오른쪽 120, 세기 100)",
        left: Some(looped(100, 30, 0x20)),
        right: Some(looped(100, 120, 0x20)),
        stream: Stream::None,
        questions: &[
            ("둘 다 당기고 있을 때 어느 트리거가 울리나요?", WHERE),
            ("좌우의 떨림 빠르기가 다른가요?", DIFFER),
        ],
    },
    Step {
        title: "양쪽에 다른 세기 (왼쪽 40, 오른쪽 150, 둘 다 60)",
        left: Some(looped(40, 60, 0x20)),
        right: Some(looped(150, 60, 0x20)),
        stream: Stream::None,
        questions: &[("둘 다 당기고 있을 때 어느 쪽이 더 센가요?", STRONGER)],
    },
    Step {
        title: "왼쪽 효과 + 그립 실시간 진동 (65 Hz)",
        left: Some(looped(100, 60, 0x20)),
        right: None,
        stream: Stream::Grips,
        questions: &[
            ("트리거를 하나씩, 그리고 같이 당겨 보세요. 어느 트리거가 울리나요?", WHERE),
            ("손바닥(그립) 진동도 같이 나오나요?", YES_NO),
        ],
    },
    Step {
        title: "왼쪽 효과 + 트리거 실시간 진동 (110 Hz)",
        left: Some(looped(100, 60, 0x20)),
        right: None,
        stream: Stream::Trigger,
        questions: &[
            ("둘 다 당기고 있을 때 어느 트리거가 울리나요?", WHERE),
            ("좌우의 느낌이 다른가요?", DIFFER),
        ],
    },
    Step {
        title: "오른쪽에 한 번짜리 효과 (세기 200, 절반 지점)",
        left: None,
        right: Some(one_shot(200, 0x80)),
        stream: Stream::None,
        questions: &[("오른쪽 트리거를 천천히 끝까지 당겼다 놓기를 몇 번 해 보세요. 어떤가요?", SHOT)],
    },
    Step {
        title: "왼쪽 효과, 시작 지점 깊게 (0xC0)",
        left: Some(looped(100, 60, 0xC0)),
        right: None,
        stream: Stream::None,
        questions: &[("왼쪽 트리거를 얕게, 그리고 끝까지 당겨 보세요. 어떤가요?", DEPTH)],
    },
];

/// The loop's param on its own: the left trigger through RATES, then pairs on the two triggers.
const RATES: [u8; 9] = [1, 2, 4, 8, 16, 30, 60, 120, 255];
const RATE_PAIRS: [(u8, u8); 5] = [(4, 16), (16, 4), (8, 60), (60, 255), (30, 120)];

fn rate_steps() -> Vec<Step> {
    let title = |text: String| -> &'static str { Box::leak(text.into_boxed_str()) };
    let single = RATES.iter().map(|&rate| Step {
        title: title(format!("왼쪽에만, 값 {rate} (세기 100)")),
        left: Some(looped(100, rate, 0x20)),
        right: None,
        stream: Stream::None,
        questions: &[("왼쪽 트리거를 당기고 있어 보세요. 어떤 떨림인가요?", CHARACTER)],
    });
    let pairs = RATE_PAIRS.iter().map(|&(left, right)| Step {
        title: title(format!("양쪽, 값 왼쪽 {left} / 오른쪽 {right} (세기 100)")),
        left: Some(looped(100, left, 0x20)),
        right: Some(looped(100, right, 0x20)),
        stream: Stream::None,
        questions: &[("둘 다 당기고 있어 보세요. 어느 쪽 떨림(박자)이 더 빠른가요?", FASTER)],
    });
    single.chain(pairs).collect()
}

fn tone(stream: Stream, index: usize) -> Block {
    let mut block = Block::default();
    let (hz, share) = if stream == Stream::Grips { (65.0, 0.2) } else { (110.0, 0.2) };
    for k in 0..SUBFRAMES {
        let n = (index + k) as f64;
        let fade = (n / (0.015 * SAMPLE_RATE as f64)).min(1.0);
        let sample = fade * share * MAX_SAMPLE * (std::f64::consts::TAU * hz * n / SAMPLE_RATE as f64).sin();
        match stream {
            Stream::Grips => {
                block.left_grip[k] = sample;
                block.right_grip[k] = sample;
            }
            _ => block.trigger[k] = sample,
        }
    }
    if stream == Stream::Trigger {
        block.route = Route::Both;
    }
    block
}

/// The button that answers, with the stream running meanwhile; Err on a lost pad.
fn answer(pad: &mut Apex6, input: &mut Input, stream: Stream, wanted: u16) -> Result<u16, String> {
    input.reset();
    if stream == Stream::None {
        loop {
            if let Some(button) = input.poll(wanted) {
                return Ok(button);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    let mut pressed = None;
    pad.stream(
        |index| {
            if (index / SUBFRAMES) % 4 == 0 {
                pressed = input.poll(wanted);
            }
            pressed.is_none().then(|| tone(stream, index))
        },
        None,
        FRAME_PERIOD,
    )
    .map_err(|e| format!("{e:?}"))?;
    Ok(pressed.unwrap_or(BACK))
}

fn feel_check(pad: &mut Apex6, input: &mut Input, steps: &[Step]) -> Result<Vec<String>, String> {
    let mut lines = Vec::new();
    println!("\n{}단계입니다. 답은 화면에 나오는 버튼으로, Back(뷰) 버튼은 그만.", steps.len());
    println!("효과는 트리거를 당기고 있을 때만 나옵니다. 검지를 양쪽 트리거에 올려 두세요.");
    println!("A를 누르면 시작합니다");
    input.reset();
    loop {
        match input.poll(A | BACK) {
            Some(BACK) => return Ok(lines),
            Some(_) => break,
            None => std::thread::sleep(Duration::from_millis(10)),
        }
    }
    'steps: for (number, step) in steps.iter().enumerate() {
        clear(pad);
        std::thread::sleep(Duration::from_millis(300));
        set_scenes(pad, step.left, step.right)?;
        println!("\n[{}/{}] {}", number + 1, steps.len(), step.title);
        lines.push(format!("[{}] {}", number + 1, step.title));
        for (question, choices) in step.questions {
            let keys: Vec<String> = choices.iter().map(|(_, key, label)| format!("{key} {label}")).collect();
            println!("  {question}\n    {}", keys.join(" / "));
            let wanted = choices.iter().fold(BACK, |all, (mask, ..)| all | mask);
            let button = answer(pad, input, step.stream, wanted)?;
            if button == BACK {
                lines.push("  (그만둠)".to_string());
                break 'steps;
            }
            let label = choices.iter().find(|(mask, ..)| *mask == button).map_or("?", |(_, _, label)| label);
            println!("    → {label}");
            lines.push(format!("  {question} → {label}  ({})", input.pulled()));
        }
        lines.push(format!("  RAM: {}", records(pad)));
    }
    clear(pad);
    Ok(lines)
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    let ram_only = mode == "ram";
    let steps = if mode == "rate" { rate_steps() } else { STEPS.iter().map(Step::copy).collect() };
    let mut pad = match Apex6::open() {
        Ok(pad) => pad,
        Err(e) => {
            eprintln!("컨트롤러를 열 수 없습니다: {e:?}");
            std::process::exit(1);
        }
    };
    let info = pad.info.clone().unwrap();
    let firmware: Vec<String> = info.firmware().iter().map(|(name, v)| format!("{name} {v}")).collect();
    println!("펌웨어: {}", firmware.join(", "));
    println!("연결: {}", if info.connection_mode == 0 { "유선" } else { "무선" });
    let mut lines = vec![
        format!(
            "트리거 내장 효과(0x53 mode 4) {} {}, ATS 햅틱 {}",
            match mode.as_str() {
                "ram" => "RAM 점검",
                "rate" => "빠르기 값 점검 (안 가림)",
                _ => "느낌 점검 (안 가림)",
            },
            ats_haptics::clock::now().stamp(),
            env!("CARGO_PKG_VERSION")
        ),
        format!("구성 요소 펌웨어: {}", firmware.join(", ")),
        format!("기기: {}, connection_mode={}, features=0x{:02X}", info.name(), info.connection_mode, info.features),
        String::new(),
    ];
    let result = if ram_only {
        ram_check(&mut pad)
    } else if Input::state().is_none() {
        Err("XInput 패드가 안 보입니다. 패드를 Xbox 모드로 연결하세요.".to_string())
    } else {
        feel_check(&mut pad, &mut Input::default(), &steps)
    };
    clear(&mut pad);
    drop(pad);
    match result {
        Ok(found) => lines.extend(found),
        Err(e) => {
            eprintln!("중단: {e}");
            lines.push(format!("중단: {e}"));
        }
    }
    let report = lines.join("\n");
    let dir = ats_haptics::blocklog::log_dir();
    let path = dir.join(format!("trigger-scene-{}.txt", ats_haptics::clock::now().stamp()));
    match std::fs::create_dir_all(&dir).and_then(|_| std::fs::write(&path, report + "\n")) {
        Ok(()) => println!("\n저장: {}", path.display()),
        Err(e) => eprintln!("\n저장 실패: {e}"),
    }
}
