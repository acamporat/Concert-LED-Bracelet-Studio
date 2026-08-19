# Protocol findings and confidence

Research snapshot: 2026-08-18. The conclusions below separate waveform facts,
CEMENT V1.1 reverse-engineering results, and W2 Gen3 inference.

## Flipper RAW encoding

Flipper's official RAW format defines each `RAW_Data` integer as a duration in
microseconds. Positive and negative signs alternate signal states; Flipper's
BinRAW documentation makes the polarity explicit as high/carrier and low/no
carrier. A long file may contain multiple `RAW_Data` lines, which are one
continuous sequence rather than separate packets.

The upstream edited PixMob RF captures use:

- `Protocol: RAW`
- `Preset: FuriHalSubGhzPresetOok650Async`
- `Frequency: 915000000` in the US folder
- `Frequency: 868000000` in the EU/UK folder
- signed runs that are exact multiples of 510 us

The 868 and 915 versions of `gold_fade_in.sub` contain identical pulse data;
only their frequency headers differ.

## Documented CEMENT V1.1 frame

The newer `sueppchen/PixMob_waveband` teardown and working CC1101 library
identify this frame for an RF Waveband whose PCB is labeled `CEMENT V1.1`:

```text
90 on-air symbols total
  16  alternating preamble: 1010101010101010
   2  sync:                 01
   8  checksum A, 6b8b encoded
   8  mode, 6b8b encoded
  48  six data values, each 6b8b encoded
   8  checksum B, 6b8b encoded
```

The decoded logical message is seven 6-bit values: mode plus six data values.
Each logical value is mapped through a 64-entry 6b8b table. The table limits
consecutive high and low runs to retain receiver synchronization. Bytes are
sent least-significant-bit first in the current library.

The checksum is a reversed CRC-12 calculated over the seven already-6b8b-
encoded payload bytes:

- reversed initial value: `0xC69` (reverse of `0x963`)
- reversed polynomial: `0x8F3` (reverse of `0xCF1`)
- low six CRC bits are line-coded into checksum A
- high six CRC bits are line-coded into checksum B

The included upstream `gold_fade_in` waveform expands to exactly 90 symbols:

```text
preamble/sync: 101010101010101001
on-air bytes:  94 84 91 B5 84 8C 45 84 AD
payload:       00 27 2F 00 20 13 00
CRC-12:        valid
```

This provides an internal consistency check stronger than merely noticing
510 us multiples.

## Timing discrepancy: 500 versus 510 us

The edited Flipper files quantize observed timings to 510 us. The newer working
CC1101 library uses a 500 us `BIT_TIME`. Both produce a roughly 45 ms, 90-symbol
frame and have reportedly operated CEMENT V1.1 hardware.

The converter therefore preserves source durations exactly. It does not
silently normalize 510 us captures to 500 us. A generated logical frame uses
510 us by default but exposes the symbol duration as a library parameter.
Hardware testing can compare both after the wristband variant is confirmed.

## Frequency discrepancy: nominal versus observed center

The older capture set uses 868.000 and 915.000 MHz. Its RF README reports that
868.000 MHz gave the best tested range on a donated European Waveband and that
the same command pulses worked after changing frequency.

The CEMENT V1.1 teardown instead reports receiver crystals of approximately
24.8117 MHz (EU) and 26.1522 MHz (US), multiplied by 35 to about 868.4 and
915.327 MHz. Its library constants are 868.41 and 915.33 MHz. These results are
not necessarily contradictory: receiver bandwidth, hardware revision,
oscillator error, Flipper preset bandwidth, and tune granularity can all allow
reception away from the nominal center.

For W2 Gen3, neither pair should be treated as confirmed. An original-controller
capture or positive component/crystal identification is the reliable route.

## W2 Gen3 identification confidence

| Evidence | What it establishes | Confidence |
|---|---|---|
| Label “W2 Gen3” only | Product/factory naming clue | Low |
| Two LEDs / similar housing | Form factor | Low |
| Officially identified Waveband 2 | RF activation, but not exact protocol revision/frequency | Medium |
| Visible IR receiver | IR capability | High for IR presence; low for exact IR protocol |
| PCB `CEMENT V1.1` plus CMT2210LH/crystal layout | Documented RF hardware family | High |
| Original-controller IQ capture | Actual center, modulation, symbol timing, frame | Very high |
| Valid low-power replay response | End-to-end compatibility for that command/setup | Conclusive practical test |

PixMob's own product pages distinguish X2 as infrared and Waveband/Waveband 2
as radio-frequency. They do not publish a mapping for “W2 Gen3.” The public
community hardware table lists CEMENT V1.1 as RF/Waveband but also does not list
W2 Gen3.

## Hardware evidence that would resolve the uncertainty

Without transmitting, collect clear photos of:

1. all external labels and molded markings;
2. both PCB sides, if the owned unit can be opened non-destructively;
3. close-ups of the receiver IC, crystal, antenna trace, and any dark IR sensor;
4. battery configuration and PCB revision text.

If an original controller becomes available, passively record separate 8 MHz
IQ windows centered at 868.4 and 915.33 MHz. A single window at either center
covers its corresponding rounded 868.0/915.0 candidate. Record quiet baselines
and isolated controller actions at low RX gain before increasing gain.

## Source boundary

The 90-symbol framing, 6b8b table, CRC parameters, CEMENT parts, and crystal-
derived frequencies come from public reverse engineering, not PixMob protocol
documentation. The official PixMob sources confirm only the high-level split
between RF Waveband products and IR X products.
