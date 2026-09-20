"""Placement recipes: which parts belong around an IC, and how close.

A recipe matches a part by *function* -- the pin names on its symbol, or its
value -- never by reference designator, so the same recipe works on any board
that uses that kind of part. Each role says which support part it wants (found
from the netlist, not from the schematic drawing), how close that part must sit
to the pin it serves, and in what order the parts are placed. The limits come
from datasheet layout sections and from measuring shipped reference boards;
every recipe names its sources, and references/recipes.md has the detail.

Role fields
  kind      part class: "C", "R", "L", "D", "Y" (crystal), "J", "Q", or "*"
  a, b      the two nets the part must touch, written as anchor pin-name
            regexes. "GND" means the ground net, "*" means any net, and
            "role:<name>" means the net of the other pad of an earlier role
            (e.g. the inductor's output side).
  near      which end is measured: "a" (default) or "b"
  max_mm    the limit, pad edge to pad edge... measured centre to centre of the
            nearest two pads, which is what the reference measurements used
  prio      placement order inside the block; lower goes first and closest
  count     how many parts this role may take (int, or 0 for "all that match")
  prefer    tie-break among candidates: "small" / "large" capacitance, or None
  optional  a missing part is not reported as a failure
"""
import re

GROUND_RE = re.compile(r"(^|/)(A|D|P|S)?GND[A-Z0-9_]*$|(^|/)VSS[A-Z0-9_]*$|(^|/)GNDD$", re.I)


class Role:
    def __init__(self, name, kind, a, b="*", max_mm=3.0, prio=5, count=1,
                 prefer=None, near="a", optional=True):
        self.name, self.kind, self.a, self.b = name, kind, a, b
        self.max_mm, self.prio, self.count = max_mm, prio, count
        self.prefer, self.near, self.optional = prefer, near, optional


class Recipe:
    def __init__(self, name, roles, pins_all=(), pins_any=(), value=None,
                 kind=None, min_pins=0, sources=(), tags=(), note=""):
        self.name, self.roles = name, sorted(roles, key=lambda r: r.prio)
        self.pins_all = [re.compile(p, re.I) for p in pins_all]
        self.pins_any = [re.compile(p, re.I) for p in pins_any]
        self.value = re.compile(value, re.I) if value else None
        self.kind, self.min_pins = kind, min_pins
        self.sources, self.tags, self.note = sources, set(tags), note

    def matches(self, part):
        if self.kind and part.kind != self.kind:
            return False
        if len(part.pads) < self.min_pins:
            return False
        names = set(part.pin_names.values())
        if self.value and self.value.search(part.value or ""):
            return True
        if not self.pins_all and not self.pins_any:
            return False
        if any(not any(p.search(n) for n in names) for p in self.pins_all):
            return False
        if self.pins_any and not any(p.search(n) for p in self.pins_any for n in names):
            return False
        return True


# ---------------------------------------------------------------------------
# Switching regulators
# ---------------------------------------------------------------------------
BUCK_SOT23 = Recipe(
    "buck: synchronous SOT-23-6 (TI D-CAP/D-CAP2 class)",
    pins_all=(r"^SW$", r"^(VIN|IN|PVIN)$", r"^(VBST|BST|BOOT|CB)$", r"^(VFB|FB)$"),
    roles=[
        Role("c_in", "C", r"^(VIN|IN|PVIN)$", "GND", max_mm=2.0, prio=1, prefer="large", optional=False),
        Role("c_boot", "C", r"^(VBST|BST|BOOT|CB)$", r"^SW$", max_mm=1.5, prio=2, optional=False),
        Role("inductor", "L", r"^SW$", "*", max_mm=4.0, prio=3, optional=False),
        Role("c_out", "C", "role:inductor", "GND", max_mm=3.0, prio=4, count=2, optional=False),
        Role("r_fb_top", "R", r"^(VFB|FB)$", "role:inductor", max_mm=2.5, prio=5),
        Role("r_fb_bot", "R", r"^(VFB|FB)$", "GND", max_mm=2.5, prio=5),
        Role("r_en", "R", r"^EN$", "*", max_mm=3.0, prio=6),
    ],
    tags=("switching",),
    sources=("TI TPS563201 SLVSD90B sec 7.4 Layout Guidelines",
             "TI TPS565201 SLVSE71 sec 10", "TI SLVA958 buck layout quick reference",
             "measured: Antmicro cm4-baseboard AP62301 (Cin 1.7, boot 1.3, FB 1.2-2.2, L 3.7 mm)",
             "measured: RPi CM4 IO board AP64501 (Cin 2.1, L 3.2, FB 1.6-2.4 mm)"),
    note="Shrink the VIN-SW-inductor-Cout loop first. FB divider on the side away from SW, "
         "grounded to the IC GND pin. No switching current under the IC.",
)

CHARGER_BUCK_BOOST = Recipe(
    "charger: NVDC buck-boost (TI BQ2579x / BQ2567x class)",
    pins_all=(r"^SW1$", r"^SW2$", r"^PMID$", r"^SYS$", r"^BTST1$", r"^BTST2$", r"^REGN$"),
    roles=[
        Role("c_sys_hf", "C", r"^SYS$", "GND", max_mm=1.8, prio=1, prefer="small", optional=False),
        Role("c_pmid_hf", "C", r"^PMID$", "GND", max_mm=1.8, prio=1, prefer="small", optional=False),
        Role("c_sys_bulk", "C", r"^SYS$", "GND", max_mm=5.0, prio=2, count=3, prefer="large"),
        Role("c_pmid_bulk", "C", r"^PMID$", "GND", max_mm=5.0, prio=2, count=3, prefer="large"),
        Role("c_btst1", "C", r"^BTST1$", r"^SW1$", max_mm=1.5, prio=3, optional=False),
        Role("c_btst2", "C", r"^BTST2$", r"^SW2$", max_mm=1.5, prio=3, optional=False),
        Role("c_regn", "C", r"^REGN$", "GND", max_mm=3.2, prio=3, optional=False),
        Role("c_vbus", "C", r"^VBUS$", "GND", max_mm=4.0, prio=4, count=2),
        Role("c_bat", "C", r"^BAT$", "GND", max_mm=5.0, prio=4, count=2),
        Role("inductor", "L", r"^SW1$", r"^SW2$", max_mm=5.5, prio=5, optional=False),
        Role("r_set", "R", r"^(PROG|ILIM_HIZ|ILIM|TS)$", "*", max_mm=4.5, prio=6, count=0),
        Role("c_set", "C", r"^(TS|BATP|SDRV)$", "*", max_mm=4.5, prio=6, count=0),
    ],
    tags=("switching",),
    sources=("TI BQ25792 datasheet sec 12.1 Layout Guidelines (priority list)",
             "TI BQ25792EVM user guide SLUUCB5E sec 3",
             "measured: flisboac/kicad-usb-pd-power-manager BQ25672 (boot 1.3, PMID/SYS 100nF 1.8, "
             "REGN 3.2, L 5.4, bulk 4.6-6.3 mm)"),
    note="Place in TI's order: SYS and PMID 100 nF on the IC's layer with no vias, then their bulk, "
         "then REGN/boot caps, then VBUS/BAT caps, then the inductor. Setting resistors away from "
         "the switching ground return.",
)

BOOST_SOT23 = Recipe(
    "boost: SOT-23 asynchronous (MT3608 / TPS61040 class)",
    pins_all=(r"^SW$", r"^(VIN|IN)$", r"^(FB|VFB)$"),
    roles=[
        Role("c_in", "C", r"^(VIN|IN)$", "GND", max_mm=2.0, prio=1, prefer="large", optional=False),
        Role("inductor", "L", r"^SW$", "*", max_mm=4.0, prio=2, optional=False),
        Role("diode", "D", r"^SW$", "*", max_mm=3.5, prio=3),
        Role("c_out", "C", "role:diode", "GND", max_mm=3.0, prio=4, count=2),
        Role("r_fb", "R", r"^(FB|VFB)$", "*", max_mm=2.5, prio=5, count=2),
        Role("r_en", "R", r"^EN$", "*", max_mm=3.0, prio=6),
    ],
    tags=("switching",),
    sources=("Aerosemi MT3608 datasheet layout notes: input cap at the pin, small SW loop, FB tap close",
             "measured: a hand-laid MT3608 board (switch node 5.45 mm)"),
)

LDO = Recipe(
    "LDO regulator (AP2112 / AMS1117 / TLV75 class)",
    pins_all=(r"^(VIN|IN)$", r"^(VOUT|OUT)$"), pins_any=(r"^(EN|CE|NC|ADJ|GND)$",),
    roles=[
        Role("c_in", "C", r"^(VIN|IN)$", "GND", max_mm=2.0, prio=1, optional=False),
        Role("c_out", "C", r"^(VOUT|OUT)$", "GND", max_mm=2.5, prio=1, count=2, optional=False),
        Role("r_adj", "R", r"^(ADJ|FB)$", "*", max_mm=2.5, prio=3, count=2),
    ],
    sources=("Diodes AP2112 datasheet: 1 uF input and output caps placed close to the IC",),
)

CURRENT_SENSE = Recipe(
    "current/power monitor (INA226 / INA219 class)",
    pins_all=(r"^IN\+$|^INP$|^VIN\+$", r"^IN-$|^INN$|^VIN-$"),
    roles=[
        Role("c_vs", "C", r"^(VS|VDD|VCC)$", "GND", max_mm=2.0, prio=1),
        Role("r_shunt_tap", "R", r"^(IN\+|INP|VIN\+|IN-|INN|VIN-)$", "*", max_mm=4.0, prio=2, count=2),
        Role("c_filter", "C", r"^(IN\+|INP|VIN\+)$", r"^(IN-|INN|VIN-)$", max_mm=3.0, prio=3),
        Role("r_addr", "R", r"^(A0|A1|ALERT)$", "*", max_mm=4.0, prio=5, count=0),
    ],
    sources=("TI INA226 datasheet layout: Kelvin taps at the shunt, filter symmetric, bypass at VS",),
    note="Taps come off the two ends of the shunt itself; the differential filter sits between the "
         "shunt and the IC.",
)

LOAD_SWITCH = Recipe(
    "load switch: current-limited (AP2151 / TPS2051 class)",
    pins_all=(r"^IN$", r"^OUT$", r"^(FLG|FAULT|~?\{?FLT\}?|OC)", r"^EN"),
    roles=[
        Role("c_in", "C", r"^IN$", "GND", max_mm=2.0, prio=1),
        Role("c_out", "C", r"^OUT$", "GND", max_mm=3.0, prio=2, count=2),
        Role("r_flag", "R", r"^(FLG|FAULT|OC)", "*", max_mm=3.0, prio=3),
        Role("r_en", "R", r"^EN", "*", max_mm=3.0, prio=3),
    ],
    sources=("Diodes AP2141/AP2151 datasheet, application information",),
)

# ---------------------------------------------------------------------------
# USB
# ---------------------------------------------------------------------------
PD_SINK = Recipe(
    "USB-PD sink controller (Hynetek HUSB238 class)",
    pins_all=(r"^CC1$", r"^CC2$"), pins_any=(r"^VSET$", r"^ISET$", r"^GATE$"),
    roles=[
        Role("c_vin", "C", r"^(VIN|VBUS|VDD)$", "GND", max_mm=2.0, prio=1, count=2, optional=False),
        Role("r_vset", "R", r"^VSET$", "*", max_mm=3.5, prio=2),
        Role("r_iset", "R", r"^ISET$", "*", max_mm=3.5, prio=2),
        Role("r_gate", "R", r"^GATE$", "*", max_mm=4.0, prio=3),
    ],
    sources=("Hynetek HUSB238 datasheet VIN pin section",
             "measured: flisboac/kicad-usb-pd-power-manager (VIN caps 1.25-2.2, VSET/ISET 1.3-3.4 mm)"),
)

USB_HUB = Recipe(
    "USB 2.0 hub controller (FE1.1s / USB2514 / CH334 class)",
    pins_all=(r"^(XIN|XI|XTAL_?IN)$", r"^(XOUT|XO|XTAL_?OUT)$"),
    pins_any=(r"^(DPU|DP0|USBDP_UP|D\+_?UP)$", r"^DP[1-4]$", r"^USB(D[PM])?_?DN?[1-4]"),
    roles=[
        Role("c_supply_hf", "C", r"^(VDD5|VD33|VD33_O|VD18|VD18_O|VDD33|VDDA33|VDD|VBUSM|VDD18|CRFILT|PLLFILT)$",
             "GND", max_mm=2.0, prio=1, count=0, prefer="small"),
        Role("crystal", "Y", r"^(XIN|XI|XTAL_?IN)$", r"^(XOUT|XO|XTAL_?OUT)$", max_mm=4.0, prio=2),
        Role("c_xtal", "C", r"^(XIN|XI|XOUT|XO|XTAL_?IN|XTAL_?OUT)$", "GND", max_mm=4.0, prio=3, count=2),
        Role("r_rext", "R", r"^(REXT|RBIAS)$", "*", max_mm=3.0, prio=3),
        Role("r_strap", "R", r"^(XRSTJ|RESET|~?\{?RST|BUSJ|OVCJ|PWRJ|DRV|LED[12]|TESTJ|CFG|SUSP)", "*",
             max_mm=5.0, prio=5, count=0),
    ],
    sources=("measured: official RPi CM4 IO board USB2514 (100 nF 1.4-1.8, straps 2.0-2.3, crystal caps 3.8-4.0 mm)",
             "FE1.1s datasheet pin table: 10 uF on VD18_O and VD33_O"),
)

USB_AUDIO = Recipe(
    "USB audio codec (C-Media CM108/CM119 class)",
    pins_all=(r"^MICIN$",), pins_any=(r"^LOL$", r"^LOR$", r"^DREG18$"),
    roles=[
        Role("c_reg", "C", r"^(DREG18|DREG33|AREG36|VREF|LOBS|VBIAS)$", "GND", max_mm=3.0, prio=1, count=0),
        Role("c_supply", "C", r"^(AVDD|DVDD|PVDD|VDD)$", "GND", max_mm=3.0, prio=2, count=0),
        Role("r_usb", "R", r"^(USBD[PM]|D[PM])$", "*", max_mm=3.5, prio=3, count=2),
        Role("c_audio", "C", r"^(MICIN|LOL|LOR)$", "*", max_mm=4.0, prio=4, count=0),
    ],
    sources=("measured: softcomplex/Digirig-Lite CM108 (all caps 1.5-3.7 mm)",),
)

USB_ESD = Recipe(
    "USB ESD array (USBLC6-2 class)",
    pins_all=(r"^I/?O1$", r"^I/?O2$", r"^(VBUS|VCC|VDD)$"),
    roles=[Role("c_rail", "C", r"^(VBUS|VCC|VDD)$", "GND", max_mm=2.0, prio=1)],
    tags=("near_connector",),
    sources=("ST USBLC6-2 datasheet sec 2.3: at the connector, in the data path, shortest GND path",),
    note="The array belongs beside the connector it protects, in line on D+/D-. The global placer "
         "pulls it there through the heavy weight on USB data nets.",
)

# ---------------------------------------------------------------------------
# Radios, modules, connectors
# ---------------------------------------------------------------------------
GNSS_MODULE = Recipe(
    "GNSS module (u-blox MAX-M8/M10 class)",
    pins_all=(r"^RF_IN$", r"^V_BCKP$"),
    roles=[
        Role("rf_connector", "J", r"^RF_IN$", "*", max_mm=6.0, prio=1),
        Role("c_vcc", "C", r"^(VCC|V_IO|V_BCKP)$", "GND", max_mm=3.0, prio=2, count=0),
    ],
    tags=("rf", "avoid_switching"),
    sources=("u-blox MAX-M10S integration manual UBX-20053088 sec 4.4 Layout",),
    note="Short 50-ohm RF line with ground vias; nothing crossing under the module on L1/L2; "
         "at least 5 mm from other RF parts; far from switching regulators and heat.",
)

M2_KEY_E = Recipe(
    "M.2 Key E socket",
    value=r"M\.?2|NGFF", kind="J", min_pins=60,
    roles=[
        # prio 6, after every regulator role (c_out is 4): all the capacitors on a
        # shared 3.3 V rail are one net, and at prio 1 the socket took the buck's
        # output pair and left it the 100 nF ones (USB-C/M.2 adapter, 2026-09-20)
        Role("c_3v3", "C", r"^(2|4|72|74)$", "GND", max_mm=4.0, prio=6, count=0),
    ],
    tags=("m2_card",),
    sources=("Embedded Artists M.2 carrier board design guide: 2x100 nF + 2x22 uF on 3.3 V at the connector",
             "MLD-NGFF-E-4.2H drawing, stack-height diagram: card centre 2.92 mm up, card 0.8 mm, "
             "so its underside is at 2.52 mm; the card's own bottom parts take 1.40 mm and 0.10 mm is air: "
             "0.90 mm MAX component height on the main board under the card"),
    note="Under the card only parts <= 0.90 mm tall (0402 yes; 0603 10 uF, 0805 and up no). The card leaves "
         "the socket on its even-pin / locating-peg side. Bulk and 0603+ go just past the socket's ends.",
)

ETH_MAGNETICS = Recipe(
    "Ethernet magnetics / transformer",
    pins_all=(r"^(TCT|MCT)[1-4]?$",), pins_any=(r"^TD[1-4]?[+-]$", r"^MX[1-4][+-]$"),
    roles=[
        Role("bob_smith", "*", r"^(TCT|MCT)[1-4]?$", "*", max_mm=6.0, prio=1, count=0),
    ],
    sources=("RPi CM4 IO board Ethernet section",),
)

PFET_SWITCH = Recipe(
    "P-FET / N-FET power switch with gate network",
    kind="Q", pins_all=(r"^S$", r"^G$", r"^D$"),
    roles=[
        Role("pair_fet", "Q", r"^S$", "*", max_mm=3.0, prio=1),
        Role("r_gate", "R", r"^G$", "*", max_mm=3.5, prio=2, count=2),
        Role("d_gate", "D", r"^G$", "*", max_mm=3.5, prio=2),
    ],
    sources=("AOS AO4407A datasheet (SOIC-8: 1-3 S, 4 G, 5-8 D)",),
    note="Back-to-back pairs share the source net; keep the pair adjacent so the common-source copper is short.",
)

# Fallbacks, tried last. They catch decoupling and bias parts around any IC or
# connector no specific recipe claimed.
GENERIC_IC = Recipe(
    "generic IC: decoupling and local passives",
    kind="U", min_pins=5, pins_any=(r".",),
    roles=[
        Role("c_decouple_hf", "C", r".", "GND", max_mm=2.5, prio=1, count=0, prefer="small"),
        Role("local_passive", "*", r".", "*", max_mm=4.0, prio=3, count=0),
    ],
    sources=("general practice: decoupling at the pin it serves, smallest value nearest",),
)

GENERIC_CONNECTOR = Recipe(
    "generic connector: filtering and protection at the pins",
    kind="J", pins_any=(r".",),
    roles=[Role("local_passive", "*", r".", "*", max_mm=6.0, prio=3, count=0)],
)

RECIPES = [CHARGER_BUCK_BOOST, BUCK_SOT23, BOOST_SOT23, PD_SINK, USB_HUB, USB_AUDIO, USB_ESD,
           CURRENT_SENSE, LOAD_SWITCH, LDO, GNSS_MODULE, M2_KEY_E, ETH_MAGNETICS, PFET_SWITCH]
FALLBACKS = [GENERIC_IC, GENERIC_CONNECTOR]


# ---------------------------------------------------------------------------
# Module outlines (keep-outs derived from connector positions)
# ---------------------------------------------------------------------------
# Raspberry Pi CM4/CM5: two Hirose DF40 100-pin connectors, pin rows 33.92 mm
# apart. With the 1.5 mm stacking connectors (DF40C-100DS-0.4V) there is 0 mm
# clearance under the module, so the whole 40 x 55 mm outline is a footprint
# keep-out on that side. Geometry from the official CM4 IO board KiCad
# footprint: holes 33 x 48 mm apart; outline centre sits 2.5 mm from the
# connector midpoint along the pin-row axis, toward pin 1 of each connector.
CM4_MODULE = dict(
    name="Raspberry Pi CM4",
    connector_value=r"DF40|CM4",
    connector_pins=100,
    row_spacing=33.92,
    across=40.0, along=55.0, centre_shift_along=2.5,
    sources=("RPi CM4 datasheet sec 4 mechanical: 1.5 mm stack = 0 mm clearance under the module",
             "RPi CM4 IO board KiCad footprint Raspberry-Pi-4-Compute-Module"),
)
