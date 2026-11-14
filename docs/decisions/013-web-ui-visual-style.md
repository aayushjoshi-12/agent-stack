# ADR 013 — Visual style of the web gateway (P24)

**Status:** accepted (2026-11-14). Chosen by the assistant from the owner's list of 19 styles
(skeuomorphism, neumorphism, art nouveau, liquid chrome, De Stijl, Memphis, psychedelic,
vaporwave, steampunk, grunge, paper cutout, glitch, anti-design, 3D inflated, isometric,
ukiyo-e, brutalism, Bauhaus, mid-century modern). The owner may overrule it before the UI
is built.

## Decision

**Neo-brutalism (17), with Bauhaus (18) discipline for layout and colour.**

- Flat surfaces, thick 2 px borders, hard offset shadows only on interactive elements, no
  gradients or blur.
- Monospace for everything an agent produces (tool calls, shell output, code); one grotesque
  sans for chrome. Type scale and spacing from a small token set.
- Bauhaus part: a strict grid, geometric status marks (circle = running, square = done,
  triangle = needs approval), and a three-colour accent palette on a paper-white / near-black
  base. Dark and light themes, both AA contrast.
- The TUI already looks like this (cards, hard edges, mono); the web UI is the same
  product in a bigger window.

## Why not the others

- Glitch, vaporwave, psychedelic, grunge, anti-design, Memphis, art nouveau, ukiyo-e: the UI
  shows long machine output for hours; decoration or noise competes with it and fails
  contrast or legibility.
- Skeuomorphism, neumorphism, 3D inflated, liquid chrome, steampunk: heavy, low-contrast
  or slow to render, and they date quickly.
- Paper cutout, De Stijl, isometric, mid-century modern: fine as illustration themes, but
  weaker as the system for a dense, interactive console. Bauhaus keeps what is useful of
  De Stijl's grid and primary colours.

## Consequences

- Design tokens live in one CSS file; components never hard-code colours.
- Motion is limited to state changes and respects `prefers-reduced-motion`.
- Verify with Playwright screenshots at 360, 768, 1280 and 1600 px, light and dark.
