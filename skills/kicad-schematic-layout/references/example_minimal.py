#!/usr/bin/env python3
"""Minimal worked example -- stock KiCad libraries only, so it runs anywhere.

One sheet: 5 V input connector, AMS1117 3.3 V regulator, and an LM358 sensor
amplifier. Small, but it exercises the parts of schlib you meet in real
designs:

  * mirror="y" on the input connector so its pins face into the circuit
  * derived library symbols (both AMS1117-3.3 and LM358 use `extends`)
  * a multi-unit part: op-amp A, op-amp B and the power unit placed separately
  * stub(): a short wire out of a pin ending in a power port or label -- the
    single most useful idiom for clean sheets
  * a rail with capacitors hanging off it, a feedback network, a follower loop
  * paper="auto" picks the smallest page the drawing fits

    python example_minimal.py --project <empty dir>
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from schlib import Design  # noqa: E402

NETS = {
    "+5V":        [("J1", 1), ("U1", 3), ("C1", 1), ("U2", 8), ("C3", 1)],
    "GND":        [("J1", 3), ("U1", 1), ("C1", 2), ("C2", 2), ("R2", 2), ("J2", 2),
                   ("U2", 5), ("U2", 4), ("C3", 2)],
    "+3V3":       [("U1", 2), ("C2", 1)],
    "SENSOR":     [("J1", 2), ("U2", 3)],
    "FB":         [("U2", 2), ("R1", 1), ("R2", 1)],
    "AMP_OUT":    [("U2", 1), ("R1", 2), ("J2", 1)],
    "U2B_FOLLOW": [("U2", 7), ("U2", 6)],
}
NO_CONNECT = []


def build(d):
    s = d.sheet("sensor-amp.kicad_sch", "3.3 V Regulator & Sensor Amplifier",
                paper="auto", comment="Stock-library example for the kicad-schematic-layout skill")

    # ---- Input connector: mirrored so pins point right, into the circuit ----
    s.place("J1", "Connector_Generic:Conn_01x03", "SENSOR IN", 25.4, 50.8, mirror="y")
    s.stub("J1", 1, 5.08, power="+5V")
    s.stub("J1", 2, 7.62, label="SENSOR")     # staggered so text clears the ports
    s.stub("J1", 3, 5.08, power="GND")

    # ---- Regulator: input and output rails with caps hanging below ----------
    s.place("U1", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", 71.12, 50.8)
    vi, vo = s.pin("U1", "VI"), s.pin("U1", "VO")
    s.stub("U1", "GND", 2.54, power="GND")

    s.place("C1", "Device:C", "10uF", vi[0] - 7.62, vi[1] + 5.08)
    s.wire(vi, (vi[0] - 12.7, vi[1]))
    s.power("+5V", vi[0] - 12.7, vi[1])
    s.wire(s.pin("C1", 1), (vi[0] - 7.62, vi[1]))
    s.stub("C1", 2, 2.54, power="GND")

    # ERC power sources. Every pin on +5V and GND is a power *input* (the
    # connector's pins are passive), so ERC reports "power pin not driven"
    # until a PWR_FLAG marks where each rail enters the board.
    s.wire((vi[0] - 2.54, vi[1]), (vi[0] - 2.54, vi[1] - 5.08))    # T on the 5 V rail
    s.power("PWR_FLAG", vi[0] - 2.54, vi[1] - 5.08)
    s.power("PWR_FLAG", 30.48, 63.5)                                  # flag + ground pair
    s.wire((30.48, 63.5), (30.48, 68.58))
    s.gnd(30.48, 68.58)

    s.place("C2", "Device:C", "10uF", vo[0] + 7.62, vo[1] + 5.08)
    s.wire(vo, (vo[0] + 12.7, vo[1]))
    s.power("+3V3", vo[0] + 12.7, vo[1])
    s.wire(s.pin("C2", 1), (vo[0] + 7.62, vo[1]))
    s.stub("C2", 2, 2.54, power="GND")

    # ---- Op-amp A: non-inverting, G = 1 + R1/R2 ------------------------------
    s.place("U2", "Amplifier_Operational:LM358", "LM358", 132.08, 55.88, unit=1)
    plus, minus, out = s.pin("U2", 3, 1), s.pin("U2", 2, 1), s.pin("U2", 1, 1)
    s.wire(plus, (plus[0] - 7.62, plus[1]))
    s.label("SENSOR", plus[0] - 7.62, plus[1], angle=180)

    fb_x = minus[0] - 2.54
    s.place("R2", "Device:R", "10k", fb_x, minus[1] + 16.51)
    s.wire(minus, (fb_x, minus[1]), s.pin("R2", 1))
    s.stub("R2", 2, 2.54, power="GND")

    # Feedback resistor horizontal, a full grid row below the op-amp so its
    # reference clears the op-amp's value text.
    s.place("R1", "Device:R", "10k", minus[0] + 6.35, minus[1] + 10.16, rot=90)
    r1a, r1b = s.pin("R1", 1), s.pin("R1", 2)
    s.wire(r1a, (fb_x, r1a[1]))                       # T onto the feedback node
    s.wire(r1b, (out[0] + 2.54, r1b[1]), (out[0] + 2.54, out[1]))

    s.place("J2", "Connector_Generic:Conn_01x02", "AMP OUT", out[0] + 17.78, out[1])
    s.wire(out, s.pin("J2", 1))
    s.label("AMP_OUT", out[0] + 5.08, out[1])
    s.stub("J2", 2, 2.54, power="GND")

    # ---- Op-amp B: unused -> grounded follower -------------------------------
    s.place("U2", "Amplifier_Operational:LM358", "LM358", 132.08, 91.44, unit=2)
    bm, bo = s.pin("U2", 6, 2), s.pin("U2", 7, 2)
    s.stub("U2", 5, 5.08, power="GND", unit=2)
    loop_y = bm[1] + 5.08
    s.wire(bo, (bo[0] + 2.54, bo[1]), (bo[0] + 2.54, loop_y),
           (bm[0] - 2.54, loop_y), (bm[0] - 2.54, bm[1]), bm)

    # ---- Op-amp power unit + decoupling ---------------------------------------
    s.place("U2", "Amplifier_Operational:LM358", "LM358", 157.48, 91.44, unit=3)
    s.stub("U2", 8, 2.54, power="+5V", unit=3)
    s.stub("U2", 4, 2.54, power="GND", unit=3)
    s.place("C3", "Device:C", "100nF", 167.64, 91.44)
    s.stub("C3", 1, 2.54, power="+5V")
    s.stub("C3", 2, 2.54, power="GND")

    # ---- Blocks and notes -------------------------------------------------------
    s.box(15.24, 38.1, 96.52, 76.2, title="INPUT & 3.3 V REGULATOR")
    s.box(106.68, 38.1, 177.8, 111.76, title="SENSOR AMPLIFIER")
    s.note("AMS1117: ~1.1 V dropout, so 5 V in leaves margin for 3.3 V out.\n"
           "Check the datasheet's output-capacitor ESR range.", 15.24, 81.28, 1.0)
    # Notes go in the free left column, not under the amplifier: the bottom
    # right of every sheet belongs to the title block.
    s.note("G = 1 + R1/R2 = 2: a 0-1.5 V sensor gives 0-3 V at AMP_OUT.\n"
           "U2 runs from +5V because an LM358 only swings to about V+ - 1.5 V.",
           15.24, 88.9, 1.0)
    s.note("Unused half B: follower with + at GND (inside the LM358's\n"
           "common-mode range) so it cannot oscillate. C3 close to pin 8.",
           15.24, 96.52, 1.0)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    d = Design(args.project, "sensor-amp", title="Sensor Amplifier", rev="A")
    build(d)
    d.write(force=args.force)
    ok = d.verify(NETS, NO_CONNECT)
    d.erc()
    d.check_text()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
