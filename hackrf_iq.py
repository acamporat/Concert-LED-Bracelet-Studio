"""Timing-accurate OOK generation for HackRF signed complex int8 streams."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence


MIN_HACKRF_SAMPLE_RATE = 2_000_000
MAX_HACKRF_SAMPLE_RATE = 20_000_000
MIN_HACKRF_FREQUENCY = 1_000_000
MAX_HACKRF_FREQUENCY = 6_000_000_000


@dataclass(frozen=True)
class WaveformPlan:
    pulses_us: tuple[int, ...]
    repeat_count: int
    packet_duration_us: int
    inter_packet_delay_us: int
    duration_us: int


@dataclass(frozen=True)
class GenerationReport:
    output_path: Path
    sample_rate_hz: int
    amplitude: int
    sample_count: int
    byte_count: int
    duration_us: int
    repeat_count: int


def _validate_pulses(pulses_us: Iterable[int]) -> tuple[int, ...]:
    pulses = tuple(int(value) for value in pulses_us)
    if not pulses:
        raise ValueError("pulses_us must not be empty")
    if any(value == 0 for value in pulses):
        raise ValueError("pulse durations must be non-zero")
    return pulses


def _append_merged(target: list[int], duration: int) -> None:
    if target and (target[-1] > 0) == (duration > 0):
        target[-1] += duration
    else:
        target.append(duration)


def plan_waveform(
    pulses_us: Iterable[int],
    *,
    repeat_count: int | None = None,
    repeat_duration_s: float | None = None,
    inter_packet_delay_us: int = 0,
) -> WaveformPlan:
    """Create a repeat schedule while retaining complete packets."""

    packet = _validate_pulses(pulses_us)
    if repeat_count is not None and repeat_duration_s is not None:
        raise ValueError("repeat_count and repeat_duration_s are mutually exclusive")
    if inter_packet_delay_us < 0:
        raise ValueError("inter_packet_delay_us must not be negative")

    packet_duration_us = sum(abs(value) for value in packet)
    if repeat_duration_s is not None:
        if not math.isfinite(repeat_duration_s) or repeat_duration_s <= 0:
            raise ValueError("repeat_duration_s must be a positive finite number")
        target_us = math.ceil(repeat_duration_s * 1_000_000)
        cycle_us = packet_duration_us + inter_packet_delay_us
        repeats = max(1, math.ceil((target_us + inter_packet_delay_us) / cycle_us))
    else:
        repeats = 1 if repeat_count is None else repeat_count
        if repeats < 1:
            raise ValueError("repeat_count must be at least 1")

    scheduled: list[int] = []
    for index in range(repeats):
        for duration in packet:
            _append_merged(scheduled, duration)
        if index + 1 < repeats and inter_packet_delay_us:
            _append_merged(scheduled, -inter_packet_delay_us)

    duration_us = repeats * packet_duration_us + (repeats - 1) * inter_packet_delay_us
    return WaveformPlan(
        pulses_us=tuple(scheduled),
        repeat_count=repeats,
        packet_duration_us=packet_duration_us,
        inter_packet_delay_us=inter_packet_delay_us,
        duration_us=duration_us,
    )


def _round_positive_ratio(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def pulse_sample_counts(
    pulses_us: Iterable[int], *, sample_rate_hz: int
) -> tuple[tuple[bool, int], ...]:
    """Quantize cumulative pulse boundaries, avoiding per-pulse timing drift."""

    pulses = _validate_pulses(pulses_us)
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")

    cumulative_us = 0
    previous_boundary = 0
    result: list[tuple[bool, int]] = []
    for index, duration in enumerate(pulses):
        cumulative_us += abs(duration)
        boundary = _round_positive_ratio(cumulative_us * sample_rate_hz, 1_000_000)
        count = boundary - previous_boundary
        if count <= 0:
            raise ValueError(
                f"pulse {index} is too short to represent at {sample_rate_hz} samples/s"
            )
        result.append((duration > 0, count))
        previous_boundary = boundary
    return tuple(result)


def iter_cs8(
    pulses_us: Iterable[int],
    *,
    sample_rate_hz: int = 2_000_000,
    amplitude: int = 64,
    chunk_samples: int = 1_048_576,
) -> Iterator[bytes]:
    """Yield interleaved signed int8 I/Q chunks for baseband OOK.

    Carrier ON is represented by ``(I=amplitude, Q=0)``. Carrier OFF is
    ``(I=0, Q=0)``. ``hackrf_transfer`` upconverts the non-zero DC baseband
    value to the configured RF center frequency.
    """

    if amplitude < 1 or amplitude > 127:
        raise ValueError("amplitude must be in the range 1..127")
    if chunk_samples < 1:
        raise ValueError("chunk_samples must be positive")

    on_sample = bytes((amplitude, 0))
    off_sample = b"\x00\x00"
    for is_on, sample_count in pulse_sample_counts(
        pulses_us, sample_rate_hz=sample_rate_hz
    ):
        sample = on_sample if is_on else off_sample
        remaining = sample_count
        while remaining:
            count = min(remaining, chunk_samples)
            yield sample * count
            remaining -= count


def write_cs8(
    output_path: str | Path,
    plan: WaveformPlan,
    *,
    sample_rate_hz: int = 2_000_000,
    amplitude: int = 64,
    overwrite: bool = False,
) -> GenerationReport:
    """Atomically write a HackRF ``.cs8`` file."""

    output = Path(output_path)
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    temp_name: str | None = None
    byte_count = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as stream:
            temp_name = stream.name
            for chunk in iter_cs8(
                plan.pulses_us,
                sample_rate_hz=sample_rate_hz,
                amplitude=amplitude,
            ):
                stream.write(chunk)
                byte_count += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists() and not overwrite:
            raise FileExistsError(f"output already exists: {output}")
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    return GenerationReport(
        output_path=output.resolve(),
        sample_rate_hz=sample_rate_hz,
        amplitude=amplitude,
        sample_count=byte_count // 2,
        byte_count=byte_count,
        duration_us=plan.duration_us,
        repeat_count=plan.repeat_count,
    )


def build_hackrf_tx_command(
    *,
    frequency_hz: int,
    sample_rate_hz: int,
    tx_gain_db: int = 0,
    executable: str = "hackrf_transfer",
    serial_number: str | None = None,
    input_path: str | Path = "-",
) -> list[str]:
    if frequency_hz < MIN_HACKRF_FREQUENCY or frequency_hz > MAX_HACKRF_FREQUENCY:
        raise ValueError("frequency_hz must be in HackRF's supported 1 MHz..6 GHz range")
    if sample_rate_hz < MIN_HACKRF_SAMPLE_RATE or sample_rate_hz > MAX_HACKRF_SAMPLE_RATE:
        raise ValueError("sample_rate_hz must be in HackRF's supported 2..20 MHz range")
    if tx_gain_db < 0 or tx_gain_db > 47:
        raise ValueError("tx_gain_db must be in the range 0..47")

    command = [
        executable,
        "-t",
        str(input_path),
        "-f",
        str(frequency_hz),
        "-s",
        str(sample_rate_hz),
        "-x",
        str(tx_gain_db),
        "-a",
        "0",
        "-p",
        "0",
    ]
    if serial_number:
        command.extend(("-d", serial_number))
    return command


def stream_to_hackrf(
    plan: WaveformPlan,
    *,
    frequency_hz: int,
    sample_rate_hz: int = 2_000_000,
    amplitude: int = 64,
    tx_gain_db: int = 0,
    executable: str = "hackrf_transfer",
    serial_number: str | None = None,
) -> int:
    """Generate a temporary IQ file and transmit it with ``hackrf_transfer``.

    The temporary file is intentional: the Windows HackRF build can close a
    ``-t -`` stdin pipe immediately with ``EINVAL`` or ``EPIPE``. A real file
    matches the proven command-line workflow and is removed after transmission.
    """

    with tempfile.TemporaryDirectory(prefix="pixmob-hackrf-") as directory:
        iq_path = Path(directory) / "transmit.cs8"
        write_cs8(
            iq_path,
            plan,
            sample_rate_hz=sample_rate_hz,
            amplitude=amplitude,
        )
        command = build_hackrf_tx_command(
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
            tx_gain_db=tx_gain_db,
            executable=executable,
            serial_number=serial_number,
            input_path=iq_path,
        )
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"hackrf_transfer exited with code {completed.returncode}{suffix}"
            )
        return completed.returncode
