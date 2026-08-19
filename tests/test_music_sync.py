from __future__ import annotations

import unittest

import numpy as np

from music_sync import AudioFrame, AudioMonitor, BeatAnalyzer, frame_color


class MusicSyncTests(unittest.TestCase):
    @staticmethod
    def tone(frequency: float, amplitude: float, *, sample_rate: int = 48_000) -> np.ndarray:
        times = np.arange(4096, dtype=np.float32) / sample_rate
        return amplitude * np.sin(2 * np.pi * frequency * times)

    def test_analyzer_detects_bass_transient_after_quiet_baseline(self) -> None:
        analyzer = BeatAnalyzer(sensitivity=85, cooldown_ms=250)
        quiet = analyzer.process(self.tone(70, 0.003), 48_000, timestamp=0.0)
        beat = analyzer.process(self.tone(70, 0.2), 48_000, timestamp=0.4)

        self.assertFalse(quiet.beat)
        self.assertTrue(beat.beat)
        self.assertGreater(beat.bass, beat.mid)
        self.assertEqual(beat.beat_count, 1)
        self.assertEqual(len(beat.waveform), 64)

    def test_analyzer_enforces_beat_cooldown(self) -> None:
        analyzer = BeatAnalyzer(sensitivity=100, cooldown_ms=500)
        analyzer.process(self.tone(70, 0.002), 48_000, timestamp=0.0)
        first = analyzer.process(self.tone(70, 0.2), 48_000, timestamp=0.6)
        second = analyzer.process(self.tone(70, 0.25), 48_000, timestamp=0.8)

        self.assertTrue(first.beat)
        self.assertFalse(second.beat)

    def test_palette_color_respects_brightness(self) -> None:
        frame = AudioFrame(
            timestamp=1.0,
            rms=0.2,
            peak=0.4,
            energy=100,
            bass=0.8,
            mid=0.1,
            treble=0.1,
            beat=True,
            bpm=120.0,
            beat_count=1,
            waveform=(0.0,) * 64,
        )

        full = frame_color(frame, palette="warm", brightness=100)
        half = frame_color(frame, palette="warm", brightness=50)

        self.assertTrue(all(0 <= value <= 255 for value in full))
        self.assertEqual(half, tuple(round(value * 0.5) for value in full))

    def test_silent_frame_maps_to_black(self) -> None:
        frame = AudioFrame(
            timestamp=1.0,
            rms=0.0,
            peak=0.0,
            energy=0,
            bass=0.0,
            mid=0.0,
            treble=0.0,
            beat=False,
            bpm=0.0,
            beat_count=0,
            waveform=(0.0,) * 64,
        )

        self.assertEqual(frame_color(frame, palette="spectrum", brightness=80), (0, 0, 0))

    def test_rf_callback_failure_does_not_become_an_audio_error(self) -> None:
        frame = AudioFrame(
            timestamp=1.0,
            rms=0.2,
            peak=0.4,
            energy=80,
            bass=0.8,
            mid=0.1,
            treble=0.1,
            beat=True,
            bpm=120.0,
            beat_count=1,
            waveform=(0.0,) * 64,
        )
        monitor = AudioMonitor()
        monitor._transmit = True
        monitor._on_beat = lambda *_args: (_ for _ in ()).throw(RuntimeError("HackRF lost"))

        monitor._handle_beat(frame)

        status = monitor.status()
        self.assertIsNone(status["error"])
        self.assertEqual(status["transmitError"], "HackRF lost")
        self.assertFalse(monitor._transmit)


if __name__ == "__main__":
    unittest.main()
