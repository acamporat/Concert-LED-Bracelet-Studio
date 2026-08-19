from pathlib import Path
import unittest

from flipper_sub import FlipperSubError, parse_flipper_sub_text, read_flipper_sub


ROOT = Path(__file__).resolve().parents[1]


class FlipperSubTests(unittest.TestCase):
    def test_reads_known_gold_capture(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "gold_fade_in_915.sub", strict=True)

        self.assertEqual(capture.frequency_hz, 915_000_000)
        self.assertEqual(capture.preset, "FuriHalSubGhzPresetOok650Async")
        self.assertEqual(len(capture.raw_data), 59)
        self.assertEqual(capture.duration_us, 45_900)
        self.assertEqual(capture.carrier_on_us + capture.carrier_off_us, 45_900)

    def test_concatenates_multiple_raw_data_lines(self) -> None:
        capture = parse_flipper_sub_text(
            """\
Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 915000000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 510 -1020
RAW_Data: 1530 -2040
""",
            strict=True,
        )

        self.assertEqual(capture.raw_data, (510, -1020, 1530, -2040))

    def test_permissive_mode_accepts_real_world_sign_irregularities(self) -> None:
        text = """\
Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 915000000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: -100 200 300 -400
"""
        self.assertEqual(parse_flipper_sub_text(text).raw_data, (-100, 200, 300, -400))
        with self.assertRaises(FlipperSubError):
            parse_flipper_sub_text(text, strict=True)

    def test_rejects_zero_duration(self) -> None:
        text = """\
Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 915000000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 510 0 -510
"""
        with self.assertRaisesRegex(FlipperSubError, "non-zero"):
            parse_flipper_sub_text(text)


if __name__ == "__main__":
    unittest.main()
