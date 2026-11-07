//! The telemetry plugin dialog (gui.py's PluginDialog): ATS/ETS2 installs with their plugin,
//! and the bundled one installed on request.

use std::cell::RefCell;
use std::path::Path;
use std::rc::Rc;

use slint::{ComponentHandle, ModelRc, VecModel};

use ats_haptics::platform::{self, Icon};
use ats_haptics::plugin_install::{self, GameInstall, BUNDLED_PLUGIN, PLUGIN_FILE};
use ats_haptics::t;

use crate::{GameRow, PluginDialog};

struct Inner {
    dialog: PluginDialog,
    games: Vec<GameInstall>,
    open: bool,
}

#[derive(Clone)]
pub struct Plugins(Rc<RefCell<Inner>>);

impl Plugins {
    pub fn new() -> Plugins {
        let dialog = PluginDialog::new().expect("the plugin dialog");
        let me = Plugins(Rc::new(RefCell::new(Inner { dialog, games: plugin_install::find_games(), open: true })));
        let weak = Rc::downgrade(&me.0);
        let get = move || weak.upgrade().map(Plugins);
        let dialog = me.0.borrow().dialog.clone_strong();
        let g = get.clone();
        dialog.on_install(move |key| {
            if let Some(p) = g() {
                p.install(&key);
            }
        });
        let g = get.clone();
        dialog.on_pick_folder(move || {
            if let Some(p) = g() {
                p.pick_folder();
            }
        });
        let g = get.clone();
        dialog.on_close_clicked(move || {
            if let Some(p) = g() {
                p.close();
            }
        });
        dialog.window().on_close_requested(move || {
            if let Some(p) = get() {
                p.0.borrow_mut().open = false;
            }
            slint::CloseRequestResponse::HideWindow
        });
        me.render();
        let _ = dialog.show();
        crate::title_bar_later(&dialog);
        me
    }

    pub fn open(&self) -> bool {
        self.0.borrow().open
    }

    pub fn component(&self) -> PluginDialog {
        self.0.borrow().dialog.clone_strong()
    }

    pub fn raise(&self) {
        crate::raise(self.0.borrow().dialog.window());
    }

    pub fn hide(&self) {
        let _ = self.0.borrow().dialog.hide();
    }

    fn close(&self) {
        let mut inner = self.0.borrow_mut();
        inner.open = false;
        let _ = inner.dialog.hide();
    }

    pub fn render(&self) {
        let inner = self.0.borrow();
        let rows: Vec<GameRow> = inner
            .games
            .iter()
            .map(|game| GameRow {
                key: game.key.as_str().into(),
                name: game.name.as_str().into(),
                path: game.path.display().to_string().into(),
                describe: game.describe().into(),
                button: if game.compatible() { t!("재설치") } else { t!("설치") }.into(),
            })
            .collect();
        inner.dialog.set_ui_font(ats_haptics::i18n::ui_font().into());
        inner.dialog.set_games(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn hwnd(&self) -> windows_sys::Win32::Foundation::HWND {
        crate::hwnd(self.0.borrow().dialog.window())
    }

    fn pick_folder(&self) {
        let hwnd = self.hwnd();
        let Some(folder) = platform::pick_folder(hwnd, &t!("ATS 또는 ETS2 설치 폴더")) else { return };
        let Some(game) = plugin_install::game_from_folder(&folder) else {
            platform::show(hwnd, &t!("플러그인"), &t!("bin\\win_x64\\amtrucks.exe 또는 eurotrucks2.exe가 있는 게임 폴더를 선택하세요."),
                           Icon::Error);
            return;
        };
        {
            let mut inner = self.0.borrow_mut();
            inner.games.retain(|g| g.key != game.key);
            inner.games.push(game);
        }
        self.render();
    }

    /// The bundled plugin as a file beside the error, for copying by hand.
    fn bundled_file() -> String {
        let path = std::env::temp_dir().join("ATSHaptics-plugin").join(PLUGIN_FILE);
        let _ = std::fs::create_dir_all(path.parent().unwrap());
        let _ = std::fs::write(&path, BUNDLED_PLUGIN);
        path.display().to_string()
    }

    fn install(&self, key: &str) {
        let hwnd = self.hwnd();
        let Some(mut game) = self.0.borrow().games.iter().find(|g| g.key == key).cloned() else { return };
        let text = format!(
            "{}\n\n{}\n\n{}",
            t!("{game}의 plugins 폴더에 RenCloud scs-telemetry.dll(v1.12.1, MIT)을 복사합니다.", game = game.name),
            game.plugins_dir().display(),
            t!("게임이 실행 중이면 다음 실행부터 적용됩니다.")
        );
        if !platform::ask_ok_cancel(hwnd, &t!("플러그인 설치"), &text) {
            return;
        }
        match plugin_install::install(&mut game) {
            Ok(target) => {
                platform::show(hwnd, &t!("플러그인 설치"), &format!("{}\n{}", t!("설치했습니다."), target.display()), Icon::Info);
                let mut inner = self.0.borrow_mut();
                if let Some(slot) = inner.games.iter_mut().find(|g| g.key == key) {
                    *slot = game;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                let text = format!(
                    "{}\n\n{}\n→ {}",
                    t!("폴더에 쓸 권한이 없습니다. 프로그램을 관리자 권한으로 실행하거나 아래 파일을 직접 복사하세요."),
                    Plugins::bundled_file(),
                    Path::new(&game.plugins_dir()).display()
                );
                platform::show(hwnd, &t!("플러그인 설치"), &text, Icon::Error);
                return;
            }
            Err(error) => {
                platform::show(hwnd, &t!("플러그인 설치"), &t!("복사하지 못했습니다: {error}", error = error), Icon::Error);
                return;
            }
        }
        self.render();
    }
}
