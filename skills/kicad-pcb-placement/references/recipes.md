# Placement recipes — what goes where, and the evidence

`scripts/recipes.py` is the machine-readable version of this page. Each
recipe matches a part by **pin names** (from KiCad's netlist) or value, finds
its support parts through the netlist, and gives each one a distance limit
and a placement order. `autoplace.py --audit` measures every limit on any
placement, including a hand-made one.

Distances are **pad centre to nearest pad centre** on the shared net, the
way the reference boards below were measured. They are limits, not targets:
closer is better until parts collide.

How the numbers were chosen: datasheet layout sections set the *order* and
the intent; the millimetres come from measuring shipped boards in KiCad
(`survey2.py`-style pad-to-pin measurement) and rounding up slightly.

---

## Synchronous buck, SOT-23-6 (TI TPS563201 / TPS565201, D-CAP2 class)

| Role | Part | Limit | Order |
|---|---|---|---|
| input cap | C, VIN–GND | 2.0 mm | 1 |
| boot cap | C, VBST–SW | 1.5 mm | 2 |
| inductor | L on SW | 4.0 mm | 3 |
| output caps (×2) | C, inductor output–GND | 3.0 mm from the inductor | 4 |
| feedback divider | R on VFB | 2.5 mm | 5 |

Rules from TI SLVSD90B §7.4 and SLVSE71 §10:
- VIN and GND copper wide; input and output caps as close as possible.
- SW short and wide; **no switching current under the IC**.
- Separate VOUT sense trace to the top feedback resistor; Kelvin the
  feedback ground to the IC GND pin; keep VFB away from SW.

Measured: Antmicro cm4-baseboard AP62301 — Cin 1.7, boot 1.3, FB 1.2–2.2,
L 3.7 mm. RPi CM4 IO board AP64501 — Cin 2.1, L 3.2, FB 1.6–2.4 mm.

## NVDC buck-boost charger (TI BQ25792 / BQ2567x)

TI's layout section is a **priority list**, and the recipe follows it:

1. SYS and PMID 100 nF, same layer as the IC, no vias — 1.8 mm
2. SYS and PMID bulk — 5 mm
3. REGN cap (3.2 mm) and BTST1/BTST2 boot caps (1.5 mm)
4. VBUS and BAT caps — 4–5 mm
5. Inductor between SW1 and SW2 — 5.5 mm
6. PROG / ILIM / TS resistors, grounded away from the switching return — 4.5 mm

Sources: BQ25792 datasheet §12.1; BQ25792EVM guide SLUUCB5E §3.
Measured: open-source BQ25672 board — boot 1.3, PMID/SYS 100 nF 1.8,
REGN 3.2, inductor 5.4, bulk 4.6–6.3 mm.

## USB-PD sink (Hynetek HUSB238)

1 µF on VIN ≤ 2 mm; VSET/ISET resistors ≤ 3.5 mm; gate resistor ≤ 4 mm.
Measured: 1.25–2.2 mm and 1.3–3.4 mm on an open-source HUSB238 board.

## USB 2.0 hub (FE1.1s, USB2514, CH334)

100 nF on each supply pin ≤ 2 mm (smallest first), crystal ≤ 4 mm with its
load caps ≤ 4 mm, REXT ≤ 3 mm, straps ≤ 5 mm. The FE1.1s needs 10 µF on
VD18_O and VD33_O (datasheet pin table). Measured on the official CM4 IO
board (USB2514): 100 nF 1.4–1.8 mm, straps 2.0–2.3 mm, crystal caps 3.8–4.0 mm.

## USB audio codec (CM108B / CM119)

Regulator and reference caps (DREG18, DREG33, AREG36, VREF) ≤ 3 mm;
USB series resistors ≤ 3.5 mm. Measured on Digirig-Lite: every cap 1.5–3.7 mm.
Keep the mic input away from switching regulators.

## USB ESD array (USBLC6-2)

At the connector, **in line** on D+/D−, shortest path to ground (ST
datasheet §2.3). The recipe only claims its rail cap; the global placer
pulls the block toward the connector through the heavy weight on USB data nets.

## Load switch (AP2151 / TPS2051)

Input cap ≤ 2 mm, output cap ≤ 3 mm, flag/enable resistors ≤ 3 mm.

## GNSS module (u-blox MAX-M10S)

From the integration manual §4.4:
- U.FL / antenna feed short: ≤ 6 mm, 50 Ω coplanar waveguide, ground vias alongside.
- Ground under the module on layers 1–2; no signals crossing under it.
- ≥ 5 mm from other RF parts; away from switching regulators and heat.

The recipe tags the block `avoid_switching`, and the global placer adds a
cost when it lands within 20 mm of a `switching` block.

## M.2 Key E socket

2× 100 nF + 2× 22 µF on 3.3 V at the connector (Embedded Artists M.2
carrier guide). The socket's capacitors claim at prio 6, after every regulator
role: on a shared 3.3 V rail they are all one net, and claiming first the socket
took a buck's output pair.

Facts measured from the MLD-NGFF-E-4.2H drawing (C52766479) and proven on a
fabricated board (USB-C to M.2 adapter, 2026-09-20). Other 4.2 mm Key-E sockets
follow the same M.2 geometry, but check their drawing:

- **Under the card: 0.90 mm maximum** on the main board. The MLD-NGFF-E-4.2H stack-height diagram puts the card's underside at 2.52 mm (centre 2.92 mm, card 0.8 mm), but the card's own bottom-side parts take 1.40 mm and 0.10 mm is air.
  Use `allow="by_height", max_height=0.90` for the card area, never `low_passives`.
- **The card leaves the socket on the even-pin / locating-peg side.** In the
  EasyEDA footprint that is local +Y, so rotation 90 sends the card toward board +X.
  Measure pad 1, pad 2 and the NPTH pegs after placing; do not reason about it. (Getting
  this backwards leaves the card hanging off the board edge.)
- **Standoff:** the section view dimensions the card stop 1.75 mm behind the peg
  centreline (pegs at local y = +1.5), so the mounting notch of an NN-mm card is
  NN − 0.25 mm from the socket centre: 29.75 mm for a 2230. Height 2.5 mm
  (LCSC C5301773, SMTSOM225BTR, an M2 SMT nut in a 3.7 mm plated hole).
- **Pins:** odd row at the back, even row under the card. 3.3 V is 2/4 at one end and
  72/74 at the other: put one 100 nF at each pair, on the card side (0402 fits), and the
  bulk just past the socket's end, outside the card.
- **USB 2.0 on Key E** is pins 3 (D+) and 5 (D−), odd row. A pair arriving from the back
  with D+ on the wrong side can go through the **key gap** (pins 24–31 do not exist:
  2.2 mm between pads 23 and 33), down under the housing and back into the pads from
  the card side; two right turns swap the sides without a via.
- `references/example_m2_clearance.py` measures every part against the housing and the
  card using 3D-model heights; `example_build_models.py` builds the housing and a
  translucent card envelope from the drawing (LCSC has no model for this socket).

## Ethernet magnetics

Bob-Smith termination (75 Ω + 1 nF/2 kV) near the centre taps ≤ 6 mm.
Pairs: 100 Ω differential, P/N matched ≤ 0.15 mm (CM4 datasheet).

## P-FET power switch / back-to-back pair

A second FET sharing the source net (reverse-polarity pair) ≤ 3 mm; gate
resistors and gate zener ≤ 3.5 mm.

## Raspberry Pi CM4 / CM5 module

Not a recipe but a derived keep-out. From the two DF40 100-pin connectors
the placer builds the 40 × 55 mm module outline (centre 2.5 mm from the
connector midpoint along the pin rows, toward pin 1) and forbids every other
footprint under it on that side: the 1.5 mm stacking connectors leave 0 mm
clearance. Sources: CM4 datasheet §4; official CM4 IO board footprint.

## Fallbacks

- **Generic IC**: one decoupling cap per supply pin ≤ 2.5 mm; passives on
  nets only this IC uses ≤ 4 mm.
- **Generic connector**: filtering/protection parts on nets local to the
  connector ≤ 6 mm.
- **Leftover passives** join the block they share the most local net with,
  preferring an IC on the same schematic sheet. They are listed as
  `attached` and not scored.

## How parts get claimed

1. Every candidate (anchor, role, part) is found from the netlist.
2. **Designator affinity first**: `CU33O1` goes to `U331` before anything
   else competes for it (common designer convention; ≥ 2 shared characters).
3. Then by role priority across the whole board.
4. A rail capacitor (net with > 6 pins) is only claimed across schematic
   sheets if no IC on its own sheet uses that rail.
5. "All matching" decoupling roles take one cap per anchor pin on the net.

## Adding a recipe

Put it in the project's `placement_config.py` as `EXTRA_RECIPES = [...]`
(tried before the built-ins), or add it to `recipes.py` if it is generally
useful. Match on pin names, never designators. Cite the datasheet section
and, if you can, a measured reference board. Run `--audit` on a known-good
board to check the limits are achievable.

## SOT-23-6 buck on two layers: TI's Figure 36, and what the audit says about it

TI's layout example for the TPS56320x puts the input capacitors directly beside the
GND/SW/VIN pins, VIN end at the VIN pin and GND end at the GND pin, takes SW through vias
to the far layer, and puts the inductor beyond the capacitors. The `BUCK_SOT23` limits
(inductor ≤ 4 mm, c_in ≤ 2 mm) come from boards that put the inductor first, so a Figure 36
placement fails those two roles by design (7.2 mm and 4.1 mm on the adapter). Say so in the
handover instead of chasing the score. Hand-place it in `FIXED`
(`references/example_placement_config_m2_adapter.py`):

- IC at 180° so pins 1–3 face the capacitors; a 0.9 mm gap between the IC and the first
  capacitor holds the SW via; a second SW via fits under the package.
- A 1210 standing on end has pads 2.7 mm wide: read pad sizes from the board before
  assuming a gap exists (the first route put SW vias into C1's pads).
- `EXTRA_RECIPES` with `c_in` `count = 2` lets the buck claim both its bulk and its 100 nF.
- Output capacitance: TI's Table 2 allows 20–68 µF with 2.2 µH typical / 4.7 µH maximum at
  3.3 V. A socket's bulk capacitor on the same rail counts. 154 µF nominal (≈ 87 µF under
  bias) with 4.7 µH put the LC pole below TI's envelope; 2.2 µH brought it back.
