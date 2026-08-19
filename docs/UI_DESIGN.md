# Concert LED Bracelet Studio Flow Builder design

The implementation reference is
[`design/pixmob-flow-builder-concept.png`](design/pixmob-flow-builder-concept.png).
It is a complete local flow editor, not a marketing page.

## Design system

- Background: true deep navy `#050c13`; panels `#0b1824`; borders `#294052`.
- Primary text: `#f5f8fb`; supporting text: `#9badbb`.
- Interaction accent: electric cyan `#3db7f2`; RF warning/action: amber-orange.
- Typography: Inter/Aptos/Segoe UI fallback with deliberately compact 11–17 px
  control typography and a 24 px product name.
- Containers: a block-library rail, open flow canvas, selected-block inspector,
  and bottom preview/transmit dock. The interface does not use a generic
  dashboard card grid.
- Radius: 6–9 px. Shadows are restrained and the selected light color is the
  only saturated ambient glow.
- Motion: 180 ms flow-preview debounce and restrained 130–160 ms transitions;
  reduced-motion preferences are respected.

## Component inventory

- Header hardware state: HackRF tool availability, fixed launch frequency, and
  fixed TX VGA gain.
- Block library: draggable or click-to-add Color, Fade, Wait, Loop, Wake, and Off.
- Flow canvas: ordered blocks, nested loop contents, drag slots, duplicate,
  delete, clear, Undo, selected state, and Start/End terminals.
- Block inspector: context-specific color/effect, wait, loop, wake, off, group,
  and RF retry controls.
- Passive preview: backend-expanded timeline, action count, RF/wait duration,
  and validation errors without transmission.
- Command dock: activity log, preview action, persistent arm switch, and Run Flow.

## Safety interaction

The normal launch is preview-only. A transmit-capable session requires
`--allow-transmit` at server startup. Even then, every transmission requires the
user to arm the switch and click Run Flow. Once enabled, the arm state persists
until the user explicitly switches it off, allowing rapid effect iteration.
The server binds only to `127.0.0.1`, requires a random same-session token on
POST requests, disables the HackRF RF amplifier and antenna power through the
shared backend, and defaults to 0 dB TX VGA gain.
