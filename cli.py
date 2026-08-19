"""Command-line interface for PixMob capture inspection and HackRF IQ generation."""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from controller import (
    ATTACK_TIMES_MS,
    HOLD_TIMES_MS,
    MODE_FOREVER,
    MODE_NAMES,
    PRESETS,
    RANDOM_PERCENT,
    RELEASE_TIMES_MS,
    WavebandCommand,
    mode_name,
    resolve_command,
)
from flipper_sub import FlipperSubError, read_flipper_sub
from hackrf_iq import (
    MAX_HACKRF_SAMPLE_RATE,
    MIN_HACKRF_SAMPLE_RATE,
    build_hackrf_tx_command,
    plan_waveform,
    stream_to_hackrf,
    write_cs8,
)
from pixmob_protocol import ProtocolDecodeError, decode_waveband_pulses


_ENGINEERING_NUMBER = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<prefix>[kKmMgG]?)\s*(?:[hH][zZ])?\s*$"
)


def parse_engineering_int(value: str) -> int:
    """Parse values such as ``2000000``, ``2M``, or ``915MHz``."""

    match = _ENGINEERING_NUMBER.match(value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid engineering number: {value!r}")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[
        match.group("prefix").lower()
    ]
    result = float(match.group("number")) * multiplier
    if not result.is_integer() or result <= 0:
        raise argparse.ArgumentTypeError(f"value must resolve to a positive integer: {value!r}")
    return int(result)


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _shell_join(command: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _add_waveform_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sample-rate",
        type=parse_engineering_int,
        default=2_000_000,
        metavar="HZ",
        help="complex sample rate; accepts 2M-style values (default: 2M)",
    )
    parser.add_argument(
        "--amplitude",
        type=int,
        choices=range(1, 128),
        default=64,
        metavar="1..127",
        help="signed int8 I amplitude while carrier is on (default: 64)",
    )
    repeats = parser.add_mutually_exclusive_group()
    repeats.add_argument(
        "--repeat-count",
        type=_positive_int,
        metavar="N",
        help="number of complete packet repetitions (default: 1)",
    )
    repeats.add_argument(
        "--repeat-duration",
        type=float,
        metavar="SECONDS",
        help="repeat complete packets until at least this duration is reached",
    )
    parser.add_argument(
        "--inter-packet-delay-us",
        type=_nonnegative_int,
        default=0,
        metavar="US",
        help="carrier-off gap inserted only between repetitions (default: 0)",
    )
    parser.add_argument(
        "--strict-sub",
        action="store_true",
        help="enforce the official positive-starting, alternating-sign RAW format",
    )


def _load_plan(args: argparse.Namespace):
    capture = read_flipper_sub(args.input, strict=args.strict_sub)
    plan = plan_waveform(
        capture.raw_data,
        repeat_count=args.repeat_count,
        repeat_duration_s=args.repeat_duration,
        inter_packet_delay_us=args.inter_packet_delay_us,
    )
    return capture, plan


def _format_duration(duration_us: int) -> str:
    return f"{duration_us / 1_000_000:.6f} s ({duration_us} us)"


def command_convert(args: argparse.Namespace) -> int:
    capture, plan = _load_plan(args)
    report = write_cs8(
        args.output,
        plan,
        sample_rate_hz=args.sample_rate,
        amplitude=args.amplitude,
        overwrite=args.force,
    )
    print(f"Wrote: {report.output_path}")
    print(f"Source frequency metadata: {capture.frequency_hz} Hz (not stored in .cs8)")
    print(f"Waveform: {_format_duration(plan.duration_us)}, {plan.repeat_count} packet(s)")
    print(f"IQ: {report.sample_count} complex samples, {report.byte_count} bytes, cs8 I/Q")
    print(f"OOK levels: ON=({args.amplitude},0), OFF=(0,0)")
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    capture = read_flipper_sub(args.input, strict=args.strict_sub)
    print(f"File: {Path(args.input).resolve()}")
    print(f"Frequency metadata: {capture.frequency_hz} Hz")
    print(f"Preset: {capture.preset}")
    print(f"Durations: {len(capture.raw_data)}")
    print(f"Total: {_format_duration(capture.duration_us)}")
    print(f"Carrier on: {capture.carrier_on_us} us")
    print(f"Carrier off: {capture.carrier_off_us} us")
    try:
        frame = decode_waveband_pulses(
            capture.raw_data,
            symbol_us=args.symbol_us,
            tolerance_us=args.tolerance_us,
        )
    except ProtocolDecodeError as exc:
        print(f"Waveband frame: not decoded ({exc})")
        return 0

    encoded = " ".join(f"{value:02X}" for value in frame.on_air_bytes)
    decoded = " ".join(f"{value:02X}" for value in frame.decoded_values)
    payload = " ".join(f"{value:02X}" for value in frame.payload_values)
    print("Waveband frame: documented 90-symbol CEMENT V1.1 layout")
    print(f"On-air 6b8b bytes: {encoded}")
    print(f"Decoded 6-bit values (including CRC halves): {decoded}")
    print(f"Logical payload (mode + six data values): {payload}")
    print(f"CRC-12: 0x{frame.crc12:03X} ({'valid' if frame.crc_valid else 'INVALID'})")
    return 0


def command_transmit(args: argparse.Namespace) -> int:
    capture, plan = _load_plan(args)
    executable = shutil.which(args.hackrf_transfer) or args.hackrf_transfer
    command = build_hackrf_tx_command(
        frequency_hz=args.frequency,
        sample_rate_hz=args.sample_rate,
        tx_gain_db=args.tx_gain,
        executable=executable,
        serial_number=args.serial,
        input_path="<temporary.cs8>",
    )

    print("Transmit plan:")
    print(f"  input: {Path(args.input).resolve()}")
    print(f"  file frequency metadata: {capture.frequency_hz} Hz")
    print(f"  requested RF frequency: {args.frequency} Hz")
    print(f"  waveform: {_format_duration(plan.duration_us)}, {plan.repeat_count} packet(s)")
    print(f"  sample rate: {args.sample_rate} Hz")
    print(f"  digital amplitude: {args.amplitude}/127")
    print(f"  TX VGA gain: {args.tx_gain} dB; RF amp: disabled; antenna power: disabled")
    print(f"  command: {_shell_join(command)}")

    if not args.yes_transmit:
        print("Dry run only: RF transmission was NOT started.")
        print("Add --yes-transmit only after verifying frequency, antenna, and local rules.")
        return 0

    if shutil.which(args.hackrf_transfer) is None and not Path(args.hackrf_transfer).exists():
        raise FileNotFoundError(f"hackrf_transfer was not found: {args.hackrf_transfer}")

    print("Starting explicitly authorized transmission...")
    stream_to_hackrf(
        plan,
        frequency_hz=args.frequency,
        sample_rate_hz=args.sample_rate,
        amplitude=args.amplitude,
        tx_gain_db=args.tx_gain,
        executable=executable,
        serial_number=args.serial,
    )
    print("Transmission complete.")
    return 0


def _timing_label(value: int | None, table: tuple[int | None, ...], zero_label: str) -> str:
    if value is None:
        return "unchanged"
    mapped = table[value]
    return zero_label if mapped is None else f"{mapped} ms"


def _execute_control_command(
    args: argparse.Namespace,
    base_command: WavebandCommand,
    *,
    target_name: str,
    duration_override_s: float | None = None,
) -> int:
    requested_mode = None if args.mode is None else MODE_NAMES[args.mode]
    command_spec = base_command.with_overrides(
        attack=args.attack,
        hold=args.hold,
        release=args.release,
        random=args.random,
        group=args.group,
        mode=requested_mode,
    )
    if command_spec.mode == MODE_FOREVER and not args.allow_persistent:
        raise ValueError(
            "forever mode may persist across a battery cycle; add --allow-persistent to acknowledge it"
        )

    is_wake = target_name == "wake"
    default_duration = 20.0 if is_wake else 0.6
    repeat_duration = (
        duration_override_s
        if duration_override_s is not None
        else args.transmit_duration
        if args.transmit_duration is not None
        else default_duration
    )
    gap_us = (
        args.inter_packet_delay_us
        if args.inter_packet_delay_us is not None
        else 4_080
        if is_wake
        else 34_100
    )
    pulses = command_spec.pulses(symbol_us=args.symbol_us)
    plan = plan_waveform(
        pulses,
        repeat_count=args.repeat_count,
        repeat_duration_s=None if args.repeat_count is not None else repeat_duration,
        inter_packet_delay_us=gap_us,
    )
    frame = decode_waveband_pulses(pulses, symbol_us=args.symbol_us, tolerance_us=0)
    executable = shutil.which(args.hackrf_transfer) or args.hackrf_transfer
    hackrf_command = build_hackrf_tx_command(
        frequency_hz=args.frequency,
        sample_rate_hz=args.sample_rate,
        tx_gain_db=args.tx_gain,
        executable=executable,
        serial_number=args.serial,
        input_path="<temporary.cs8>",
    )

    payload = " ".join(f"{value:02X}" for value in command_spec.payload_values)
    on_air = " ".join(f"{value:02X}" for value in frame.on_air_bytes)
    red6, green6, blue6 = command_spec.quantized_rgb
    print(f"Control target: {target_name}")
    print(
        f"  RGB requested: ({command_spec.red}, {command_spec.green}, {command_spec.blue}); "
        f"encoded 6-bit: ({red6}, {green6}, {blue6})"
    )
    print(f"  mode: {mode_name(command_spec.mode)}; group: {command_spec.group}")
    print(
        "  effect: "
        f"attack={_timing_label(command_spec.attack, ATTACK_TIMES_MS, '0 ms')}, "
        f"hold={_timing_label(command_spec.hold, HOLD_TIMES_MS, 'infinite')}, "
        f"release={_timing_label(command_spec.release, RELEASE_TIMES_MS, 'background')}, "
        f"random={RANDOM_PERCENT[command_spec.random]}%"
    )
    print(f"  logical payload: {payload}")
    print(f"  on-air bytes: {on_air}; CRC-12=0x{frame.crc12:03X} valid")
    print(f"  RF frequency: {args.frequency} Hz; symbol: {args.symbol_us} us")
    print(f"  waveform: {_format_duration(plan.duration_us)}, {plan.repeat_count} packet(s)")
    print(f"  packet gap: {gap_us} us; sample rate: {args.sample_rate} Hz")
    print(
        f"  digital amplitude: {args.amplitude}/127; TX gain: {args.tx_gain} dB; "
        "RF amp: disabled; antenna power: disabled"
    )
    print(f"  command: {_shell_join(hackrf_command)}")

    if not args.yes_transmit:
        print("Dry run only: RF transmission was NOT started.")
        return 0
    if shutil.which(args.hackrf_transfer) is None and not Path(args.hackrf_transfer).exists():
        raise FileNotFoundError(f"hackrf_transfer was not found: {args.hackrf_transfer}")

    print("Starting explicitly authorized control transmission...")
    stream_to_hackrf(
        plan,
        frequency_hz=args.frequency,
        sample_rate_hz=args.sample_rate,
        amplitude=args.amplitude,
        tx_gain_db=args.tx_gain,
        executable=executable,
        serial_number=args.serial,
    )
    print("Control transmission complete.")
    return 0


def _run_control_target(
    args: argparse.Namespace, target: str, *, duration_override_s: float | None = None
) -> int:
    normalized = target.strip().lower()
    if normalized == "wake":
        return _execute_control_command(
            args,
            PRESETS["keepalive"],
            target_name="wake",
            duration_override_s=duration_override_s,
        )
    return _execute_control_command(
        args,
        resolve_command(target),
        target_name=target,
        duration_override_s=duration_override_s,
    )


def command_presets(_args: argparse.Namespace) -> int:
    print("Available control targets:")
    for name, preset in PRESETS.items():
        print(f"  {name:<12} {preset.description}")
    print("  wake         repeat keepalive packets for 20 seconds by default")
    print("  #RRGGBB      arbitrary hexadecimal color")
    print("  R,G,B        arbitrary decimal color")
    return 0


def command_control(args: argparse.Namespace) -> int:
    if args.target:
        return _run_control_target(args, args.target)

    print("Interactive PixMob controller")
    print("Enter a preset, #RRGGBB, R,G,B, 'wake [seconds]', 'list', or 'quit'.")
    print(
        "RF is enabled for this session."
        if args.yes_transmit
        else "Dry-run session: no RF will be transmitted."
    )
    while True:
        try:
            line = input("pixmob> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in {"quit", "exit", "q"}:
            return 0
        if line.lower() in {"list", "help", "?"}:
            command_presets(args)
            continue
        if line.lower().startswith("wake"):
            parts = line.split()
            if len(parts) > 2:
                print("error: use 'wake' or 'wake SECONDS'", file=sys.stderr)
                continue
            duration = None
            if len(parts) == 2:
                try:
                    duration = float(parts[1])
                except ValueError:
                    print("error: wake duration must be a number", file=sys.stderr)
                    continue
                if duration <= 0:
                    print("error: wake duration must be positive", file=sys.stderr)
                    continue
            _run_control_target(args, "wake", duration_override_s=duration)
            continue
        if line.lower().startswith("rgb "):
            parts = line.split()
            if len(parts) == 4:
                line = ",".join(parts[1:])
        try:
            _run_control_target(args, line)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)


def command_capture(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not args.force:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which(args.hackrf_transfer) or args.hackrf_transfer
    if shutil.which(args.hackrf_transfer) is None and not Path(args.hackrf_transfer).exists():
        raise FileNotFoundError(f"hackrf_transfer was not found: {args.hackrf_transfer}")
    if args.sample_rate < MIN_HACKRF_SAMPLE_RATE or args.sample_rate > MAX_HACKRF_SAMPLE_RATE:
        raise ValueError("sample rate must be in HackRF's supported 2..20 MHz range")
    if args.duration <= 0:
        raise ValueError("duration must be positive")

    sample_count = round(args.sample_rate * args.duration)
    command = [
        executable,
        "-r",
        str(output),
        "-f",
        str(args.frequency),
        "-s",
        str(args.sample_rate),
        "-n",
        str(sample_count),
        "-l",
        str(args.lna_gain),
        "-g",
        str(args.vga_gain),
        "-a",
        "0",
        "-p",
        "0",
    ]
    if args.serial:
        command.extend(("-d", args.serial))
    print(f"Passive capture command: {_shell_join(command)}")
    if args.dry_run:
        print("Dry run only: capture was not started.")
        return 0
    subprocess.run(command, check=True)
    print(f"Captured {sample_count} complex samples to {output.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="concert-led-bracelet",
        description="Convert and inspect Flipper Sub-GHz RAW OOK captures for HackRF.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="convert a RAW .sub file to HackRF cs8")
    convert.add_argument("input", type=Path)
    convert.add_argument("output", type=Path)
    _add_waveform_arguments(convert)
    convert.add_argument("--force", action="store_true", help="replace an existing output file")
    convert.set_defaults(handler=command_convert)

    inspect = subparsers.add_parser("inspect", help="inspect RAW timing and try Waveband decoding")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--strict-sub", action="store_true")
    inspect.add_argument("--symbol-us", type=_positive_int, default=510)
    inspect.add_argument("--tolerance-us", type=_nonnegative_int, default=80)
    inspect.set_defaults(handler=command_inspect)

    transmit = subparsers.add_parser(
        "transmit", help="stream OOK IQ to HackRF (dry-run unless explicitly authorized)"
    )
    transmit.add_argument("input", type=Path)
    _add_waveform_arguments(transmit)
    transmit.add_argument(
        "--frequency",
        type=parse_engineering_int,
        required=True,
        metavar="HZ",
        help="intentional RF center frequency; never inferred silently",
    )
    transmit.add_argument(
        "--tx-gain", type=int, choices=range(0, 48), default=0, metavar="0..47"
    )
    transmit.add_argument("--serial", help="HackRF serial number when multiple devices are attached")
    transmit.add_argument("--hackrf-transfer", default="hackrf_transfer")
    transmit.add_argument(
        "--yes-transmit",
        action="store_true",
        help="actually key the HackRF after displaying the complete plan",
    )
    transmit.set_defaults(handler=command_transmit)

    presets = subparsers.add_parser("presets", help="list generated controller targets")
    presets.set_defaults(handler=command_presets)

    control = subparsers.add_parser(
        "control",
        help="send a generated color/effect or open the interactive controller",
    )
    control.add_argument(
        "target",
        nargs="?",
        help="preset, wake, #RRGGBB, or R,G,B; omit for an interactive prompt",
    )
    control.add_argument(
        "--frequency",
        type=parse_engineering_int,
        required=True,
        metavar="HZ",
        help="intentional RF center frequency; use the frequency validated on your wristband",
    )
    control.add_argument("--sample-rate", type=parse_engineering_int, default=2_000_000)
    control.add_argument(
        "--amplitude", type=int, choices=range(1, 128), default=64, metavar="1..127"
    )
    control.add_argument(
        "--tx-gain", type=int, choices=range(0, 48), default=0, metavar="0..47"
    )
    control.add_argument("--symbol-us", type=_positive_int, default=510)
    control_repeats = control.add_mutually_exclusive_group()
    control_repeats.add_argument("--repeat-count", type=_positive_int, metavar="N")
    control_repeats.add_argument(
        "--transmit-duration",
        type=float,
        metavar="SECONDS",
        help="RF burst duration (default: 0.6; wake: 20)",
    )
    control.add_argument(
        "--inter-packet-delay-us",
        type=_nonnegative_int,
        default=None,
        metavar="US",
        help="default: 34100 for effects; 4080 for wake",
    )
    control.add_argument("--attack", type=int, choices=range(8))
    control.add_argument("--hold", type=int, choices=range(8))
    control.add_argument("--release", type=int, choices=range(8))
    control.add_argument("--random", type=int, choices=range(8))
    control.add_argument("--group", type=int, choices=range(32))
    control.add_argument("--mode", choices=tuple(MODE_NAMES))
    control.add_argument(
        "--allow-persistent",
        action="store_true",
        help="acknowledge that forever mode may persist across a battery cycle",
    )
    control.add_argument("--serial")
    control.add_argument("--hackrf-transfer", default="hackrf_transfer")
    control.add_argument(
        "--yes-transmit",
        action="store_true",
        help="actually key the HackRF; otherwise controller commands are dry runs",
    )
    control.set_defaults(handler=command_control)

    capture = subparsers.add_parser("capture", help="passively record signed int8 IQ from HackRF")
    capture.add_argument("output", type=Path)
    capture.add_argument("--frequency", type=parse_engineering_int, required=True, metavar="HZ")
    capture.add_argument("--sample-rate", type=parse_engineering_int, default=8_000_000)
    capture.add_argument("--duration", type=float, default=5.0, metavar="SECONDS")
    capture.add_argument("--lna-gain", type=int, choices=range(0, 41, 8), default=16)
    capture.add_argument("--vga-gain", type=int, choices=range(0, 63, 2), default=16)
    capture.add_argument("--serial")
    capture.add_argument("--hackrf-transfer", default="hackrf_transfer")
    capture.add_argument("--force", action="store_true")
    capture.add_argument("--dry-run", action="store_true")
    capture.set_defaults(handler=command_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (FlipperSubError, FileNotFoundError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
