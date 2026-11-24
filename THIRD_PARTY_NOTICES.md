# Third-party notices

SCS Haptics is licensed under the GNU General Public License v3.0 or later (see `LICENSE`).
It includes or is derived from the following works. The release zip also carries
`THIRD_PARTY_LICENSES.txt`: every Rust crate compiled into the executables, with its version,
declared license and license texts, as `tools/collect_licenses.ps1` collects them from
`Cargo.lock` when `build.ps1` packs a release.

## ApexSenseBridge — GPL-3.0-or-later

<https://github.com/ReynArts/ApexSenseBridge>

The APEX 6 Pro HID protocol in `src/apex6.rs` and `python/apex6.py` (motor enable `0x53`,
realtime motor `0x57`, packet layout, checksum and init sequence) was ported from
ApexSenseBridge's `src/flydigi/Apex6Protocol.cpp`, `src/flydigi/Apex5Device.cpp` and
`src/dualsense/Apex6HapticBridge.cpp`. ApexSenseBridge is distributed under the GNU General
Public License v3.0 or later.

## scs-sdk-plugin (RenCloud) — MIT

<https://github.com/RenCloud/scs-sdk-plugin>

`plugin/src` and `plugin/inc` are the plugin's source at release V.1.12.1 (commit `d7216cb`), with
this project's changes: each is marked `SCS Haptics build` in the source and listed in
`plugin/README.md`. `resources/scs-sdk-plugin/scs-telemetry.dll` is the Win64 build of it that
`tools/build_plugin.ps1` makes. `SCSHaptics.exe` carries it inside and copies it into the game's
`plugins` folder from its plugin window. Releases up to 0.23.0 carried RenCloud's own unmodified
build instead (`release_v_1_12_1.zip`, DLL SHA-256
`1d03dbc7a975e72203c60a7b9998021ceb8800b836bf28a131279979ad386cd4`). The shared-memory layout read
by `src/telemetry.rs` and `python/telemetry.py` follows the plugin's `scs-telemetry-common.hpp`
(revision 12). Its MIT license is in `plugin/LICENSE`, `resources/scs-sdk-plugin/LICENSE` and
`THIRD_PARTY_LICENSES.txt`.

## SCS SDK (SCS Software) — MIT

<https://modding.scssoft.com/wiki/Documentation/Engine/SDK/Telemetry>

`plugin/scs_sdk/include` holds the headers of the SCS Telemetry SDK 1.16, unchanged, which the
plugin is compiled against. Their license is in `plugin/scs_sdk/sdk_license.txt` and in
`THIRD_PARTY_LICENSES.txt`.

## truck-telemetry — layout reference

<https://pypi.org/project/truck-telemetry/>

Field offsets in `python/telemetry.py`, and from there `src/telemetry.rs`, were computed from this
package's revision 12 structure definitions. No code from the package is included.

## Slint — used under GPL-3.0-only (also offered under the Slint Royalty-free and Software licenses)

<https://slint.dev>, <https://github.com/slint-ui/slint>

The window toolkit compiled into `SCSHaptics.exe` (`slint` 1.18 and its `i-slint-*` crates,
with the winit backend and the software renderer). Slint is offered under GPL-3.0-only or one
of two Slint licenses; this program takes GPL-3.0. Because that is GPL-3.0 *only*, the
`SCSHaptics.exe` that combines it with this program's own GPL-3.0-or-later code is distributed
under GPL-3.0.

## HIDAPI — hidapi crate (MIT) and the HIDAPI C library (GPL-3.0, BSD or the original HIDAPI license)

<https://github.com/ruabmbua/hidapi-rs>, <https://github.com/libusb/hidapi>

All three executables talk to the controller through the `hidapi` crate, which compiles the
HIDAPI C library into them. The library's license lets its user choose among GPL-3.0, a BSD
license and the original HIDAPI license; all three texts are in `THIRD_PARTY_LICENSES.txt`.

## Other Rust crates

| Crate | License | Used for |
|---|---|---|
| `serde`, `serde_json` | MIT OR Apache-2.0 | `config.json`, the debug log's settings file, GitHub's release data |
| `indexmap` | Apache-2.0 OR MIT | keeping `config.json`'s keys in their order |
| `sha2` | MIT OR Apache-2.0 | checking a downloaded update against GitHub's SHA-256 |
| `windows-sys` | MIT OR Apache-2.0 | Windows API calls (WinHTTP, XInput, shared memory, registry, dialogs) |
| `raw-window-handle` | MIT OR Apache-2.0 OR Zlib | reaching the native window under Slint |
| `winit` (through Slint) | Apache-2.0 | the window and its input events |

The crates these pull in are under MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, BSL-1.0, Zlib,
Unlicense, CC0-1.0 and Unicode-3.0 (the ICU4X text-segmentation data) licenses, mostly offered as
a choice between two or three of them. `THIRD_PARTY_LICENSES.txt` names each one with the
texts.

## The Python implementation — not in the release executables

The Python files in `python/` are the program as released up to 0.19.0, kept as
the reference the Rust port is tested against (`tests/parity.rs`). Run from source they use
cython-hidapi (<https://github.com/trezor/cython-hidapi>, used under GPL-3.0) and CPython with
Tcl/Tk. Releases up to 0.19.0 bundled those with PyInstaller (bootloader under GPL-2.0 with the
bootloader exception); releases built from the Rust code contain none of them.
