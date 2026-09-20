# Placement conventions, and why

Rules with no reason attached get applied where they don't belong. Each one
here says what it is for, so you can tell when it doesn't apply.

## Floorplanning

### Fixed points first
Placement is a constraint problem, and some constraints don't move:

| Constraint | Comes from | Consequence |
|---|---|---|
| Board outline | enclosure, panel, existing product | everything else |
| Mounting holes | the same | a 6 mm keep-clear ring for the washer |
| Connector positions | the cable, the enclosure cut-out | that edge is spoken for |
| Antenna | physics | a board corner, no copper near it |
| A display or a big module | mechanical | often one whole face |

Ask about the enclosure before you invent a board size. If there isn't one,
say what size you chose and why.

### Then blocks, then parts
Draw the board as four to eight rectangles on paper (or in comments) before
any coordinate: POWER IN, REGULATION, MCU, SENSOR FRONT END, OUTPUT. Give
each one a patch. Place the anchor part of each block, then its entourage,
then whatever is left.

Signal flow across the board should match the schematic's: input on one
edge, output on another. A board where you can point at the input and trace
to the output with a finger is a board someone can debug.

### Sizing
A rough starting point for a simple two-layer board: sum the footprint
areas, multiply by 3 to 4 for routing channels and clearances, then add the
edge margin. Round up to something an enclosure likes.

Too tight is expensive: you will find out during routing, when every fix
means moving parts you already justified. Leave channels — a board with no
corridor between blocks cannot be routed on two layers.

Leave **3 mm** clear inside the board edge for parts where you can, and
never put a footprint under a mounting hole's washer.

## Connectors, buttons, LEDs

- A connector's mating opening sits **flush with the board edge**, not set
  back. Its courtyard will hang off the board; that's correct, and `check()`
  reports it as a warning you should expect.
- Leave the cable's approach clear. A USB-C socket with a 10 mm capacitor
  beside it is a socket you can't plug into with a moulded cable.
- Buttons on an edge or a face a finger can reach, not under a module.
- LEDs where they can be seen: an edge, or beside the connector they
  describe.
- Keep the board's own silkscreen away from the connector's — DRC flags
  silk over the edge, and at a connector that is usually intentional.

## RF and antennas

From Espressif's hardware design guidelines, and they generalise to most
chip and PCB-trace antennas:

- Put the antenna at a **board edge**, ideally at a corner.
- **No copper** under or beside it — not a trace, not a pour, not a plane on
  any layer. Enforce it with a rule area.
- The keep-out should forbid tracks, vias, pads and copper pour, but
  **allow footprints**: the module's own antenna end has to sit over it, and
  forbidding footprints makes DRC complain about the part you placed
  deliberately.
- Extend the keep-out past the antenna's own outline — a module's ground
  pad, the enclosure, and a nearby pour all detune it.
- Keep the antenna as far as possible from switching converters and
  crystals, so those go in the opposite corner.
- If the board goes in a metal enclosure, say so to the user — a board-edge
  antenna inside a metal box doesn't work, and no placement fixes it.

## Switching converters

The loop that carries the switched current — input cap → switch → diode or
low-side FET → output cap → back to the input cap — is a loop antenna, and
its enclosed area sets the board's radiated emissions.

- **Shrink the loop area first.** Everything else about a converter layout
  is secondary.
- The **input capacitor** goes as close to the IC's VIN and GND pins as the
  footprints allow. Closer than the inductor, closer than anything.
- The **switching node** (the IC's SW pin, the inductor, the diode's anode)
  is the noisy net: short and wide. Keep it small in *area*, not just short.
- The **inductor** goes right at the SW pin, oriented so its SW-side
  terminal faces it.
- The **feedback divider** goes at the FB pin, away from the switching node.
  FB is high impedance; running it past SW couples noise straight into the
  control loop.
- Put the whole converter in the corner furthest from anything sensitive.

For the MT3608 specifically, the datasheet's layout section asks for exactly
this: input cap at the pin, a small SW loop, and the feedback tap close to
FB.

## Current sensing

A shunt measured badly is a shunt wasted.

- **Kelvin connection**: the sense taps come off the two ends of the shunt
  itself, not off the power trace either side. Any copper shared between
  the current path and the sense path adds its own resistance to your
  measurement.
- The sense amplifier goes near the shunt, with its input filter between the
  two. Filter components sit symmetrically — a differential pair with
  different path lengths is a common-mode problem.
- Route the two sense lines as a close pair, over unbroken ground.
- The amplifier's digital side (I²C, SPI) faces the MCU, so the analogue and
  digital halves of the part point at the right neighbours.

This is what the INA226 datasheet's layout section asks for, and it applies
to every shunt amplifier.

## Decoupling and bypass

- A decoupling capacitor belongs **at the pin it decouples**, on the same
  side of the board, with the shortest possible path to that pin and to
  ground.
- Smallest value nearest the pin; bulk further out.
- A cap placed by the regulator instead of the load decouples the regulator,
  which nothing needed.
- A multi-pin device gets one per supply pin, not one per device.
- Say in the comment which pin each one serves. It is the only way the next
  person knows whether they may move it.

## Orientation and alignment

- Rotate each IC so its used pins face the parts they connect to. For a
  module with pins down one flank, that flank faces into the board.
- Then make like parts **share an axis and a rotation**. A row of 0402s all
  at 90° looks deliberate and assembles cleanly; the same row at four
  different angles looks like a mistake and costs pick-and-place time.
- Polarised parts (diodes, electrolytics, ICs) ideally all point the same
  way, so an assembly error is visible at a glance.
- A two-pin part's rotation decides *which pin* faces which way — check the
  pad numbers, not the body shape, when the net matters (a diode's anode, an
  inductor's SW side).

## Sides

For a simple board, default to **everything on the top layer**. Two-sided
assembly means a second reflow and costs real money per board.

Module carriers are different: a compute-module or SoM carrier is usually double-sided
by design (the module on one side, radios and connectors on the other), and
the user may *want* most parts on the side facing the enclosure. Ask, then
encode it in `DEFAULT_SIDES` / `SIDE_COST` / `SIDE_RULES` rather than
hard-coding a side.

- **Through-hole pads exist on both sides.** A bottom-side part under a
  top-side header's pins shorts to them — this happened on a real board.
  The placer treats every PTH pad as occupied on both sides.
- **Modules on flush connectors forbid the area under them.** A module on
  1.5 mm-stack mezzanine connectors (DF40 and similar) has 0 mm clearance: no
  footprints under its outline on that side.
- **Cards over the board allow only low parts.** Under an M.2 card on a 4.2 mm
  socket the main board may carry **0.90 mm** at most: the MLD-NGFF-E-4.2H stack-height diagram puts the card's underside at 2.52 mm (centre 2.92 mm, card 0.8 mm), but the card's own bottom-side parts take 1.40 mm and 0.10 mm is air.
  0402 passives (0.55 mm) yes; a 0603 10 µF (0.8–0.95 mm), 0805 and anything taller no.
  Express it as `allow="by_height", max_height=0.90`.
- A footprint flipped to the bottom has its geometry mirrored, and which axis
  depends on the flip call. Let `autoplace.py --selftest` measure it; check
  the render, don't reason about it.

## Capacity

Sum what the parts need (courtyard ∪ pads, plus part spacing) and compare it
with the free area per side after holes, fixed parts and keep-outs.
`autoplace.py` prints this. Roughly: under 40% routes comfortably on four
layers, 40–50% is tight, above 50% the board needs to grow or lose a
constraint. Say so early — it is the one placement problem no algorithm fixes.

## Silkscreen

- References go outside the part outline, clear of pads, readable with the
  board in one or two orientations (0° and 90°, never 180°).
- A reference belongs next to its part, unambiguously. Between two 0402s is
  not "next to" either.
- Keep the line width at or above the fab's minimum — 0.15 mm for JLCPCB.
  Below it, you get a DRC warning per part and unreadable printing.
- Polarity and pin-1 marks matter more than the designators. Don't let a
  designator sit on one.
- If a part is too crowded for its reference, hide it and put the
  identification in the assembly drawing rather than printing overlapping
  text.

## Thermal

- A regulator dissipating more than a few hundred milliwatts needs copper to
  spread into: give it room for a pour, and don't box it in with tall parts.
- Keep heat away from anything that measures: references, ADCs, sense
  amplifiers, crystals.
- Thermal pads want vias. If a library footprint has them unnumbered, they
  connect to nothing — see `troubleshooting.md`.

## What to tell the user

Placement decisions are invisible in the file. Hand over:

- a table of which block went where and why;
- the measurements that back the claims (net spans for the loops you said
  were tight);
- what's left: unrouted, plus any library-footprint problems;
- the warning that re-running the script resets every position.
