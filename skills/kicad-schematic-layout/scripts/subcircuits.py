#!/usr/bin/env python3
"""subcircuits -- proven building blocks for schlib designs.

Each block is one function of a board -- a regulator with its capacitors, a
USB-C sink, an ESP32-S3 with its reset and boot network -- carrying three
things the generator would otherwise write by hand:

    parts    PARTS entries: (lib_id, value, footprint, LCSC, manufacturer, MPN)
    nets     NETS entries, with the net names you pass in
    draw()   the block drawn the conventional way, inside its own titled block

so the spec and the drawing come from the same place and `Design.verify()`
proves they agree. Stock KiCad symbols only; passives and ICs that appear in
the user's own verified BOM carry that LCSC number, everything else leaves
it blank for the lcsc/bom skills to fill.

    from schlib import Design
    from subcircuits import Spec, Refs, Ldo, UsbCSink, Esp32S3Core

    refs, spec = Refs(), Spec()
    usb = spec.add(UsbCSink(refs, vbus="+5V"))
    ldo = spec.add(Ldo(refs, vin="+5V", vout="+3V3"))
    mcu = spec.add(Esp32S3Core(refs, gpio={"IO8": "I2C_SDA", "IO9": "I2C_SCL"}))
    spec.lint()                              # design rules, before drawing

    d = Design(project_dir, "board")
    s = d.sheet("board.kicad_sch", "Board", paper="auto")
    for block in (usb, ldo, mcu):
        block.draw(s)                        # each in its own block
    s.arrange()
    d.write()
    d.verify(spec.NETS, spec.NO_CONNECT)

Change a part before drawing by editing `block.parts[ref]`; the design notes
each block puts on the sheet are in `block.notes`.
"""
from __future__ import annotations

import re
from collections import defaultdict

from schlib import lint_spec, snap

# ---------------------------------------------------------------------------
# Parts catalogue -- lines lifted from a verified, ordered BOM, so the LCSC
# numbers are known-good (not guessed). Anything missing here gets a blank
# LCSC field: fill it with the lcsc skill rather than from memory.
# ---------------------------------------------------------------------------
R0402 = "Resistor_SMD:R_0402_1005Metric"
C0402 = "Capacitor_SMD:C_0402_1005Metric"
PASSIVES = {
    ("R", "10"):    (R0402, "C25077", "Uniroyal", "0402WGF100JTCE"),
    ("R", "330"):   (R0402, "C25104", "Uniroyal", "0402WGF3300TCE"),
    ("R", "4.7k"):  (R0402, "C25900", "Uniroyal", "0402WGF4701TCE"),
    ("R", "5.1k"):  (R0402, "C25905", "Uniroyal", "0402WGF5101TCE"),
    ("R", "10k"):   (R0402, "C25744", "Uniroyal", "0402WGF1002TCE"),
    ("R", "100k"):  (R0402, "C25741", "Uniroyal", "0402WGF1003TCE"),
    ("R", "390k"):  (R0402, "C2909352", "Uniroyal", "FRC0402F3903TS"),
    ("R", "3.32"):  ("Resistor_SMD:R_0805_2012Metric", "C3013220", "Uniroyal", "FRC0805F3R32TS"),
    ("C", "100nF"): (C0402, "C1525", "CCTC", "CL05B104KO5NNNC"),
    ("C", "1uF"):   (C0402, "C52923", "Samwha/CCTC", "CL05A105KA5NQNC"),
    ("C", "10uF"):  ("Capacitor_SMD:C_0603_1608Metric", "C19702", "CCTC", "CL10A106KP8NNNC"),
    ("C", "22uF"):  ("Capacitor_SMD:C_0805_2012Metric", "C45783", "CCTC", "CL21A226MAQNNNE"),
    ("C", "10uF/50V"): ("Capacitor_SMD:C_1210_3225Metric", "C2918502", "Samsung/CCTC",
                        "CS3225X7R106K500NRL"),
}


def passive(kind, value):
    """PARTS tuple for a resistor or capacitor, from the catalogue if known."""
    lib_id = {"R": "Device:R", "C": "Device:C"}[kind]
    fp, lcsc, mfr, mpn = PASSIVES.get((kind, value), (R0402 if kind == "R" else C0402,
                                                       "", "", ""))
    return (lib_id, value, fp, lcsc, mfr, mpn)


class Refs:
    """Reference allocator shared by every block of a design: R1, R2, C1 ...

    reserve() the refs a hand-written part of the design already uses."""

    def __init__(self):
        self.used = set()

    def reserve(self, *refs):
        self.used.update(refs)

    def next(self, prefix):
        n = 1
        while f"{prefix}{n}" in self.used:
            n += 1
        ref = f"{prefix}{n}"
        self.used.add(ref)
        return ref


class Spec:
    """PARTS / NETS / NO_CONNECT assembled from blocks (plus anything added by
    hand), in the form Design.verify() and lint_spec() take."""

    def __init__(self):
        self.PARTS = {}
        self.NETS = defaultdict(list)
        self.NO_CONNECT = []

    def add(self, block):
        for ref, p in block.parts.items():
            if ref in self.PARTS:
                raise ValueError(f"{ref} is defined twice")
            self.PARTS[ref] = p
        for net, pins in block.nets.items():
            self.NETS[net] += pins
        self.NO_CONNECT += block.no_connect
        return block

    def lint(self, lib=None, project_dir=None, **kw):
        return lint_spec(self.PARTS, dict(self.NETS), self.NO_CONNECT, lib=lib,
                         project_dir=project_dir, **kw)


def _has_power_symbol(lib, name):
    try:
        lib.get(f"power:{name}")
        return True
    except Exception:                                   # noqa: BLE001
        return False


class Block:
    """Base: parts, nets and no-connects, plus the drawing helpers."""

    title = "BLOCK"

    def __init__(self, refs):
        self.refs = refs
        self.parts = {}
        self.nets = defaultdict(list)
        self.no_connect = []
        self.notes = []

    # ---- spec ----------------------------------------------------------------
    def part(self, prefix, tup):
        ref = self.refs.next(prefix)
        self.parts[ref] = tup
        return ref

    def conn(self, net, *pins):
        """conn("GND", ("U1", "2"), ("C1", "2")) -- pins by number."""
        for ref, pin in pins:
            self.nets[net].append((ref, str(pin)))

    def nc(self, ref, *pins):
        self.no_connect += [(ref, str(p)) for p in pins]

    # ---- drawing -------------------------------------------------------------
    def put(self, s, ref, x, y, rot=0, mirror=None):
        lib_id, value, fp, lcsc, mfr, mpn = self.parts[ref]
        props = {"Footprint": fp}
        for k, v in (("LCSC", lcsc), ("Manufacturer", mfr), ("MPN", mpn)):
            if v:
                props[k] = v
        s.place(ref, lib_id, value, snap(x), snap(y), rot, mirror, props=props)
        return ref

    def put_pin(self, s, ref, pin, at, rot=0, mirror=None):
        """Place `ref` so that its pin `pin` lands exactly on `at`."""
        self.put(s, ref, 0, 0, rot, mirror)
        px, py = s.pin(ref, pin)
        sym = s._sym(ref)[0]
        sym["x"], sym["y"] = round(at[0] - px, 4), round(at[1] - py, 4)
        s._pin_pts = s._fields = s._items = None
        return ref

    def rail_symbol(self, s, name, x, y, up=True):
        """Rail symbol at (x, y): a power port if KiCad has one by that name,
        else a net label reading away from the wire."""
        if _has_power_symbol(s.lib, name):
            if name.upper().startswith("GND") or name.upper() in ("VSS", "GNDA", "GNDD"):
                s.power(name, x, y)                     # hangs below
            else:
                s.power(name, x, y, rot=0 if up else 180)
        else:
            s.label(name, x, y, angle=90 if up else 270)

    def drop(self, s, name, at, length=2.54, up=True):
        """Short vertical wire from `at` ending in a rail symbol."""
        x, y = at
        end = (x, round(y - length if up else y + length, 4))
        s.wire(at, end)
        self.rail_symbol(s, name, *end, up=up)
        return end

    def draw_notes(self, s, x, y):
        for i, n in enumerate(self.notes):
            s.note(n, snap(x), snap(y + i * 2.54), 1.0)

    def draw(self, s, x=None, y=None, notes=True):
        """Draw into its own titled block on sheet `s`. (x, y) is where the
        main part goes; by default the block starts clear to the right of
        everything already drawn. arrange() moves blocks anyway, but they must
        not overlap until it does: relief runs first, and two blocks drawn on
        top of each other look connected to it."""
        if x is None or y is None:
            x0, _y0, x1, _y1 = s.content_bbox()
            x = x1 + 76.2 if (x1 - x0) > 0 else 50.8
            y = 101.6
        with s.block(self.title) as b:
            self._draw(s, snap(x), snap(y))
            if notes and self.notes:
                here = {n: (b["start"][i], len(getattr(s, n)))
                        for i, n in enumerate(s._LISTS)}
                bb = s._bbox(here)
                self.draw_notes(s, bb[0] if bb else x, (bb[3] + 5.08) if bb else y + 20)
        return self

    def _draw(self, s, x, y):
        raise NotImplementedError


# ===========================================================================
# Power
# ===========================================================================

class Ldo(Block):
    """Linear regulator with input and output capacitors.

    part "AP2112K-3.3" (600 mA, EN pin, 1 uF ceramic in/out per datasheet) or
    "AMS1117-3.3" (1 A, needs >= 22 uF out -- older parts want tantalum ESR).
    en: None ties EN to VIN (always on); a net name brings it out."""

    title = "3.3 V LDO"

    def __init__(self, refs, vin="+5V", vout="+3V3", part="AP2112K-3.3", en=None,
                 gnd="GND", title=None):
        super().__init__(refs)
        self.vin, self.vout, self.gnd, self.en, self.kind = vin, vout, gnd, en, part
        self.title = title or f"{vout.lstrip('+')} LDO"
        if part == "AP2112K-3.3":
            self.u = self.part("U", ("Regulator_Linear:AP2112K-3.3", "AP2112K-3.3",
                                     "Package_TO_SOT_SMD:SOT-23-5", "C51118", "Diodes Inc",
                                     "AP2112K-3.3TRG1"))
            self.pins = dict(vin="1", gnd="2", en="3", nc="4", vout="5")
            self.cin = self.part("C", passive("C", "1uF"))
            self.cout = self.part("C", passive("C", "1uF"))
            self.notes = ["AP2112K: 1 uF ceramic on VIN and VOUT (datasheet min),",
                          "both within 2 mm of the pins. 600 mA max, 250 mV dropout."]
        elif part == "AMS1117-3.3":
            self.u = self.part("U", ("Regulator_Linear:AMS1117-3.3", "AMS1117-3.3",
                                     "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "C6186",
                                     "Advanced Monolithic Systems", "AMS1117-3.3"))
            self.pins = dict(vin="3", gnd="1", vout="2")
            self.cin = self.part("C", passive("C", "10uF"))
            self.cout = self.part("C", passive("C", "22uF"))
            self.notes = ["AMS1117: >= 22 uF on VOUT for stability; ~1.1 V dropout,",
                          "so VIN >= 4.5 V for a 3.3 V output."]
        else:
            raise ValueError(f"Ldo part {part!r}: use 'AP2112K-3.3' or 'AMS1117-3.3'")
        p = self.pins
        self.conn(vin, (self.u, p["vin"]), (self.cin, 1))
        self.conn(vout, (self.u, p["vout"]), (self.cout, 1))
        self.conn(gnd, (self.u, p["gnd"]), (self.cin, 2), (self.cout, 2))
        if "en" in p:
            if en is None:
                self.conn(vin, (self.u, p["en"]))
            else:
                self.conn(en, (self.u, p["en"]))
        if "nc" in p:
            self.nc(self.u, p["nc"])

    def _draw(self, s, x, y):
        u, p = self.u, self.pins
        self.put(s, u, x, y)
        vi, vo = s.pin(u, p["vin"]), s.pin(u, p["vout"])
        xi, xo = vi[0] - 10.16, vo[0] + 10.16
        # input: rail drops in from above, the capacitor hangs below the node
        s.wire(vi, (xi, vi[1]))
        self.drop(s, self.vin, (xi, vi[1]), 5.08)
        self.put_pin(s, self.cin, 1, (xi, vi[1]))
        s.stub(self.cin, 2, 2.54, power=self.gnd)
        s.wire(vo, (xo, vo[1]))
        self.drop(s, self.vout, (xo, vo[1]), 5.08)
        self.put_pin(s, self.cout, 1, (xo, vo[1]))
        s.stub(self.cout, 2, 2.54, power=self.gnd)
        s.stub(u, p["gnd"], 2.54, power=self.gnd)
        if "en" in p:
            en = s.pin(u, p["en"])
            if self.en is None:
                # EN tied up to VIN, joining the input wire between pin and node
                s.wire(en, (vi[0] - 2.54, en[1]), (vi[0] - 2.54, vi[1]))
            else:
                s.wire(en, (vi[0] - 2.54, en[1]), (vi[0] - 2.54, en[1] + 7.62))
                s.label(self.en, vi[0] - 2.54, en[1] + 7.62, angle=270)
        if "nc" in p:
            s.nc(u, p["nc"])


class Mt3608Boost(Block):
    """MT3608 boost converter: Vout = 0.6 V x (1 + Rtop / Rbot).

    Default 5 V -> 24 V (390k / 10k) for a 4-20 mA loop supply. en: None ties
    EN to VIN; a net name gives firmware control of the output, with a 100k
    pull-up to `en_pull` so the output is defined while the MCU is in reset
    (en_pull=None to leave it to the caller). Not short-circuit protection:
    a boost cannot disconnect its output (VIN -> L -> D is a DC path)."""

    title = "BOOST"

    def __init__(self, refs, vin="+5V", vout="+24V", en=None, gnd="GND", r_top="390k",
                 r_bot="10k", title=None, sw_net=None, fb_net=None, en_pull="+3V3",
                 flag=True):
        super().__init__(refs)
        self.flag = flag
        self.vin, self.vout, self.en, self.gnd = vin, vout, en, gnd
        self.title = title or f"{vout.lstrip('+')} BOOST"
        self.u = self.part("U", ("Regulator_Switching:MT3608", "MT3608",
                                 "Package_TO_SOT_SMD:SOT-23-6", "C84817", "Aerosemi", "MT3608"))
        self.l = self.part("L", ("Device:L", "4.7uH", "Inductor_SMD:L_Sunlord_MWSA0412S",
                                 "C167874", "Sunlord", "FNR4030S4R7MT"))
        self.d = self.part("D", ("Diode:SS34", "SS34", "Diode_SMD:D_SMA", "C8678", "MDD", "SS34"))
        self.cin = self.part("C", passive("C", "10uF/50V"))
        self.cout = self.part("C", passive("C", "10uF/50V"))
        self.rt = self.part("R", passive("R", r_top))
        self.rb = self.part("R", passive("R", r_bot))
        sw = sw_net or f"{self.u}_SW"
        fb = fb_net or f"{self.u}_FB"
        u = self.u
        self.conn(vin, (u, 5), (self.l, 1), (self.cin, 1))
        self.conn(sw, (u, 1), (self.l, 2), (self.d, 2))
        self.conn(vout, (self.d, 1), (self.cout, 1), (self.rt, 1))
        self.conn(fb, (u, 3), (self.rt, 2), (self.rb, 1))
        self.conn(gnd, (u, 2), (self.cin, 2), (self.cout, 2), (self.rb, 2))
        self.conn(en if en else vin, (u, 4))
        self.r_en = None
        if en and en_pull:
            self.r_en = self.part("R", passive("R", "100k"))
            self.conn(en_pull, (self.r_en, 1))
            self.conn(en, (self.r_en, 2))
        self.en_pull = en_pull
        self.nc(u, 6)
        from schlib import parse_value
        vt, vb = parse_value(r_top), parse_value(r_bot)
        vo = 0.6 * (1 + vt / vb) if vt and vb else None
        self.notes = [f"Vout = 0.6 V x (1 + {r_top} / {r_bot})"
                      + (f" = {vo:.1f} V" if vo else ""),
                      "MT3608: 28 V max out, 4 A switch limit. Keep the SW node",
                      "(pin 1, L, D) small; Cout and D close to each other."]

    def _draw(self, s, x, y):
        u = self.u
        self.put(s, u, x, y)
        vin, en, swp, fbp = (s.pin(u, 5), s.pin(u, 4), s.pin(u, 1), s.pin(u, 3))
        xn = vin[0] - 15.24                  # input node, clear of the EN run
        top = vin[1] - 10.16                 # inductor row, clear of U's text
        xs = swp[0] + 5.08                   # switch node
        # input: rail on top of the node column, L across the top, Cin below
        s.wire(vin, (xn, vin[1]))
        s.wire((xn, vin[1]), (xn, top))
        self.drop(s, self.vin, (xn, top), 2.54)
        self.put_pin(s, self.l, 1, (xn + 5.08, top), rot=90)
        s.wire((xn, top), s.pin(self.l, 1))
        s.wire(s.pin(self.l, 2), (xs, top), (xs, swp[1]))
        s.wire(swp, (xs, swp[1]))
        self.put_pin(s, self.cin, 1, (xn, vin[1]))
        s.stub(self.cin, 2, 2.54, power=self.gnd)
        if self.en:
            # EN runs down and out under the input capacitor; its pull-up
            # stands on that run, and the net leaves on a label
            n = (xn - 5.08, en[1] + 10.16)
            s.wire(en, (en[0] - 1.27, en[1]), (en[0] - 1.27, n[1]), n)
            if self.r_en:
                self.put_pin(s, self.r_en, 2, n)
                s.stub(self.r_en, 1, 2.54, power=self.en_pull)
            s.wire(n, (n[0] - 5.08, n[1]))
            s.label(self.en, n[0] - 5.08, n[1], angle=180)
        else:
            s.wire(en, (en[0] - 1.27, en[1]), (en[0] - 1.27, vin[1]))
        s.stub(u, 2, 2.54, power=self.gnd)
        s.nc(u, 6)
        # diode to the output node; divider hangs from it, Cout beside it
        self.put_pin(s, self.d, 2, (xs + 2.54, swp[1]), rot=180)
        s.wire((xs, swp[1]), s.pin(self.d, 2))
        xo = s.pin(self.d, 1)[0] + 5.08
        s.wire(s.pin(self.d, 1), (xo, swp[1]))
        self.drop(s, self.vout, (xo, swp[1]), 5.08)
        self.put_pin(s, self.rt, 1, (xo, swp[1]))
        fbn = s.pin(self.rt, 2)
        self.put_pin(s, self.rb, 1, fbn)
        s.stub(self.rb, 2, 2.54, power=self.gnd)
        s.wire(fbp, (fbp[0] + 2.54, fbp[1]), (fbp[0] + 2.54, fbn[1]), fbn)
        xc = xo + 10.16
        s.wire((xo, swp[1]), (xc, swp[1]))
        self.put_pin(s, self.cout, 1, (xc, swp[1]))
        s.stub(self.cout, 2, 2.54, power=self.gnd)
        if self.flag:
            # a diode drives this rail, and ERC only trusts a power output
            s.wire((xc, swp[1]), (xc, swp[1] - 2.54))
            s.power("PWR_FLAG", xc, swp[1] - 2.54)


# ===========================================================================
# Interfaces
# ===========================================================================

class UsbCSink(Block):
    """USB-C receptacle as a 5 V sink with USB 2.0 data.

    5.1k on each CC pin to GND (the sink's Rd -- without them a C-to-C cable
    delivers no VBUS), both D+/D- pairs joined for plug flipping, SBU unused,
    and a USBLC6-2SC6 on the data lines when esd=True.

    Name the pair so KiCad sees a differential pair: dp="USB_D_P", dm="USB_D_N"
    (it wants _P/_N or +/-; the historical defaults USB_DP/USB_DM are not one).

    esd_vbus=False leaves the array's VBUS pin (5) open. In a flow-through
    layout that pin sits between D+ and D-, so on a 2-layer board the only way
    to it is a bottom trace that slots the ground under the data lines. ST's
    Figure 6 clamps an I/O strike through the internal Transil to GND, and
    VCL is specified I/O to GND, so the array works either way; give VBUS its
    own TVS."""

    title = "USB-C INPUT"

    def __init__(self, refs, vbus="+5V", dp="USB_DP", dm="USB_DM", gnd="GND", esd=True,
                 cc=("CC1", "CC2"), shield="GND", title=None, flag=True, esd_vbus=True):
        super().__init__(refs)
        self.esd_vbus = esd_vbus
        self.flag = flag           # PWR_FLAGs: this is where VBUS and GND enter
        self.vbus, self.dp, self.dm, self.gnd, self.cc, self.shield = vbus, dp, dm, gnd, cc, shield
        self.title = title or self.title
        self.j = self.part("J", ("Connector:USB_C_Receptacle_USB2.0_16P", "USB-C",
                                 "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
                                 "C165948", "HRO", "TYPE-C-31-M-12"))
        self.r1 = self.part("R", passive("R", "5.1k"))
        self.r2 = self.part("R", passive("R", "5.1k"))
        j = self.j
        self.conn(vbus, (j, "A4"), (j, "A9"), (j, "B4"), (j, "B9"))
        self.conn(gnd, (j, "A1"), (j, "A12"), (j, "B1"), (j, "B12"), (self.r1, 2), (self.r2, 2))
        self.conn(cc[0], (j, "A5"), (self.r1, 1))
        self.conn(cc[1], (j, "B5"), (self.r2, 1))
        self.conn(dp, (j, "A6"), (j, "B6"))
        self.conn(dm, (j, "A7"), (j, "B7"))
        self.nc(j, "A8", "B8")
        if shield:
            self.conn(shield, (j, "SH"))
        else:
            self.nc(j, "SH")
        self.esd = None
        if esd:
            self.esd = self.part("U", ("Power_Protection:USBLC6-2SC6", "USBLC6-2SC6",
                                       "Package_TO_SOT_SMD:SOT-23-6", "C7519", "STMicroelectronics",
                                       "USBLC6-2SC6"))
            self.conn(dm, (self.esd, 1), (self.esd, 6))
            self.conn(dp, (self.esd, 3), (self.esd, 4))
            if esd_vbus:
                self.conn(vbus, (self.esd, 5))
            else:
                self.nc(self.esd, 5)
            self.conn(gnd, (self.esd, 2))
        self.notes = ["CC1/CC2: 5.1k to GND each -- one shared resistor",
                      "breaks C-to-C cables. Route D+/D- as a 90 ohm pair."]

    def _draw(self, s, x, y):
        j = self.j
        self.put(s, j, x, y)
        vb = s.pin(j, "A4")
        s.wire(vb, (vb[0] + 5.08, vb[1]))
        self.drop(s, self.vbus, (vb[0] + 5.08, vb[1]), 2.54)
        g = s.stub(j, "A1", 2.54, power=self.gnd)
        if self.flag:
            # both rails enter the board here, and the connector's pins are
            # passive: ERC needs to be told something drives them
            s.wire((vb[0] + 5.08, vb[1]), (vb[0] + 12.7, vb[1]), (vb[0] + 12.7, vb[1] - 2.54))
            s.power("PWR_FLAG", vb[0] + 12.7, vb[1] - 2.54)
            s.wire(g, (g[0] + 5.08, g[1]))
            s.power("PWR_FLAG", g[0] + 5.08, g[1], rot=180)
        if self.shield:
            s.stub(j, "SH", 2.54, power=self.shield)
        else:
            s.nc(j, "SH")
        s.nc(j, "A8", "B8")
        s.stub(j, "A5", 2.54, label=self.cc[0])
        s.stub(j, "B5", 12.7, label=self.cc[1])
        # join each flip pair with a short jog, then label the pair once
        for a, b, net, out in (("A7", "B7", self.dm, "A7"), ("A6", "B6", self.dp, "B6")):
            pa, pb = s.pin(j, a), s.pin(j, b)
            xj = pa[0] + 2.54
            s.wire(pa, (xj, pa[1]))
            s.wire(pb, (xj, pb[1]))
            s.wire((xj, pa[1]), (xj, pb[1]))
            po = pa if out == a else pb
            s.wire((xj, po[1]), (xj + 7.62, po[1]))
            s.label(net, xj + 7.62, po[1])
        # CC resistors: a pair standing to the right, each on its own label
        rx = vb[0] + 25.4
        for i, (r, net) in enumerate(((self.r1, self.cc[0]), (self.r2, self.cc[1]))):
            self.put(s, r, rx + i * 10.16, y - 5.08)
            s.stub(r, 1, 2.54, label=net)
            s.stub(r, 2, 2.54, power=self.gnd)
        if self.esd:
            e = self.esd
            self.put(s, e, rx + 5.08, y + 15.24)
            s.stub(e, 1, 5.08, label=self.dm)
            s.stub(e, 3, 5.08, label=self.dp)
            s.stub(e, 6, 5.08, label=self.dm)
            s.stub(e, 4, 5.08, label=self.dp)
            if self.esd_vbus:
                s.stub(e, 5, 2.54, power=self.vbus)
            else:
                s.nc(e, 5)
            s.stub(e, 2, 2.54, power=self.gnd)


class I2cPullups(Block):
    """Pull-ups for one I2C bus: one pair per bus, not one per device."""

    title = "I2C PULL-UPS"

    def __init__(self, refs, sda="I2C_SDA", scl="I2C_SCL", rail="+3V3", value="4.7k",
                 title=None):
        super().__init__(refs)
        self.sda, self.scl, self.rail = sda, scl, rail
        self.title = title or self.title
        self.r1 = self.part("R", passive("R", value))
        self.r2 = self.part("R", passive("R", value))
        self.conn(rail, (self.r1, 1), (self.r2, 1))
        self.conn(sda, (self.r1, 2))
        self.conn(scl, (self.r2, 2))
        self.notes = [f"{value}: 100/400 kHz up to ~200 pF of bus."]

    def _draw(self, s, x, y):
        self.put(s, self.r1, x, y)
        pitch = s.part_pitch([self.r1]) + 2.54
        self.put(s, self.r2, x + pitch, y)
        t1, t2 = s.pin(self.r1, 1), s.pin(self.r2, 1)
        top = t1[1] - 2.54
        s.wire(t1, (t1[0], top), (t2[0], top), t2)
        self.drop(s, self.rail, (t1[0], top), 2.54)
        s.stub(self.r1, 2, 5.08, label=self.sda)
        s.stub(self.r2, 2, 2.54, label=self.scl)


class LedIndicator(Block):
    """Rail -> resistor -> LED -> GND. 330 R from 3.3 V is ~3 mA on a green LED."""

    title = "INDICATOR"

    def __init__(self, refs, rail="+3V3", r="330", gnd="GND", title=None, net=None,
                 led=("Device:LED", "Power", "LED_SMD:LED_0805_2012Metric", "C84256", "", "NCD0805R1")):
        super().__init__(refs)
        self.rail, self.gnd = rail, gnd
        self.title = title or self.title
        self.r = self.part("R", passive("R", r))
        self.led = self.part("LED", led)
        self.conn(rail, (self.r, 1))
        self.conn(net or f"{self.led}_A", (self.r, 2), (self.led, 2))
        self.conn(gnd, (self.led, 1))

    def _draw(self, s, x, y):
        self.put(s, self.r, x, y)
        s.stub(self.r, 1, 2.54, power=self.rail)
        self.put_pin(s, self.led, 2, (x, s.pin(self.r, 2)[1] + 2.54), rot=90)
        s.wire(s.pin(self.r, 2), s.pin(self.led, 2))
        s.stub(self.led, 1, 2.54, power=self.gnd)


# ===========================================================================
# Processor
# ===========================================================================

class Esp32S3Core(Block):
    """ESP32-S3-WROOM-1 with what it needs to boot: 10 uF + 100 nF on 3V3, an
    EN RC (10k / 1 uF -- EN must rise after 3V3 settles) with a RESET button,
    a BOOT button on IO0, and native USB on IO19/IO20 (pins 13/14).

    gpio {pin name: net} brings pins out on labels; every other GPIO gets a
    no-connect flag. RXD0/TXD0 are left unconnected unless named in gpio."""

    title = "ESP32-S3"

    def __init__(self, refs, rail="+3V3", gnd="GND", dp="USB_DP", dm="USB_DM", en="EN",
                 boot="IO0", gpio=None, title=None, lib=None):
        super().__init__(refs)
        self.rail, self.gnd, self.dp, self.dm, self.en, self.boot = rail, gnd, dp, dm, en, boot
        self.gpio = dict(gpio or {})
        self.title = title or self.title
        self.u = self.part("U", ("RF_Module:ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1-N4",
                                 "RF_Module:ESP32-S3-WROOM-1", "C2913197", "Espressif",
                                 "ESP32-S3-WROOM-1-N4"))
        self.c_bulk = self.part("C", passive("C", "10uF"))
        self.c_hf = self.part("C", passive("C", "100nF"))
        self.r_en = self.part("R", passive("R", "10k"))
        self.c_en = self.part("C", passive("C", "1uF"))
        self.sw_rst = self.part("SW", ("Switch:SW_Push", "RESET",
                                       "Button_Switch_SMD:SW_SPST_TL3342", "", "", ""))
        self.sw_boot = self.part("SW", ("Switch:SW_Push", "BOOT",
                                        "Button_Switch_SMD:SW_SPST_TL3342", "", "", ""))
        u = self.u
        self.conn(rail, (u, 2), (self.c_bulk, 1), (self.c_hf, 1), (self.r_en, 1))
        self.conn(gnd, (u, 1), (u, 40), (u, 41), (self.c_bulk, 2), (self.c_hf, 2),
                  (self.c_en, 2), (self.sw_rst, 1), (self.sw_boot, 1))
        self.conn(en, (u, 3), (self.r_en, 2), (self.c_en, 1), (self.sw_rst, 2))
        self.conn(boot, (u, 27), (self.sw_boot, 2))
        self.conn(dm, (u, 13))
        self.conn(dp, (u, 14))
        self._pin_by_name = None
        self._lib = lib
        self.notes = ["EN: 10k / 1 uF delays reset release until 3V3 is stable.",
                      "IO0 low at reset = download mode (internal pull-up).",
                      "Keep copper out from under the antenna end of the module."]
        self._resolve(lib)

    FIXED = {"GND", "3V3", "EN", "IO0", "USB_D-", "USB_D+"}
    PINS = {  # name -> number for RF_Module:ESP32-S3-WROOM-1 (stock KiCad 10 symbol)
        "IO4": 4, "IO5": 5, "IO6": 6, "IO7": 7, "IO15": 8, "IO16": 9, "IO17": 10, "IO18": 11,
        "IO8": 12, "IO3": 15, "IO46": 16, "IO9": 17, "IO10": 18, "IO11": 19, "IO12": 20,
        "IO13": 21, "IO14": 22, "IO21": 23, "IO47": 24, "IO48": 25, "IO45": 26, "IO35": 28,
        "IO36": 29, "IO37": 30, "IO38": 31, "IO39": 32, "IO40": 33, "IO41": 34, "IO42": 35,
        "RXD0": 36, "TXD0": 37, "IO2": 38, "IO1": 39}

    def _resolve(self, lib):
        for name, net in self.gpio.items():
            if name not in self.PINS:
                raise ValueError(f"ESP32-S3-WROOM-1 has no free pin {name!r}; "
                                 f"choose from {sorted(self.PINS)}")
            self.conn(net, (self.u, self.PINS[name]))
        for name, num in self.PINS.items():
            if name not in self.gpio:
                self.nc(self.u, num)

    def _draw(self, s, x, y):
        u = self.u
        self.put(s, u, x, y)
        s.stub(u, 1, 2.54, power=self.gnd)
        s.stub(u, 2, 2.54, power=self.rail)
        # USB and named GPIOs on labels; the rest flagged unused
        s.stub(u, 13, 5.08, label=self.dm)
        s.stub(u, 14, 5.08, label=self.dp)
        for name, net in self.gpio.items():
            s.stub(u, self.PINS[name], 5.08, label=net)
        for name, num in self.PINS.items():
            if name not in self.gpio:
                s.nc(u, num)
        # EN: pull-up above the node, RC capacitor below the next node along,
        # RESET button at the far end to GND
        en = s.pin(u, 3)
        n1 = (en[0] - 7.62, en[1])
        n2 = (en[0] - 15.24, en[1])
        s.wire(en, n1, n2)
        self.put_pin(s, self.r_en, 2, n1)
        s.stub(self.r_en, 1, 2.54, power=self.rail)
        self.put_pin(s, self.c_en, 1, n2)
        s.stub(self.c_en, 2, 2.54, power=self.gnd)
        self.put_pin(s, self.sw_rst, 2, (n2[0] - 5.08, n2[1]))
        s.wire(n2, s.pin(self.sw_rst, 2))
        s.stub(self.sw_rst, 1, 2.54, power=self.gnd)
        s.label(self.en, *n1)
        # IO0: labelled at the module; BOOT button drawn below the EN network
        s.stub(u, 27, 2.54, label=self.boot)
        bx, by = n2[0] - 5.08, en[1] + 22.86
        self.put_pin(s, self.sw_boot, 2, (bx, by))
        s.stub(self.sw_boot, 2, 5.08, label=self.boot)
        s.stub(self.sw_boot, 1, 2.54, power=self.gnd)
        # decoupling: a bank above-right of the module, one rail and one GND
        top = s.pin(u, 2)
        s.rail_bank([self.c_bulk, self.c_hf], top[0] + 22.86, top[1] - 7.62, self.rail,
                    place=lambda r, px, py: self.put(s, r, px, py), gnd=self.gnd,
                    note="10 uF + 100 nF at pin 2")


# ===========================================================================
# Measurement
# ===========================================================================

class Ina226LoopSense(Block):
    """4-20 mA loop input: terminal, TVS, low-side shunt and INA226.

    The loop supply feeds terminal 1 out to the transmitter, which returns on
    terminal 2 through the shunt to GND. The INA226 reads the shunt through a
    10 R / 100 nF / 10 R differential filter, Kelvin-connected at the shunt,
    and its VBUS pin watches the loop supply. With 3.32 R the +/-81.92 mV
    full scale reaches 24.7 mA, so a 23.5 mA over-range sensor still reads.
    Address pins to GND: 0x40."""

    title = "4-20 mA LOOP SENSE"

    def __init__(self, refs, supply="+24V", rail="+3V3", sda="I2C_SDA", scl="I2C_SCL",
                 gnd="GND", shunt="3.32", rtn="LOOP_RTN", inp="INA_INP", inn="INA_INN",
                 alert=None, title=None):
        super().__init__(refs)
        self.supply, self.rail, self.sda, self.scl, self.gnd = supply, rail, sda, scl, gnd
        self.rtn, self.inp, self.inn, self.alert = rtn, inp, inn, alert
        self.title = title or self.title
        self.u = self.part("U", ("Sensor_Energy:INA226", "INA226",
                                 "Package_SO:MSOP-10_3x3mm_P0.5mm", "C49851",
                                 "Texas Instruments", "INA226AIDGSR"))
        self.j = self.part("J", ("Connector:Screw_Terminal_01x02", "4-20mA Loop",
                                 "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
                                 "", "", ""))
        self.tvs = self.part("D", ("Diode:SMAJ36A", "SMAJ36A", "Diode_SMD:D_SMA", "C113967",
                                   "Jiangsu Changjing", "SMAJ36A"))
        self.rs = self.part("R", passive("R", shunt))
        self.rp = self.part("R", passive("R", "10"))
        self.rn = self.part("R", passive("R", "10"))
        self.cf = self.part("C", passive("C", "100nF"))
        self.cd = self.part("C", passive("C", "100nF"))
        u = self.u
        self.conn(supply, (self.j, 1), (self.tvs, 1), (u, 8))
        self.conn(rtn, (self.j, 2), (self.tvs, 2), (self.rs, 1), (self.rp, 1))
        self.conn(inp, (self.rp, 2), (self.cf, 1), (u, 10))
        self.conn(inn, (self.rn, 2), (self.cf, 2), (u, 9))
        self.conn(gnd, (self.rs, 2), (self.rn, 1), (u, 7), (u, 1), (u, 2), (self.cd, 2))
        self.conn(rail, (u, 6), (self.cd, 1))
        self.conn(sda, (u, 4))
        self.conn(scl, (u, 5))
        if alert:
            self.conn(alert, (u, 3))
        else:
            self.nc(u, 3)
        self.notes = [f"Shunt {shunt} R: full scale 81.92 mV / {shunt} R.",
                      "Take the filter resistors from the shunt pads (Kelvin).",
                      "A0 = A1 = GND: I2C address 0x40."]

    def _draw(self, s, x, y):
        u = self.u
        self.put(s, u, x, y)
        s.stub(u, 6, 2.54, power=self.rail)
        s.stub(u, 7, 2.54, power=self.gnd)
        s.stub(u, 4, 5.08, label=self.sda)
        s.stub(u, 5, 5.08, label=self.scl)
        s.stub(u, 1, 2.54, power=self.gnd)
        s.stub(u, 2, 5.08, power=self.gnd)
        if self.alert:
            s.stub(u, 3, 5.08, label=self.alert)
        else:
            s.nc(u, 3)
        s.stub(u, 8, 2.54, power=self.supply)
        vp, vn = s.pin(u, 10), s.pin(u, 9)
        # differential filter: Cf across INP/INN, 10 R from each shunt end
        p = (vp[0] - 12.7, vp[1])
        s.wire(vp, p)
        self.put_pin(s, self.cf, 1, p)
        m = s.pin(self.cf, 2)
        s.wire(vn, (vn[0] - 5.08, vn[1]), (vn[0] - 5.08, m[1]), m)
        self.put_pin(s, self.rp, 2, p, rot=90)
        self.put_pin(s, self.rn, 2, m, rot=90)
        a, b = s.pin(self.rp, 1), s.pin(self.rn, 1)
        xs = a[0] - 5.08
        self.put_pin(s, self.rs, 1, (xs, a[1]))
        s.wire(a, (xs, a[1]))
        s.wire(b, s.pin(self.rs, 2))
        s.wire(s.pin(self.rs, 2), (xs, s.pin(self.rs, 2)[1] + 2.54))
        s.power(self.gnd, xs, s.pin(self.rs, 2)[1] + 2.54)
        # the loop: terminal 2 returns through the shunt; TVS across the terminal
        xt = xs - 7.62
        self.put_pin(s, self.tvs, 2, (xt, a[1]), rot=270)
        s.wire((xt, a[1]), (xs, a[1]))
        self.put_pin(s, self.j, 2, (xt - 7.62, a[1]), mirror="y")
        s.wire(s.pin(self.j, 2), (xt, a[1]))
        t1 = s.pin(self.tvs, 1)
        j1 = s.pin(self.j, 1)
        s.wire(j1, (j1[0] + 2.54, j1[1]), (j1[0] + 2.54, t1[1]), t1)
        self.drop(s, self.supply, t1, 2.54)
        s.rail_bank([self.cd], x + 15.24, y - 17.78, self.rail,
                    place=lambda r, px, py: self.put(s, r, px, py), gnd=self.gnd)
