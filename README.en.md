# ATS Haptics for Flydigi APEX 6 Pro

[한국어](README.md) · **English** · [日本語](README.ja.md) · [简体中文](README.zh.md)

Drives the voice-coil (VCM) motors of the Flydigi APEX 6 Pro directly from American Truck Simulator / Euro Truck Simulator 2 telemetry. Unlike X-Haptics, which analyses game audio, the vibration is built from game data such as engine rpm, road surface, suspension and pedals, and every effect can be tuned live in a window.

![Main window](docs/screenshot-main.png)

> The screenshots show the Korean interface. The window follows the Windows display language; pick another one from the language menu at the top right.

## Contents

- [Requirements](#requirements)
- [Install and quick start](#install-and-quick-start)
- [The window](#the-window)
- [Telemetry plugin window](#telemetry-plugin-window)
- [Start with the game (Steam launch option)](#start-with-the-game-steam-launch-option)
- [Updates](#updates)
- [Tuning tips](#tuning-tips)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Running from source and building](#running-from-source-and-building)
- [License](#license)

## Requirements

- Windows 10/11
- Flydigi **APEX 6 Pro** (wired or 2.4 GHz dongle; Bluetooth and the non-Pro APEX 6 are not supported)
- American Truck Simulator or Euro Truck Simulator 2
- **Turn off the xHaptics Engine** while this runs. It drives the same motors and the vibrations would mix. For the Space Station web page, closing its tab is enough.

## Install and quick start

1. Download `ATSHaptics-<version>.zip` from [Releases](../../releases) and unpack it anywhere.
2. Run `ATSHaptics\ATSHaptics.exe`. There is no installer.
3. If no usable telemetry plugin is found for your game, the [plugin window](#telemetry-plugin-window) opens by itself. Press **Install**.
4. First check that the controller vibrates with **Test drive** → **Start** (a 60-second scenario, no game needed).
5. Choose **ATS/ETS2 game** → **Start** and launch the game. The app takes the controller when the game starts and releases it when the game ends.

To have the window open with the game every time, set the [Steam launch option](#start-with-the-game-steam-launch-option).

## The window

Rest the pointer on any slider, switch, button or value for a tooltip explaining what it does. The tooltips are short versions of the tables below.

### Top bar and output control

| Item | Description |
|---|---|
| **Input: ATS/ETS2 game** | Builds vibration from the game's telemetry. The controller is only used while ATS (`amtrucks.exe`) or ETS2 (`eurotrucks2.exe`) is running. |
| **Input: Test drive** | Plays a fixed 60-second delivery run on a loop, no game needed: engine start → trailer coupling → wheelspin at launch → acceleration and shifting (splitter, range) → motorway (left signal, left pothole, right rumble strip) → concrete joints → gravel (right signal) → wooden bridge → snow → wheelspin on ice → downhill retarder and engine brake → hard stop (wheel lock, brake squeal) → toll gate → hazards on, reversing into the dock with a bump (impact) → parking brake → trailer uncoupling → delivery complete → engine off. The trailer wheels hit each bump 0.45 s after the truck. Use it to check or tune effects. |
| **▶ Start / ■ Stop** | One button. The green **Start** begins output and turns into a red **Stop**. Stopping turns the motors off and releases the controller. The input (game/test drive) cannot be changed while running. |
| **Start when the game runs** (switch) | When on, a stopped window starts by itself the moment the game runs. If you stop it yourself during a game, it stays stopped until that game ends. The switch is saved immediately. |
| **GitHub ↗** | Opens this program's GitHub repository (source, releases, questions) in the browser. |
| **Language menu** (e.g. `English ▾`) | Picks the interface language: 한국어, English, 日本語, 简体中文, Deutsch, Français, Español, Português (Brasil), Polski, Русский. The window redraws at once, tuning and the running session are kept, and the choice is saved immediately. Until you pick one, the Windows display language is used, or English if it is not in the list. Status messages and the event log follow the same language; lines already logged stay as they were. |
| **Telemetry plugin** | Opens the [plugin window](#telemetry-plugin-window). |
| **Update x.y.z** | Only appears when GitHub has a newer release. See [Updates](#updates). |

### Output level and meters

The **final strength** sent to the controller, applied after all the effect levels below have been mixed. Range 0–150%, default 100%.

| Item | Description |
|---|---|
| **Master** | Applies to the grips and the trigger. |
| **Grip** | Applies to the left and right grip motors only. |
| **Trigger** | Applies to the trigger motors only. 0% turns trigger vibration off completely. |

At higher levels a soft limiter keeps overlapping effects from clipping harshly. Above 100%, weak effects get stronger while effects that are already strong barely change.

The **meters** show what actually goes to the left grip, right grip and trigger. Use them to see at which stage a weak or missing vibration gets lost.

### Driving effects

The switch on the left turns each effect on or off. An effect that is off is greyed out but keeps its slider and output, so turning it back on restores it.

**Output to** on the right picks where each effect vibrates: **Grip** (blue) | **Trigger** (orange) | **Both** (violet), for driving and event effects alike. The slider takes the colour of the chosen output, so you can see at a glance what goes where. Picking an output on an effect that is off turns it back on. "Default output" in the tables is the initial setting. Sided effects (road, bumps, turn signals) sent to the trigger are summed into it; trigger effects sent to the grips play on both grips. Changes apply at once and are kept when you **Save**.

| Effect | Default (range) | Default output | What it does |
|---|---|---|---|
| **Engine** | 50% (0–200) | Both grips | The engine firing frequency (rpm ÷ 60 × cylinders ÷ 2, so rpm ÷ 20 for a six) plus its second harmonic. Stronger with more throttle and higher rpm. Each firing differs a little from the last (±8%); at idle the cylinders fire unevenly and it lopes, evening out as rpm and load rise. Under load a third harmonic makes it harsh. Nothing while the engine is off. |
| **Road** | 60% (0–200) | Left/right grip separately | Strength and feel come from the surface under the wheels and the speed, plus fine suspension movement. Strength runs ice < asphalt < concrete < snow < wood < metal < grass < dirt < gravel. Shaped after the low end of real tyre recordings, each surface shivers at its own pitch (ice 140 Hz, grass and metal 130 Hz, asphalt 100 Hz, gravel 85 Hz, snow 70 Hz, wood 65 Hz), lower when slow (60% at a standstill). Gravel, dirt, snow and coarse paving add the click of loose stones, mostly small with now and then a big one (about 3 per metre on gravel). Like recorded road noise it swells a few times a second (about 4 Hz), and where the suspension works hard the cab rattles: 55 Hz knocks, up to 40 a second. It also tells paving apart (smooth `road_smooth` < ordinary road < coarse `road_coarse` < dirt-covered `road_dirt`). |
| **Bumps** | 100% (0–200) | Grip on the wheel's side | A hit when the suspension moves suddenly (potholes, kerbs, joints). Shaped after recorded bumps, it follows the size of the hit: a small joint is a short, high tap (65 Hz, 0.025 s), a big pothole a low, heavy thud (40 Hz, 0.06 s), and after a big hit the cab rattles softly for about 0.3 s. Left wheels go to the left grip, right wheels to the right grip. Trailer wheels follow at half strength and a little higher (+10 Hz). |
| **Rumble strips** | 80% (0–200) | Grip on the wheel's side | A "brrrrt" when you drive onto a centre-line or shoulder rumble strip. It is detected from the per-wheel surface the game reports (`rumble_stripe`), so it works even when the suspension barely moves. The frequency follows the groove spacing (about 30 cm) and speed: about 83 Hz at 90 km/h. Trailer wheels on the strip alone give half strength. |
| **Wheelspin** | 70% (0–200) | Both grips | A rough, grinding 45 Hz when the driven wheels clearly turn faster than the truck under throttle (ice, snow, mud, hard launches), chattering at 13 Hz like a tyre gripping and slipping. Wheels locked by braking are handled by **Throttle/brake**. |
| **Gear shift** | 80% (0–200) | Both grips | A "clunk" on every gear change: the driving vibration drops out for 0.02 s as if the clutch were pressed, then a firm 62 Hz engagement with a 38 Hz tail. Works with automatic gearboxes too. |
| **Retarder** | 50% (0–200) | Both grips | A hum proportional to the retarder stage that rises in pitch with speed (40 Hz standing, 65 Hz from 90 km/h) and fades at low speed. The engine brake is a knock at the engine's firing rate (rpm ÷ 20 for six cylinders), stronger at higher rpm and gone while you press the throttle. |
| **Impact** | 100% (0–200) | Both grips | A strong 50 Hz pulse when acceleration jumps sharply (collisions, big hits). The threshold is set under [Detection](#detection). |
| **Throttle/brake** | 40% (0–100) | Trigger | Follows the pedal you are pressing. On the throttle, 100 Hz that grows with pedal travel. On the brake, a 110 Hz pulse chopped at 12 Hz when the wheels lock; without lock, a light vibration proportional to speed and braking. |
| **└ Engine pulse** | 0% (0–100) | Follows Throttle/brake | How much the throttle trigger swells with the engine firing rhythm. 0% is steady with no engine feel in the trigger; higher makes the rhythm clearer (older versions were fixed at 80%). The pulse follows the same firings as the engine vibration on the grips, so the uneven idle and each firing's slightly different strength come through the trigger too. No effect on the brake side. |
| **Ease at steady speed** | 60% (0–100) | - | To tire your hands less on long hauls, **Engine** and **Throttle/brake** fade by this amount over a few seconds while rpm, engine load and pedals hold steady (about half in 5 s, nearly all in 10 s). Accelerating, shifting, or lifting or pressing the throttle brings full strength back at once. 60% is about −7 dB at steady speed, 80% about −12 dB. 0% disables it. The wheel-lock pulse is a warning and never fades. |

### Event effects

| Effect | Default | Default output | What it does |
|---|---|---|---|
| **Pedals** | 60% | Trigger | A short "thud" the moment throttle or brake reaches the floor (97% or more) |
| **Brake squeal** | 40% | Trigger | A 230 Hz squeal while braking gently to a stop below about 14 km/h. Like the recorded ones it comes in separate squeals of 0.15–0.5 s, each a little off the last in pitch |
| **Trailer coupled** | 90% | Both grips | A heavy "thump" when coupling; a thump followed by an air hiss when uncoupling |
| **Parking brake** | 70% | Both grips | Set: a "clunk", then the spring brakes venting (swelling over 0.15 s, dying over most of a second). Released: a low "thunk", then a short puff 0.3 s later |
| **Air brake** | 50% | Both grips | Letting go of the brake pedal after more than 0.3 s vents the brake chambers: a 50 Hz "thump", then a short "pssht" (swelling over 0.04 s, dying over about 0.35 s). Louder the harder you braked. It comes with every release, so it does not duck the driving effects |
| **Turn signals** | 50% | Grip on the signalling side | A "tick" with every blink |
| **Engine start/stop** | 80% | Both grips | Cranking and a "vroom" on start; a shudder as it dies on stop |
| **Range/splitter** | 50% | Both grips | A "click" when you use the range selector or splitter switch of an H-shifter. Silent with automatic or sequential shifting. |
| **Game alerts** | 50% | Both grips | A double "tap-tap" on toll gates, fines, refuelling, finished deliveries, ferries and trains |
| **└ Duck for events** | 70% (0–100) | - | How much an event effect (gear shifts and impacts included) briefly lowers the driving vibration on the same output (engine, road, rumble strips, wheelspin, retarder, throttle/brake) so the event is not drowned out. At 70%, driving vibration drops to 30% at the peak of the event and recovers within about 0.1 s. 0% disables it. Bumps come from the road itself, so they do not duck the driving vibration. |

All event effects range from 0 to 200%.

### Detection

| Item | Default (range) | Description |
|---|---|---|
| **Bump threshold** | 20 cm/s (5–100) | A bump pulse fires when the suspension suddenly moves faster than this. Lower reacts to small bumps, higher only to big hits. |
| **Impact threshold** | 12 m/s² (4–40) | An impact pulse fires when acceleration changes by more than this from one game frame to the next. Keep it well above the **Peak accel.** you see under [Telemetry](#telemetry) in normal driving. |
| **Engine cylinders** | 6 (4–12) | Used for the engine vibration frequency. Most stock ATS trucks have an inline six; set 8 for trucks such as the ETS2 Scania V8. Telemetry does not report the cylinder count, so it is set by hand. |

### Telemetry

What the game is sending right now. Use it to check that data arrives when the vibration feels wrong.

| Item | Description |
|---|---|
| **Speed / RPM / Gear** | Straight from the game. Reverse gears are negative, neutral is 0. |
| **Pedals** | Throttle and brake input (0–1). Values the game applies itself, such as cruise control, are not included. |
| **Road** | The game's internal name of the surface under the first wheel (e.g. `road`, `gravel`, `grass`). |
| **Trigger** | The pedal the trigger channel is following right now (throttle/brake/-). |
| **Peak accel.** | The highest horizontal acceleration over the last 0.1 s. Useful for setting the impact threshold. |

### Event log

Gear shifts, impacts, floored pedals, trailer, parking brake, turn signals, engine start/stop and game alerts are logged with the time, and so are controller connection problems. The latest 50 entries are shown.

### Save, defaults, debug log and timing

| Item | Description |
|---|---|
| **Save** | Saves the sliders, on/off states and outputs (grip/trigger/both) to `%APPDATA%\ATSHaptics\config.json`. While there are unsaved changes the **Save** button turns blue and "Changed · not saved" is shown, and closing the window asks whether to save. |
| **Defaults** | Resets every level, threshold and output to its default and turns every effect on. Saving is a separate step. |
| **Debug log** (switch) | When on, every time the app takes the controller (Start, the game launching, reconnecting after a drop) it writes each 8 ms block sent to the controller as one line in `%APPDATA%\ATSHaptics\logs\blocks-<date>-<time>.csv`. Columns: time (to the ms), gap since the previous block (over 8 ms means output ran late), trigger route, peak trigger / left grip / right grip value (±112 is full scale), speed, gear, throttle, brake, surface, and the events of that block. About 30 MB per hour; only the latest 10 files are kept. The switch is saved at once and applies from the next **Start**. The event log shows the file name when recording begins. |
| **Log folder ↗** | Opens the debug log folder in Explorer. |
| **Last session: late frames / max ms** (right) | How often the previous session missed the 8 ms output period, and the worst delay. Close to 0 is normal. |

### Status line

Shown to the right of the output controls as `controller status · telemetry status`. The dot in front is green while vibrating, yellow when started but not yet holding the controller (waiting for the game and so on), red on errors and grey when stopped.

| Message | Meaning and what to do |
|---|---|
| `Stopped` | Not outputting. |
| `Controller not in use · Waiting for the game (ATS/ETS2 not running)` | Started, but the game is not running yet. The controller is not held. |
| `APEX 6 Pro · Driving (Local\…Telemetry)` | Working normally. The name in brackets is the shared memory of the plugin being read. |
| `… · Paused / menu` | The game is paused or in a menu. Vibration stops. |
| `… · Telemetry stalled (minimized or loading)` | Game data has not updated for over 0.3 s, e.g. while loading or with the window minimized. |
| `… · Waiting for telemetry (game loading or no plugin)` | The game is running but there is no telemetry. If it persists after loading, install the plugin. |
| `No telemetry: … plugin revision N (supported: 12) …` | The installed plugin's data layout revision does not match. Install from the plugin window. |
| `No controller (check the cable or 2.4G dongle and the power) · retrying in 2 s` | The controller was not found. Check the connection; the app retries every 2 seconds. |
| `Controller hidden: started by Steam, so Steam Input hides the pad …` | In a process started by Steam (for example ATSHaptics added as a non-Steam game), the pad is invisible while Steam Input is on for the game. Start the window yourself or use the launch option below. |
| `Controller not responding (press a button to wake it if it is asleep)` | The dongle is present but the controller does not answer. Press a button to wake it; the app continues by itself. |
| `Controller disconnected (…)` | The connection dropped while driving. The app continues by itself once it is back. |
| `Game closed, controller released` | The game ended and the controller was released. The app waits for the next launch. |
| `Error: …` | A problem that does not recover by itself (for example a model other than the APEX 6 Pro). Details go to `%APPDATA%\ATSHaptics\logs\error.log`. |

## Telemetry plugin window

![Telemetry plugin window](docs/screenshot-plugins.png)

Vibration is built from the data exported by the telemetry plugin installed in the game (the RenCloud scs-sdk-plugin family, data layout rev 12). This window finds the ATS/ETS2 install folders in your Steam libraries and shows which plugins are in `bin\win_x64\plugins`.

| Shown | Meaning |
|---|---|
| `RenCloud`, `TruckDash` | Compatible. Nothing to do. |
| `SimHub` | The SimHub plugin. Its data layout is not verified, so it is shown as "compatibility unknown". |
| `No telemetry plugin` | Press **Install**. |

- **Install / Reinstall:** copies the bundled RenCloud `scs-telemetry.dll` (v1.12.1, MIT) into the game's `plugins` folder. Other plugins are left alone. If the game is running, it takes effect from the next launch.
- **Choose game folder…:** for installs outside Steam or ones that were not found, point to the game folder (the one containing `bin\win_x64\amtrucks.exe` or `eurotrucks2.exe`).
- If the game folder is not writable, the error dialog names the file to copy and where. Run as administrator or copy it yourself.
- With a plugin present, the game may show an "Advanced SDK features" notice at startup. It means the telemetry plugin loaded correctly.

## Start with the game (Steam launch option)

In your Steam library, open the game's **Properties → General → Launch options** and enter the following, with the path changed to where you unpacked the app:

```
"C:\Tools\ATSHaptics\ATSHaptics.exe" --launch %command%
```

- When the game starts, the window **opens minimised and starts by itself**. When the game ends, the window closes too, asking first if there are unsaved changes.
- Nothing stays resident. The game is checked every 2 seconds only while the window is open.
- Put any existing launch options (e.g. `-nointro`, `-rdevice dx11`) after `%command%`.
- If the window is already open, no second window appears; if that window is stopped, it starts with the **ATS/ETS2 game** input (regardless of the auto-start switch). That window does not close when the game ends.
- The window is started outside the process Steam launched (through WMI). Steam injects its overlay into the game and its child processes and, with Steam Input on for the game, hides the pad from them. This way the app gets the pad even with Steam Input on.

## Updates

When the window opens it checks GitHub for the latest release. If there is a newer one, an **Update x.y.z** button appears at the top right. Offline, the check is skipped quietly.

- The button shows the release notes; on confirmation it downloads the zip and checks it against the SHA-256 published on GitHub.
- After the download the window closes (asking first about unsaved changes). Once the program has fully exited, the new files are copied over the install folder and the new version starts. Settings (`%APPDATA%\ATSHaptics`) are kept.
- The install folder is backed up first; if copying fails, the previous version is restored and started. If the program has not exited after 2 minutes, nothing is changed.
- No updates while the game runs: when started through the Steam launch option, the executable stays in use until the game ends. Quit the game, open the window again and update.
- If the install folder is not writable (e.g. `Program Files`) or you run from source, only the release page is opened.
- The install log is kept at `%TEMP%\ATSHaptics-update-<version>\unpacked\apply-update.log`.

## Tuning tips

1. Start with **Test drive** and set the master level while watching the meters.
2. In real driving, toggle effects one at a time to compare. Switches apply immediately.
3. If engine or trigger vibration tires your hands on long hauls, raise **Ease at steady speed** to 70–90% before lowering levels. Steady stretches get quiet while the feel of acceleration and shifting stays. If it still bothers you, lower **Engine** to 20–30%.
4. If the trigger is too much for your fingers, lower **Output level → Trigger**. The balance between effects stays the same.
5. To feel an effect in your fingers (e.g. the engine in the throttle trigger), set its output to **Trigger** or **Both**.
6. If bump pulses come too often on paved roads, raise the **Bump threshold**.
7. If events such as turn signals or gear shifts get lost while driving, raise **Duck for events** or lower **Engine** and **Road**. If you dislike the driving vibration dipping on every event, lower the ducking.
8. (Running from source) If the engine feels weak at idle and strong only when cruising, or high ticks such as the turn signals feel faint, match the motors' strength across frequencies with `tone.py calibrate`. The motors ring hardest near 65 Hz (grips) and 100 Hz (triggers), so the same drive feels different at each frequency. Raise or lower each test tone until it feels as strong as the reference; that factor then applies to every effect at that frequency (`eq` in `config.json`, from the next start). Without a measurement nothing is corrected; `tone.py calibrate --reset` clears it.
9. When you like it, press **Save**.

## Troubleshooting

**No vibration at all.**
- Check that the status reads `APEX 6 Pro · Driving`. For any other message, see the [status table](#status-line).
- Turn off the xHaptics Engine if it is running.
- If the meters move but nothing vibrates, check the output levels and the connection type (Bluetooth is not supported).

**No vibration after starting the game until I restart the program.** A bug in 0.1.0. When the game or Steam initialises the gamepad while loading, or the controller re-pairs with the dongle after sleeping, the controller firmware loses its motor setup while data transfers keep succeeding, so no retry happened. The motor setup is now re-sent periodically while outputting (every 1 s when silent, every 4 s while vibrating), and it recovers within seconds.

**Telemetry values only show `-`.**
- Check whether the game is paused or in a menu.
- In the plugin window, check for a compatible plugin (RenCloud, TruckDash); if there is none, install it and restart the game.

**Both triggers vibrate.** A limitation of the controller. See [Known limitations](#known-limitations).

**The window does not open when I start the game from Steam.**
- Check the path and quotes in the launch option.
- If a window is already open, no new one appears (check the taskbar).

**The text is in the wrong language.** Pick one from the language menu at the top right. The choice is saved as `app.language` in `config.json`; delete that value to follow the Windows display language again.

**I want to reset the settings.** Press **Defaults** → **Save**, or delete `%APPDATA%\ATSHaptics\config.json`.

**The vibration sometimes kicks oddly.** Turn on **Debug log** at the bottom, press **Start** again, and note the clock time when it happens. Around that time, a `gap_ms` well above 8 means output ran late; a sudden jump in `trigger`/`left_grip`/`right_grip` means this program sent it. If both look normal, the cause is outside the program (radio interference, vibration from the game or Steam Input, firmware). Send the file and the time when you report it.

**I want to collect information for a report.** Run `ATSHaptics.exe --selftest`; it writes the version, controller detection and game/plugin detection to `%TEMP%\ATSHaptics-selftest.txt`. If the status showed `Error: …`, also send `%APPDATA%\ATSHaptics\logs\error.log`, where unexpected errors are logged with the time.

## Known limitations

- There is only one trigger channel, so it vibrates **both triggers together** with no left/right distinction. The grips are independent. Verified on controller firmware 9.0.5.5 with `tone.py split` (a blind check against a reference tone): routing to the left or right, or alternating every block, felt the same on both triggers. Flydigi Space Station and X-Haptics also treat the triggers as a single part.
- Telemetry carries no engine cylinder count, wiper speed and the like, so those effects are set by hand or left out.
- Based on unofficial reverse engineering; controller firmware updates may change the behaviour.

## Running from source and building

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe app.py                    # window (or start_gui.cmd)
.\.venv\Scripts\python.exe ats_haptics.py --monitor   # command line: --simulate, --exit-with-game, per-effect levels
.\.venv\Scripts\python.exe tone.py probe              # diagnostics: probe | tone | sweep | walk | route-test | diag | split | calibrate
.\.venv\Scripts\python.exe tone.py split              # blind left/right trigger check, results in logs\trigger-split-*.txt
.\.venv\Scripts\python.exe tone.py calibrate          # match strength across frequencies, --reset clears it
powershell -ExecutionPolicy Bypass -File build.ps1    # dist\ATSHaptics-<version>.zip (source included)
.\.venv\Scripts\python.exe i18n.py                  # translation check: missing, unused or {placeholder}-mismatched strings
.\.venv\Scripts\python.exe -m unittest              # regression tests (tests\)
```

Interface strings use the Korean source text in the code as the key (`t("정지됨")`). Translations live in `MESSAGES` in `locales\<language>.py`; a missing translation falls back to English, then Korean. After changing or adding a string, run `i18n.py` to check that every language table has it.

Steam launch option from source: `"<repo>\.venv\Scripts\pythonw.exe" "<repo>\launch.py" %command%`

Structure:

```
ATS ─(scs-telemetry shared memory)─▶ telemetry.py ─▶ effects.py (1 kHz synthesis) ─▶ apex6.py ─(HID 0x57, 8 ms)─▶ APEX 6 Pro
                                       gamewatch.py: game detection     plugin_install.py: plugin detection/install
                                       updater.py: GitHub release check and self-update
                                       i18n.py + locales\: interface languages    gui.py + widgets.py: window, tooltips
                                       blocklog.py: debug log           errorlog.py: unexpected error log
```

## License

GPL-3.0-or-later (`LICENSE`). The APEX 6 protocol implementation is ported from [ApexSenseBridge](https://github.com/ReynArts/ApexSenseBridge) (GPL-3.0-or-later), and the bundled telemetry plugin is [RenCloud/scs-sdk-plugin](https://github.com/RenCloud/scs-sdk-plugin) (MIT). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details. The release zip carries the corresponding source in its `source\` folder.

Unofficial and not affiliated with Flydigi or SCS Software.
