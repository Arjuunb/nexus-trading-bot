# Nexus Pet Design and Asset Prompt

Use this prompt to extend the Nexus companion family without drifting from the
dashboard's existing visual language.

## Product role

Design a small mascot system for the TradeLogX Nexus trading dashboard. Nexus
pets are read-only operator companions: they monitor Trading Instances, reflect
the authoritative worker state, and direct attention to blockers. They never
imply that a trade was placed when execution was blocked.

The result must feel smart, modern, calm, slightly futuristic, and appropriate
for a serious fintech product. It may be warm and memorable, but must not feel
childish, noisy, or toy-like.

## Character family

All pets share one compact robot architecture: a rounded graphite shell, dark
face screen, restrained champagne-gold trim, two expressive light-based eyes,
small articulated hands and feet, and a crisp silhouette at 48–96 px.

The approved roster is:

- **Sprig** — default companion; green eyes and a two-leaf sprout; represents
  steady growth and calm monitoring.
- **Pulse** — green pulse eyes; represents market rhythm and worker heartbeat.
- **Orbit** — cyan eyes and a thin orbital motif; represents multi-instance
  oversight and boundaries.
- **Glint** — warm-white eyes and one restrained star glint; represents subtle
  details that need attention.
- **Echo** — violet eyes and small wave-like ear fins; represents repeated
  signals without noise amplification.
- **Nova** — blue-white eyes and a minimal light flare; represents important
  milestones without celebration clutter.
- **Volt** — amber eyes and a tiny lightning marker; represents urgent but
  controlled events.
- **Kiro** — teal eyes and a clean angular forehead inset; represents precise,
  focused execution.

Sprig is the default because the leaf is the most distinctive brand element
and its growth metaphor works in healthy, waiting, and learning states.

## Laptop behaviour

The default non-interactive state is **working**, not standing idle. The pet
sits or stands at a small graphite laptop with a minimal green candlestick
chart. It types gently, blinks occasionally, and makes subtle head and leaf
movements. This communicates that the bot is working in the background without
claiming that an order is being executed.

Interaction sequence:

1. **Cursor far away — IDLE WORK:** type slowly, watch the chart, blink, and
   breathe subtly.
2. **Cursor nearby — AWARE:** stop typing, dim the laptop slightly, lift the
   head, and look toward the pointer.
3. **Direct hover — GREET:** show happy eyes, make one restrained wave, and
   perform a tiny bounce. Keep the laptop nearby rather than making it vanish.
4. **Click — STATUS:** open the factual status panel. Animation must remain
   secondary to status readability.
5. **Warning/error:** use amber/red state lighting and an explicit indicator;
   never use a happy animation for unhealthy state.
6. **Offline:** stop typing, dim the laptop and eyes, and remove the active
   floor glow.

Respect `prefers-reduced-motion`, pause animation when the document is hidden,
and avoid React rerenders for continuous cursor tracking.

### How Sprig behaves like a pet

Sprig's painted eyes are covered by live LED eyes: the same colour, size and tilt
as the painting, measured per pose from the sprite sheet. This lets the eyes
move. The component turns its inputs into one pose and one eye state (the
`derivePose` function) and writes them to the root's dataset, never to React
state.

| When | Sprig |
|---|---|
| Idle, engine running | Types at the laptop, breathes, blinks every 2.5–6 s, and every 20–40 s looks up for a moment |
| Pointer nearby | Looks up from the laptop, and its eyes follow the pointer; it leans slightly toward it |
| Pointer on Sprig | Waves, with its painted happy eyes |
| Stroked back and forth, or pressed and held (touch, pen or mouse) | A small happy hop and three leaf-green hearts. Holding does not open the status panel |
| Click or tap | A small hop, and the status panel opens; Sprig looks at you while it is open |
| Nothing running (offline) | Dozes at the laptop: eyes closed, slow breathing, a drifting "z", no floor glow. It wakes to greet you |
| Paused | Drowsy, half-closed eyes |
| Warning, trade loss or error | The amber alert pose, sprout drooping a little. Petting never makes it happy while something is wrong |
| Analysing (an instance starting, warming up, syncing or recovering) | Stays at the laptop; its eyes sweep across the screen |
| Signal found, or a trade opens | Looks up from the laptop, attentive |
| A trade closes in profit | Waves (only while nothing is wrong) |

### What makes it feel alive

- **The sprout is a separate layer on a spring.** It sways in a light breeze.
  When Sprig hops, is patted, greets you or changes pose, the sprout lags
  behind, overshoots, swings back a few times and settles, like a real stem.
  The swing is capped at 18°.
- **The body leans toward the pointer on a spring**, from the floor up. It does
  not snap.
- **The eyes are never frozen.** While working, Sprig looks down at its laptop.
  Small, quick eye movements come every 0.7–2.6 s. They stop while its eyes
  follow the pointer, because then they are fixed on it.
- **Blinks are asymmetric.** The eye closes fast and opens slower, and now and
  then Sprig blinks twice.
- **Poses fade into each other in 0.11 s.** The old pose stays solid under the
  new one, so Sprig never turns see-through. A longer fade reads as a double
  exposure.
- **The laptop screen is lit.** It gives off a soft, flickering glow, and a blip
  appears where the newest candle is drawn. When Sprig dozes, the screen dims.
  The robot itself stays solid while asleep.
- **The contact shadow reacts to a hop or a pat.** It shrinks and fades as
  Sprig leaves the ground, then returns when it lands.

Reduced motion turns off every animation, the springs, and the blink, glance
and eye-movement timers. The poses still follow state, so Sprig stays
informative. A hidden tab pauses everything. E2e tests cover the pointer, petting,
click, offline, error and analysing rows, the gaze at the laptop, blinking, reduced
motion and the sprout's swing. The paused, signal, open-trade and winning-trade rows
are not yet covered by an e2e test.

## Raster asset-generation prompt

```text
Use case: stylized-concept
Asset type: transparent production mascot assets for a premium dark fintech dashboard
Primary request: Create a cohesive Nexus trading-companion robot family and interaction poses.
Subject: compact rounded graphite robot, matte dark metal, minimal face screen,
two luminous eyes, restrained champagne-gold trim, small articulated hands and
feet. Sprig has a signature two-leaf sprout. Include a tiny graphite laptop with
a simple green candlestick chart.
Style: polished 2.5D UI mascot render, crisp silhouette, soft ambient occlusion,
premium and charming but not childish.
Interaction poses: idle typing at laptop; cursor-aware with typing stopped and
head lifted; direct-hover wave with happy eyes; attentive amber alert pose.
Composition: consistent three-quarter-front camera, identical proportions and
lighting, isolated cells with generous gutters, readable at 48–96 px.
Palette: graphite black, charcoal, restrained champagne gold, Nexus green;
character-specific cyan, violet, blue-white, amber, or teal only as small light accents.
Constraints: genuine transparent alpha; no painted checkerboard; no text,
labels, logos, scenery, watermark, excessive props, duplicated limbs, or
overlapping cells.
Avoid: childish chibi exaggeration, glossy toy plastic, ornate gold, rainbow
neon, cyberpunk clutter, human anatomy, or an expression that contradicts the
reported bot state.
```

## Implementation rule

The default Sprig footer pet uses the approved transparent production artwork
(`docs/nexus-pet/sprig-production-poses-v4.png`, kept out of the build). The sheet
is four equal 620×724 cells, one pose per cell: working, aware, greeting and warning.
Each cell has transparent gutters, and no pose may cross into the next cell, or the
frame shows a sliver of the neighbouring pose.

`scripts/split_sprig_sprout.py` splits that sheet into the two sheets the app ships:
`public/nexus-pet-concepts/sprig-body-v5.png` and `sprig-sprout-v5.png`. Every pixel
goes to exactly one layer, so the two stacked reproduce the approved art bit for bit.
The script asserts this. It also prints each pose's stem pivot, which the CSS uses as
the sprout's rotation origin. Run it again whenever the art changes. E2e tests check
both sheets' cell edges, and that the sprout never overlaps the body and lies only
above the head.

Sprig belongs to the platform, not to a card on top of it. It rests on the footer's top
edge, grounded by a soft contact shadow and a faint glow in the state colour. No CSS
`filter` is used on the pet: WebKit (every iPhone browser) draws a filter on this
clipped, animated layer around its rectangle, which reads as a dark box. An e2e test
checks that there is no filter and that the art sits on the footer line. For the same reason Safari's tap highlight is switched off on Sprig: touching or holding it (a pat) would otherwise draw a grey rounded box over it. The phone e2e test checks that too. CSS selects the working, aware, greeting, or warning pose from
authoritative application state. The code-native SVG remains a fallback for
roster members that do not yet have matching full-body pose sheets. State,
accessibility, reduced-motion behaviour, and cursor interactions remain
deterministic and auditable.
