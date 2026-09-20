"""Placement brief for the USB-C to M.2 Key-E HaLow adapter, read by autoplace.py.
Coordinates are board-relative mm, origin top-left, Y down.

Decisions this encodes:
  - 56.5 x 40.5 mm, single-sided assembly (everything on top), JLCPCB 2-layer.
    The socket sits 2.5 mm below centre: the buck runs along the top edge and
    its 3 mm inductor needs real clearance to the socket housing (2.9 mm now,
    0.4 mm when the board was 38 mm). check_m2_clearance.py measures it.
    Length: the buck row sits above the socket, so the socket only has to
    clear the USB-C, its ESD array and the pair's 8.5 mm jog down to pins
    3/5. That put the socket 7.5 mm further left than the first layout.
  - USB-C (J1) on the left edge, shell front 0.5 mm proud of the board edge.
  - M.2 socket (J2) mid-board, card running right (+X) to the standoff H5.
    MLD-NGFF-E-4.2H drawing: the card stop is 1.75 mm behind the locating
    pegs, which puts the 2230 mounting notch 29.75 mm from the socket centre
    on the peg / even-pin side. Card underside is 2.52 mm up: H5 is 2.5 mm.
  - Under the card the drawing allows 0.90 mm parts only (0402 passives).
  - M3 holes H1-H4 at the corners, clear of the card so screw heads fit.
  - The buck lives between the USB-C and the socket, away from the card's
    MMCX end.
  - The card is 22 mm wide, centred on the socket: y = 10.5..32.5.

To pin a part after reviewing a floor plan: lock it in KiCad or add it to FIXED.
"""
NAME = "USBC_M2E_HaLow_Adapter"
BOARD_SIZE = (56.5, 40.5)
ORIGIN = (100.0, 50.0)
CORNER_RADIUS = 2.0

HOLES = []                  # the holes are schematic parts H1-H5, fixed below
HOLE_KEEPOUT = 6.0

SOCKET_X, MID_Y = 22.0, 21.5

# ref: (x, y, rotation, side)
FIXED = {
    "J1": (3.15, MID_Y, 270, "top"),
    "J2": (SOCKET_X, MID_Y, 90, "top"),
    "H5": (SOCKET_X + 29.75, MID_Y, 0, "top"),
    "H1": (4.0, 4.0, 0, "top"),
    "H2": (52.5, 4.0, 0, "top"),
    "H3": (4.0, 36.5, 0, "top"),
    "H4": (52.5, 36.5, 0, "top"),
    # 100 nF at each pair of 3.3 V pins (2/4 at MID_Y + 9, 72/74 at MID_Y - 9), on the
    # card side of the even-pin row. 0402 is 0.55 mm tall: legal under the card.
    "C8": (SOCKET_X + 5.8, MID_Y + 8.75, 0, "top"),
    "C9": (SOCKET_X + 5.8, MID_Y - 8.75, 0, "top"),
    # bulk and 10 uF just past the socket's pin 2/4 end, outside the card (too tall to go under it);
    # pinned because route_board.py runs +3V3 straight through their pads
    "C6": (SOCKET_X + 6.07, MID_Y + 13.4, 270, "top"),
    "C7": (SOCKET_X + 3.77, MID_Y + 12.9, 270, "top"),

    # --- 3.3 V buck, hand-placed after TI TPS563201 datasheet sec 10 (Layout
    # Guidelines + Figure 36), unrolled along the top edge because the 7 mm
    # inductor cannot stand perpendicular in a 7.7 mm strip.
    #   U2 at 180: GND/SW/VIN pins face right, VIN on top, GND below.
    #   C2 then C1 directly beside those pins, VIN end up, GND end down: the
    #   input loop closes on the top layer without passing under the IC.
    #   A 0.9 mm gap between U2 and C2 holds the SW vias (Figure 36 does the
    #   same); SW runs on the bottom layer to L1's left pad.
    #   L1 next, SW pad toward the IC, VOUT pad toward the socket.
    #   C4/C5 at the VOUT pad, beside the socket's 3.3 V pins 72/74.
    #   R3/R4/C3 on the quiet left side: FB node 1.4 mm from pin 4, 12 mm
    #   from the SW pad, VOUT sensed by its own trace (guidelines 6-9).
    "U2": (12.9, 4.3, 180, "top"),
    "C2": (16.0, 4.3, 270, "top"),
    "C1": (18.2, 4.3, 270, "top"),
    "L1": (24.15, 4.3, 0, "top"),
    "C4": (30.6, 3.2, 0, "top"),
    "C5": (30.6, 5.4, 0, "top"),
    "R3": (9.8, 2.15, 0, "top"),
    "R4": (9.8, 3.35, 180, "top"),
    "C3": (9.8, 5.45, 0, "top"),

    # --- USB-C side, pinned for route_board.py. U1 at 180 is flow-through: D+
    # crosses the package on the top row (pins 4 -> 3), D- on the bottom row
    # (6 -> 1), matching the order the pair leaves J1 (ST USBLC6-2 layout).
    "U1": (13.3, MID_Y, 180, "top"),
    "D1": (10.8, MID_Y - 3.9, 0, "top"),
    "R1": (10.3, MID_Y - 1.65, 0, "top"),
    "R2": (10.3, MID_Y + 2.15, 0, "top"),

    # --- fiducials (asymmetric triangle) and probe pads, all clear of the card outline
    "FID1": (10.0, 37.5, 0, "top"),
    "FID2": (46.0, 4.0, 0, "top"),
    "FID3": (46.0, 36.2, 0, "top"),
    "TP1": (33.0, 35.0, 0, "top"),      # +3V3
    "TP2": (12.0, 14.6, 0, "top"),      # VBUS
    "TP3": (15.5, 14.6, 0, "top"),      # GND
}

DEFAULT_SIDES = ["top"]

KEEPOUTS = [
    dict(name="M.2 card underside", side="top", rect=(SOCKET_X + 4.7, MID_Y - 11.0, SOCKET_X + 26.5, MID_Y + 11.0),
         allow="by_height", max_height=0.90),
]

REF_SIZE, REF_THICKNESS = 1.0, 0.15   # JLCPCB legend minimum: 1.0 mm tall, 0.15 mm line
# no room for a legal 1.0 mm legend between these 0402s and H1/U2; they stay on F.Fab
REF_HIDE = ("C2", "C3", "R3", "R4")
# Designators moved by hand in KiCad are detected and kept (ledger: .pcbgen-refs.json).
# Name one here, or use "all", to hand it back to the tidy step for one run.
REF_RETIDY = ()
PART_GAP = 0.25
BLOCK_GAP = 0.8
EDGE_MARGIN = 0.8

# ---------------------------------------------------------------------------
# Project recipes. Every +3V3 capacitor is on one net, so the built-in claim
# order (socket first) hands the buck's 22 uF output pair to the socket. Here
# the socket claims last, and the buck keeps both of its input capacitors.
# ---------------------------------------------------------------------------
import copy                                                     # noqa: E402
from recipes import BUCK_SOT23, M2_KEY_E                        # noqa: E402

_buck = copy.copy(BUCK_SOT23)
_buck.roles = [copy.copy(r) for r in BUCK_SOT23.roles]
for _r in _buck.roles:
    if _r.name == "c_in":
        _r.count = 2            # C1 bulk + C2 100 nF, both at VIN/GND
_m2 = copy.copy(M2_KEY_E)
_m2.roles = [copy.copy(r) for r in M2_KEY_E.roles]
for _r in _m2.roles:
    _r.prio = 6                 # after the buck's c_out (prio 4)
    _r.max_mm = 6.0             # C6 (1206) and C7 (0603) are too tall to go under the card
EXTRA_RECIPES = [_buck, _m2]
