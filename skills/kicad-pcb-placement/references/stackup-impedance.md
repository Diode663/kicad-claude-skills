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

## Outer-layer widths

| Target | Width / gap | Source |
|---|---|---|
| 50 Ω single-ended | 0.324 mm | JITX JLC04161H_7628 library |
| 90 Ω differential | 0.233 / 0.15 mm | JITX JLC04161H_7628 library |
| 100 Ω differential | 0.172 / 0.15 mm | JITX JLC04161H_7628 library |

**Unconfirmed.** An independent IPC-2141A estimate reads about 7% high
against these (96 Ω for the 90 Ω geometry). That formula is known to be
crude, but the difference is enough that **widths must be taken from
JLCPCB's own impedance calculator before routing.** Record the confirmed
values here when you have them.

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
