from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import unittest

from cli import main
from controller import PRESETS, color_command, resolve_command
from flipper_sub import read_flipper_sub
from pixmob_protocol import decode_waveband_pulses


ROOT = Path(__file__).resolve().parents[1]


class ControllerTests(unittest.TestCase):
    def test_color_payload_uses_green_red_blue_wire_order(self) -> None:
        command = color_command(255, 128, 0)

        self.assertEqual(command.payload_values, (0, 32, 63, 0, 8, 18, 0))
        self.assertTrue(decode_waveband_pulses(command.pulses()).crc_valid)

    def test_fade_gold_generation_exactly_matches_working_capture(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "gold_fade_in_915.sub")

        self.assertEqual(PRESETS["fade-gold"].payload_values, (0, 0x27, 0x2F, 0, 0x20, 0x13, 0))
        self.assertEqual(PRESETS["fade-gold"].pulses(), capture.raw_data)

    def test_keepalive_matches_nothing_capture(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "nothing_915.sub")

        self.assertEqual(PRESETS["keepalive"].payload_values, (0, 0, 0, 0, 8, 15, 0))
        self.assertEqual(PRESETS["keepalive"].pulses(), capture.raw_data)

    def test_resolves_hex_and_decimal_rgb(self) -> None:
        self.assertEqual(resolve_command("#FF8000").quantized_rgb, (63, 32, 0))
        self.assertEqual(resolve_command("1,2,3").quantized_rgb, (0, 0, 0))

    def test_control_defaults_to_dry_run(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["control", "red", "--frequency", "915M"])

        self.assertEqual(result, 0)
        self.assertIn("CRC-12=", output.getvalue())
        self.assertIn("RF transmission was NOT started", output.getvalue())

    def test_forever_mode_requires_separate_acknowledgement(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = main(
                [
                    "control",
                    "red",
                    "--frequency",
                    "915M",
                    "--mode",
                    "forever",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--allow-persistent", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
