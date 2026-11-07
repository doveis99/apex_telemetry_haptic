//! Draw a window offscreen into a PNG: the same software renderer as on screen, without
//! opening a window or touching the controller.
//!
//!   ATSHaptics.exe --snapshot OUT.png [--language CODE] [--screen main|plugins|calibration|session|result]

use std::cell::RefCell;
use std::rc::Rc;

use slint::platform::software_renderer::{MinimalSoftwareWindow, PremultipliedRgbaColor, RepaintBufferType};
use slint::platform::{Platform, WindowAdapter};
use slint::ComponentHandle;

struct Offscreen {
    windows: Rc<RefCell<Vec<Rc<MinimalSoftwareWindow>>>>,
}

impl Platform for Offscreen {
    fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, slint::PlatformError> {
        let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
        self.windows.borrow_mut().push(Rc::clone(&window));
        Ok(window)
    }
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

/// An RGBA image as PNG, its pixels in stored (uncompressed) deflate blocks.
pub fn png(width: u32, height: u32, rgba: &[u8]) -> Vec<u8> {
    let mut raw = Vec::with_capacity((width as usize * 4 + 1) * height as usize);
    for row in rgba.chunks(width as usize * 4) {
        raw.push(0); // no filter
        raw.extend_from_slice(row);
    }
    let mut zlib = vec![0x78, 0x01];
    let blocks: Vec<&[u8]> = raw.chunks(65_535).collect();
    for (i, block) in blocks.iter().enumerate() {
        zlib.push((i + 1 == blocks.len()) as u8);
        zlib.extend_from_slice(&(block.len() as u16).to_le_bytes());
        zlib.extend_from_slice(&(!(block.len() as u16)).to_le_bytes());
        zlib.extend_from_slice(block);
    }
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in &raw {
        a = (a + byte as u32) % 65_521;
        b = (b + a) % 65_521;
    }
    zlib.extend_from_slice(&((b << 16) | a).to_be_bytes());
    let mut out = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
    let mut chunk = |kind: &[u8], data: &[u8]| {
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        let mut body = kind.to_vec();
        body.extend_from_slice(data);
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc32(&body).to_be_bytes());
    };
    let mut header = Vec::new();
    header.extend_from_slice(&width.to_be_bytes());
    header.extend_from_slice(&height.to_be_bytes());
    header.extend_from_slice(&[8, 6, 0, 0, 0]); // 8-bit RGBA
    chunk(b"IHDR", &header);
    chunk(b"IDAT", &zlib);
    chunk(b"IEND", &[]);
    out
}

fn draw(window: &MinimalSoftwareWindow, width: u32, height: u32) -> Vec<u8> {
    window.set_size(slint::PhysicalSize::new(width, height));
    let mut pixels = vec![PremultipliedRgbaColor::default(); (width * height) as usize];
    for _ in 0..3 {
        // a few turns so the layout and the timers settle
        slint::platform::update_timers_and_animations();
        window.request_redraw();
        window.draw_if_needed(|renderer| {
            renderer.render(&mut pixels, width as usize);
        });
    }
    pixels.iter().flat_map(|p| [p.red, p.green, p.blue, 255]).collect() // the window is opaque
}

pub fn run(args: &[String]) -> i32 {
    let Some(out) = args.first() else {
        eprintln!("--snapshot OUT.png [--language CODE] [--screen main|plugins|calibration|session|result]");
        return 2;
    };
    let value = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let language = value("--language").unwrap_or_else(|| "ko".into());
    let screen = value("--screen").unwrap_or_else(|| "main".into());
    let windows = Rc::new(RefCell::new(Vec::new()));
    slint::platform::set_platform(Box::new(Offscreen { windows: Rc::clone(&windows) })).expect("the offscreen platform");
    ats_haptics::i18n::set_language(&language);
    let mut height_of: Option<Box<dyn Fn() -> f32>> = None;
    let (width, height, keep): (f32, f32, Box<dyn std::any::Any>) = match screen.as_str() {
        "plugins" => {
            let plugins = crate::plugins::Plugins::new();
            crate::select_language(&language);
            plugins.render();
            let dialog = plugins.component();
            (dialog.get_content_width(), dialog.get_content_height(), Box::new(plugins))
        }
        "calibration" | "session" | "result" => {
            let calibration = crate::calibration::Calibration::new();
            crate::select_language(&language);
            calibration.relabel();
            let dialog = calibration.component();
            calibration.show_sample(&screen);
            let measured = dialog.clone_strong();
            height_of = Some(Box::new(move || measured.get_content_height()));
            (506.0, dialog.get_content_height(), Box::new(calibration))
        }
        _ => {
            let app = crate::window::App::new(true, false);
            crate::select_language(&language);
            let ui = app.borrow().ui.clone_strong();
            let _ = ui.show();
            app.borrow_mut().sample_events();
            if screen == "tip" {
                // the road slider's explanation, as if the pointer rested on it
                let tips = ui.global::<crate::Tips>();
                let road = ats_haptics::i18n::t(
                    crate::specs::SLIDER_TIPS.iter().find(|(k, _)| *k == "road").unwrap().1, &[]);
                tips.set_text(road.into());
                tips.set_pointer_x(300.0);
                tips.set_top(280.0);
                tips.set_bottom(300.0);
                tips.set_shown(true);
            }
            (ui.get_content_width(), ui.get_content_height(), Box::new(app))
        }
    };
    slint::platform::update_timers_and_animations();
    let window = windows.borrow().first().cloned().expect("a window");
    // wrapped text needs the width before it knows its height
    window.set_size(slint::PhysicalSize::new(width.ceil() as u32, height.ceil().max(1.0) as u32));
    // one render first: the screen's `if` parts are only put in place as it draws
    draw(&window, width.ceil() as u32, height.ceil().max(1.0) as u32);
    let height = height_of.as_ref().map_or(height, |f| f());
    let (w, h) = (width.ceil() as u32, height.ceil() as u32);
    let rgba = draw(&window, w, h);
    drop(keep);
    match std::fs::write(out, png(w, h, &rgba)) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("{out}: {error}");
            1
        }
    }
}
