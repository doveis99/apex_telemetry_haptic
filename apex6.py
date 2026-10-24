"""Flydigi APEX 6 Pro realtime voice-coil output.

Packet layout and init sequence are ported from ApexSenseBridge
(GPL-3.0-or-later, see THIRD_PARTY_NOTICES.md):
src/flydigi/Apex6Protocol.cpp, src/flydigi/Apex5Device.cpp and
src/dualsense/Apex6HapticBridge.cpp.

Stream model: every 8 ms one 33-byte report carries eight 1 kHz subframes,
each with a trigger, left-grip and right-grip sample (signed, +-112 max).
The trigger channel is a single waveform routed to left, right or both.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Sequence

import hid

from i18n import t

VENDOR_ID = 0x37D7
PRODUCT_ID = 0x2502
USAGE_PAGE = 0xFFA0

REPORT_SIZE = 33
MAGIC = (0x5A, 0xA5)
CMD_GET_INFO = 0x01
CMD_MOTOR_ROUTE = 0x53
CMD_REALTIME_MOTOR = 0x57

PRO_DEVICE_TYPES = {0x96: "APEX 6 Pro", 0x98: "APEX 6 Pro (Phantom Blade Zero)"}
FEATURE_GRIP_HAPTIC = 0x80
FEATURE_TRIGGER_HAPTIC = 0x10
# Protocol-frame offsets of the big-endian u16 versions in the 0x01 reply, in
# the order Space Station's parser reads them (fwMain, fwRecv, fwSi,
# fwTrigger, fwScreen, fwAdc, fwSstar; the page labels fwSstar "RF").
FIRMWARE_OFFSETS = {"controller": 15, "dongle": 17, "si": 19, "trigger": 21, "screen": 23, "adc": 25, "rf": 27}

SAMPLE_RATE = 1000
SUBFRAMES = 8
FRAME_PERIOD = SUBFRAMES / SAMPLE_RATE  # 8 ms
MAX_SAMPLE = 112  # ASB never exceeds this; keeps headroom below the int8 limit
ENABLE_DELAY = 0.094
IDLE_KEEPALIVE_FRAMES = 6
# The firmware drops the motor route when something else configures the
# motors (the game or Steam initialising the gamepad as it loads) or when the
# controller sleeps and re-pairs with the dongle. Realtime writes keep
# succeeding into the void, so nothing fails and only a fresh start used to
# recover. Re-send the route while streaming: often during silence, where a
# hiccup cannot be felt, and rarely while vibrating.
REASSERT_IDLE_S = 1.0
REASSERT_ACTIVE_S = 4.0


class Route(IntEnum):
    LEFT = 0
    RIGHT = 1
    BOTH = 2
    MUTE = 3


def _checksum(report: Sequence[int]) -> int:
    # Sum of protocol-frame bytes 2..30, i.e. report bytes 3..31.
    return sum(report[3:32]) & 0xFF


def build_command(command: int, payload: bytes = b"") -> bytes:
    payload = bytes(payload[:27])
    report = bytearray(REPORT_SIZE)
    report[1], report[2] = MAGIC
    report[3] = command
    report[4] = len(payload) + 2
    report[5:5 + len(payload)] = payload
    report[32] = _checksum(report)
    return bytes(report)


DEFAULT_TRIGGER_MODE = 0x02  # third byte of the 0x12 route frame as sent by ASB and xHaptics


def motor_enable_reports(trigger_mode: int = DEFAULT_TRIGGER_MODE) -> tuple[bytes, bytes]:
    # The second frame's mode byte is the only field varied in experiments;
    # its meaning is not documented, the default is the captured value.
    return (
        build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x02, 0x03, 0x00, 0x40])),
        build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x12, trigger_mode, 0x40, 0x00])),
    )


MOTOR_ENABLE = motor_enable_reports()
MOTOR_DISABLE = (
    build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x02, 0x00])),
    build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x12, 0x00])),
)


def _encode(sample: float) -> int:
    value = int(round(sample))
    value = max(-MAX_SAMPLE, min(MAX_SAMPLE, value))
    return value + 128  # 0x80 is electrical zero


@dataclass
class Block:
    """Eight 1 kHz subframes for the three output channels."""

    trigger: Sequence[float] = field(default_factory=lambda: [0.0] * SUBFRAMES)
    left_grip: Sequence[float] = field(default_factory=lambda: [0.0] * SUBFRAMES)
    right_grip: Sequence[float] = field(default_factory=lambda: [0.0] * SUBFRAMES)
    route: Route = Route.MUTE
    # None: set the trigger-valid bit only when routed. True/False forces it,
    # for diagnostics such as "trigger samples with route MUTE".
    trigger_bit: bool | None = None

    def is_silent(self) -> bool:
        return not any(self.trigger) and not any(self.left_grip) and not any(self.right_grip)


def build_realtime(block: Block, enable_trigger: bool = True, enable_grips: bool = True) -> bytes:
    if block.trigger_bit is not None:
        enable_trigger = block.trigger_bit
    else:
        enable_trigger = enable_trigger and block.route != Route.MUTE
    payload = bytearray(25)
    payload[0] = (0x80
                  | (0x10 if enable_grips else 0)
                  | (0x08 if enable_grips else 0)
                  | (0x04 if enable_trigger else 0)
                  | int(block.route))
    for i in range(SUBFRAMES):
        offset = 1 + i * 3
        payload[offset] = _encode(block.trigger[i]) if enable_trigger else 0x80
        payload[offset + 1] = _encode(block.left_grip[i]) if enable_grips else 0x80
        payload[offset + 2] = _encode(block.right_grip[i]) if enable_grips else 0x80
    return build_command(CMD_REALTIME_MOTOR, bytes(payload))


SILENT_REPORT = build_realtime(Block(route=Route.MUTE), enable_trigger=False)


@dataclass
class DeviceInfo:
    device_type: int
    connection_mode: int
    features: int
    # The whole 0x01 reply, for diagnostics.
    raw: bytes = b""

    @property
    def firmware(self) -> dict[str, str]:
        """Component firmware versions, one nibble per digit (0x90 0x55 -> 9.0.5.5).

        Components reporting 0 are absent (the dongle on a cable, for one) and
        left out, as Space Station's device page does.
        """
        if len(self.raw) < 29:
            return {}
        return {name: ".".join(f"{self.raw[i]:02X}{self.raw[i + 1]:02X}")
                for name, i in FIRMWARE_OFFSETS.items() if self.raw[i] or self.raw[i + 1]}

    @property
    def name(self) -> str:
        return PRO_DEVICE_TYPES.get(self.device_type, f"unknown 0x{self.device_type:02X}")

    @property
    def is_pro(self) -> bool:
        return self.device_type in PRO_DEVICE_TYPES

    @property
    def grip_haptics(self) -> bool:
        return bool(self.features & FEATURE_GRIP_HAPTIC)

    @property
    def trigger_haptics(self) -> bool:
        return bool(self.features & FEATURE_TRIGGER_HAPTIC)


def _protocol_frame(data: Sequence[int]) -> list[int] | None:
    # hidapi may or may not keep the leading report-ID 0 byte.
    if len(data) >= REPORT_SIZE and data[0] == 0 and (data[1], data[2]) == MAGIC:
        return list(data[1:REPORT_SIZE])
    if len(data) >= REPORT_SIZE - 1 and (data[0], data[1]) == MAGIC:
        return list(data[:REPORT_SIZE - 1])
    return None


def _parse_device_info(data: Sequence[int]) -> DeviceInfo | None:
    frame = _protocol_frame(data)
    if frame is None or frame[2] != CMD_GET_INFO:
        return None
    if frame[31] != sum(frame[2:31]) & 0xFF:
        return None
    return DeviceInfo(device_type=frame[5], connection_mode=frame[6], features=frame[29], raw=bytes(frame))


class DeviceUnavailable(RuntimeError):
    """Controller absent, asleep or disconnected; worth retrying later."""


def find_path() -> bytes:
    for entry in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if entry["usage_page"] == USAGE_PAGE:
            return entry["path"]
    if _steam_overlay_loaded():
        raise DeviceUnavailable(t("컨트롤러 안 보임: Steam이 실행한 프로세스라 Steam Input이 패드를 숨김 "
                                  "(창을 따로 실행하거나 게임의 Steam Input 끄기)"))
    raise DeviceUnavailable(t("컨트롤러 없음 (유선/2.4G 동글 연결, 전원 확인)"))


def _steam_overlay_loaded() -> bool:
    """True inside a process Steam injected its overlay into (see app.launch)."""
    try:
        import ctypes
        get_module = ctypes.windll.kernel32.GetModuleHandleW
        get_module.restype = ctypes.c_void_p
        return bool(get_module("gameoverlayrenderer64.dll"))
    except (AttributeError, OSError):
        return False


@dataclass
class StreamStats:
    frames: int = 0
    late_frames: int = 0  # started more than one period behind schedule
    max_lateness_ms: float = 0.0
    max_write_ms: float = 0.0
    total_write_ms: float = 0.0

    def summary(self) -> str:
        avg = self.total_write_ms / self.frames if self.frames else 0.0
        return (f"frames={self.frames} late={self.late_frames} "
                f"max_lateness={self.max_lateness_ms:.2f}ms "
                f"write avg={avg:.2f}ms max={self.max_write_ms:.2f}ms")


class Apex6:
    """Open the vendor interface, verify identity and stream motor blocks.

    Use as a context manager so the motors are always muted and released.
    """

    def __init__(self) -> None:
        self._dev = hid.device()
        self.info: DeviceInfo | None = None
        self._enabled = False
        self._trigger_mode = DEFAULT_TRIGGER_MODE

    def __enter__(self) -> "Apex6":
        try:
            self._dev.open_path(find_path())
        except OSError as exc:
            raise DeviceUnavailable(t("컨트롤러를 열 수 없음 ({error})", error=exc)) from exc
        try:
            self.info = self.identify()
        except BaseException:
            self._dev.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._enabled:
                self.disable()
        except (DeviceUnavailable, OSError):
            pass  # unplugged mid-session: nothing left to mute
        finally:
            self._dev.close()

    def _write(self, report: bytes) -> None:
        try:
            written = self._dev.write(report)
        except OSError as exc:
            raise DeviceUnavailable(t("컨트롤러 연결 끊김 ({error})", error=exc)) from exc
        if written < 0:
            raise DeviceUnavailable(t("컨트롤러 연결 끊김 ({error})", error=self._dev.error()))

    def identify(self, timeout: float = 0.6) -> DeviceInfo:
        # Drop stale input before the exchange. cython-hidapi treats a 0 ms
        # timeout as "block forever", so poll with 1 ms instead.
        try:
            for _ in range(128):
                if not self._dev.read(64, 1):
                    break
            self._write(build_command(CMD_GET_INFO))
            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                data = self._dev.read(64, 50)
                if data and (info := _parse_device_info(data)):
                    if not info.is_pro:
                        raise RuntimeError(t("APEX 6 Pro가 아님 (device type 0x{type})", type=f"{info.device_type:02X}"))
                    return info
        except OSError as exc:
            raise DeviceUnavailable(t("컨트롤러 연결 끊김 ({error})", error=exc)) from exc
        # The 2.4 GHz dongle stays enumerated while the controller sleeps.
        raise DeviceUnavailable(t("컨트롤러 응답 없음 (절전 중이면 버튼을 눌러 깨우세요)"))

    def enable(self, trigger_mode: int = DEFAULT_TRIGGER_MODE) -> None:
        self._trigger_mode = trigger_mode
        self.reassert()
        self._enabled = True
        time.sleep(ENABLE_DELAY)

    def reassert(self) -> None:
        """Send the motor route again; harmless when it is still in place."""
        for report in motor_enable_reports(self._trigger_mode):
            self._write(report)

    def reconfigure(self, trigger_mode: int) -> None:
        if self._enabled:
            self.disable()
        self.enable(trigger_mode)

    def disable(self) -> None:
        for _ in range(3):
            self._write(SILENT_REPORT)
            time.sleep(FRAME_PERIOD)
        for report in MOTOR_DISABLE:
            self._write(report)
        self._enabled = False

    def write_block(self, block: Block) -> None:
        self._write(build_realtime(block))

    def stream(self, render: Callable[[int], Block | None], max_seconds: float | None = None) -> StreamStats:
        """Call render(first_sample_index) every 8 ms and send its block.

        render returns None to stop. Writes are scheduled on absolute
        deadlines so one late frame does not shift every later one.
        """
        if not self._enabled:
            self.enable()
        stats = StreamStats()
        sample_index = 0
        silent_streak = 0
        start = time.perf_counter()
        deadline = start
        last_reassert = start
        while max_seconds is None or deadline - start < max_seconds:
            block = render(sample_index)
            if block is None:
                break
            now = time.perf_counter()
            lateness = (now - deadline) * 1000.0
            stats.max_lateness_ms = max(stats.max_lateness_ms, lateness)
            if lateness > FRAME_PERIOD * 1000.0:
                stats.late_frames += 1
                deadline = now  # resync instead of bursting to catch up
            # Like ASB: after a run of silent blocks only send a keepalive
            # every ~48 ms to spare the 2.4 GHz link.
            silent_streak = silent_streak + 1 if block.is_silent() else 0
            if silent_streak <= SUBFRAMES or silent_streak % IDLE_KEEPALIVE_FRAMES == 0:
                self.write_block(block)
                write_ms = (time.perf_counter() - now) * 1000.0
                stats.total_write_ms += write_ms
                stats.max_write_ms = max(stats.max_write_ms, write_ms)
                stats.frames += 1
            if now - last_reassert >= (REASSERT_IDLE_S if silent_streak else REASSERT_ACTIVE_S):
                self.reassert()
                last_reassert = now
            sample_index += SUBFRAMES
            deadline += FRAME_PERIOD
            remaining = deadline - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        return stats
