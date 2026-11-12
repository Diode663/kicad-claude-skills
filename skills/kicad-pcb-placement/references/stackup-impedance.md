# Stackup and controlled impedance (for placement and critical routing)

Placement decides which layer a fast net can use and where its vias go, so
settle the stackup before placing anything with USB, PCIe or Ethernet.

## JLCPCB default 4-layer: JLC04161H-7628 (1.6 mm)

| Layer | Material | Thickness |
|---|---|---|
| L1 top | copper | 0.035 mm |
| prepreg | 7628, εr 4.4 | 0.2104 mm |
| L2 | copper | 0.0152 mm |
| core | εr 4.6 | 1.065 mm |
| L3 | copper | 0.0152 mm |
| prepreg | 7628, εr 4.4 | 0.2104 mm |
| L4 bottom | copper | 0.035 mm |

Source: jlcpcb.com/impedance.

**Recommended plane assignment: L2 = GND, L3 = GND**, with power on
outer-layer pours. This is what the official Raspberry Pi CM4 IO board
(4-layer) does: both inner layers solid ground apart from a small 3.3 V
island. It gives every outer-layer trace a ground reference, which matters
the moment a fast net changes sides (a module on the bottom, M.2 on the top).
Put a ground via beside every signal via on a layer change.

## Outer-layer widths -- JLCPCB's own, confirmed in their calculator 2026-09-20

jlcpcb.com/pcb-impedance-calculator: rigid, 4 layers, 1.6 mm, 1 oz outer / 0.5 oz
inner, stackup JLC04161H-7628, signal L1 over L2 (L4 over L3 is identical).
W1 (the width at the foot of the trace) is the number to draw.

These are in `pcblib.JLC_IMPEDANCE` as data: `jlc_width("JLC04161H-7628", "diff", 90, 6)`
returns `(0.2474, 0.1524)`, and `jlc_pour_rules()` writes the matching `.kicad_dru`.
When you read a new stackup or gap from the calculator, add it to that table as
well as here.

| Target | Gap | Width (mil) | Width (mm) |
|---|---|---|---|
| 90 ohm differential, microstrip | 5 mil | 8.61 | 0.2187 |
| | **6 mil** | **9.74** | **0.2474** |
| | 7 mil | 10.88 | 0.2764 |
| | 8 mil | 11.44 | 0.2906 |
| 100 ohm differential, microstrip | 5 mil | 6.05 | 0.1537 |
| | **6 mil** | **7.19** | **0.1826** |
| | 8 mil | 8.89 | 0.2258 |
| | 10 mil | 10.02 | 0.2545 |
| 50 ohm single-ended, microstrip | -- | 14.12 | 0.3586 |
| 50 ohm coplanar (ground gap) | 6 mil | 10.36 | 0.2631 |
| | **8 mil** | **12.04** | **0.3058** |
| | 10 mil | 12.61 | 0.3203 |
| | 20 mil | 13.73 | 0.3487 |

Their model: H1 8.2835 mil at **Er 4.3** (not the 4.4 on the material sheet),
finished outer copper 1.6 mil, etch taper W2 = W1 - 0.5 mil, solder mask
1.0 / 0.6 / 1.0 mil at Er 3.8.

**A pour beside a microstrip pair changes it.** Their coplanar-differential model:
pour 20 mil away, 90 ohm needs 9.80 mil (+0.6 %); pour 8 mil away, 9.24 mil (-5 %).
So either keep zones 0.5 mm from the pair classes with a `.kicad_dru` rule and use
the microstrip figures, or route coplanar on purpose and use the coplanar figure
with the gap held by a rule.

What this replaced: the JITX library figures (0.324 / 0.233 / 0.172 mm) and an
IPC-2141 estimate (0.322 / 0.234 / 0.186). Against JLCPCB's own solver the 90 ohm
figures were 5 % narrow (~93 ohm) and the plain 50 ohm ones 10 % narrow (~53 ohm).
Neither source models the solder mask or the etch taper.

**Driving the calculator from a script:** it is a Vue app. Its results live in the
page component's `impedanceList[templateIndex][row].impedancePicInfo.parameterData`
(`W1.defaultValue` is the answer; `H1`, `Er1`, `T1`, `C1..C3`, `CEr` are the model
inputs), filled only after that stackup's tab has been opened. The page needs a
real viewport: with the browser pane hidden its transitions never finish and the
result cells show stale and new values together.

## Interface rules

| Interface | Impedance | Within-pair match | Between pairs | Source |
|---|---|---|---|---|
| USB 2.0 | 90 Ω diff | ≤ 0.15 mm | — | CM4 datasheet |
| PCIe Gen2 | 90 Ω diff (M.2 spec says 85) | ≤ 0.1 mm | not required | CM4 datasheet; Embedded Artists |
| PCIe clock | 100 Ω diff | ≤ 0.1 mm | — | Embedded Artists M.2 guide |
| Gigabit Ethernet | 100 Ω diff | ≤ 0.15 mm | ≤ 50 mm | CM4 datasheet |
| SDIO | 50 Ω SE, 3W spacing, no stubs | 100 mil (≤ 50 MHz) | — | Embedded Artists |
| GNSS RF | 50 Ω coplanar waveguide + via fence | — | — | u-blox MAX-M10S manual |

- The CM4 matches every pair internally to 0.05 mm, and its internal P/N
  lengths are equal, so no compensation is needed on the carrier (CM4
  datasheet tables 7–8).
- The CM4 has AC coupling caps on PCIe TX and CLK. The RX caps belong on the
  device side; an M.2 card carries its own.
- PCIe to a connector is labelled from the host side, so no TX/RX swap.

## Net names decide whether the netclass does anything

A netclass's `diff_pair_width` / `diff_pair_gap` apply only to nets KiCad has
recognised as a differential pair, and it recognises a pair from the two
*names* alone — `BOARD::MatchDpSuffix` reads the name backwards, skips any
run of digits and underscores, and requires `P`, `N`, `+` or `-` next, upper
case. The constraint is gated on `A.inDiffPair( '*' )` in `drc_engine.cpp`,
so an unrecognised pair silently routes at the class's ordinary
`track_width`.

    USB_P / USB_N      pair        USB_DP   / USB_DM    NOT a pair
    USBP  / USBN       pair        TD1P     / TD1M      NOT a pair
    USB+  / USB-       pair        MX1P_INT / MX1M_INT  NOT a pair
    TD1_P / TD1_N      pair        USB_p    / USB_n     NOT a pair

On one real 4-layer board, 20 pair nets were named this way and **none** were
recognised — the impedance widths in the netclasses would have had no effect
at all. `Board.check_diff_pairs()` asks pcbnew directly and lists any net in
a `diff_pair_width` class that has no partner; run it with
`check_netclasses()` whenever you set impedance, before routing. The fix
belongs in the schematic (`kicad-schematic-layout`,
`references/layout-conventions.md` has the full table and `lint_spec`'s
`dp-name` rule catches it at spec time).

## What the reference boards used

| Board | Layers | Plane use | PCIe/USB netclass |
|---|---|---|---|
| RPi CM4 IO board | 4 | In1 GND, In2 GND (+3.3 V island) | 90R: 0.150 / 0.250 |
| Antmicro cm4-baseboard | 6 | GND on In1 and In4, mixed In2/In3 | PCIe 0.17 / 0.15, USB 0.182 / 0.196 |
| Hermes FE1.1s hub | 4 | 5 V / GND / 5 V / GND | 90 Ω: 0.239 / 0.150 |

Widths differ because stackups differ. Copy the method, not the numbers.

## Power trace widths

Size by current; JLCPCB outer layers are 1 oz (35 µm). Roughly, for a 10 °C
rise on an outer layer: 1 A ≈ 0.3 mm, 2 A ≈ 0.7 mm, 3 A ≈ 1.1 mm,
5 A ≈ 2.2 mm (IPC-2221 approximation). Prefer pours for regulator loops and
battery paths. Inner layers are 0.5 oz: roughly double the width, or keep
power off them.

## JLCPCB standard 2-layer: JLC0216A (1.6 mm) — confirmed in their calculator 2026-09-20

`JLC_STACKUPS["JLC0216A"]`: 0.030 mm copper / 1.43 mm core (Dk 4.5) / 0.030 mm copper.
JLCPCB's impedance calculator does list 2 layers. For 90 Ω it only gets there as a
**coplanar** differential pair, L1 referenced to L2:

| | mil | mm |
|---|---|---|
| trace width | 13.17 | 0.3345 |
| pair gap | 6 | 0.1524 |
| trace to coplanar ground | 8 | 0.2032 |

A plain edge-coupled microstrip over 1.43 mm would need ~1.2 mm traces, which cannot reach
0.3 mm pads. So: ground pour along both sides at 0.2032 mm, unbroken bottom ground under the
pair, and a `.kicad_dru` rule so zones keep exactly that gap from the netclass
(`references/example_setup_fab_jlc_2layer.py` writes the stackup, netclasses, JLCPCB's
2-layer rule values with their source pages, and the `.kicad_dru`). The pair pitch
(0.487 mm) is almost exactly the 0.5 mm pad pitch of USB-C and M.2, so it leaves the pads
at full width; neck to 0.25 mm only where a neighbouring pad is closer than the clearance.

JLCPCB offers *tested* impedance (±10 %, coupon) on 4+ layers only, and track width
tolerance is ±20 %. For one USB 2.0 pair under ~35 mm that is harmless.

Driving the calculator: the Type list loads per layer count and answers with HTTP 590 when
hit quickly; reload, pick Layers = 2, wait, then open Type. Results appear in the page text
under "Trace Width / Trace Spacing / Impedance trace to copper".

JLCPCB 2-layer rule values used (capabilities page, 2026-09-20; limits in brackets):
track/space 0.127 (0.10), via 0.6/0.3 (holes of 0.15–0.25 cost more), PTH annular ring 0.18
absolute / 0.25 recommended, pad hole to hole 0.45, PTH to track 0.28, NPTH to track 0.2,
copper to routed edge 0.3 (0.2), SMD pad to pad 0.15, pad to silkscreen 0.15, legend 1.0 mm
tall / 0.15 mm line. Economic PCBA: 2-layer, 0.8–1.6 mm, single-sided, 0402 minimum, no
rails or fiducials required; each part's page says "PCBA Type: Economic and Standard" or not.
