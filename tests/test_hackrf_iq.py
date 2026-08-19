from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from flipper_sub import read_flipper_sub
from hackrf_iq import (
    build_hackrf_tx_command,
    iter_cs8,
    plan_waveform,
    pulse_sample_counts,
    stream_to_hackrf,
    write_cs8,
)


ROOT = Path(__file__).resolve().parents[1]


class HackRFIQTests(unittest.TestCase):
    def test_generates_interleaved_signed_int8_ook(self) -> None:
        data = b"".join(iter_cs8((1, -2), sample_rate_hz=2_000_000, amplitude=64))

        self.assertEqual(data, bytes((64, 0)) * 2 + bytes((0, 0)) * 4)

    def test_cumulative_quantization_avoids_timing_drift(self) -> None:
        # 1 us is 2.5 samples. Boundary rounding gives 3, then 2, for 5 total.
        counts = pulse_sample_counts((1, -1), sample_rate_hz=2_500_000)

        self.assertEqual(counts, ((True, 3), (False, 2)))

    def test_repeat_duration_uses_complete_packets_and_gap(self) -> None:
        plan = plan_waveform(
            (510, -510), repeat_duration_s=0.003, inter_packet_delay_us=100
        )

        self.assertEqual(plan.repeat_count, 3)
        self.assertEqual(plan.duration_us, 3_260)
        self.assertEqual(sum(abs(value) for value in plan.pulses_us), 3_260)

    def test_known_capture_exact_size_and_first_edges(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "gold_fade_in_915.sub")
        plan = plan_waveform(capture.raw_data)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gold.cs8"
            report = write_cs8(output, plan, sample_rate_hz=2_000_000, amplitude=64)
            data = output.read_bytes()

        self.assertEqual(report.sample_count, 91_800)
        self.assertEqual(report.byte_count, 183_600)
        self.assertEqual(data[: 1_020 * 2], bytes((64, 0)) * 1_020)
        self.assertEqual(data[1_020 * 2 : 2_040 * 2], bytes((0, 0)) * 1_020)

    def test_tx_command_disables_rf_amp_and_antenna_power(self) -> None:
        command = build_hackrf_tx_command(
            frequency_hz=915_000_000,
            sample_rate_hz=2_000_000,
            tx_gain_db=0,
        )

        self.assertEqual(command[:3], ["hackrf_transfer", "-t", "-"])
        self.assertIn("915000000", command)
        self.assertEqual(command[command.index("-a") + 1], "0")
        self.assertEqual(command[command.index("-p") + 1], "0")

    def test_tx_command_accepts_real_iq_file(self) -> None:
        command = build_hackrf_tx_command(
            frequency_hz=915_000_000,
            sample_rate_hz=2_000_000,
            input_path=Path("known-good.cs8"),
        )

        self.assertEqual(command[:3], ["hackrf_transfer", "-t", "known-good.cs8"])

    def test_transmit_uses_and_cleans_up_temporary_iq_file(self) -> None:
        observed = {}

        def fake_run(command, **kwargs):
            iq_path = Path(command[2])
            observed["path"] = iq_path
            observed["exists_during_run"] = iq_path.exists()
            observed["data"] = iq_path.read_bytes()
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, "", "")

        plan = plan_waveform((1, -1))
        with patch("hackrf_iq.subprocess.run", side_effect=fake_run):
            result = stream_to_hackrf(
                plan,
                frequency_hz=915_000_000,
                sample_rate_hz=2_000_000,
                amplitude=64,
            )

        self.assertEqual(result, 0)
        self.assertTrue(observed["exists_during_run"])
        self.assertEqual(observed["data"], bytes((64, 0)) * 2 + bytes((0, 0)) * 2)
        self.assertFalse(observed["path"].exists())
        self.assertTrue(observed["kwargs"]["capture_output"])


if __name__ == "__main__":
    unittest.main()
