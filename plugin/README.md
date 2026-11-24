# scs-telemetry.dll — SCS Haptics build

The telemetry plugin SCS Haptics installs into ATS / ETS2. It is
[RenCloud's scs-sdk-plugin](https://github.com/RenCloud/scs-sdk-plugin) at release V.1.12.1
(commit `d7216cb`, MIT), compiled against SCS Telemetry SDK 1.16, with the changes below.

**The shared memory keeps revision 12's layout.** Every field is at the offset RenCloud's release
has it, the mapping is still `Local\SCSTelemetry` and the revision number is still 12, so a
program written for RenCloud's release reads this build unchanged, and SCS Haptics reads
RenCloud's release unchanged. `inc/scs-telemetry-common.hpp` ends in `static_assert`s on the
offsets: a change that moves a field does not compile.

| Folder | What |
|---|---|
| `src`, `inc`, `scs-telemetry.def` | the plugin (RenCloud's `scs-telemetry`), changes marked `SCS Haptics build` |
| `scs_sdk` | SCS Telemetry SDK 1.16 headers, unchanged (MIT, `sdk_license.txt`) |
| `test/host.cpp` | loads a plugin DLL as the game does and checks what it writes |

## Changes from RenCloud V.1.12.1

1. **A frame's time is written after its values.** RenCloud's build writes `time`,
   `simulatedTime` and `renderTime` when the frame starts and each channel afterwards, with
   nothing to stop a reader in between: a reader that takes a new time as a new frame can get it
   with the frame before's values, and then never sees this frame's. The times now go out on the
   SDK's `frame_end` event.
2. **Shutdown twice no longer writes to unmapped memory.** The game calls
   `scs_telemetry_shutdown`, then `DllMain` calls it again as the DLL unloads; the second call
   wrote through the pointer the first had unmapped
   ([upstream issue 128](https://github.com/RenCloud/scs-sdk-plugin/issues/128)).
3. **SDK 1.15's car and bus jobs are left alone.** Games from 1.61 send `car_job` and `bus_job`
   configurations and `car_job.*` events to every plugin. RenCloud's build warns about each in
   `game.log.txt`, reads the configurations as substances and the events as a cancelled truck
   job (a car job's cancel penalty lands in `jobCancelledPenalty`). They are now ignored: the
   revision 12 layout has no place for them. Anything else unknown is still warned about, and
   no longer stored.
4. **Each trailer's wheel lift goes to its own slot.** `wheel.lift` and `wheel.lift.offset` of
   all ten trailers were registered into trailer 0.
5. **A trailer whose configuration changes keeps its channel values.** Each `trailer.N`
   configuration event used to zero the whole trailer, `attached`, wheels on ground and
   deflections included, and the game sends a channel only when its value changes: a trailer
   whose configuration changes while it is hooked up (cargo going into an owned trailer) read as
   unhooked until the game sent those again. Now only what the configuration fills is cleared; a
   trailer that is gone (an empty configuration) is still zeroed whole.
6. **New: minutes to the next mandatory break** (SDK 1.15's `mandatory.break`), as an `int` at
   offset 644, the first four bytes of zone 3's spare room.
7. **New: the build number** (`PLUGIN_BUILD`), as an `unsigned int` at offset 452, the first four
   bytes of zone 2's spare room. RenCloud's release leaves 0 there, so a reader can tell which
   build feeds it; SCS Haptics shows it in its status line and writes it to a debug log's
   settings file.
8. The plugin reports the telemetry versions of SDK 1.16 (ETS2 1.21, ATS 1.08) and writes one
   line with its build name to the game log when it starts. The C runtime is linked in, so the
   game needs no particular Visual C++ runtime for it.

Not changed: the gameplay event flags (`jobDelivered`, `fined`, `tollgate`, `ferry`, `train`)
still flip at each event and stay, as RenCloud's readers expect since revision 11. SCS Haptics
turns a flip into an event on its side (`EventPulses` in `src/telemetry.rs`).

## Building

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_plugin.ps1
```

Needs the Visual Studio C++ build tools. It compiles the plugin and the test host into
`target\plugin`, runs the host against the new DLL as ETS2 and as ATS, and only then copies the
DLL to `resources\scs-sdk-plugin\scs-telemetry.dll`, which `SCSHaptics.exe` embeds. The build
takes no time stamps (`/Brepro`): the same source and compiler give the same DLL.

After a change to the plugin, raise `PLUGIN_BUILD` in `inc/scs-telemetry-common.hpp`: the build
name is how the app's plugin window tells one build from another.

## The test host

```powershell
target\plugin\host.exe <scs-telemetry.dll> [--ats] [--dump <file>] [--check]
```

It plays configurations, five frames with a value for every registered channel, gameplay events,
a trailer's configuration changing, a trailer going away and two shutdowns into the DLL, reads `Local\SCSTelemetry` from outside as the app does, and
prints what it found. `--check` fails unless every finding is what this build should give.
`--dump` saves the 21,600 bytes of the memory map after the frames; two builds given the same
session can then be compared byte by byte. Against RenCloud's own V.1.12.1 DLL the dumps differ
in five places only: the telemetry version number (offset 60), the build number (452), the
mandatory break (644) and the wheel-lift slots of trailers 0 and 1.
