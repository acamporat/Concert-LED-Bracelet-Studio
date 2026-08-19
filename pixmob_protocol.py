"""Documented PixMob CEMENT V1.1 Waveband framing helpers.

This module is deliberately separate from generic waveform conversion. A
Flipper RAW capture can always be converted without assuming that it is a
PixMob frame or that a W2 Gen3 uses this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


BASE_SYMBOL_US = 510
FRAME_SYMBOLS = 90
PREAMBLE_AND_SYNC = tuple(int(bit) for bit in "101010101010101001")
CRC12_INITIAL_REVERSED = 0xC69
CRC12_POLYNOMIAL_REVERSED = 0x8F3

# 6-bit values to encoded bytes before the transmitter sends each byte LSB-first.
LINE_CODE_BIG_ENDIAN = (
    0x21, 0x35, 0x2C, 0x34, 0x66, 0x26, 0xAC, 0x24,
    0x46, 0x56, 0x44, 0x54, 0x64, 0x6D, 0x4C, 0x6C,
    0x92, 0xB2, 0xA6, 0xA2, 0xB4, 0x94, 0x86, 0x96,
    0x42, 0x62, 0x2A, 0x6A, 0xB6, 0x36, 0x22, 0x32,
    0x31, 0xB1, 0x95, 0xB5, 0x91, 0x99, 0x85, 0x89,
    0xA5, 0xA4, 0x8C, 0x84, 0xA1, 0xA9, 0x8D, 0xAD,
    0x9A, 0x8A, 0x5A, 0x4A, 0x49, 0x59, 0x52, 0x51,
    0x25, 0x2D, 0x69, 0x29, 0x4D, 0x45, 0x61, 0x65,
)


class ProtocolDecodeError(ValueError):
    """Raised when a waveform is not a documented 90-symbol Waveband frame."""


def reverse_bits8(value: int) -> int:
    value &= 0xFF
    value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
    value = ((value & 0xCC) >> 2) | ((value & 0x33) << 2)
    return ((value & 0xAA) >> 1) | ((value & 0x55) << 1)


LINE_CODE_ON_AIR = tuple(reverse_bits8(value) for value in LINE_CODE_BIG_ENDIAN)
_ON_AIR_TO_VALUE = {encoded: value for value, encoded in enumerate(LINE_CODE_ON_AIR)}


@dataclass(frozen=True)
class WavebandFrame:
    symbols: tuple[int, ...]
    on_air_bytes: tuple[int, ...]
    decoded_values: tuple[int, ...]
    payload_values: tuple[int, ...]
    crc12: int
    crc_valid: bool


def pulse_durations_to_symbols(
    durations_us: Iterable[int],
    *,
    symbol_us: int = BASE_SYMBOL_US,
    tolerance_us: int = 80,
) -> tuple[int, ...]:
    """Expand signed run lengths into OOK symbols.

    Each duration is rounded to the nearest symbol count, then checked against
    ``tolerance_us``. Positive durations become 1 and negative durations 0.
    """

    if symbol_us <= 0:
        raise ValueError("symbol_us must be positive")
    if tolerance_us < 0:
        raise ValueError("tolerance_us must not be negative")

    symbols: list[int] = []
    for index, duration in enumerate(durations_us):
        if duration == 0:
            raise ProtocolDecodeError(f"duration {index} is zero")
        magnitude = abs(duration)
        count = (magnitude + symbol_us // 2) // symbol_us
        if count < 1:
            raise ProtocolDecodeError(f"duration {index} is shorter than half a symbol")
        error = abs(magnitude - count * symbol_us)
        if error > tolerance_us:
            raise ProtocolDecodeError(
                f"duration {index} ({duration} us) is {error} us from a {symbol_us} us multiple"
            )
        symbols.extend((1 if duration > 0 else 0,) * count)
    return tuple(symbols)


def symbols_to_pulse_durations(
    symbols: Iterable[int], *, symbol_us: int = BASE_SYMBOL_US
) -> tuple[int, ...]:
    if symbol_us <= 0:
        raise ValueError("symbol_us must be positive")
    values = tuple(int(bit) for bit in symbols)
    if not values or any(bit not in (0, 1) for bit in values):
        raise ValueError("symbols must be a non-empty sequence of 0 and 1")

    pulses: list[int] = []
    state = values[0]
    count = 1
    for bit in values[1:]:
        if bit == state:
            count += 1
            continue
        pulses.append(count * symbol_us * (1 if state else -1))
        state = bit
        count = 1
    pulses.append(count * symbol_us * (1 if state else -1))
    return tuple(pulses)


def _crc12_from_encoded_payload(encoded_payload: Sequence[int]) -> int:
    if len(encoded_payload) != 7:
        raise ValueError("encoded payload must contain seven bytes")
    register = CRC12_INITIAL_REVERSED
    for encoded in encoded_payload:
        register ^= encoded
        for _ in range(8):
            if register & 1:
                register = (register >> 1) ^ CRC12_POLYNOMIAL_REVERSED
            else:
                register >>= 1
    return register & 0xFFF


def decode_waveband_symbols(symbols: Sequence[int]) -> WavebandFrame:
    """Decode and CRC-check one documented CEMENT V1.1 frame."""

    normalized = tuple(int(bit) for bit in symbols)
    if len(normalized) != FRAME_SYMBOLS:
        raise ProtocolDecodeError(
            f"expected {FRAME_SYMBOLS} symbols, got {len(normalized)}"
        )
    if any(bit not in (0, 1) for bit in normalized):
        raise ProtocolDecodeError("symbols must contain only 0 and 1")
    if normalized[: len(PREAMBLE_AND_SYNC)] != PREAMBLE_AND_SYNC:
        raise ProtocolDecodeError("preamble/sync does not match 101010101010101001")

    payload_bits = normalized[len(PREAMBLE_AND_SYNC) :]
    on_air_bytes = tuple(
        int("".join(str(bit) for bit in payload_bits[offset : offset + 8]), 2)
        for offset in range(0, len(payload_bits), 8)
    )
    try:
        decoded_values = tuple(_ON_AIR_TO_VALUE[value] for value in on_air_bytes)
    except KeyError as exc:
        raise ProtocolDecodeError(f"invalid 6b8b on-air byte: 0x{exc.args[0]:02X}") from exc

    encoded_payload = tuple(reverse_bits8(value) for value in on_air_bytes[1:8])
    crc12 = _crc12_from_encoded_payload(encoded_payload)
    expected_first = reverse_bits8(LINE_CODE_BIG_ENDIAN[crc12 & 0x3F])
    expected_last = reverse_bits8(LINE_CODE_BIG_ENDIAN[(crc12 >> 6) & 0x3F])
    crc_valid = on_air_bytes[0] == expected_first and on_air_bytes[8] == expected_last
    return WavebandFrame(
        symbols=normalized,
        on_air_bytes=on_air_bytes,
        decoded_values=decoded_values,
        payload_values=decoded_values[1:8],
        crc12=crc12,
        crc_valid=crc_valid,
    )


def decode_waveband_pulses(
    durations_us: Iterable[int],
    *,
    symbol_us: int = BASE_SYMBOL_US,
    tolerance_us: int = 80,
) -> WavebandFrame:
    symbols = pulse_durations_to_symbols(
        durations_us, symbol_us=symbol_us, tolerance_us=tolerance_us
    )
    return decode_waveband_symbols(symbols)


def encode_waveband_payload(
    payload_values: Sequence[int], *, symbol_us: int = BASE_SYMBOL_US
) -> tuple[int, ...]:
    """Encode seven 6-bit logical values into signed OOK pulse durations."""

    values = tuple(int(value) for value in payload_values)
    if len(values) != 7 or any(value < 0 or value > 0x3F for value in values):
        raise ValueError("payload_values must contain seven values in the range 0..63")

    encoded_payload = tuple(LINE_CODE_BIG_ENDIAN[value] for value in values)
    crc12 = _crc12_from_encoded_payload(encoded_payload)
    encoded_frame = (
        LINE_CODE_BIG_ENDIAN[crc12 & 0x3F],
        *encoded_payload,
        LINE_CODE_BIG_ENDIAN[(crc12 >> 6) & 0x3F],
    )
    symbols = list(PREAMBLE_AND_SYNC)
    for encoded in encoded_frame:
        symbols.extend((encoded >> bit_index) & 1 for bit_index in range(8))
    return symbols_to_pulse_durations(symbols, symbol_us=symbol_us)
