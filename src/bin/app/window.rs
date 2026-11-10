//! The live tuning window's logic (port of gui.py's App): sliders, switches and routes change
//! the running effects immediately; 저장 writes config.json; presets switch tunings with one
//! click; ▶ plays one effect alone; a poll every 100 ms shows the output's state.
//!
//! Every handler borrows the App with try_borrow_mut and does nothing when it is busy: a
//! message box runs its own message loop, during which the poll timer still fires.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::rc::{Rc, Weak};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

use ats_haptics::blocklog::log_dir;
use ats_haptics::effects::{Effect, Equalizer, Gains, AMT, GEARBOXES};
use ats_haptics::gamewatch::GameWatcher;
use ats_haptics::i18n::{self, LANGUAGES};
use ats_haptics::platform::{self, Icon, LaunchEvent};
use ats_haptics::preview::{Player, Preview, ROAD_SURFACES, SURFACE_STEP_S};
use ats_haptics::pymath::round_int;
use ats_haptics::runner::{clock_hms, Source};
use ats_haptics::scenarios;
use ats_haptics::settings::{damaged_copy, AppOptions, Settings};
use ats_haptics::t;
use ats_haptics::telemetry::{format_g, SIMULATED_FPS};
use ats_haptics::updater::{self, Release};

use crate::calibration::Calibration;
use crate::plugins::Plugins;
use crate::specs::{Spec, CARD_WIDE, CYLINDERS_RANGE, DRIVES, EFFECT_SLIDERS, EVENT_SLIDERS, OUTPUT_SLIDERS,
                   SLIDER_TIPS, SURFACE_NAMES, THRESHOLD_SLIDERS};
use crate::worker::Worker;
use crate::{hwnd, AppWindow, Choice, Preset, Row};

const POLL_MS: u64 = 100;
const METER_DECAY: f64 = 0.8;
const EVENTS_SHOWN: usize = 50;

pub fn rgb(hex: u32) -> slint::Color {
    slint::Color::from_rgb_u8((hex >> 16) as u8, (hex >> 8) as u8, hex as u8)
}

pub const ACCENT: u32 = 0x5aa2ff;
pub const GRIP: u32 = 0x5aa2ff;
pub const TRIGGER: u32 = 0xff9a4d;
pub const BOTH: u32 = 0xb58cff;
pub const FAINT: u32 = 0x555c6b;
pub const GO: u32 = 0x2ec27e;
pub const STOP: u32 = 0xf05252;
pub const WARN: u32 = 0xf5b942;

const TEST_TIP: &str = "이 효과만 몇 초 동안 울려 봅니다. 지금 세기, 진동 위치, 모터 보정 그대로이고, 꺼 둔 효과도 울립니다. \
                        요철, 차선 요철, 방향지시등은 왼쪽 다음 오른쪽 순서입니다. 출력 중에는 그동안 주행 진동 대신 울립니다.";

fn level_color(key: &str) -> u32 {
    match key {
        "grip_level" => GRIP,
        "trigger_level" | "throttle_pulse" => TRIGGER,
        _ => ACCENT,
    }
}

fn route_color(route: &str) -> u32 {
    match route {
        "trigger" => TRIGGER,
        "both" => BOTH,
        _ => GRIP,
    }
}

#[derive(Default)]
struct UpdateState {
    release: Option<Release>,
    progress: Option<f64>,
    ready: Option<PathBuf>,
    error: Option<String>,
}

enum Prompt {
    New,
    Rename(String),
}

pub struct App {
    pub ui: AppWindow,
    settings: Settings,
    gains: Gains,
    saved: Gains,
    options: AppOptions,
    dry_run: bool,
    exit_with_game: bool,
    launch_event: Option<LaunchEvent>,
    worker: Option<Worker>,
    had_worker: bool,
    connected: Option<String>,
    game_ended: bool,
    player: Player,
    calibration: Option<Calibration>,
    plugins: Option<Plugins>,
    error: Option<String>,
    /// shown as the status while nothing runs: config.json could not be read
    notice: Option<String>,
    tested: bool,
    meter_values: [f64; 3],
    watcher: GameWatcher,
    last_game: Option<&'static str>,
    source: String,
    /// what 테스트 주행 plays: "" the 60 s drive, a scenario's name or a debug log's path
    drive: String,
    update: Arc<Mutex<UpdateState>>,
    update_shown: bool,
    events: VecDeque<String>,
    prompt: Option<Prompt>,
    pub closed: bool,
    status: String,
    status_color: u32,
    rows: [Rc<VecModel<Row>>; 4], // output, effects, events, thresholds
    meters: Rc<VecModel<f32>>,
    events_model: Rc<VecModel<SharedString>>,
    timer: Option<slint::Timer>,
    me: Weak<RefCell<App>>,
}

fn specs(group: usize) -> &'static [Spec] {
    match group {
        0 => &OUTPUT_SLIDERS,
        1 => &EFFECT_SLIDERS,
        2 => &EVENT_SLIDERS,
        _ => &THRESHOLD_SLIDERS,
    }
}

fn find_spec(key: &str) -> Option<(usize, usize, &'static Spec)> {
    (0..4).find_map(|g| specs(g).iter().position(|s| s.key == key).map(|i| (g, i, &specs(g)[i])))
}

/// Run `f` on the App unless it is busy (a message box's loop re-entering) or gone.
pub fn with(app: &Weak<RefCell<App>>, f: impl FnOnce(&mut App)) {
    if let Some(app) = app.upgrade() {
        if let Ok(mut app) = app.try_borrow_mut() {
            f(&mut app);
        }
    }
}

impl App {
    pub fn new(dry_run: bool, exit_with_game: bool) -> Rc<RefCell<App>> {
        let settings = Settings::default();
        let gains = settings.load_gains();
        let options = settings.load_app_options();
        let ui = AppWindow::new().expect("the window");
        let app = Rc::new(RefCell::new(App {
            ui,
            saved: gains.clone(),
            gains,
            settings,
            options,
            dry_run,
            exit_with_game,
            launch_event: LaunchEvent::open(),
            worker: None,
            had_worker: false,
            connected: None,
            game_ended: false,
            player: Player::default(),
            calibration: None,
            plugins: None,
            error: None,
            notice: None,
            tested: false,
            meter_values: [0.0; 3],
            watcher: GameWatcher::new(),
            last_game: None,
            source: "game".into(),
            drive: String::new(),
            update: Arc::new(Mutex::new(UpdateState::default())),
            update_shown: false,
            events: VecDeque::new(),
            prompt: None,
            closed: false,
            status: t!("정지됨"),
            status_color: FAINT,
            rows: Default::default(),
            meters: Rc::new(VecModel::from(vec![0.0f32; 3])),
            events_model: Rc::new(VecModel::default()),
            timer: None,
            me: Weak::new(),
        }));
        let me = Rc::downgrade(&app);
        {
            let mut a = app.borrow_mut();
            a.me = me.clone();
            a.wire();
            a.build();
            a.warn_damaged();
        }
        app
    }

    /// Say so when config.json could not be read: the window opened on defaults, which is
    /// otherwise the only sign that the presets and the calibration in it were not taken.
    fn warn_damaged(&mut self) {
        if !self.settings.damaged() {
            return;
        }
        let copy = damaged_copy(&self.settings.path);
        let name = copy.file_name().unwrap_or_default().to_string_lossy().into_owned();
        let line =
            t!("설정 파일을 읽을 수 없어 기본값으로 시작했습니다. 저장하면 원래 파일은 {name}(으)로 남습니다.", name = name);
        self.add_event(&line);
        self.status = line.clone();
        self.status_color = WARN;
        self.notice = Some(line);
        self.show_status();
    }

    /// A line of the window's own in the event list, stamped as the drive's are.
    fn add_event(&mut self, line: &str) {
        self.events.push_front(format!("{} {line}", clock_hms()));
        self.show_events();
    }

    fn show_events(&mut self) {
        self.events.truncate(EVENTS_SHOWN);
        self.events_model.set_vec(self.events.iter().map(|e| SharedString::from(e.as_str())).collect::<Vec<_>>());
    }

    /// The 2 kHz output went back to 1 kHz on its own (rate2k::Pace): the switch follows, and
    /// stays off for the next start, as if it had been switched off by hand.
    fn output_2k_gave_way(&mut self) {
        self.options.output_2k = false;
        self.ui.set_output_2k(false);
        self.player.set_output_2k(false);
        self.stored(self.settings.save_app_options(&self.options));
    }

    /// Start polling, and look for a newer release in the background.
    pub fn start_polling(&mut self, check_updates: bool) {
        let me = self.me.clone();
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::Repeated, Duration::from_millis(POLL_MS), move || with(&me, |app| app.poll()));
        self.timer = Some(timer);
        if check_updates {
            let update = Arc::clone(&self.update);
            std::thread::spawn(move || {
                // offline or rate limited: try again next start
                if let Ok(Some(release)) = updater::newer_release() {
                    update.lock().unwrap().release = Some(release);
                }
                // well after the start: the script that installed this version has ended by now
                std::thread::sleep(Duration::from_secs(20));
                updater::clean_up_downloads();
            });
        }
    }

    // -- layout -------------------------------------------------------------------

    /// Fill everything the window shows in the current language.
    fn build(&mut self) {
        let ui = &self.ui;
        let version = env!("CARGO_PKG_VERSION");
        let mut title = format!("{} {version} - APEX 6 Pro", t!("ATS 햅틱"));
        if self.dry_run {
            title.push_str(" (dry-run)");
        }
        if self.exit_with_game {
            title.push_str(&format!(" · {}", t!("게임 종료 시 닫힘")));
        }
        ui.set_window_title(title.into());
        ui.set_subtitle(format!("APEX 6 Pro · v{version}{}", if self.dry_run { " · dry-run" } else { "" }).into());
        ui.set_github_tip(format!("{}\n{}", t!("GitHub 저장소 열기: 소스 코드, 릴리스, 문의(Issues)"), updater::PAGE_URL).into());
        ui.set_ui_font(i18n::ui_font().into());
        let language = i18n::language();
        let name = LANGUAGES.iter().find(|(c, _)| *c == language).map_or(language, |(_, n)| n);
        ui.set_language_label(format!("{name}  ▼").into());
        ui.set_language(language.into());
        ui.set_languages(choices(LANGUAGES.iter().map(|(c, n)| (c.to_string(), n.to_string()))));
        ui.set_drives(choices(DRIVES.iter().map(|(v, l)| (v.to_string(), t!(l)))));
        ui.set_surfaces(choices(SURFACE_NAMES.iter().map(|(v, l)| (v.to_string(), t!(l)))));
        ui.set_all_surfaces_label(t!("전부 차례로 (얼음 → 자갈, 각 {seconds}초)", seconds = format_g(SURFACE_STEP_S)).into());
        ui.set_log_dir_tip(format!("{}\n{}", t!("디버그 기록이 쌓이는 폴더를 엽니다."), log_dir().display()).into());
        ui.set_auto_start(self.options.auto_start_on_game);
        ui.set_debug_log(self.options.debug_log);
        ui.set_output_2k(self.options.output_2k);
        // the loaded tuning takes the sliders' steps, as setting each slider does in gui.py
        for group in 0..4 {
            for spec in specs(group) {
                let value = self.gains.float(spec.key).unwrap() * spec.scale;
                *self.gains.float_mut(spec.key).unwrap() = round_int(value) as f64 / spec.scale;
            }
        }
        for (group, model) in self.rows.clone().iter().enumerate() {
            let rows: Vec<Row> = specs(group).iter().map(|spec| self.row(spec)).collect();
            model.set_vec(rows);
        }
        ui.set_output_rows(ModelRc::from(Rc::clone(&self.rows[0])));
        ui.set_effect_rows(ModelRc::from(Rc::clone(&self.rows[1])));
        ui.set_event_rows(ModelRc::from(Rc::clone(&self.rows[2])));
        ui.set_threshold_rows(ModelRc::from(Rc::clone(&self.rows[3])));
        ui.set_meters(ModelRc::from(Rc::clone(&self.meters)));
        ui.set_events(ModelRc::from(Rc::clone(&self.events_model)));
        ui.set_cylinders(self.gains.cylinders as i32);
        ui.set_gearbox(if GEARBOXES.contains(&self.gains.gearbox.as_str()) { self.gains.gearbox.as_str() } else { AMT }.into());
        self.build_presets();
        self.changed();
        self.show_run_state();
        self.show_status();
    }

    fn row(&self, spec: &Spec) -> Row {
        let value = round_int(self.gains.float(spec.key).unwrap() * spec.scale);
        let effect = Effect::from_name(spec.key);
        let off = self.gains.disabled.iter().any(|d| d == spec.key);
        let route = effect.map(|e| self.gains.route_of(e).name()).unwrap_or("grip");
        let color = match effect {
            Some(_) if off => FAINT,
            Some(_) => route_color(route),
            None => level_color(spec.key),
        };
        let tip = SLIDER_TIPS.iter().find(|(k, _)| *k == spec.key).map(|(_, tip)| t!(tip)).unwrap_or_default();
        let mut test_tip = t!(TEST_TIP);
        if spec.key == "road" {
            test_tip.push(' ');
            test_tip.push_str(&t!("노면은 재질을 골라 80 km/h로 그 위만 달려 보거나, 모든 재질을 차례로 달려 볼 수 있습니다."));
        }
        Row {
            key: spec.key.into(),
            label: t!(spec.label).into(),
            tip: tip.into(),
            value: value as f32,
            minimum: spec.low as f32,
            maximum: spec.high as f32,
            shown: if spec.unit == "%" { format!("{value}%") } else { format!("{value} {}", spec.unit) }.into(),
            color: rgb(color),
            toggle: effect.is_some(),
            enabled: !off,
            route: route.into(),
            test_tip: test_tip.into(),
            rule_before: CARD_WIDE.contains(&spec.key),
        }
    }

    fn refresh_row(&self, key: &str) {
        if let Some((group, index, spec)) = find_spec(key) {
            self.rows[group].set_row_data(index, self.row(spec));
        }
    }

    // -- wiring ---------------------------------------------------------------------

    fn wire(&mut self) {
        let ui = &self.ui;
        macro_rules! on {
            ($setter:ident, |$app:ident $(, $arg:ident : $ty:ty)*| $body:expr) => {{
                let me = self.me.clone();
                ui.$setter(move |$($arg: $ty),*| with(&me, |$app| { $body; }));
            }};
        }
        on!(on_language_picked, |app, code: SharedString| app.set_language(&code));
        on!(on_github, |_app| platform::open(updater::PAGE_URL));
        on!(on_plugins, |app| app.open_plugins());
        on!(on_update, |app| app.start_update());
        on!(on_source_picked, |app, source: SharedString| {
            app.source = source.to_string();
            app.show_run_state();
        });
        on!(on_drive_picked, |app, drive: SharedString| app.set_drive(drive.to_string()));
        on!(on_pick_log, |app| app.pick_log());
        on!(on_run, |app| app.toggle_run());
        on!(on_auto_start_toggled, |app, on: bool| app.set_auto_start(on));
        on!(on_preset_clicked, |app, name: SharedString| app.switch_preset(&name));
        on!(on_preset_action, |app, name: SharedString, action: SharedString| {
            if action == "rename" {
                app.ask_preset_name(Prompt::Rename(name.to_string()));
            } else {
                app.delete_preset(&name);
            }
        });
        on!(on_new_preset, |app| app.ask_preset_name(Prompt::New));
        on!(on_slider_moved, |app, key: SharedString, value: f32| app.slider_moved(&key, value as f64));
        on!(on_effect_toggled, |app, key: SharedString, on: bool| app.toggle(&key, on));
        on!(on_route_picked, |app, key: SharedString, route: SharedString| app.route(&key, &route));
        on!(on_test, |app, key: SharedString, surfaces: SharedString| app.test_effect(&key, &surfaces));
        on!(on_calibration, |app| app.open_calibration());
        on!(on_cylinders_step, |app, step: i32| app.step_cylinders(step as i64));
        on!(on_gearbox_picked, |app, gearbox: SharedString| app.set_gearbox(&gearbox));
        on!(on_save, |app| app.save());
        on!(on_reset, |app| app.apply(&Gains::default()));
        on!(on_debug_log_toggled, |app, on: bool| app.set_debug_log(on));
        on!(on_output_2k_toggled, |app, on: bool| app.set_output_2k(on));
        on!(on_open_log_dir, |_app| platform::open_folder(&log_dir()));
        on!(on_prompt_done, |app, ok: bool| app.prompt_done(ok));
        let me = self.me.clone();
        ui.window().on_close_requested(move || {
            with(&me, |app| app.on_close());
            slint::CloseRequestResponse::KeepWindowShown
        });
    }

    fn hwnd(&self) -> windows_sys::Win32::Foundation::HWND {
        hwnd(self.ui.window())
    }

    // -- language -----------------------------------------------------------------

    fn set_language(&mut self, code: &str) {
        if code == i18n::language() {
            return;
        }
        self.options.language = code.to_string();
        let stored = self.settings.save_app_options(&self.options);
        crate::select_language(code);
        self.build();
        if let Some(calibration) = &mut self.calibration {
            calibration.relabel();
        }
        if let Some(plugins) = &mut self.plugins {
            plugins.render();
        }
        self.stored(stored);
    }

    // -- controls -------------------------------------------------------------------

    /// After any change of the tuning: the output takes it at its next block; 저장 lights up.
    fn changed(&mut self) {
        if let Some(worker) = &self.worker {
            worker.shared.lock().unwrap().gains = Some(self.gains.clone());
        }
        let dirty = self.gains != self.saved;
        self.ui.set_dirty(dirty);
        self.ui.set_save_state(if dirty { t!("변경됨 · 저장 안 됨") } else { String::new() }.into());
        self.ui.set_save_state_color(rgb(WARN));
    }

    /// True when a write to config.json went through; a failed one (a full disk, a folder that
    /// may not be written) is said beside 저장 instead of passing for saved.
    fn stored(&self, result: std::io::Result<()>) -> bool {
        if let Err(error) = &result {
            self.ui.set_save_state(t!("저장하지 못했습니다: {error}", error = error).into());
            self.ui.set_save_state_color(rgb(STOP));
        }
        result.is_ok()
    }

    fn slider_moved(&mut self, key: &str, value: f64) {
        let Some((_, _, spec)) = find_spec(key) else { return };
        let value = round_int(value.clamp(spec.low, spec.high));
        let current = round_int(self.gains.float(key).unwrap() * spec.scale);
        if value == current {
            return;
        }
        *self.gains.float_mut(key).unwrap() = value as f64 / spec.scale;
        self.refresh_row(key);
        self.changed();
    }

    fn toggle(&mut self, key: &str, on: bool) {
        let mut disabled: Vec<String> = self.gains.disabled.iter().filter(|d| *d != key).cloned().collect();
        if !on {
            disabled.push(key.to_string());
        }
        disabled.sort();
        self.gains.disabled = disabled;
        self.refresh_row(key);
        self.changed();
    }

    /// Picking a route on an effect that is off turns it back on there.
    fn route(&mut self, key: &str, route: &str) {
        self.gains.routes.insert(key.to_string(), route.to_string());
        self.toggle(key, true);
    }

    fn step_cylinders(&mut self, step: i64) {
        let value = self.gains.cylinders + step;
        if (CYLINDERS_RANGE.0..=CYLINDERS_RANGE.1).contains(&value) && value != self.gains.cylinders {
            self.gains.cylinders = value;
            self.ui.set_cylinders(value as i32);
            self.changed();
        }
    }

    fn set_gearbox(&mut self, gearbox: &str) {
        self.ui.set_gearbox(gearbox.into());
        if gearbox != self.gains.gearbox {
            self.gains.gearbox = gearbox.to_string();
            self.changed();
        }
    }

    fn set_auto_start(&mut self, on: bool) {
        // an app option, not a tuning value: saved right away
        self.options.auto_start_on_game = on;
        self.stored(self.settings.save_app_options(&self.options));
        self.ui.set_auto_start(on);
        self.last_game = None; // a game that is already running counts as newly detected
    }

    fn set_debug_log(&mut self, on: bool) {
        // saved right away, and a running drive starts or stops its log on the next block
        self.options.debug_log = on;
        self.stored(self.settings.save_app_options(&self.options));
        self.ui.set_debug_log(on);
        if let Some(worker) = &self.worker {
            worker.log_blocks.store(on, std::sync::atomic::Ordering::Relaxed);
        }
    }

    fn set_output_2k(&mut self, on: bool) {
        // saved right away; a running drive switches at its next 8 ms block, a test at its next
        self.options.output_2k = on;
        self.stored(self.settings.save_app_options(&self.options));
        self.ui.set_output_2k(on);
        if let Some(worker) = &self.worker {
            worker.output_2k.store(on, std::sync::atomic::Ordering::Relaxed);
        }
        self.player.set_output_2k(on);
    }

    /// Write the tuning to config.json, and into the preset in use. False when it could not be
    /// written: the changes stay unsaved and the reason shows beside 저장.
    fn save(&mut self) -> bool {
        let preset = self.active_preset();
        let mut result = self.settings.save_gains(&self.gains);
        if result.is_ok() && !preset.is_empty() {
            result = self.settings.save_preset(&preset, &self.gains);
        }
        if !self.stored(result) {
            return false;
        }
        self.saved = self.gains.clone();
        self.changed();
        self.ui.set_save_state(
            if preset.is_empty() { t!("✓ 저장됨") } else { t!("✓ '{name}'에 저장됨", name = preset) }.into(),
        );
        self.ui.set_save_state_color(rgb(GO));
        true
    }

    /// Set every slider, switch, route, cylinder count and gearbox to these gains; saving stays separate.
    fn apply(&mut self, gains: &Gains) {
        for group in 0..4 {
            for spec in specs(group) {
                let value = round_int(gains.float(spec.key).unwrap() * spec.scale);
                *self.gains.float_mut(spec.key).unwrap() = value as f64 / spec.scale;
            }
        }
        let mut disabled: Vec<String> =
            gains.disabled.iter().filter(|d| Effect::from_name(d).is_some()).cloned().collect();
        disabled.sort();
        self.gains.disabled = disabled;
        let mut routes = ats_haptics::effects::default_routes();
        for (key, route) in &gains.routes {
            routes.insert(key.clone(), route.clone());
        }
        self.gains.routes = routes;
        if (CYLINDERS_RANGE.0..=CYLINDERS_RANGE.1).contains(&gains.cylinders) {
            self.gains.cylinders = gains.cylinders;
        }
        self.ui.set_cylinders(self.gains.cylinders as i32);
        let gearbox = if GEARBOXES.contains(&gains.gearbox.as_str()) { gains.gearbox.as_str() } else { AMT };
        self.gains.gearbox = gearbox.to_string();
        self.ui.set_gearbox(gearbox.into());
        for (group, model) in self.rows.iter().enumerate() {
            for (index, spec) in specs(group).iter().enumerate() {
                model.set_row_data(index, self.row(spec));
            }
        }
        self.changed();
    }

    // -- presets --------------------------------------------------------------------

    /// The preset in use, or "" when none is or it was removed from config.json by hand.
    fn active_preset(&self) -> String {
        let name = &self.options.preset;
        if !name.is_empty() && self.settings.load_presets().iter().any(|(n, _)| n == name) {
            name.clone()
        } else {
            String::new()
        }
    }

    fn build_presets(&self) {
        let active = self.active_preset();
        let mut names: Vec<String> = self.settings.load_presets().into_iter().map(|(n, _)| n).collect();
        names.sort_by_key(|n| n.to_lowercase());
        let presets: Vec<Preset> =
            names.into_iter().map(|name| Preset { active: name == active, name: name.into() }).collect();
        self.ui.set_presets(ModelRc::from(Rc::new(VecModel::from(presets))));
    }

    /// Before leaving a tuning with unsaved changes: save them, drop them, or stay (false).
    fn keep_changes(&mut self) -> bool {
        if self.gains == self.saved {
            return true;
        }
        let answer =
            platform::ask_yes_no_cancel(self.hwnd(), &t!("프리셋 전환"), &t!("저장하지 않은 변경이 있습니다. 바꾸기 전에 저장할까요?"));
        if answer == Some(true) && !self.save() {
            return false; // not written: stay on this tuning rather than lose the changes
        }
        answer.is_some()
    }

    fn switch_preset(&mut self, name: &str) {
        if name == self.active_preset() && self.gains == self.saved {
            return;
        }
        if !self.settings.load_presets().iter().any(|(n, _)| n == name) || !self.keep_changes() {
            self.build_presets();
            return;
        }
        // read after keep_changes: saving there writes into the preset in use, which may be this one
        if let Some((_, gains)) = self.settings.load_presets().into_iter().find(|(n, _)| n == name) {
            self.use_preset(name, &gains);
        }
    }

    /// Make this preset the tuning in use, and the one the next start opens with.
    fn use_preset(&mut self, name: &str, gains: &Gains) {
        self.apply(gains);
        let stored = self.set_active_preset(name).and_then(|_| self.settings.save_gains(&self.gains));
        self.saved = self.gains.clone(); // what the preset holds
        self.changed();
        if self.stored(stored) {
            self.ui.set_save_state(t!("✓ '{name}'(으)로 바꿈", name = name).into());
            self.ui.set_save_state_color(rgb(GO));
        }
    }

    fn set_active_preset(&mut self, name: &str) -> std::io::Result<()> {
        self.options.preset = name.to_string();
        let stored = self.settings.save_app_options(&self.options);
        self.build_presets();
        stored
    }

    fn ask_preset_name(&mut self, prompt: Prompt) {
        let (title, initial) = match &prompt {
            Prompt::New => (t!("새 프리셋"), String::new()),
            Prompt::Rename(old) => (t!("이름 바꾸기"), old.clone()),
        };
        self.ui.set_prompt_title(title.into());
        self.ui.set_prompt_text(initial.into());
        self.ui.set_prompt_visible(true);
        self.prompt = Some(prompt);
    }

    fn prompt_done(&mut self, ok: bool) {
        self.ui.set_prompt_visible(false);
        let Some(prompt) = self.prompt.take() else { return };
        let (title, initial) = match &prompt {
            Prompt::New => (t!("새 프리셋"), String::new()),
            Prompt::Rename(old) => (t!("이름 바꾸기"), old.clone()),
        };
        let name = self.ui.get_prompt_text().trim().to_string();
        if !ok || name.is_empty() || name == initial {
            return;
        }
        let presets = self.settings.load_presets();
        if presets.iter().any(|(n, _)| *n == name)
            && !platform::ask_yes_no(self.hwnd(), &title, &t!("'{name}' 프리셋이 이미 있습니다. 덮어쓸까요?", name = name))
        {
            return;
        }
        match prompt {
            Prompt::New => {
                let stored = self.settings.save_preset(&name, &self.gains).and_then(|_| self.set_active_preset(&name));
                if self.stored(stored) {
                    self.save(); // the startup tuning follows it
                }
            }
            Prompt::Rename(old) => {
                let Some((_, gains)) = presets.into_iter().find(|(n, _)| *n == old) else { return };
                // the old name goes only once the new one is written
                let stored = self.settings.save_preset(&name, &gains).and_then(|_| self.settings.delete_preset(&old));
                if !self.stored(stored) {
                    self.build_presets();
                    return;
                }
                if self.options.preset == old {
                    let stored = self.set_active_preset(&name);
                    self.stored(stored);
                } else if self.options.preset == name {
                    // it replaced the preset in use: the window takes what that holds now
                    self.use_preset(&name, &gains);
                } else {
                    self.build_presets();
                }
            }
        }
    }

    fn delete_preset(&mut self, name: &str) {
        let question = t!("'{name}' 프리셋을 지울까요? 지금 설정은 그대로 남습니다.", name = name);
        if !platform::ask_yes_no(self.hwnd(), &t!("삭제"), &question) {
            return;
        }
        if !self.stored(self.settings.delete_preset(name)) {
            return;
        }
        if self.options.preset == name {
            let stored = self.set_active_preset("");
            self.stored(stored);
        } else {
            self.build_presets();
        }
    }

    // -- the drive --------------------------------------------------------------------

    fn drive_label(&self) -> String {
        if let Some((_, label)) = DRIVES.iter().find(|(v, _)| *v == self.drive) {
            return t!(label);
        }
        let stem = Path::new(&self.drive).file_stem().unwrap_or_default().to_string_lossy().into_owned();
        let parts: Vec<&str> = stem.split('-').collect(); // blocks-20261031-183522
        let date = match parts.as_slice() {
            [_, day, clock, ..] if day.len() == 8 && clock.len() >= 4 => {
                format!("{}/{} {}:{}", &day[4..6], &day[6..8], &clock[..2], &clock[2..4])
            }
            _ => stem.clone(),
        };
        t!("기록 {date}", date = date)
    }

    fn set_drive(&mut self, drive: String) {
        self.drive = drive;
        self.source = "sim".into();
        self.show_run_state();
    }

    fn pick_log(&mut self) {
        let dir = log_dir();
        let initial = dir.exists().then_some(dir.as_path());
        if let Some(path) =
            platform::pick_file(self.hwnd(), &t!("재생할 디버그 기록"), initial, &[("CSV", "blocks-*.csv"), ("CSV", "*.csv")])
        {
            self.set_drive(path.to_string_lossy().into_owned());
        }
    }

    fn running(&self) -> bool {
        self.worker.as_ref().is_some_and(Worker::running)
    }

    fn toggle_run(&mut self) {
        if self.running() {
            self.stop();
        } else {
            self.start();
        }
    }

    fn show_run_state(&self) {
        let ui = &self.ui;
        let running = self.running();
        let (text, kind, enabled) = if !running {
            (t!("▶  시작"), "go", true)
        } else if self.worker.as_ref().is_some_and(Worker::stopping) {
            (t!("정지 중…"), "ghost", false)
        } else {
            (t!("■  정지"), "danger", true)
        };
        ui.set_run_text(text.into());
        ui.set_run_kind(kind.into());
        ui.set_run_enabled(enabled);
        ui.set_source(self.source.as_str().into());
        ui.set_source_locked(running);
        ui.set_drive(self.drive.as_str().into());
        ui.set_drive_label(self.drive_label().into());
        ui.set_drive_enabled(!running);
    }

    pub fn start(&mut self) {
        if self.running() || self.calibration.as_ref().is_some_and(Calibration::busy) {
            return; // the calibration holds the controller; a game launch waits too
        }
        self.player.stop(); // a test takes no longer than letting the controller go
        self.error = None;
        self.connected = None;
        self.game_ended = false;
        self.events.clear();
        self.events_model.set_vec(Vec::<SharedString>::new());
        let source = if self.source == "sim" {
            if self.drive.is_empty() {
                Source::Simulated(Some(SIMULATED_FPS))
            } else if let Some(scenario) = scenarios::scenario(&self.drive) {
                Source::Scenario(scenario, Some(SIMULATED_FPS))
            } else {
                Source::Replay { log: PathBuf::from(&self.drive), from: None, to: None }
            }
        } else {
            Source::Game
        };
        // read at each start: a new calibration applies
        let eq = Equalizer::for_player(&self.settings.load_eq());
        self.worker = Some(Worker::start(&self.gains, eq, source, self.options.debug_log, self.options.output_2k,
                                         self.dry_run, self.exit_with_game));
        self.had_worker = true;
        self.show_run_state();
    }

    fn stop(&mut self) {
        if let Some(worker) = &self.worker {
            worker.request_stop();
        }
        self.show_run_state();
    }

    // -- effect tests, calibration, plugins ----------------------------------------------

    /// Play this effect alone for a few seconds, in place of the drive while it runs.
    fn test_effect(&mut self, key: &str, surfaces: &str) {
        let Some(effect) = Effect::from_name(key) else { return };
        let surfaces: Vec<&str> = match surfaces {
            "" => Vec::new(),
            "all" => ROAD_SURFACES.to_vec(),
            one => vec![one],
        };
        if self.running() {
            let worker = self.worker.as_ref().unwrap();
            if self.connected.is_some() || self.dry_run {
                let preview = Preview::new(effect, &self.gains, worker.eq.clone(), &surfaces);
                worker.shared.lock().unwrap().preview = Some(preview);
            } else {
                platform::show(self.hwnd(), &t!("효과 테스트"),
                               &t!("출력이 게임을 기다리는 중이라 지금은 컨트롤러를 쓰지 않습니다. 정지한 다음 테스트하세요."),
                               Icon::Info);
            }
            return;
        }
        if self.dry_run || self.calibration.as_ref().is_some_and(Calibration::busy) {
            return; // no controller, or it holds it
        }
        self.error = None;
        let preview = Preview::new(effect, &self.gains, Equalizer::for_player(&self.settings.load_eq()), &surfaces);
        self.player.play(preview, self.options.output_2k);
    }

    fn open_calibration(&mut self) {
        if let Some(calibration) = &self.calibration {
            if calibration.open() {
                calibration.raise();
                return;
            }
        }
        if self.running() {
            platform::show(self.hwnd(), &t!("모터 보정"), &t!("출력을 정지한 다음 보정하세요."), Icon::Info);
            return;
        }
        self.calibration = Some(Calibration::new());
    }

    pub fn open_plugins(&mut self) {
        if let Some(plugins) = &self.plugins {
            if plugins.open() {
                plugins.raise();
                return;
            }
        }
        self.plugins = Some(Plugins::new());
    }

    /// At startup: open the plugin dialog if no installed game can feed telemetry.
    pub fn check_plugins(&mut self) {
        let games = ats_haptics::plugin_install::find_games();
        if !games.is_empty() && !games.iter().any(|g| g.compatible()) {
            self.status = t!("호환되는 텔레메트리 플러그인이 없습니다 → '텔레메트리 플러그인'");
            self.show_status();
            self.open_plugins();
        }
    }

    // -- closing --------------------------------------------------------------------

    pub fn on_close(&mut self) {
        if self.gains != self.saved {
            match platform::ask_yes_no_cancel(self.hwnd(), &t!("저장"), &t!("바뀐 설정을 저장할까요?")) {
                None => return,
                Some(true) => {
                    if !self.save() {
                        // closing now would lose the changes without a word
                        platform::show(self.hwnd(), &t!("저장"), &self.ui.get_save_state(), Icon::Error);
                        return;
                    }
                }
                Some(false) => {}
            }
        }
        self.stop();
        self.player.stop();
        if let Some(calibration) = &mut self.calibration {
            calibration.abort();
        }
        if let Some(worker) = &mut self.worker {
            worker.join(2.0); // lets the controller release its motors
        }
        self.watcher.close();
        self.closed = true;
        self.timer = None;
        let _ = self.ui.hide();
        if let Some(calibration) = &self.calibration {
            calibration.hide();
        }
        if let Some(plugins) = &self.plugins {
            plugins.hide();
        }
        let _ = slint::quit_event_loop();
    }

    // -- updates ----------------------------------------------------------------------

    fn start_update(&mut self) {
        let Some(release) = self.update.lock().unwrap().release.clone() else { return };
        let notes: String = release.notes.chars().take(600).collect();
        let notes = if release.notes.chars().count() > 600 { notes + "…" } else { notes };
        let current = env!("CARGO_PKG_VERSION");
        let text = format!("{}\n\n{notes}", t!("새 버전 {new}이 있습니다 (현재 {current}).", new = release.version(), current = current))
            .trim_end()
            .to_string();
        if !updater::can_self_update(&release) {
            if platform::ask_yes_no(self.hwnd(), &t!("업데이트"), &format!("{text}\n\n{}", t!("릴리스 페이지를 열까요?"))) {
                platform::open(&release.page);
            }
            return;
        }
        if self.watcher.game().is_some() {
            platform::show(self.hwnd(), &t!("업데이트"), &t!("게임이 실행 중입니다. 게임을 끝낸 뒤 업데이트하세요."), Icon::Info);
            return;
        }
        if !platform::ask_yes_no(self.hwnd(), &t!("업데이트"),
                                 &format!("{text}\n\n{}", t!("내려받아 설치하고 프로그램을 다시 시작할까요?"))) {
            return;
        }
        self.ui.set_update_enabled(false);
        let update = Arc::clone(&self.update);
        update.lock().unwrap().progress = Some(0.0);
        std::thread::spawn(move || {
            let progress = Arc::clone(&update);
            let result = updater::download(&release, |done| progress.lock().unwrap().progress = Some(done));
            let mut state = update.lock().unwrap();
            match result {
                Ok(folder) => state.ready = Some(folder),
                Err(error) => state.error = Some(error),
            }
        });
    }

    fn poll_update(&mut self) {
        let (release, error, ready, progress) = {
            let mut state = self.update.lock().unwrap();
            (state.release.clone(), state.error.take(), state.ready.take(), state.progress)
        };
        let Some(release) = release else { return };
        let label = t!("업데이트 {version}", version = release.version());
        if !self.update_shown {
            self.update_shown = true;
            self.ui.set_update_text(label.as_str().into());
            self.ui.set_update_visible(true);
        }
        if let Some(error) = error {
            platform::show(self.hwnd(), &t!("업데이트"), &t!("업데이트하지 못했습니다: {error}", error = error), Icon::Error);
            self.update.lock().unwrap().progress = None;
            self.ui.set_update_enabled(true);
            self.ui.set_update_text(label.into());
        } else if let Some(source) = ready {
            self.ui.set_update_text(t!("설치 중…").into());
            self.on_close();
            if self.closed {
                // copies once this process has exited
                if let Err(error) = updater::apply_on_exit(&source) {
                    // the window is gone already: say why nothing will come back up
                    platform::show(std::ptr::null_mut(), &t!("업데이트"),
                                   &t!("업데이트하지 못했습니다: {error}", error = error), Icon::Error);
                }
            } else {
                // the save prompt was cancelled
                self.update.lock().unwrap().progress = None;
                self.ui.set_update_enabled(true);
                self.ui.set_update_text(label.into());
            }
        } else if let Some(done) = progress {
            self.ui.set_update_text(t!("내려받는 중 {percent}", percent = format!("{:.0}%", done * 100.0)).into());
            self.ui.set_update_enabled(false);
        }
    }

    // -- the periodic refresh -------------------------------------------------------------

    fn show_status(&self) {
        self.ui.set_status(self.status.as_str().into());
        self.ui.set_status_color(rgb(self.status_color));
    }

    fn poll(&mut self) {
        self.poll_update();
        if self.closed {
            return;
        }
        let mut running = self.running();
        // Auto start on the game appearing, not on every poll, so a manual 정지 holds until
        // the next launch. A Steam launch while the window was already open starts it too.
        let game = self.watcher.game();
        let launched = self.launch_event.as_ref().is_some_and(LaunchEvent::requested);
        if !running && (launched || (game.is_some() && self.last_game.is_none() && self.options.auto_start_on_game)) {
            self.source = "game".into();
            self.start();
            running = true;
        }
        self.last_game = game;
        self.show_run_state();
        let status = self.worker.as_ref().map(Worker::take_status);
        if let Some(status) = &status {
            self.connected = status.connected.clone();
            self.game_ended = status.game_ended;
            if let Some(stats) = &status.session_stats {
                self.ui.set_timing(
                    t!("지난 세션: 지연 프레임 {late} / 최대 {max} ms", late = stats.late_frames,
                       max = format!("{:.1}", stats.max_lateness_ms))
                    .into(),
                );
            }
            if let Some(error) = &status.error {
                self.error = Some(error.clone());
            }
            for line in &status.events {
                self.events.push_front(line.clone());
            }
            if !status.events.is_empty() {
                self.show_events();
            }
        }
        // only the output switches it off by itself: the drive says why in its events
        let gave_way = self.worker.as_ref().is_some_and(|w| !w.output_2k.load(std::sync::atomic::Ordering::Relaxed));
        if self.options.output_2k && gave_way {
            self.output_2k_gave_way();
        }
        if let Some(share) = self.player.take_gave_way() {
            let [what, why] = ats_haptics::rate2k::gave_way_lines(share);
            self.add_event(&why);
            self.add_event(&what);
            self.output_2k_gave_way();
        }
        if running {
            let status = status.unwrap_or_default();
            let device = self.connected.clone().unwrap_or_else(|| {
                if self.dry_run { t!("컨트롤러 없음") } else { t!("컨트롤러 미사용") }
            });
            let state = if status.state.is_empty() { t!("시작 중") } else { status.state.clone() };
            self.status = format!("{device} · {state}");
            self.status_color = if self.connected.is_some() || self.dry_run { GO } else { WARN };
            self.show_meters(status.peaks, true);
            self.show_telemetry(&status);
        } else {
            if let Some(error) = self.player.take_error() {
                self.error = Some(error);
            }
            let testing = self.player.busy();
            if testing {
                if let Some((effect, surface)) = self.player.playing() {
                    let label = crate::specs::EFFECT_SLIDERS
                        .iter()
                        .chain(&crate::specs::EVENT_SLIDERS)
                        .find(|s| s.key == effect.name())
                        .map(|s| t!(s.label))
                        .unwrap_or_default();
                    let mut name = label;
                    if let Some((_, surface)) = SURFACE_NAMES.iter().find(|(k, _)| *k == surface) {
                        name.push_str(&format!(" · {}", t!(surface)));
                    }
                    self.status = t!("효과 테스트: {name}", name = name);
                }
                self.status_color = GO;
                self.tested = true;
            } else if let Some(error) = &self.error {
                self.status = t!("오류: {error}", error = error);
                self.status_color = STOP;
            } else if let Some(notice) = &self.notice {
                self.status = notice.clone();
                self.status_color = WARN;
                self.tested = false;
            } else {
                if self.had_worker || self.tested {
                    self.status = t!("정지됨");
                    self.tested = false;
                }
                self.status_color = FAINT;
            }
            let peaks = self.player.take_peaks();
            self.show_meters(peaks, testing);
            if self.game_ended && self.exit_with_game {
                self.exit_with_game = false; // ask at most once if the save prompt is cancelled
                self.on_close();
                if self.closed {
                    return;
                }
            }
        }
        self.show_status();
    }

    /// A drive under way, made up, for the offscreen snapshot.
    pub fn sample_events(&mut self) {
        let lines = ["22:11:02 스플리터", "22:11:02 변속 5단", "22:11:00 변속 4단", "22:10:59 변속 3단",
                     "22:10:58 변속 2단", "22:10:57 페달 바닥"];
        self.events_model.set_vec(lines.iter().map(|l| SharedString::from(*l)).collect::<Vec<_>>());
        let frame = ats_haptics::telemetry::Frame {
            speed: 47.0 / 3.6,
            rpm: 1611.0,
            gear: 5,
            user_throttle: 0.8,
            ..Default::default()
        };
        let status = crate::worker::Status {
            frame: Some(frame),
            surface: "road".into(),
            trigger_mode: "throttle",
            ..Default::default()
        };
        self.show_telemetry(&status);
        self.meter_values = [0.0; 3];
        self.show_meters([0.62, 0.55, 0.28], true);
        self.status = format!("{} · {}", t!("컨트롤러 없음"), t!("주행 중 ({source})", source = "simulated 60 fps"));
        self.status_color = GO;
        self.show_status();
    }

    fn show_meters(&mut self, peaks: [f64; 3], live: bool) {
        for (i, peak) in peaks.into_iter().enumerate() {
            self.meter_values[i] = if live { peak.max(self.meter_values[i] * METER_DECAY) } else { 0.0 };
            if self.meters.row_data(i) != Some(self.meter_values[i] as f32) {
                self.meters.set_row_data(i, self.meter_values[i] as f32);
            }
        }
    }

    fn show_telemetry(&self, status: &crate::worker::Status) {
        let ui = &self.ui;
        let Some(frame) = &status.frame else {
            for setter in [AppWindow::set_speed, AppWindow::set_rpm, AppWindow::set_gear, AppWindow::set_pedal,
                           AppWindow::set_surface, AppWindow::set_trigger_pedal, AppWindow::set_accel] {
                setter(ui, "-".into());
            }
            return;
        };
        ui.set_speed(format!("{:.0} km/h", frame.speed.abs() * 3.6).into());
        ui.set_rpm(format!("{:.0}", frame.rpm).into());
        ui.set_gear(frame.gear.to_string().into());
        ui.set_pedal(t!("엑셀 {throttle} / 브레이크 {brake}", throttle = format!("{:.2}", frame.user_throttle),
                        brake = format!("{:.2}", frame.user_brake)).into());
        ui.set_surface(if status.surface.is_empty() { "-".into() } else { status.surface.as_str().into() });
        ui.set_trigger_pedal(match status.trigger_mode {
            "throttle" => t!("엑셀"),
            "brake" => t!("브레이크"),
            _ => "-".into(),
        }.into());
        ui.set_accel(format!("{:.1} m/s²", status.max_accel).into());
    }
}

fn choices(items: impl Iterator<Item = (String, String)>) -> ModelRc<Choice> {
    ModelRc::from(Rc::new(VecModel::from(
        items.map(|(value, label)| Choice { value: value.into(), label: label.into() }).collect::<Vec<_>>(),
    )))
}
