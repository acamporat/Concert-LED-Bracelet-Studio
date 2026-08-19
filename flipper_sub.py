"""Parser and writer for Flipper Zero Sub-GHz RAW ``.sub`` files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class FlipperSubError(ValueError):
    """Raised when a Flipper RAW file is malformed or unsupported."""


@dataclass(frozen=True)
class FlipperSubRaw:
    """A parsed Flipper Sub-GHz RAW waveform.

    Durations are in microseconds. Positive values mean carrier/high and
    negative values mean no carrier/low.
    """

    filetype: str
    version: int
    frequency_hz: int
    preset: str
    protocol: str
    raw_data: tuple[int, ...]
    extra_fields: tuple[tuple[str, str], ...] = ()

    @property
    def duration_us(self) -> int:
        return sum(abs(value) for value in self.raw_data)

    @property
    def carrier_on_us(self) -> int:
        return sum(value for value in self.raw_data if value > 0)

    @property
    def carrier_off_us(self) -> int:
        return sum(-value for value in self.raw_data if value < 0)


_REQUIRED_SINGLE_FIELDS = ("Filetype", "Version", "Frequency", "Preset", "Protocol")


def _one(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key, [])
    if not values:
        raise FlipperSubError(f"missing required field: {key}")
    if len(values) != 1:
        raise FlipperSubError(f"field {key} must appear exactly once")
    return values[0]


def _parse_positive_int(value: str, field: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise FlipperSubError(f"{field} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise FlipperSubError(f"{field} must be positive, got {parsed}")
    return parsed


def parse_flipper_sub_text(text: str, *, strict: bool = False) -> FlipperSubRaw:
    """Parse a Flipper RAW file from text.

    Multiple ``RAW_Data`` lines are concatenated in file order. By default the
    parser accepts real-world captures that begin low or contain adjacent
    durations with the same sign. ``strict=True`` enforces the official RAW
    requirement that data starts positive and alternates signs.
    """

    fields: dict[str, list[str]] = {}
    ordered_fields: list[tuple[str, str]] = []

    for line_number, original_line in enumerate(text.splitlines(), start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise FlipperSubError(f"line {line_number}: expected 'Field: value'")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise FlipperSubError(f"line {line_number}: empty field name")
        fields.setdefault(key, []).append(value)
        ordered_fields.append((key, value))

    for key in _REQUIRED_SINGLE_FIELDS:
        _one(fields, key)

    filetype = _one(fields, "Filetype")
    if filetype != "Flipper SubGhz RAW File":
        raise FlipperSubError(
            "unsupported Filetype; expected 'Flipper SubGhz RAW File', "
            f"got {filetype!r}"
        )

    version = _parse_positive_int(_one(fields, "Version"), "Version")
    if version != 1:
        raise FlipperSubError(f"unsupported Version: {version}")

    frequency_hz = _parse_positive_int(_one(fields, "Frequency"), "Frequency")
    preset = _one(fields, "Preset")
    if not preset:
        raise FlipperSubError("Preset must not be empty")
    protocol = _one(fields, "Protocol")
    if protocol != "RAW":
        raise FlipperSubError(f"unsupported Protocol: {protocol!r}; expected 'RAW'")

    raw_lines = fields.get("RAW_Data", [])
    if not raw_lines:
        raise FlipperSubError("missing required field: RAW_Data")

    raw_data: list[int] = []
    for raw_line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line:
            raise FlipperSubError(f"RAW_Data occurrence {raw_line_number} is empty")
        for token in raw_line.split():
            try:
                duration = int(token, 10)
            except ValueError as exc:
                raise FlipperSubError(f"invalid RAW_Data duration: {token!r}") from exc
            if duration == 0:
                raise FlipperSubError("RAW_Data durations must be non-zero")
            raw_data.append(duration)

    if strict:
        if raw_data[0] < 0:
            raise FlipperSubError("strict RAW_Data must start with a positive duration")
        for index, (previous, current) in enumerate(zip(raw_data, raw_data[1:]), start=1):
            if (previous > 0) == (current > 0):
                raise FlipperSubError(
                    f"strict RAW_Data signs do not alternate at values {index} and {index + 1}"
                )

    extras = tuple(
        (key, value)
        for key, value in ordered_fields
        if key not in {*_REQUIRED_SINGLE_FIELDS, "RAW_Data"}
    )
    return FlipperSubRaw(
        filetype=filetype,
        version=version,
        frequency_hz=frequency_hz,
        preset=preset,
        protocol=protocol,
        raw_data=tuple(raw_data),
        extra_fields=extras,
    )


def read_flipper_sub(path: str | Path, *, strict: bool = False) -> FlipperSubRaw:
    return parse_flipper_sub_text(Path(path).read_text(encoding="utf-8-sig"), strict=strict)


def format_flipper_sub(capture: FlipperSubRaw, *, values_per_line: int = 512) -> str:
    """Serialize a RAW capture using the documented Flipper file layout."""

    if values_per_line <= 0 or values_per_line > 512:
        raise ValueError("values_per_line must be between 1 and 512")
    lines = [
        f"Filetype: {capture.filetype}",
        f"Version: {capture.version}",
        f"Frequency: {capture.frequency_hz}",
        f"Preset: {capture.preset}",
    ]
    lines.extend(f"{key}: {value}" for key, value in capture.extra_fields)
    lines.append(f"Protocol: {capture.protocol}")
    for offset in range(0, len(capture.raw_data), values_per_line):
        values = capture.raw_data[offset : offset + values_per_line]
        lines.append("RAW_Data: " + " ".join(str(value) for value in values))
    return "\n".join(lines) + "\n"


def make_raw_capture(
    durations_us: Iterable[int],
    *,
    frequency_hz: int,
    preset: str = "FuriHalSubGhzPresetOok650Async",
) -> FlipperSubRaw:
    values = tuple(int(value) for value in durations_us)
    if not values or any(value == 0 for value in values):
        raise FlipperSubError("durations_us must contain only non-zero durations")
    if frequency_hz <= 0:
        raise FlipperSubError("frequency_hz must be positive")
    return FlipperSubRaw(
        filetype="Flipper SubGhz RAW File",
        version=1,
        frequency_hz=frequency_hz,
        preset=preset,
        protocol="RAW",
        raw_data=values,
    )
