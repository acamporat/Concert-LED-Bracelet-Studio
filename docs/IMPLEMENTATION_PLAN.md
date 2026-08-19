# Technical implementation plan

## Phase 0 — evidence baseline (complete)

- Pin upstream reference commits and preserve fixture provenance.
- Verify Flipper RAW semantics against official documentation.
- Compare edited 868/915 captures byte-for-byte at the pulse layer.
- Cross-check the older 510 us capture notes against the newer CEMENT V1.1
  implementation, 6b8b mapping, frame layout, CRC, and crystal frequencies.
- Keep W2 Gen3 compatibility explicitly unconfirmed.

## Phase 1 — conversion and safe device I/O (complete)

- `flipper_sub.py`: generic RAW parser/writer with multiple-line support.
- `hackrf_iq.py`: cumulative-boundary timing quantization and chunked temporary-file cs8 OOK.
- `pixmob_protocol.py`: optional CEMENT frame encode/decode and CRC validation.
- `cli.py`: inspect, convert, explicit/dry-run transmit, and passive capture.
- `concert_led_bracelet_studio.py`: primary direct Python entry point.
- `pixmob_hackrf.py`: retained compatibility entry point.
- `commands/`: small, attributed known-capture fixtures.
- `captures/`: ignored local recording location.
- Unit tests against known 868/915 commands; no RF tests.

Acceptance checks:

- 510 us at 2 MHz produces exactly 1,020 complex samples.
- ON is a nonzero complex sample and OFF is exactly zero.
- A 45.9 ms frame at 2 MHz is exactly 91,800 samples / 183,600 bytes.
- Repetition never truncates a packet.
- Fractional sample boundaries do not accumulate timing drift.
- Known gold and nothing frames decode to 90 symbols with valid CRC-12.
- Transmit defaults to a dry run, gain 0, RF amp off, and antenna power off.

## Phase 2 — passive IQ analysis (next)

Implement in `tools/` with NumPy as an optional analysis dependency:

1. Generate signed int8 IQ incrementally without loading a large recording in memory.
2. Calculate magnitude-squared envelope and robust noise-floor statistics.
3. Apply hysteretic ON/OFF thresholding and minimum-run filtering.
4. Export signed pulse durations and reconstruct Flipper RAW `.sub` files.
5. Cluster run lengths against candidate 500 and 510 us symbol intervals.
6. Search for the 18-symbol CEMENT preamble/sync and validate frame CRC.
7. Compare decoded or raw frames against the `commands/` catalog.
8. Produce a downsampled envelope plot with transition annotations.

Acceptance checks:

- Synthetic IQ round-trips to the original pulse list within one sample.
- Thresholding works with configurable noise, DC offset, and amplitude.
- Large captures are processed in bounded memory.
- Unknown frames are reported as unknown rather than forced into CEMENT format.

## Phase 3 — command catalog and ergonomic CLI (complete)

Implemented after the reported successful 915 MHz replay:

- Added `controller.py` with RGB quantization, effect fields, groups, modes,
  presets, arbitrary color parsing, and CEMENT frame generation.
- Added `control TARGET`, `presets`, and an interactive terminal controller.
- Generated `fade-gold` and `keepalive` are test-proven byte/pulse-identical to
  the upstream ground-truth captures.
- Arbitrary commands are displayed with logical payload, on-air bytes, CRC,
  RF settings, and duration before the explicit transmit gate.
- Persistent mode requires a separate `--allow-persistent` acknowledgement.

## Phase 4 — owned-device bench validation and catalog expansion

Proceed only after visual/passive identification and local radio-rule review:

1. Record whether the reported successful 915 MHz run visibly activated the
   wristband and which effect appeared.
2. Photograph and identify the W2 Gen3 PCB/receiver path.
3. Bench-test generated `fade-gold`, then red, blue, white, and off at gain 0.
4. Compare generated commands against raw replay before testing timing changes.
5. If needed, compare 500 versus 510 us and nearby evidence-backed centers.
6. Test bounded wake repetitions, increasing duration before gain.
7. Log exact hardware, antenna/attenuation, distance, frequency, rate, timing,
   amplitude, gain, command hash, and observed response.

No automated sweep transmitter or broad frequency brute force is planned.
