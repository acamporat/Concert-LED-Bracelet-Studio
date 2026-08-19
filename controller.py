"""High-level PixMob CEMENT V1.1 color/effect command construction."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from pixmob_protocol import encode_waveband_payload


MODE_CONTINUOUS = 0x00
MODE_ONE_SHOT = 0x10
MODE_FOREVER = 0x11

MODE_NAMES = {
    "continuous": MODE_CONTINUOUS,
    "one-shot": MODE_ONE_SHOT,
    "forever": MODE_FOREVER,
}

ATTACK_TIMES_MS = (0, 30, 100, 200, 500, 1000, 2000, 4000)
HOLD_TIMES_MS = (0, 30, 100, 200, 500, 1000, 2500, None)
RELEASE_TIMES_MS = (None, 30, 100, 200, 500, 1000, 2000, 4000)
RANDOM_PERCENT = (0, 10, 20, 35, 50, 65, 80, 95)


@dataclass(frozen=True)
class WavebandCommand:
    name: str
    red: int
    green: int
    blue: int
    attack: int = 1
    hold: int = 2
    release: int = 2
    random: int = 0
    group: int = 0
    mode: int = MODE_CONTINUOUS
    description: str = ""

    def __post_init__(self) -> None:
        for channel_name in ("red", "green", "blue"):
            value = getattr(self, channel_name)
            if value < 0 or value > 255:
                raise ValueError(f"{channel_name} must be in the range 0..255")
        for timing_name in ("attack", "hold", "release", "random"):
            value = getattr(self, timing_name)
            if value < 0 or value > 7:
                raise ValueError(f"{timing_name} must be in the range 0..7")
        if self.group < 0 or self.group > 31:
            raise ValueError("group must be in the range 0..31")
        if self.mode not in MODE_NAMES.values():
            raise ValueError("unsupported mode")

    @property
    def quantized_rgb(self) -> tuple[int, int, int]:
        return (self.red >> 2, self.green >> 2, self.blue >> 2)

    @property
    def payload_values(self) -> tuple[int, ...]:
        red6, green6, blue6 = self.quantized_rgb
        return (
            self.mode,
            green6,
            red6,
            blue6,
            (self.attack << 3) | self.random,
            (self.release << 3) | self.hold,
            self.group,
        )

    def pulses(self, *, symbol_us: int = 510) -> tuple[int, ...]:
        return encode_waveband_payload(self.payload_values, symbol_us=symbol_us)

    def with_overrides(
        self,
        *,
        attack: int | None = None,
        hold: int | None = None,
        release: int | None = None,
        random: int | None = None,
        group: int | None = None,
        mode: int | None = None,
    ) -> "WavebandCommand":
        return replace(
            self,
            attack=self.attack if attack is None else attack,
            hold=self.hold if hold is None else hold,
            release=self.release if release is None else release,
            random=self.random if random is None else random,
            group=self.group if group is None else group,
            mode=self.mode if mode is None else mode,
        )


def color_command(
    red: int,
    green: int,
    blue: int,
    *,
    name: str = "custom",
    attack: int = 1,
    hold: int = 2,
    release: int = 2,
    random: int = 0,
    group: int = 0,
    mode: int = MODE_CONTINUOUS,
) -> WavebandCommand:
    return WavebandCommand(
        name=name,
        red=red,
        green=green,
        blue=blue,
        attack=attack,
        hold=hold,
        release=release,
        random=random,
        group=group,
        mode=mode,
        description=f"custom RGB({red}, {green}, {blue})",
    )


PRESETS: dict[str, WavebandCommand] = {
    "red": WavebandCommand("red", 255, 0, 0, description="red pulse/fade"),
    "green": WavebandCommand("green", 0, 255, 0, description="green pulse/fade"),
    "blue": WavebandCommand("blue", 0, 0, 255, description="blue pulse/fade"),
    "white": WavebandCommand("white", 255, 255, 255, description="white pulse/fade"),
    "purple": WavebandCommand("purple", 180, 0, 255, description="purple pulse/fade"),
    "cyan": WavebandCommand("cyan", 0, 255, 255, description="cyan pulse/fade"),
    "gold": WavebandCommand("gold", 188, 156, 0, description="gold pulse/fade"),
    # This payload exactly matches the CRC-valid upstream gold_fade_in capture.
    "fade-gold": WavebandCommand(
        "fade-gold",
        188,
        156,
        0,
        attack=4,
        hold=3,
        release=2,
        random=0,
        description="upstream gold_fade_in timing and color",
    ),
    "fade-red": WavebandCommand(
        "fade-red", 255, 0, 0, attack=4, hold=3, release=2, description="slow red fade"
    ),
    "fade-blue": WavebandCommand(
        "fade-blue", 0, 0, 255, attack=4, hold=3, release=2, description="slow blue fade"
    ),
    "fade-white": WavebandCommand(
        "fade-white",
        255,
        255,
        255,
        attack=4,
        hold=3,
        release=2,
        description="slow white fade",
    ),
    # Matches the decoded payload of the upstream nothing.sub keepalive frame.
    "keepalive": WavebandCommand(
        "keepalive",
        0,
        0,
        0,
        attack=1,
        hold=7,
        release=1,
        random=0,
        description="upstream nothing/wake payload; no visible color expected",
    ),
}
PRESETS["off"] = replace(PRESETS["keepalive"], name="off")


_HEX_COLOR = re.compile(r"^#?(?P<rgb>[0-9a-fA-F]{6})$")


def resolve_command(value: str) -> WavebandCommand:
    """Resolve a preset, ``#RRGGBB``, or ``R,G,B`` value."""

    normalized = value.strip().lower()
    if normalized in PRESETS:
        return PRESETS[normalized]

    hex_match = _HEX_COLOR.match(normalized)
    if hex_match:
        rgb = hex_match.group("rgb")
        return color_command(
            int(rgb[0:2], 16),
            int(rgb[2:4], 16),
            int(rgb[4:6], 16),
            name=f"#{rgb.upper()}",
        )

    parts = [part.strip() for part in normalized.split(",")]
    if len(parts) == 3:
        try:
            channels = tuple(int(part, 10) for part in parts)
        except ValueError as exc:
            raise ValueError(f"invalid RGB color: {value!r}") from exc
        return color_command(*channels, name=value)

    raise ValueError(
        f"unknown control target {value!r}; use a preset, #RRGGBB, or R,G,B"
    )


def mode_name(mode: int) -> str:
    for name, value in MODE_NAMES.items():
        if value == mode:
            return name
    return f"0x{mode:02X}"
