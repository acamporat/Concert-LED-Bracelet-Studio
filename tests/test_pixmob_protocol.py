from pathlib import Path
import unittest

from flipper_sub import read_flipper_sub
from pixmob_protocol import (
    PREAMBLE_AND_SYNC,
    decode_waveband_pulses,
    encode_waveband_payload,
    pulse_durations_to_symbols,
)


ROOT = Path(__file__).resolve().parents[1]


class PixMobProtocolTests(unittest.TestCase):
    def test_decodes_known_gold_capture_and_crc(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "gold_fade_in_915.sub")
        frame = decode_waveband_pulses(capture.raw_data)

        self.assertEqual(len(frame.symbols), 90)
        self.assertEqual(frame.symbols[:18], PREAMBLE_AND_SYNC)
        self.assertEqual(
            frame.on_air_bytes,
            (0x94, 0x84, 0x91, 0xB5, 0x84, 0x8C, 0x45, 0x84, 0xAD),
        )
        self.assertEqual(frame.payload_values, (0x00, 0x27, 0x2F, 0x00, 0x20, 0x13, 0x00))
        self.assertTrue(frame.crc_valid)

    def test_decodes_known_nothing_capture_and_crc(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "nothing_915.sub")
        frame = decode_waveband_pulses(capture.raw_data)

        self.assertEqual(len(frame.symbols), 90)
        self.assertTrue(frame.crc_valid)
        self.assertEqual(frame.payload_values, (0, 0, 0, 0, 0x08, 0x0F, 0))

    def test_encoder_round_trips_known_payload(self) -> None:
        payload = (0x00, 0x27, 0x2F, 0x00, 0x20, 0x13, 0x00)
        pulses = encode_waveband_payload(payload)
        decoded = decode_waveband_pulses(pulses)

        self.assertEqual(decoded.payload_values, payload)
        self.assertTrue(decoded.crc_valid)

    def test_cleaned_capture_is_510_us_symbol_rle(self) -> None:
        capture = read_flipper_sub(ROOT / "commands" / "gold_fade_in_868.sub")
        symbols = pulse_durations_to_symbols(capture.raw_data)

        self.assertEqual(len(symbols), 90)


if __name__ == "__main__":
    unittest.main()
