from __future__ import annotations

import unittest

from ui_server import ControllerService, TransmitDisabledError, UIConfig, UIRequestError


class FakeMusicMonitor:
    def __init__(self) -> None:
        self.running = False
        self.started = None
        self.on_beat = None

    def start(self, **kwargs) -> None:
        self.running = True
        self.started = kwargs
        self.on_beat = kwargs["on_beat"]

    def stop(self) -> None:
        self.running = False

    def status(self):
        return {
            "running": self.running,
            "transmit": bool(self.running and self.started and self.started["transmit"]),
            "error": None,
            "transmitError": None,
            "device": {"id": 110, "name": "Stereo Mix", "sampleRate": 48_000},
            "palette": "spectrum",
            "brightness": 80,
            "frame": None,
        }


class ControllerServiceTests(unittest.TestCase):
    def test_preview_generates_crc_valid_real_packet(self) -> None:
        service = ControllerService(UIConfig())

        preview = service.preview(
            {
                "target": "custom",
                "red": 0,
                "green": 128,
                "blue": 255,
                "attack": 4,
                "hold": 3,
                "release": 2,
                "random": 0,
                "group": 0,
                "mode": "continuous",
                "repeatCount": 8,
            }
        )

        self.assertTrue(preview["crcValid"])
        self.assertEqual(preview["frameSymbols"], 90)
        self.assertEqual(preview["rgb"], [0, 128, 255])
        self.assertEqual(preview["quantizedRgb"], [0, 32, 63])
        self.assertEqual(preview["repeatCount"], 8)
        self.assertEqual(len(preview["symbols"]), 90)

    def test_fade_gold_ui_preview_matches_working_capture(self) -> None:
        service = ControllerService(UIConfig())
        preset = next(item for item in service.preset_data() if item["name"] == "fade-gold")

        preview = service.preview({**preset, "target": "fade-gold", "repeatCount": 1})

        self.assertEqual(preview["logicalPayload"], "00 27 2F 00 20 13 00")
        self.assertEqual(preview["onAirBytes"], "94 84 91 B5 84 8C 45 84 AD")
        self.assertEqual(preview["crc12"], "8FB")

    def test_transmit_is_rejected_when_server_was_not_enabled(self) -> None:
        service = ControllerService(UIConfig(allow_transmit=False))

        with self.assertRaises(TransmitDisabledError):
            service.transmit({"armed": True, "confirmation": "TRANSMIT"})

    def test_transmit_requires_per_command_arm_confirmation(self) -> None:
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=lambda *_args, **_kwargs: 0,
        )

        with self.assertRaises(TransmitDisabledError):
            service.transmit({"red": 255, "armed": False})

    def test_enabled_transmit_calls_injected_streamer_without_real_rf(self) -> None:
        calls = []

        def fake_streamer(plan, **kwargs):
            calls.append((plan, kwargs))
            return 0

        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=fake_streamer,
        )
        result = service.transmit(
            {
                "target": "custom",
                "red": 255,
                "green": 0,
                "blue": 0,
                "attack": 1,
                "hold": 2,
                "release": 2,
                "random": 0,
                "group": 0,
                "mode": "continuous",
                "repeatCount": 1,
                "armed": True,
                "confirmation": "TRANSMIT",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["frequency_hz"], 915_000_000)
        self.assertEqual(calls[0][1]["tx_gain_db"], 0)

    def test_transmit_rejects_out_of_range_request_gain(self) -> None:
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=lambda *_args, **_kwargs: 0,
        )

        with self.assertRaisesRegex(UIRequestError, "txGainDb must be in the range 0..47"):
            service.transmit(
                {"armed": True, "confirmation": "TRANSMIT", "txGainDb": 48}
            )

    def test_wake_duration_is_bounded(self) -> None:
        service = ControllerService(UIConfig())

        with self.assertRaises(UIRequestError):
            service.preview({"target": "wake", "wakeDurationS": 61})

    def test_forever_mode_remains_locked_by_default(self) -> None:
        service = ControllerService(UIConfig())

        with self.assertRaises(UIRequestError):
            service.preview({"mode": "forever"})

    def test_passive_music_monitor_does_not_require_or_emit_rf(self) -> None:
        monitor = FakeMusicMonitor()
        service = ControllerService(
            UIConfig(allow_transmit=False),
            streamer=lambda *_args, **_kwargs: self.fail("streamer should not run"),
            music_monitor=monitor,
        )

        status = service.music_start(
            {
                "deviceId": 110,
                "sensitivity": 70,
                "brightness": 80,
                "minIntervalMs": 700,
                "palette": "spectrum",
                "transmit": False,
            }
        )

        self.assertTrue(status["running"])
        self.assertFalse(status["transmit"])
        self.assertFalse(monitor.started["transmit"])

    def test_armed_music_sync_transmits_rate_limited_beat_command(self) -> None:
        monitor = FakeMusicMonitor()
        streamed = []
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=lambda plan, **kwargs: streamed.append((plan, kwargs)) or 0,
            waiter=lambda _delay: None,
            music_monitor=monitor,
            hackrf_probe=lambda *_args: (True, None),
        )

        status = service.music_start(
            {
                "deviceId": 110,
                "sensitivity": 75,
                "brightness": 90,
                "minIntervalMs": 800,
                "palette": "warm",
                "txGainDb": 9,
                "transmit": True,
                "armed": True,
                "confirmation": "TRANSMIT",
            }
        )
        monitor.on_beat(None, (200, 80, 10))

        self.assertTrue(status["transmit"])
        self.assertEqual(len(streamed), 1)
        self.assertEqual(streamed[0][1]["tx_gain_db"], 9)
        self.assertEqual(streamed[0][0].repeat_count, 1)
        self.assertEqual(service.music_status()["transmissionCount"], 1)

    def test_music_sync_retries_transient_hackrf_open_failure(self) -> None:
        monitor = FakeMusicMonitor()
        attempts = []
        waits = []

        def flaky_streamer(plan, **kwargs):
            attempts.append((plan, kwargs))
            if len(attempts) == 1:
                raise RuntimeError(
                    "hackrf_transfer exited with code 1: hackrf_open() failed: HackRF not found (-5)"
                )
            return 0

        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=flaky_streamer,
            waiter=waits.append,
            music_monitor=monitor,
            hackrf_probe=lambda *_args: (True, None),
        )
        service.music_start(
            {
                "deviceId": 110,
                "minIntervalMs": 700,
                "transmit": True,
                "armed": True,
                "confirmation": "TRANSMIT",
            }
        )

        monitor.on_beat(None, (120, 60, 10))

        status = service.music_status()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(waits, [0.5, 0.2])
        self.assertEqual(status["transmissionCount"], 1)
        self.assertEqual(status["retryCount"], 1)
        self.assertTrue(status["transmit"])

    def test_music_sync_requires_explicit_arm_for_rf(self) -> None:
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            music_monitor=FakeMusicMonitor(),
        )

        with self.assertRaises(TransmitDisabledError):
            service.music_start({"deviceId": 110, "transmit": True})

    def test_music_sync_preflight_preserves_passive_monitor_when_hackrf_is_missing(self) -> None:
        monitor = FakeMusicMonitor()
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            music_monitor=monitor,
            hackrf_probe=lambda *_args: (False, "HackRF One is not connected"),
        )
        service.music_start({"deviceId": 110, "transmit": False})

        with self.assertRaisesRegex(FileNotFoundError, "not connected"):
            service.music_start(
                {
                    "deviceId": 110,
                    "transmit": True,
                    "armed": True,
                    "confirmation": "TRANSMIT",
                }
            )

        self.assertTrue(monitor.running)
        self.assertFalse(monitor.started["transmit"])

    def test_flow_preview_expands_nested_loop_without_transmitting(self) -> None:
        service = ControllerService(UIConfig())
        color = {
            "id": "blue",
            "type": "color",
            "label": "Blue",
            "target": "custom",
            "red": 0,
            "green": 0,
            "blue": 255,
            "attack": 1,
            "hold": 2,
            "release": 2,
            "random": 0,
            "group": 0,
            "mode": "one-shot",
            "repeatCount": 3,
        }
        report = service.preview_flow(
            {
                "blocks": [
                    {
                        "id": "loop",
                        "type": "loop",
                        "label": "Loop 2x",
                        "count": 2,
                        "loopDelayMs": 100,
                        "children": [color, {"id": "wait", "type": "wait", "durationMs": 250}],
                    }
                ]
            }
        )

        self.assertEqual(report["blockCount"], 3)
        self.assertEqual(report["transmissionCount"], 2)
        self.assertEqual(report["expandedStepCount"], 5)
        self.assertEqual([item["kind"] for item in report["timeline"]], [
            "transmit", "wait", "wait", "transmit", "wait"
        ])
        self.assertEqual(report["waitDurationUs"], 600_000)

    def test_flow_transmit_streams_one_continuous_waveform_with_silent_waits(self) -> None:
        streamed = []
        waited = []

        def fake_streamer(plan, **kwargs):
            streamed.append((plan, kwargs))
            return 0

        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=fake_streamer,
            waiter=waited.append,
        )
        result = service.transmit_flow(
            {
                "armed": True,
                "confirmation": "TRANSMIT",
                "txGainDb": 12,
                "blocks": [
                    {
                        "id": "red",
                        "type": "color",
                        "label": "Red",
                        "red": 255,
                        "green": 0,
                        "blue": 0,
                        "attack": 1,
                        "hold": 2,
                        "release": 2,
                        "random": 0,
                        "group": 0,
                        "mode": "one-shot",
                        "repeatCount": 2,
                    },
                    {"id": "pause", "type": "wait", "durationMs": 125},
                    {"id": "off", "type": "off", "label": "Off", "repeatCount": 2},
                ],
            }
        )

        self.assertTrue(result["transmitted"])
        self.assertEqual(result["transmissionCount"], 2)
        self.assertEqual(len(streamed), 1)
        self.assertEqual(waited, [])
        self.assertEqual(streamed[0][1]["frequency_hz"], 915_000_000)
        self.assertEqual(streamed[0][1]["tx_gain_db"], 12)
        self.assertEqual(result["txGainDb"], 12)
        self.assertEqual(streamed[0][0].duration_us, result["totalDurationUs"])
        self.assertTrue(any(duration <= -125_000 for duration in streamed[0][0].pulses_us))

    def test_flow_transmit_rejects_wait_only_flow(self) -> None:
        service = ControllerService(
            UIConfig(allow_transmit=True, hackrf_transfer=__file__),
            streamer=lambda *_args, **_kwargs: self.fail("streamer should not run"),
        )

        with self.assertRaisesRegex(UIRequestError, "no RF command blocks"):
            service.transmit_flow(
                {
                    "armed": True,
                    "confirmation": "TRANSMIT",
                    "blocks": [{"id": "pause", "type": "wait", "durationMs": 125}],
                }
            )

    def test_flow_rejects_empty_loop(self) -> None:
        service = ControllerService(UIConfig())

        with self.assertRaises(UIRequestError):
            service.preview_flow(
                {"blocks": [{"id": "loop", "type": "loop", "count": 3, "children": []}]}
            )


if __name__ == "__main__":
    unittest.main()
