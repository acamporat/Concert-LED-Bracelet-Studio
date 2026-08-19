"""Local audio analysis for passive and explicitly armed PixMob music sync."""

from __future__ import annotations

from dataclasses import dataclass
import math
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

try:  # Optional at install time; the UI reports a clear error when unavailable.
    import sounddevice as sd
except ImportError:  # pragma: no cover - exercised only without the optional extra
    sd = None


PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "spectrum": (
        (255, 42, 42), (255, 166, 0), (255, 232, 0), (0, 220, 120),
        (0, 160, 255), (74, 70, 255), (190, 45, 255), (255, 45, 155),
    ),
    "neon": ((0, 240, 255), (255, 40, 210), (122, 70, 255), (50, 255, 130)),
    "warm": ((255, 40, 20), (255, 105, 0), (255, 185, 0), (255, 225, 90)),
    "cool": ((0, 120, 255), (0, 225, 255), (65, 70, 255), (175, 45, 255)),
}


@dataclass(frozen=True)
class AudioFrame:
    timestamp: float
    rms: float
    peak: float
    energy: int
    bass: float
    mid: float
    treble: float
    beat: bool
    bpm: float
    beat_count: int
    waveform: tuple[float, ...]


class BeatAnalyzer:
    """Adaptive energy/bass detector suitable for local reactive lighting."""

    def __init__(self, *, sensitivity: int = 70, cooldown_ms: int = 450) -> None:
        if sensitivity < 1 or sensitivity > 100:
            raise ValueError("sensitivity must be in the range 1..100")
        if cooldown_ms < 100:
            raise ValueError("cooldown_ms must be at least 100")
        self.sensitivity = sensitivity
        self.cooldown_s = cooldown_ms / 1000
        self._bass_average = 0.0
        self._rms_average = 0.0
        self._last_beat = -math.inf
        self._beat_times: list[float] = []
        self._beat_count = 0

    @staticmethod
    def _band_energy(
        magnitudes: np.ndarray, frequencies: np.ndarray, low: float, high: float
    ) -> float:
        mask = (frequencies >= low) & (frequencies < high)
        if not np.any(mask):
            return 0.0
        values = magnitudes[mask]
        return float(np.sqrt(np.mean(values * values)))

    def process(
        self, samples: np.ndarray, sample_rate: int, *, timestamp: float | None = None
    ) -> AudioFrame:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim == 2:
            values = values.mean(axis=1)
        if values.ndim != 1 or values.size < 32:
            raise ValueError("samples must contain at least 32 mono frames")

        now = time.monotonic() if timestamp is None else float(timestamp)
        values = np.nan_to_num(values, copy=False)
        rms = float(np.sqrt(np.mean(values * values)))
        peak = float(np.max(np.abs(values)))
        windowed = values * np.hanning(values.size)
        magnitudes = np.abs(np.fft.rfft(windowed))
        frequencies = np.fft.rfftfreq(values.size, 1 / sample_rate)
        bass_raw = self._band_energy(magnitudes, frequencies, 35, 180)
        mid_raw = self._band_energy(magnitudes, frequencies, 180, 2_000)
        treble_raw = self._band_energy(magnitudes, frequencies, 2_000, 10_000)
        total = bass_raw + mid_raw + treble_raw + 1e-12
        bass, mid, treble = bass_raw / total, mid_raw / total, treble_raw / total

        if self._bass_average == 0:
            self._bass_average = bass_raw
            self._rms_average = rms
        threshold = max(1.06, 2.0 - self.sensitivity * 0.012)
        beat = bool(
            rms > 0.0015
            and now - self._last_beat >= self.cooldown_s
            and bass_raw > max(1e-9, self._bass_average * threshold)
            and rms > max(0.001, self._rms_average * 1.08)
        )
        if beat:
            self._last_beat = now
            self._beat_count += 1
            self._beat_times.append(now)
            self._beat_times = self._beat_times[-12:]

        alpha = 0.035
        self._bass_average = (1 - alpha) * self._bass_average + alpha * bass_raw
        self._rms_average = (1 - alpha) * self._rms_average + alpha * rms
        if len(self._beat_times) >= 2:
            intervals = np.diff(self._beat_times)
            intervals = intervals[(intervals >= 0.25) & (intervals <= 2.0)]
            bpm = 60 / float(np.median(intervals)) if intervals.size else 0.0
        else:
            bpm = 0.0

        db = 20 * math.log10(max(rms, 1e-6))
        energy = int(round(max(0.0, min(100.0, (db + 60) * (100 / 60)))))
        indexes = np.linspace(0, values.size - 1, 64).astype(int)
        waveform = tuple(float(max(-1, min(1, item))) for item in values[indexes])
        return AudioFrame(
            timestamp=now,
            rms=rms,
            peak=peak,
            energy=energy,
            bass=bass,
            mid=mid,
            treble=treble,
            beat=beat,
            bpm=bpm,
            beat_count=self._beat_count,
            waveform=waveform,
        )


def frame_color(
    frame: AudioFrame, *, palette: str = "spectrum", brightness: int = 80
) -> tuple[int, int, int]:
    if palette not in PALETTES:
        raise ValueError(f"unknown palette: {palette}")
    if brightness < 1 or brightness > 100:
        raise ValueError("brightness must be in the range 1..100")
    colors = PALETTES[palette]
    if palette == "spectrum":
        dominant = max(range(3), key=(frame.bass, frame.mid, frame.treble).__getitem__)
        offsets = (0, 3, 5)
        color = colors[(offsets[dominant] + frame.beat_count) % len(colors)]
    else:
        color = colors[frame.beat_count % len(colors)]
    scale = 0.0 if frame.peak <= 0.001 else (brightness / 100) * max(0.25, frame.energy / 100)
    return tuple(max(0, min(255, round(channel * scale))) for channel in color)


def list_audio_inputs() -> list[dict[str, Any]]:
    if sd is None:
        return []
    devices: list[dict[str, Any]] = []
    for index, info in enumerate(sd.query_devices()):
        if int(info["max_input_channels"]) < 1:
            continue
        name = str(info["name"])
        recommended = "stereo mix" in name.lower() or "voicemeeter out b1" in name.lower()
        devices.append(
            {
                "id": index,
                "name": name,
                "channels": min(2, int(info["max_input_channels"])),
                "sampleRate": int(info["default_samplerate"]),
                "recommended": recommended,
            }
        )
    devices.sort(key=lambda item: (not item["recommended"], item["name"].lower()))
    return devices


class AudioMonitor:
    """Threaded local input monitor with an optional beat callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: AudioFrame | None = None
        self._error: str | None = None
        self._transmit_error: str | None = None
        self._device: dict[str, Any] | None = None
        self._palette = "spectrum"
        self._brightness = 80
        self._transmit = False
        self._on_beat: Callable[[AudioFrame, tuple[int, int, int]], None] | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        *,
        device_id: int,
        sensitivity: int = 70,
        cooldown_ms: int = 450,
        palette: str = "spectrum",
        brightness: int = 80,
        transmit: bool = False,
        on_beat: Callable[[AudioFrame, tuple[int, int, int]], None] | None = None,
    ) -> None:
        if sd is None:
            raise RuntimeError("Music Mode requires the optional sounddevice package")
        devices = {item["id"]: item for item in list_audio_inputs()}
        if device_id not in devices:
            raise ValueError("selected audio input is unavailable")
        if palette not in PALETTES:
            raise ValueError("palette must be spectrum, neon, warm, or cool")
        self.stop()
        self._stop.clear()
        self._latest = None
        self._error = None
        self._transmit_error = None
        self._device = devices[device_id]
        self._palette = palette
        self._brightness = brightness
        self._transmit = transmit
        self._on_beat = on_beat
        analyzer = BeatAnalyzer(sensitivity=sensitivity, cooldown_ms=cooldown_ms)
        self._thread = threading.Thread(
            target=self._run, args=(analyzer,), name="pixmob-music-monitor", daemon=True
        )
        self._thread.start()

    def _run(self, analyzer: BeatAnalyzer) -> None:
        assert sd is not None and self._device is not None
        try:
            blocksize = 2048
            audio_blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=3)

            def audio_callback(
                indata: np.ndarray, _frames: int, _time_info: Any, status: Any
            ) -> None:
                if status.input_overflow:
                    return
                block = indata.copy()
                try:
                    audio_blocks.put_nowait(block)
                except queue.Full:
                    try:
                        audio_blocks.get_nowait()
                    except queue.Empty:
                        pass
                    audio_blocks.put_nowait(block)

            with sd.InputStream(
                device=self._device["id"],
                channels=self._device["channels"],
                samplerate=self._device["sampleRate"],
                blocksize=blocksize,
                dtype="float32",
                callback=audio_callback,
            ):
                while not self._stop.is_set():
                    try:
                        data = audio_blocks.get(timeout=0.15)
                    except queue.Empty:
                        continue
                    frame = analyzer.process(data, self._device["sampleRate"])
                    with self._lock:
                        self._latest = frame
                    if frame.beat:
                        self._handle_beat(frame)
        except Exception as exc:  # pragma: no cover - depends on host audio driver
            with self._lock:
                self._error = str(exc)

    def _handle_beat(self, frame: AudioFrame) -> None:
        """Keep the audio monitor alive when a beat-triggered RF call fails."""

        if not self._transmit or self._on_beat is None:
            return
        try:
            self._on_beat(
                frame,
                frame_color(frame, palette=self._palette, brightness=self._brightness),
            )
        except Exception as exc:
            with self._lock:
                self._transmit_error = str(exc)
                self._transmit = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            frame = self._latest
            error = self._error
            transmit_error = self._transmit_error
        color = frame_color(frame, palette=self._palette, brightness=self._brightness) if frame else (0, 0, 0)
        return {
            "running": self.running,
            "transmit": self._transmit and self.running,
            "error": error,
            "transmitError": transmit_error,
            "device": self._device,
            "palette": self._palette,
            "brightness": self._brightness,
            "frame": None
            if frame is None
            else {
                "rms": frame.rms,
                "peak": frame.peak,
                "energy": frame.energy,
                "bass": frame.bass,
                "mid": frame.mid,
                "treble": frame.treble,
                "beat": frame.beat,
                "bpm": frame.bpm,
                "beatCount": frame.beat_count,
                "waveform": list(frame.waveform),
                "rgb": list(color),
            },
        }
