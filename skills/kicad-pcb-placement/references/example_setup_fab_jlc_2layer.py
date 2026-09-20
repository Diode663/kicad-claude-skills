#!/usr/bin/env python3
"""Fab setup for the USB-C to M.2 Key-E HaLow adapter: stackup, netclasses and
JLCPCB's design rules, for a turnkey (JLCPCB fab + Economic PCBA) order.

Run whenever the netlist changes, and always before routing:

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" setup_fab.py

Idempotent. It writes three things and moves nothing:
  * the stackup into the .kicad_pcb,
  * netclasses + board constraints into the .kicad_pro,
  * <project>.kicad_dru -- the JLCPCB rules KiCad's constraint table cannot
    express (KiCad loads that file by name, no import step).

Sources, all read on 2026-09-20:
  [CAP]  https://jlcpcb.com/capabilities/pcb-capabilities   (Drilling, Traces, Legend, Outline)
  [PCBA] https://jlcpcb.com/capabilities/pcb-assembly-capabilities
  [CALC] https://jlcpcb.com/pcb-impedance-calculator

Stackup [CALC]: 2 layers, 1.6 mm, 1 oz -> "JLC0216A": 0.030 mm copper,
1.43 mm core, 0.030 mm copper. FR-4 Dk 4.5 for 2-layer boards [CAP].

USB 2.0 pair [CALC]: 90 ohm "Coplanar Differential Pair", L1 over L2:
    width 13.17 mil = 0.3345 mm, pair gap 6 mil = 0.1524 mm,
    trace to coplanar ground 8 mil = 0.2032 mm.
  It only reaches 90 ohm as a COPLANAR pair: the ground pour must run along
  both sides at 0.2032 mm and the bottom layer must be unbroken ground under
  it. The .kicad_dru rule below makes zones keep exactly that gap from the
  pair. JLCPCB offers controlled impedance (the +/-10 % guarantee and the
  test coupon) on 4+ layers only [CAP]; on this board the geometry is theirs
  but the result is not tested. Track width tolerance is +/-20 % [CAP].
  The pair is ~21 mm long, so even the tolerance extremes are harmless.
  At J1 and J2 the pads are 0.3 mm on a 0.5 mm pitch: neck down to
  0.25 / 0.2 mm for the last millimetre.

Other routing notes for two layers:
  * The bottom layer is the ground plane. Only the SW link from U2 to L1 (TI
    Figure 36 puts it on the bottom too) and short hops may cut it, never
    under the USB pair.
  * Buck output return (TI guideline 10): pour GND on the TOP layer from C4/C5
    along the strip between L1 and the socket back to the C1/C2/U2 GND island.

Rule values are JLCPCB's no-surcharge tier with margin, not their absolute
limits: 0.3 mm via holes (0.15-0.25 mm holes cost more [CAP]), 0.127 mm
track/space (limit 0.10), 0.3 mm copper to edge (limit 0.2).

Economic PCBA [PCBA]: 2-layer, 0.8-1.6 mm, single-sided, 10 x 10 mm minimum,
no edge rails or fiducials needed, 0402 minimum package, 0.4 mm minimum pin
pitch. This board meets all of that; the M.2 socket is 0.5 mm pitch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcblib                       # noqa: E402
from pcblib import Board, say       # noqa: E402
import placement_config as cfg      # noqa: E402

NAME = cfg.NAME
W, H = cfg.BOARD_SIZE

STACKUP = "JLC0216A"
pcblib.JLC_STACKUPS[STACKUP] = {
    "thickness": 1.6,
    "finish": "HAL lead-free",
    "layers": [
        ("F.Cu", "copper", 0.030, None, None),
        ("dielectric 1", "core", 1.43, "FR-4 core", 4.5),
        ("B.Cu", "copper", 0.030, None, None),
    ],
}

USB_W, USB_GAP, USB_COPLANAR = 0.3345, 0.1524, 0.2032

CLASSES = {
    "USB_90": {
        "track_width": USB_W, "diff_pair_width": USB_W, "diff_pair_gap": USB_GAP,
        "diff_pair_via_gap": 0.25, "clearance": 0.15,
        "via_diameter": 0.6, "via_drill": 0.3,
    },
    # a width to route to, not a substitute for a pour: VBUS carries ~0.9 A
    # and +3V3 ~1.2 A in transmit bursts. Pour them; several vias per change.
    "POWER": {
        "track_width": 1.0, "clearance": 0.2,
        "via_diameter": 0.8, "via_drill": 0.4,
    },
}

# pair names end in _P/_N so KiCad recognises the differential pair
PATTERNS = [
    ("USB_90", "*USB_D_?"),
    ("POWER", "VBUS"), ("POWER", "+3V3"),
]

DEFAULT_CLASS = {"track_width": 0.25, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3}

RULES = {
    "min_clearance": 0.127,                 # [CAP] limit 0.10
    "min_track_width": 0.127,               # [CAP] limit 0.10
    "min_connection": 0.127,
    "min_via_diameter": 0.5,                # [CAP] >= hole + 0.15; no-surcharge tier
    "min_via_annular_width": 0.1,
    "min_through_hole_diameter": 0.3,       # [CAP] 0.15-0.25 mm holes cost more
    "min_hole_to_hole": 0.25,               # [CAP] via hole to hole 0.2 (pads: 0.45, in the .kicad_dru)
    "min_hole_clearance": 0.25,             # [CAP] via hole to track 0.2
    "min_copper_edge_clearance": 0.3,       # [CAP] routed edge 0.2
    "min_silk_clearance": 0.15,             # [CAP] pad to silkscreen
    "min_text_height": 1.0,                 # [CAP] legend 40 mil
    "min_text_thickness": 0.15,             # [CAP] legend line 0.15
    "min_resolved_spokes": 1,               # 0402 pads beside other nets get one 0.3 mm spoke; ample for signal ground
    "solder_mask_to_copper_clearance": 0.0,
    "max_error": 0.005,
    "use_height_for_length_calcs": True,
}

DRU = """(version 1)
# JLCPCB 2-layer, 1 oz -- rules the board constraint table cannot express.
# Generated by setup_fab.py; sources and dates are in its docstring.

(rule "JLC: coplanar ground gap for the 90 ohm USB pair (calculator: 8 mil)"
\t(condition "A.NetClass == 'USB_90' && B.Type == 'Zone'")
\t(constraint clearance (min %(coplanar)smm)))

(rule "JLC: PTH annular ring, 2-layer 1 oz (0.25 recommended, 0.18 absolute)"
\t(condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole'")
\t(constraint annular_width (min 0.18mm)))

(rule "JLC: PTH hole to copper (0.35 recommended, 0.28 minimum)"
\t(condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole'")
\t(constraint hole_clearance (min 0.28mm)))

(rule "JLC: NPTH to copper"
\t(condition "A.Pad_Type == 'NPTH, mechanical'")
\t(constraint hole_clearance (min 0.2mm)))

(rule "JLC: pad hole to pad hole"
\t(condition "A.Type == 'Pad' && B.Type == 'Pad'")
\t(constraint hole_to_hole (min 0.45mm)))

(rule "JLC: NPTH minimum 0.5 mm"
\t(condition "A.Pad_Type == 'NPTH, mechanical'")
\t(constraint hole_size (min 0.5mm)))

(rule "JLC: SMD pad to pad, different nets"
\t(condition "A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD' && A.Net != B.Net")
\t(constraint clearance (min 0.15mm)))

(rule "JLC: pad to silkscreen"
\t(layer outer)
\t(condition "A.Type == 'Pad' && (B.Type == 'Text' || B.Type == 'Graphic')")
\t(constraint silk_clearance (min 0.15mm)))

(rule "JLC: legend 1.0 mm tall, 0.15 mm line"
\t(layer "?.Silkscreen")
\t(condition "A.Type == 'Text' || A.Type == 'Text Box'")
\t(constraint text_height (min 1.0mm))
\t(constraint text_thickness (min 0.15mm)))
""" % {"coplanar": USB_COPLANAR}


def main():
    import json
    b = Board(__file__, NAME, size=(W, H), origin=cfg.ORIGIN)
    b._stale_holes.clear()
    b._stale_zones.clear()
    b.set_netclasses(CLASSES, PATTERNS, RULES)
    # the Default class too: JLCPCB's no-surcharge via and a sane signal width
    pro_path = os.path.join(b.dir, NAME + ".kicad_pro")
    pro = json.load(open(pro_path, encoding="utf-8"))
    for c in pro["net_settings"]["classes"]:
        if c["name"] == "Default":
            c.update(DEFAULT_CLASS)
    json.dump(pro, open(pro_path, "w", encoding="utf-8", newline="\n"), indent=2)
    b.check_netclasses()
    b.set_stackup(STACKUP)
    dru = os.path.join(b.dir, NAME + ".kicad_dru")
    open(dru, "w", encoding="utf-8", newline="\n").write(DRU)
    say(f"wrote {os.path.basename(dru)}: {DRU.count('(rule ')} JLCPCB rules")
    say("")
    say("USB pair: %.4f / %.4f mm, ground pour %.4f mm either side (JLCPCB calculator, JLC0216A)."
        % (USB_W, USB_GAP, USB_COPLANAR))
    say("JLCPCB does not test impedance on 2-layer boards; the geometry is theirs, the result is untested.")


if __name__ == "__main__":
    main()
