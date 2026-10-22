# Third-party notices

ATS Haptics is licensed under the GNU General Public License v3.0 or later (see `LICENSE`).
It includes or is derived from the following works.

## ApexSenseBridge — GPL-3.0-or-later

<https://github.com/ReynArts/ApexSenseBridge>

The APEX 6 Pro HID protocol in `apex6.py` (motor enable `0x53`, realtime motor `0x57`, packet
layout, checksum and init sequence) was ported from ApexSenseBridge's
`src/flydigi/Apex6Protocol.cpp`, `src/flydigi/Apex5Device.cpp` and
`src/dualsense/Apex6HapticBridge.cpp`. ApexSenseBridge is distributed under the GNU General
Public License v3.0 or later.

## scs-sdk-plugin (RenCloud) — MIT

<https://github.com/RenCloud/scs-sdk-plugin>

`third_party/scs-sdk-plugin/scs-telemetry.dll` is the unmodified Win64 build from release
V.1.12.1 (`release_v_1_12_1.zip`, SHA-256
`3679ec81407de0ebfb45241e496e2ad31e30a6e409ef0246fc86195bfee9c9a5`; DLL SHA-256
`1d03dbc7a975e72203c60a7b9998021ceb8800b836bf28a131279979ad386cd4`). The shared-memory layout
read by `telemetry.py` follows that plugin's `scs-telemetry-common.hpp` (revision 12). Its MIT
license is in `third_party/scs-sdk-plugin/LICENSE`.

## truck-telemetry — layout reference

<https://pypi.org/project/truck-telemetry/>

Field offsets in `telemetry.py` were computed from this package's revision 12 structure
definitions. No code from the package is included.

## cython-hidapi — used under GPL-3.0 (also offered under BSD and the original HIDAPI license)

<https://github.com/trezor/cython-hidapi>

Bundled in the packaged executable as the `hid` module.

## Python — PSF License; Tcl/Tk — BSD-style license

The packaged executable contains the CPython 3.12 runtime and Tcl/Tk for the window, under their
respective licenses (<https://docs.python.org/3/license.html>, <https://www.tcl.tk/software/tcltk/license.html>).

## PyInstaller — GPL-2.0 with bootloader exception

<https://pyinstaller.org> — the bootloader in `ATSHaptics.exe` is distributed under the
PyInstaller bootloader exception, which permits distributing the bundled application under
its own license.
