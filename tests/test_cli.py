from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from cli import main, parse_engineering_int


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_engineering_numbers(self) -> None:
        self.assertEqual(parse_engineering_int("2M"), 2_000_000)
        self.assertEqual(parse_engineering_int("915MHz"), 915_000_000)
        self.assertEqual(parse_engineering_int("868.4M"), 868_400_000)

    def test_transmit_is_dry_run_without_explicit_flag(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "transmit",
                    str(ROOT / "commands" / "gold_fade_in_915.sub"),
                    "--frequency",
                    "915M",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("RF transmission was NOT started", output.getvalue())
        self.assertIn("TX VGA gain: 0 dB", output.getvalue())

    def test_lists_controller_presets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["presets"])

        self.assertEqual(result, 0)
        self.assertIn("fade-gold", output.getvalue())
        self.assertIn("wake", output.getvalue())

    def test_convert_known_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "gold.cs8"
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "convert",
                        str(ROOT / "commands" / "gold_fade_in_915.sub"),
                        str(output_path),
                        "--sample-rate",
                        "2M",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(output_path.stat().st_size, 183_600)


if __name__ == "__main__":
    unittest.main()
