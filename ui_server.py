"""Local browser UI and JSON API for Concert LED Bracelet Studio.

The server binds to loopback only. RF transmission has two independent gates:
the process must be started with ``--allow-transmit`` and the browser request
must contain the deliberate arm/confirmation values used by the UI.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Callable
import webbrowser

from controller import (
    ATTACK_TIMES_MS,
    HOLD_TIMES_MS,
    MODE_FOREVER,
    MODE_ONE_SHOT,
    MODE_NAMES,
    PRESETS,
    RANDOM_PERCENT,
    RELEASE_TIMES_MS,
    WavebandCommand,
    color_command,
    mode_name,
)
from hackrf_iq import plan_waveform, stream_to_hackrf
from pixmob_protocol import decode_waveband_pulses

try:
    from music_sync import AudioMonitor, PALETTES, list_audio_inputs
except ImportError:  # pragma: no cover - optional music dependency boundary
    AudioMonitor = None
    PALETTES = {}
    list_audio_inputs = None


_ENGINEERING_NUMBER = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<prefix>[kKmMgG]?)\s*(?:[hH][zZ])?\s*$"
)
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/wristband.png": ("wristband.png", "image/png"),
}


class UIRequestError(ValueError):
    """A safe, user-displayable API request error."""


class TransmitDisabledError(PermissionError):
    """RF was requested without satisfying all safety gates."""


def probe_hackrf_device(
    transfer_executable: str, serial_number: str | None = None
) -> tuple[bool, str | None]:
    """Use the sibling HackRF utility for a passive device-presence preflight."""

    transfer_path = Path(transfer_executable)
    candidates = [
        transfer_path.with_name("hackrf_info.exe"),
        transfer_path.with_name("hackrf_info"),
    ]
    path_tool = shutil.which("hackrf_info")
    if path_tool:
        candidates.append(Path(path_tool))
    info_tool = next((candidate for candidate in candidates if candidate.exists()), None)
    if info_tool is None:
        return True, None
    command = [str(info_tool)]
    if serial_number:
        command.extend(("-d", serial_number))
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"HackRF presence check failed: {exc}"
    detail = " ".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode == 0 and "Found HackRF" in detail:
        return True, None
    if "No HackRF boards found" in detail or "HackRF not found" in detail:
        return False, "HackRF One is not connected. Reconnect its USB cable and try again; passive monitoring is still running."
    return False, f"HackRF presence check failed{': ' + detail if detail else ''}"


FLOW_MAX_DEPTH = 4
FLOW_MAX_BLOCKS = 100
FLOW_MAX_EXPANDED_STEPS = 500
FLOW_MAX_DURATION_US = 10 * 60 * 1_000_000


@dataclass(frozen=True)
class PlannedFlowStep:
    """One flattened, validated action in a visual controller flow."""

    kind: str
    source_id: str
    label: str
    duration_us: int
    command: WavebandCommand | None = None
    waveform: Any | None = None
    frame: Any | None = None
    is_wake: bool = False


@dataclass(frozen=True)
class UIConfig:
    frequency_hz: int = 915_000_000
    sample_rate_hz: int = 2_000_000
    amplitude: int = 64
    tx_gain_db: int = 0
    symbol_us: int = 510
    normal_repeat_count: int = 8
    normal_gap_us: int = 34_100
    wake_duration_s: float = 20.0
    wake_gap_us: int = 4_080
    allow_transmit: bool = False
    allow_persistent: bool = False
    hackrf_transfer: str = "hackrf_transfer"
    serial_number: str | None = None

    def __post_init__(self) -> None:
        if self.frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        if self.sample_rate_hz < 2_000_000 or self.sample_rate_hz > 20_000_000:
            raise ValueError("sample_rate_hz must be in the range 2..20 MHz")
        if self.amplitude < 1 or self.amplitude > 127:
            raise ValueError("amplitude must be in the range 1..127")
        if self.tx_gain_db < 0 or self.tx_gain_db > 47:
            raise ValueError("tx_gain_db must be in the range 0..47")
        if self.normal_repeat_count < 1:
            raise ValueError("normal_repeat_count must be at least 1")
        if self.normal_gap_us < 0 or self.wake_gap_us < 0:
            raise ValueError("packet gaps must not be negative")
        if not math.isfinite(self.wake_duration_s) or self.wake_duration_s <= 0:
            raise ValueError("wake_duration_s must be positive")


def _bounded_int(
    data: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = data.get(name, default)
    if isinstance(value, bool):
        raise UIRequestError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise UIRequestError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise UIRequestError(f"{name} must be in the range {minimum}..{maximum}")
    return parsed


def _time_label(value: int | None, *, none_label: str) -> str:
    return none_label if value is None else f"{value} ms"


def _hex_bytes(values: tuple[int, ...]) -> str:
    return " ".join(f"{value:02X}" for value in values)


class ControllerService:
    """Pure request planning plus an injectable RF execution boundary."""

    def __init__(
        self,
        config: UIConfig,
        *,
        streamer: Callable[..., int] = stream_to_hackrf,
        waiter: Callable[[float], Any] = time.sleep,
        music_monitor: Any | None = None,
        hackrf_probe: Callable[[str, str | None], tuple[bool, str | None]] = probe_hackrf_device,
    ) -> None:
        self.config = config
        self._streamer = streamer
        self._waiter = waiter
        self._hackrf_probe = hackrf_probe
        self._transmit_lock = threading.Lock()
        self._music_monitor = (
            music_monitor
            if music_monitor is not None
            else AudioMonitor() if AudioMonitor is not None else None
        )
        self._music_lock = threading.Lock()
        self._music_settings: dict[str, Any] = {}
        self._music_transmissions = 0
        self._music_retries = 0

    def music_devices(self) -> dict[str, Any]:
        if list_audio_inputs is None:
            return {
                "available": False,
                "devices": [],
                "error": "install concert-led-bracelet-studio[music]",
            }
        devices = list_audio_inputs()
        return {"available": True, "devices": devices, "error": None}

    def music_status(self) -> dict[str, Any]:
        if self._music_monitor is None:
            return {
                "available": False,
                "running": False,
                "transmit": False,
                "error": "Music Mode dependencies are unavailable",
                "frame": None,
                "settings": {},
                "transmissionCount": 0,
                "retryCount": 0,
            }
        status = self._music_monitor.status()
        with self._music_lock:
            settings = dict(self._music_settings)
            transmissions = self._music_transmissions
            retries = self._music_retries
        return {
            "available": True,
            **status,
            "settings": settings,
            "transmissionCount": transmissions,
            "retryCount": retries,
        }

    def music_start(self, data: dict[str, Any]) -> dict[str, Any]:
        if self._music_monitor is None:
            raise UIRequestError("Music Mode dependencies are unavailable")
        device_id = _bounded_int(data, "deviceId", -1, 0, 10_000)
        sensitivity = _bounded_int(data, "sensitivity", 70, 1, 100)
        brightness = _bounded_int(data, "brightness", 80, 1, 100)
        min_interval_ms = _bounded_int(data, "minIntervalMs", 1_000, 300, 5_000)
        tx_gain_db = _bounded_int(data, "txGainDb", self.config.tx_gain_db, 0, 47)
        palette = str(data.get("palette", "spectrum")).strip().lower()
        if palette not in PALETTES:
            raise UIRequestError("palette must be spectrum, neon, warm, or cool")
        transmit = data.get("transmit") is True
        executable: str | None = None
        if transmit:
            if not self.config.allow_transmit:
                raise TransmitDisabledError(
                    "RF transmit is disabled; restart the UI with --allow-transmit"
                )
            if data.get("armed") is not True or data.get("confirmation") != "TRANSMIT":
                raise TransmitDisabledError("arm RF transmit before starting Music Sync")
            executable = shutil.which(self.config.hackrf_transfer)
            if executable is None and Path(self.config.hackrf_transfer).exists():
                executable = str(Path(self.config.hackrf_transfer).resolve())
            if executable is None:
                raise FileNotFoundError(
                    f"hackrf_transfer was not found: {self.config.hackrf_transfer}"
                )
            device_available, device_error = self._hackrf_probe(
                executable, self.config.serial_number
            )
            if not device_available:
                raise FileNotFoundError(
                    device_error
                    or "HackRF One is not connected. Reconnect its USB cable and try again."
                )

        with self._music_lock:
            self._music_settings = {
                "deviceId": device_id,
                "sensitivity": sensitivity,
                "brightness": brightness,
                "minIntervalMs": min_interval_ms,
                "palette": palette,
                "txGainDb": tx_gain_db,
            }
            self._music_transmissions = 0
            self._music_retries = 0

        def on_beat(_frame: Any, rgb: tuple[int, int, int]) -> None:
            if not transmit or executable is None:
                return
            if not self._transmit_lock.acquire(blocking=False):
                return
            try:
                command = color_command(
                    *rgb,
                    name="music-beat",
                    attack=0,
                    hold=2,
                    release=4,
                    mode=MODE_ONE_SHOT,
                )
                waveform = plan_waveform(
                    command.pulses(symbol_us=self.config.symbol_us), repeat_count=1
                )
                for attempt in range(3):
                    try:
                        self._streamer(
                            waveform,
                            frequency_hz=self.config.frequency_hz,
                            sample_rate_hz=self.config.sample_rate_hz,
                            amplitude=self.config.amplitude,
                            tx_gain_db=tx_gain_db,
                            executable=executable,
                            serial_number=self.config.serial_number,
                        )
                        break
                    except RuntimeError as exc:
                        transient = any(
                            marker in str(exc).lower()
                            for marker in (
                                "hackrf not found (-5)",
                                "pipe error (-1000)",
                                "resource busy",
                            )
                        )
                        if not transient or attempt == 2:
                            raise
                        with self._music_lock:
                            self._music_retries += 1
                        self._waiter(0.5 * (attempt + 1))
                with self._music_lock:
                    self._music_transmissions += 1
                # Give WinUSB a short handle-release window before another beat.
                self._waiter(0.2)
            finally:
                self._transmit_lock.release()

        self._music_monitor.start(
            device_id=device_id,
            sensitivity=sensitivity,
            cooldown_ms=min_interval_ms,
            palette=palette,
            brightness=brightness,
            transmit=transmit,
            on_beat=on_beat,
        )
        return self.music_status()

    def music_stop(self) -> dict[str, Any]:
        if self._music_monitor is not None:
            self._music_monitor.stop()
        return self.music_status()

    def public_config(self) -> dict[str, Any]:
        executable = shutil.which(self.config.hackrf_transfer)
        if executable is None and Path(self.config.hackrf_transfer).exists():
            executable = str(Path(self.config.hackrf_transfer).resolve())
        return {
            "frequencyHz": self.config.frequency_hz,
            "sampleRateHz": self.config.sample_rate_hz,
            "amplitude": self.config.amplitude,
            "txGainDb": self.config.tx_gain_db,
            "symbolUs": self.config.symbol_us,
            "normalRepeatCount": self.config.normal_repeat_count,
            "normalGapUs": self.config.normal_gap_us,
            "wakeDurationS": self.config.wake_duration_s,
            "wakeGapUs": self.config.wake_gap_us,
            "transmitAllowed": self.config.allow_transmit,
            "persistentAllowed": self.config.allow_persistent,
            "hackrfTransferFound": executable is not None,
            "hackrfTransfer": executable or self.config.hackrf_transfer,
        }

    def preset_data(self) -> list[dict[str, Any]]:
        names = (
            "red",
            "green",
            "blue",
            "white",
            "gold",
            "fade-gold",
            "off",
            "purple",
            "cyan",
            "fade-red",
            "fade-blue",
            "fade-white",
        )
        output = []
        for name in names:
            command = PRESETS[name]
            output.append(
                {
                    "name": name,
                    "label": name.replace("-", " ").title(),
                    "red": command.red,
                    "green": command.green,
                    "blue": command.blue,
                    "attack": command.attack,
                    "hold": command.hold,
                    "release": command.release,
                    "random": command.random,
                    "group": command.group,
                    "mode": mode_name(command.mode),
                    "description": command.description,
                }
            )
        output.insert(
            7,
            {
                "name": "wake",
                "label": "Wake",
                "red": 0,
                "green": 0,
                "blue": 0,
                "attack": PRESETS["keepalive"].attack,
                "hold": PRESETS["keepalive"].hold,
                "release": PRESETS["keepalive"].release,
                "random": PRESETS["keepalive"].random,
                "group": PRESETS["keepalive"].group,
                "mode": mode_name(PRESETS["keepalive"].mode),
                "description": "repeat the upstream keepalive frame",
            },
        )
        return output

    def _command_from_request(self, data: dict[str, Any]) -> tuple[WavebandCommand, bool]:
        target = str(data.get("target", "custom")).strip().lower()
        is_wake = target == "wake"
        base = PRESETS["keepalive"] if is_wake else None

        mode_value = str(data.get("mode", "continuous")).strip().lower()
        if mode_value not in MODE_NAMES:
            raise UIRequestError("mode must be continuous, one-shot, or forever")
        mode = MODE_NAMES[mode_value]
        if mode == MODE_FOREVER and not self.config.allow_persistent:
            raise UIRequestError(
                "forever mode is locked; restart with --allow-persistent to enable it"
            )

        command = WavebandCommand(
            name=target,
            red=base.red if base is not None else _bounded_int(data, "red", 255, 0, 255),
            green=base.green if base is not None else _bounded_int(data, "green", 0, 0, 255),
            blue=base.blue if base is not None else _bounded_int(data, "blue", 0, 0, 255),
            attack=base.attack if base is not None else _bounded_int(data, "attack", 1, 0, 7),
            hold=base.hold if base is not None else _bounded_int(data, "hold", 2, 0, 7),
            release=base.release if base is not None else _bounded_int(data, "release", 2, 0, 7),
            random=base.random if base is not None else _bounded_int(data, "random", 0, 0, 7),
            group=_bounded_int(data, "group", 0, 0, 31),
            mode=base.mode if base is not None else mode,
            description="UI-generated command",
        )
        return command, is_wake

    def plan(self, data: dict[str, Any]) -> tuple[WavebandCommand, Any, Any, bool]:
        command, is_wake = self._command_from_request(data)
        pulses = command.pulses(symbol_us=self.config.symbol_us)
        if is_wake:
            raw_duration = data.get("wakeDurationS", self.config.wake_duration_s)
            try:
                duration = float(raw_duration)
            except (TypeError, ValueError) as exc:
                raise UIRequestError("wakeDurationS must be a number") from exc
            if not math.isfinite(duration) or duration < 1 or duration > 60:
                raise UIRequestError("wakeDurationS must be in the range 1..60")
            waveform = plan_waveform(
                pulses,
                repeat_duration_s=duration,
                inter_packet_delay_us=self.config.wake_gap_us,
            )
        else:
            repeat_count = _bounded_int(
                data,
                "repeatCount",
                self.config.normal_repeat_count,
                1,
                100,
            )
            waveform = plan_waveform(
                pulses,
                repeat_count=repeat_count,
                inter_packet_delay_us=self.config.normal_gap_us,
            )
        frame = decode_waveband_pulses(
            pulses,
            symbol_us=self.config.symbol_us,
            tolerance_us=0,
        )
        return command, waveform, frame, is_wake

    def preview(self, data: dict[str, Any]) -> dict[str, Any]:
        command, waveform, frame, is_wake = self.plan(data)
        red6, green6, blue6 = command.quantized_rgb
        return {
            "target": "wake" if is_wake else command.name,
            "wake": is_wake,
            "rgb": [command.red, command.green, command.blue],
            "quantizedRgb": [red6, green6, blue6],
            "mode": mode_name(command.mode),
            "group": command.group,
            "effect": {
                "attack": command.attack,
                "attackLabel": _time_label(ATTACK_TIMES_MS[command.attack], none_label="0 ms"),
                "hold": command.hold,
                "holdLabel": _time_label(HOLD_TIMES_MS[command.hold], none_label="infinite"),
                "release": command.release,
                "releaseLabel": _time_label(
                    RELEASE_TIMES_MS[command.release], none_label="background"
                ),
                "random": command.random,
                "randomLabel": f"{RANDOM_PERCENT[command.random]}%",
            },
            "logicalPayload": _hex_bytes(command.payload_values),
            "onAirBytes": _hex_bytes(frame.on_air_bytes),
            "crc12": f"{frame.crc12:03X}",
            "crcValid": frame.crc_valid,
            "symbols": list(frame.symbols),
            "pulsesUs": list(command.pulses(symbol_us=self.config.symbol_us)),
            "frameSymbols": len(frame.symbols),
            "packetDurationUs": waveform.packet_duration_us,
            "repeatCount": waveform.repeat_count,
            "totalDurationUs": waveform.duration_us,
            "interPacketDelayUs": waveform.inter_packet_delay_us,
            "rf": self.public_config(),
        }

    def _expand_flow_blocks(
        self,
        blocks: Any,
        *,
        depth: int = 0,
    ) -> list[PlannedFlowStep]:
        if not isinstance(blocks, list):
            raise UIRequestError("flow blocks must be a list")
        if not blocks:
            raise UIRequestError("add at least one block to the flow")
        if len(blocks) > FLOW_MAX_BLOCKS:
            raise UIRequestError(f"a flow level may contain at most {FLOW_MAX_BLOCKS} blocks")
        if depth > FLOW_MAX_DEPTH:
            raise UIRequestError(f"loops may be nested at most {FLOW_MAX_DEPTH} levels")

        result: list[PlannedFlowStep] = []
        for index, raw_block in enumerate(blocks):
            if not isinstance(raw_block, dict):
                raise UIRequestError(f"flow block {index + 1} must be an object")
            kind = str(raw_block.get("type", "")).strip().lower()
            source_id = str(raw_block.get("id", f"block-{index + 1}"))[:80]
            label = str(raw_block.get("label", kind.replace("-", " ").title()))[:80]

            if kind == "loop":
                loop_count = _bounded_int(raw_block, "count", 2, 1, 100)
                loop_delay_ms = _bounded_int(raw_block, "loopDelayMs", 0, 0, 60_000)
                child_steps = self._expand_flow_blocks(
                    raw_block.get("children", []),
                    depth=depth + 1,
                )
                projected = len(result) + loop_count * len(child_steps) + max(0, loop_count - 1)
                if projected > FLOW_MAX_EXPANDED_STEPS:
                    raise UIRequestError(
                        f"expanded flow may contain at most {FLOW_MAX_EXPANDED_STEPS} actions"
                    )
                for iteration in range(loop_count):
                    result.extend(child_steps)
                    if iteration + 1 < loop_count and loop_delay_ms:
                        result.append(
                            PlannedFlowStep(
                                kind="wait",
                                source_id=source_id,
                                label=f"{label} delay",
                                duration_us=loop_delay_ms * 1_000,
                            )
                        )
                continue

            if kind == "wait":
                duration_ms = _bounded_int(raw_block, "durationMs", 500, 0, 60_000)
                result.append(
                    PlannedFlowStep(
                        kind="wait",
                        source_id=source_id,
                        label=label or "Wait",
                        duration_us=duration_ms * 1_000,
                    )
                )
                continue

            if kind not in {"color", "fade", "wake", "off"}:
                raise UIRequestError(
                    "flow block type must be color, fade, wait, loop, wake, or off"
                )

            command_data = dict(raw_block)
            if kind == "wake":
                command_data["target"] = "wake"
            elif kind == "off":
                off = PRESETS["off"]
                command_data.update(
                    {
                        "target": "off",
                        "red": off.red,
                        "green": off.green,
                        "blue": off.blue,
                        "attack": off.attack,
                        "hold": off.hold,
                        "release": off.release,
                        "random": off.random,
                        "mode": mode_name(off.mode),
                    }
                )
            else:
                command_data.setdefault("target", "custom")

            command, waveform, frame, is_wake = self.plan(command_data)
            result.append(
                PlannedFlowStep(
                    kind="transmit",
                    source_id=source_id,
                    label=label or ("Wake" if is_wake else command.name),
                    duration_us=waveform.duration_us,
                    command=command,
                    waveform=waveform,
                    frame=frame,
                    is_wake=is_wake,
                )
            )
            if len(result) > FLOW_MAX_EXPANDED_STEPS:
                raise UIRequestError(
                    f"expanded flow may contain at most {FLOW_MAX_EXPANDED_STEPS} actions"
                )

        return result

    def _count_flow_blocks(self, blocks: list[dict[str, Any]]) -> int:
        count = 0
        for block in blocks:
            count += 1
            if isinstance(block, dict) and block.get("type") == "loop":
                children = block.get("children", [])
                if isinstance(children, list):
                    count += self._count_flow_blocks(children)
        return count

    def _flow_report(
        self,
        blocks: list[dict[str, Any]],
        steps: list[PlannedFlowStep],
    ) -> dict[str, Any]:
        total_duration_us = sum(step.duration_us for step in steps)
        if total_duration_us > FLOW_MAX_DURATION_US:
            raise UIRequestError("flow duration may not exceed 10 minutes")

        cursor_us = 0
        timeline = []
        rf_duration_us = 0
        transmissions = 0
        for step in steps:
            item = {
                "kind": step.kind,
                "sourceId": step.source_id,
                "label": step.label,
                "startUs": cursor_us,
                "durationUs": step.duration_us,
            }
            if step.kind == "transmit" and step.command is not None and step.frame is not None:
                transmissions += 1
                rf_duration_us += step.duration_us
                item.update(
                    {
                        "target": "wake" if step.is_wake else step.command.name,
                        "color": None
                        if step.is_wake
                        else f"#{step.command.red:02X}{step.command.green:02X}{step.command.blue:02X}",
                        "repeatCount": step.waveform.repeat_count,
                        "crc12": f"{step.frame.crc12:03X}",
                    }
                )
            timeline.append(item)
            cursor_us += step.duration_us

        return {
            "ok": True,
            "blockCount": self._count_flow_blocks(blocks),
            "expandedStepCount": len(steps),
            "transmissionCount": transmissions,
            "totalDurationUs": total_duration_us,
            "rfDurationUs": rf_duration_us,
            "waitDurationUs": total_duration_us - rf_duration_us,
            "timeline": timeline,
        }

    def preview_flow(self, data: dict[str, Any]) -> dict[str, Any]:
        blocks = data.get("blocks")
        if not isinstance(blocks, list):
            raise UIRequestError("flow blocks must be a list")
        steps = self._expand_flow_blocks(blocks)
        return self._flow_report(blocks, steps)

    def transmit_flow(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.config.allow_transmit:
            raise TransmitDisabledError(
                "RF transmit is disabled; restart the UI with --allow-transmit"
            )
        if data.get("armed") is not True or data.get("confirmation") != "TRANSMIT":
            raise TransmitDisabledError("arm RF transmit before running the flow")
        tx_gain_db = _bounded_int(data, "txGainDb", self.config.tx_gain_db, 0, 47)

        executable = shutil.which(self.config.hackrf_transfer)
        if executable is None and Path(self.config.hackrf_transfer).exists():
            executable = str(Path(self.config.hackrf_transfer).resolve())
        if executable is None:
            raise FileNotFoundError(
                f"hackrf_transfer was not found: {self.config.hackrf_transfer}"
            )

        blocks = data.get("blocks")
        if not isinstance(blocks, list):
            raise UIRequestError("flow blocks must be a list")
        steps = self._expand_flow_blocks(blocks)
        report = self._flow_report(blocks, steps)

        # Keep one HackRF session open for the entire visual flow. Reopening
        # hackrf_transfer for every block is unreliable on Windows because the
        # USB device may not be released before the next process starts. Wait
        # blocks become carrier-OFF IQ spans in the same continuous stream.
        flow_pulses: list[int] = []
        for step in steps:
            if step.kind == "wait":
                durations = (-step.duration_us,) if step.duration_us else ()
            else:
                durations = step.waveform.pulses_us
            for duration in durations:
                if flow_pulses and (flow_pulses[-1] > 0) == (duration > 0):
                    flow_pulses[-1] += duration
                else:
                    flow_pulses.append(duration)

        if report["transmissionCount"] == 0:
            raise UIRequestError("flow contains no RF command blocks")
        flow_waveform = plan_waveform(flow_pulses, repeat_count=1)
        if flow_waveform.duration_us != report["totalDurationUs"]:
            raise RuntimeError("internal flow duration mismatch")

        if not self._transmit_lock.acquire(blocking=False):
            raise UIRequestError("another transmission is already in progress")
        try:
            self._streamer(
                flow_waveform,
                frequency_hz=self.config.frequency_hz,
                sample_rate_hz=self.config.sample_rate_hz,
                amplitude=self.config.amplitude,
                tx_gain_db=tx_gain_db,
                executable=executable,
                serial_number=self.config.serial_number,
            )
        finally:
            self._transmit_lock.release()

        return {**report, "transmitted": True, "txGainDb": tx_gain_db}

    def transmit(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.config.allow_transmit:
            raise TransmitDisabledError(
                "RF transmit is disabled; restart the UI with --allow-transmit"
            )
        if data.get("armed") is not True or data.get("confirmation") != "TRANSMIT":
            raise TransmitDisabledError("arm RF transmit before sending a command")
        tx_gain_db = _bounded_int(data, "txGainDb", self.config.tx_gain_db, 0, 47)

        executable = shutil.which(self.config.hackrf_transfer)
        if executable is None and Path(self.config.hackrf_transfer).exists():
            executable = str(Path(self.config.hackrf_transfer).resolve())
        if executable is None:
            raise FileNotFoundError(
                f"hackrf_transfer was not found: {self.config.hackrf_transfer}"
            )

        command, waveform, frame, is_wake = self.plan(data)
        if not self._transmit_lock.acquire(blocking=False):
            raise UIRequestError("another transmission is already in progress")
        try:
            self._streamer(
                waveform,
                frequency_hz=self.config.frequency_hz,
                sample_rate_hz=self.config.sample_rate_hz,
                amplitude=self.config.amplitude,
                tx_gain_db=tx_gain_db,
                executable=executable,
                serial_number=self.config.serial_number,
            )
        finally:
            self._transmit_lock.release()
        return {
            "ok": True,
            "target": "wake" if is_wake else command.name,
            "repeatCount": waveform.repeat_count,
            "durationUs": waveform.duration_us,
            "crc12": f"{frame.crc12:03X}",
            "txGainDb": tx_gain_db,
        }


def make_handler(service: ControllerService, csrf_token: str) -> type[BaseHTTPRequestHandler]:
    class PixMobUIHandler(BaseHTTPRequestHandler):
        server_version = "PixMobStudio/0.4"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self'; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> dict[str, Any]:
            if self.headers.get("X-PixMob-Token") != csrf_token:
                raise TransmitDisabledError("invalid local session token")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise UIRequestError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise UIRequestError("invalid Content-Length") from exc
            if length < 2 or length > 64_000:
                raise UIRequestError("request body size is invalid")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UIRequestError("request body is not valid JSON") from exc
            if not isinstance(value, dict):
                raise UIRequestError("request body must be a JSON object")
            return value

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/config":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "config": service.public_config(),
                        "presets": service.preset_data(),
                        "csrfToken": csrf_token,
                    },
                )
                return
            if path == "/api/music/devices":
                self._send_json(HTTPStatus.OK, service.music_devices())
                return
            if path == "/api/music/status":
                self._send_json(HTTPStatus.OK, service.music_status())
                return
            asset = _ASSETS.get(path)
            if asset is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            filename, content_type = asset
            try:
                content = resources.files("pixmob_ui_assets").joinpath(filename).read_bytes()
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            try:
                data = self._read_json()
                if self.path == "/api/preview":
                    self._send_json(HTTPStatus.OK, service.preview(data))
                    return
                if self.path == "/api/flow/preview":
                    self._send_json(HTTPStatus.OK, service.preview_flow(data))
                    return
                if self.path == "/api/transmit":
                    self._send_json(HTTPStatus.OK, service.transmit(data))
                    return
                if self.path == "/api/flow/transmit":
                    self._send_json(HTTPStatus.OK, service.transmit_flow(data))
                    return
                if self.path == "/api/music/start":
                    self._send_json(HTTPStatus.OK, service.music_start(data))
                    return
                if self.path == "/api/music/stop":
                    self._send_json(HTTPStatus.OK, service.music_stop())
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except TransmitDisabledError as exc:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except (UIRequestError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive server boundary
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"controller error: {exc}"},
                )

    return PixMobUIHandler


def parse_engineering_int(value: str) -> int:
    match = _ENGINEERING_NUMBER.match(value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid engineering number: {value!r}")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[
        match.group("prefix").lower()
    ]
    result = float(match.group("number")) * multiplier
    if not result.is_integer() or result <= 0:
        raise argparse.ArgumentTypeError("value must resolve to a positive integer")
    return int(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the local Concert LED Bracelet Studio browser UI"
    )
    parser.add_argument("--frequency", type=parse_engineering_int, default=915_000_000)
    parser.add_argument("--sample-rate", type=parse_engineering_int, default=2_000_000)
    parser.add_argument("--amplitude", type=int, choices=range(1, 128), default=64)
    parser.add_argument("--tx-gain", type=int, choices=range(0, 48), default=0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--hackrf-transfer", default="hackrf_transfer")
    parser.add_argument("--serial")
    parser.add_argument(
        "--allow-transmit",
        action="store_true",
        help="enable the UI's separate arm-and-transmit controls",
    )
    parser.add_argument(
        "--allow-persistent",
        action="store_true",
        help="unlock protocol forever mode, which may persist across a battery cycle",
    )
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 1 or args.port > 65535:
        raise ValueError("port must be in the range 1..65535")
    config = UIConfig(
        frequency_hz=args.frequency,
        sample_rate_hz=args.sample_rate,
        amplitude=args.amplitude,
        tx_gain_db=args.tx_gain,
        allow_transmit=args.allow_transmit,
        allow_persistent=args.allow_persistent,
        hackrf_transfer=args.hackrf_transfer,
        serial_number=args.serial,
    )
    service = ControllerService(config)
    token = secrets.token_urlsafe(32)
    handler = make_handler(service, token)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Concert LED Bracelet Studio: {url}")
    if config.allow_transmit:
        print(
            f"RF enabled at {config.frequency_hz} Hz, {config.tx_gain_db} dB; "
            "the UI still requires the RF arm switch before transmission."
        )
    else:
        print("Preview-only mode: RF transmission is disabled.")
    print("Press Ctrl+C to stop the local server.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Concert LED Bracelet Studio.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
