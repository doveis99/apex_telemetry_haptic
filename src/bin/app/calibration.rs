//! The motor calibration dialog (gui.py's CalibrationDialog): matches each frequency's
//! strength by feel (calibrate::Session) on a worker thread. The answers come from the
//! buttons, the keyboard or the pad's own buttons; a 50 ms poll shows where the worker stands.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use ats_haptics::apex6::{Apex6, PadError};
use ats_haptics::calibrate::{self, Answer, PadButtons, Session, CHANNELS};
use ats_haptics::errorlog::panic_message;
use ats_haptics::platform;
use ats_haptics::settings::Settings;
use ats_haptics::t;

use crate::CalibrationDialog;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Start,
    Playing,
    Waiting,
    Done,
    Error,
}

/// What the worker last reported.
#[derive(Clone)]
struct Progress {
    phase: Phase,
    /// (channel, frequency, neighbour), its place among the channel's and how many
    current: Option<(String, f64, f64)>,
    index: usize,
    count: usize,
    trial: usize,
    finished: Vec<String>,
    curves: BTreeMap<String, BTreeMap<String, f64>>,
    error: String,
    /// why a finished motor's curve could not be written to config.json
    save_error: Option<String>,
}

impl Default for Progress {
    fn default() -> Progress {
        Progress {
            phase: Phase::Start,
            current: None,
            index: 0,
            count: 0,
            trial: 0,
            finished: Vec::new(),
            curves: BTreeMap::new(),
            error: String::new(),
            save_error: None,
        }
    }
}

fn report(session: &Session, phase: Phase, progress: &Mutex<Progress>) {
    let mut p = progress.lock().unwrap();
    p.phase = phase;
    p.current = session.current().cloned();
    if let Some((channel, _, _)) = session.current() {
        let first = session.steps.iter().position(|s| s.0 == *channel).unwrap_or(0);
        p.index = session.index - first + 1;
        p.count = calibrate::freqs(channel).len();
    }
    p.trial = session.staircase.trials + 1;
    p.finished = session.finished.clone();
    p.curves = session.curves.clone();
}

fn name(channel: &str) -> String {
    if channel == "grip" {
        t!("그립")
    } else {
        t!("트리거")
    }
}

struct Inner {
    dialog: CalibrationDialog,
    channels: [bool; 2],
    answers: Option<mpsc::Sender<Answer>>,
    cancelled: Arc<AtomicBool>,
    progress: Arc<Mutex<Progress>>,
    thread: Option<JoinHandle<()>>,
    shown: String,
    open: bool,
    closing: bool,
    timer: Option<slint::Timer>,
}

#[derive(Clone)]
pub struct Calibration(Rc<RefCell<Inner>>);

impl Calibration {
    pub fn new() -> Calibration {
        let dialog = CalibrationDialog::new().expect("the calibration dialog");
        let inner = Rc::new(RefCell::new(Inner {
            dialog,
            channels: [true, true],
            answers: None,
            cancelled: Arc::new(AtomicBool::new(false)),
            progress: Arc::new(Mutex::new(Progress::default())),
            thread: None,
            shown: String::new(),
            open: true,
            closing: false,
            timer: None,
        }));
        let me = Calibration(inner);
        me.wire();
        {
            let inner = me.0.borrow();
            inner.dialog.set_ui_font(ats_haptics::i18n::ui_font().into());
            let _ = inner.dialog.show();
            crate::title_bar_later(&inner.dialog);
        }
        me.relabel();
        let poll = Rc::downgrade(&me.0);
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::Repeated, Duration::from_millis(50), move || {
            if let Some(inner) = poll.upgrade() {
                Calibration(inner).poll();
            }
        });
        me.0.borrow_mut().timer = Some(timer);
        me
    }

    fn wire(&self) {
        let dialog = self.0.borrow().dialog.clone_strong();
        let weak = Rc::downgrade(&self.0);
        let me = move || weak.upgrade().map(Calibration);
        let m = me.clone();
        dialog.on_channel_toggled(move |channel, on| {
            if let Some(c) = m() {
                let mut inner = c.0.borrow_mut();
                let i = CHANNELS.iter().position(|x| *x == channel.as_str()).unwrap_or(0);
                inner.channels[i] = on;
                inner.dialog.set_grip_on(inner.channels[0]);
                inner.dialog.set_trigger_on(inner.channels[1]);
            }
        });
        let m = me.clone();
        dialog.on_begin(move || {
            if let Some(c) = m() {
                c.begin();
            }
        });
        let m = me.clone();
        dialog.on_reset(move || {
            if let Some(c) = m() {
                c.reset();
            }
        });
        let m = me.clone();
        dialog.on_close_clicked(move || {
            if let Some(c) = m() {
                c.close();
            }
        });
        let m = me.clone();
        dialog.on_answer(move |answer| {
            if let Some(c) = m() {
                let answer = match answer.as_str() {
                    "first" => Answer::First,
                    "second" => Answer::Second,
                    "replay" => Answer::Replay,
                    _ => Answer::Cancel,
                };
                c.answer(answer);
            }
        });
        let m = me.clone();
        dialog.on_restart(move || {
            if let Some(c) = m() {
                c.0.borrow().progress.lock().unwrap().phase = Phase::Start;
            }
        });
        dialog.window().on_close_requested(move || {
            if let Some(c) = me() {
                c.close();
            }
            slint::CloseRequestResponse::KeepWindowShown
        });
    }

    pub fn busy(&self) -> bool {
        self.0.borrow().thread.as_ref().is_some_and(|t| !t.is_finished())
    }

    pub fn open(&self) -> bool {
        self.0.borrow().open
    }

    pub fn component(&self) -> CalibrationDialog {
        self.0.borrow().dialog.clone_strong()
    }

    /// A session or result screen with made-up values, for the offscreen snapshot.
    pub fn show_sample(&self, screen: &str) {
        let mut inner = self.0.borrow_mut();
        inner.timer = None; // the poll would put the start screen back
        let d = &inner.dialog;
        match screen {
            "session" => {
                d.set_screen("session".into());
                d.set_progress(t!("{name} {index}/{count} · {freq} Hz ↔ {neighbour} Hz", name = name("grip"), index = 2,
                                  count = 9, freq = "100", neighbour = "80").into());
                d.set_trial(t!("비교 {trial}번째", trial = 3).into());
                d.set_state(t!("어느 쪽이 더 셌나요?").into());
            }
            "result" => {
                d.set_screen("end".into());
                let progress = Progress {
                    phase: Phase::Done,
                    finished: vec!["grip".into()],
                    curves: BTreeMap::from([(
                        "grip".to_string(),
                        BTreeMap::from([("30".into(), 3.89), ("50".into(), 2.94), ("65".into(), 1.0), ("100".into(), 2.98)]),
                    )]),
                    ..Progress::default()
                };
                Calibration::render_end(&inner, &progress);
            }
            _ => Calibration::render_start(&inner),
        }
    }

    pub fn raise(&self) {
        crate::raise(self.0.borrow().dialog.window());
    }

    pub fn hide(&self) {
        let _ = self.0.borrow().dialog.hide();
    }

    /// The texts that come from Rust, in the current language.
    pub fn relabel(&self) {
        let mut inner = self.0.borrow_mut();
        inner.dialog.set_ui_font(ats_haptics::i18n::ui_font().into());
        let pads: Vec<SharedString> = ["← / LB", "→ / RB", "X", "B"].iter().map(|b| t!("패드 {buttons}", buttons = b).into()).collect();
        let keys: Vec<SharedString> = ["← / 1", "→ / 2", "R", "Esc"].iter().map(|k| t!("키보드 {keys}", keys = k).into()).collect();
        inner.dialog.set_pads(ModelRc::from(Rc::new(VecModel::from(pads))));
        inner.dialog.set_keyboard(ModelRc::from(Rc::new(VecModel::from(keys))));
        inner.shown.clear(); // redraw the screen's own texts
    }

    fn render_start(inner: &Inner) {
        let own = Settings::default().load_eq();
        let sources: Vec<String> = CHANNELS
            .iter()
            .map(|c| {
                if own.contains_key(*c) {
                    t!("{name} (직접 측정)", name = name(c))
                } else {
                    t!("{name} (기본값)", name = name(c))
                }
            })
            .collect();
        let d = &inner.dialog;
        d.set_sources(t!("지금 보정: {channels}", channels = sources.join(", ")).into());
        d.set_grip_label(t!("{name} ({count}개 주파수)", name = name("grip"), count = calibrate::freqs("grip").len()).into());
        d.set_trigger_label(
            t!("{name} ({count}개 주파수)", name = name("trigger"), count = calibrate::freqs("trigger").len()).into(),
        );
        d.set_grip_on(inner.channels[0]);
        d.set_trigger_on(inner.channels[1]);
    }

    fn render_end(inner: &Inner, progress: &Progress) {
        let text = if progress.phase == Phase::Error {
            t!("보정하지 못했습니다: {error}", error = progress.error)
        } else if let Some(error) = &progress.save_error {
            // measured, but not kept: the curves below are all there is of it
            t!("저장하지 못했습니다: {error}", error = error)
        } else if !progress.finished.is_empty() {
            let names: Vec<String> = progress.finished.iter().map(|c| name(c)).collect();
            t!("보정을 저장했습니다: {channels}. 다음 시작부터 적용됩니다.", channels = names.join(", "))
        } else {
            t!("저장한 보정이 없습니다. 모터 하나를 끝까지 맞추면 저장됩니다.")
        };
        inner.dialog.set_result(text.into());
        let lines: Vec<SharedString> = progress
            .finished
            .iter()
            .map(|channel| {
                let mut curve: Vec<(&String, &f64)> = progress.curves[channel].iter().collect();
                curve.sort_by(|a, b| a.0.parse::<f64>().unwrap_or(0.0).total_cmp(&b.0.parse::<f64>().unwrap_or(0.0)));
                // no-break spaces keep "50 Hz ×1.19" whole
                let parts: Vec<String> = curve.iter().map(|(f, g)| format!("{f}\u{a0}Hz\u{a0}×{g:.2}")).collect();
                format!("{}: {}", name(channel), parts.join("  ")).into()
            })
            .collect();
        inner.dialog.set_curves(ModelRc::from(Rc::new(VecModel::from(lines))));
    }

    fn poll(&self) {
        let Ok(mut inner) = self.0.try_borrow_mut() else { return };
        if !inner.open {
            return;
        }
        if inner.closing && !inner.thread.as_ref().is_some_and(|t| !t.is_finished()) {
            // once the current tone has stopped
            inner.open = false;
            let _ = inner.dialog.hide();
            return;
        }
        let progress = inner.progress.lock().unwrap().clone();
        let screen = match progress.phase {
            Phase::Start => "start",
            Phase::Playing | Phase::Waiting => "session",
            Phase::Done | Phase::Error => "end",
        };
        if screen != inner.shown {
            inner.shown = screen.to_string();
            inner.dialog.set_screen(screen.into());
            match screen {
                "start" => Calibration::render_start(&inner),
                "end" => Calibration::render_end(&inner, &progress),
                _ => {}
            }
        }
        if screen == "session" {
            if let Some((channel, freq, neighbour)) = &progress.current {
                inner.dialog.set_progress(
                    t!("{name} {index}/{count} · {freq} Hz ↔ {neighbour} Hz", name = name(channel), index = progress.index,
                       count = progress.count, freq = ats_haptics::telemetry::format_g(*freq),
                       neighbour = ats_haptics::telemetry::format_g(*neighbour))
                    .into(),
                );
                inner.dialog.set_trial(t!("비교 {trial}번째", trial = progress.trial).into());
                let state = if inner.cancelled.load(Ordering::Relaxed) {
                    t!("그만두는 중…")
                } else if progress.phase == Phase::Playing {
                    t!("첫 번째 → 두 번째 재생 중…")
                } else {
                    t!("어느 쪽이 더 셌나요?")
                };
                inner.dialog.set_state(state.into());
            }
        }
    }

    fn begin(&self) {
        let channels: Vec<&str> = {
            let inner = self.0.borrow();
            CHANNELS.iter().zip(inner.channels).filter(|(_, on)| *on).map(|(c, _)| *c).collect()
        };
        if channels.is_empty() || self.busy() {
            return;
        }
        let session = Session::new(&channels);
        let (send, receive) = mpsc::channel();
        let mut inner = self.0.borrow_mut();
        inner.cancelled.store(false, Ordering::Relaxed);
        inner.answers = Some(send);
        *inner.progress.lock().unwrap() = Progress::default();
        report(&session, Phase::Playing, &inner.progress);
        let progress = Arc::clone(&inner.progress);
        let cancelled = Arc::clone(&inner.cancelled);
        inner.thread = Some(std::thread::spawn(move || work(session, receive, progress, cancelled)));
    }

    fn answer(&self, answer: Answer) {
        if self.busy() {
            let inner = self.0.borrow();
            if answer == Answer::Cancel {
                inner.cancelled.store(true, Ordering::Relaxed);
            }
            if let Some(send) = &inner.answers {
                let _ = send.send(answer);
            }
        } else if answer == Answer::Cancel {
            self.close();
        }
    }

    fn reset(&self) {
        let hwnd = crate::hwnd(self.0.borrow().dialog.window());
        if platform::ask_yes_no(hwnd, &t!("모터 보정"), &t!("저장된 보정을 지우고 기본값으로 되돌릴까요?")) {
            if let Err(error) = Settings::default().save_eq(&serde_json::Map::new()) {
                platform::show(hwnd, &t!("모터 보정"), &t!("저장하지 못했습니다: {error}", error = error), platform::Icon::Error);
            }
            self.0.borrow_mut().shown.clear(); // redraw "지금 보정"
        }
    }

    /// Stop a running calibration and wait for the controller to be let go.
    pub fn abort(&mut self) {
        if self.busy() {
            self.answer(Answer::Cancel);
            let deadline = std::time::Instant::now() + Duration::from_secs(4);
            while self.busy() && std::time::Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }

    fn close(&self) {
        if self.busy() {
            self.answer(Answer::Cancel);
            self.0.borrow_mut().closing = true; // the poll hides it once the tone has stopped
            return;
        }
        let mut inner = self.0.borrow_mut();
        inner.open = false;
        let _ = inner.dialog.hide();
    }
}

fn work(mut session: Session, answers: mpsc::Receiver<Answer>, progress: Arc<Mutex<Progress>>,
        cancelled: Arc<AtomicBool>) {
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<(), PadError> {
        let mut buttons = PadButtons::default();
        let mut listen = || answers.try_recv().ok().or_else(|| buttons.poll());
        let mut pad = Apex6::open()?;
        let settings = Settings::default();
        while session.current().is_some() && !cancelled.load(Ordering::Relaxed) {
            report(&session, Phase::Playing, &progress);
            let mut answer = calibrate::play(&mut pad, &session, &mut listen)?;
            if answer.is_none() {
                report(&session, Phase::Waiting, &progress);
                answer = Some(calibrate::wait(&mut listen, &|| cancelled.load(Ordering::Relaxed)));
            }
            let answer = answer.unwrap();
            if answer == Answer::Cancel {
                break;
            }
            if session.answer(answer).is_some() {
                // each motor is kept as it is finished; the next one's save tries again
                let saved = calibrate::save(&session, &settings);
                progress.lock().unwrap().save_error = saved.err().map(|error| error.to_string());
            }
        }
        report(&session, Phase::Done, &progress);
        Ok(())
    }));
    let error = match result {
        Ok(Ok(())) => return,
        Ok(Err(error)) => error.to_string(),
        Err(payload) => panic_message(payload.as_ref()), // the hook has logged it
    };
    let mut p = progress.lock().unwrap();
    p.phase = Phase::Error;
    p.error = error;
}
