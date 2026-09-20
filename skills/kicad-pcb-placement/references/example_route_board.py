#!/usr/bin/env python3
"""Route the USB-C to M.2 Key-E HaLow adapter (2 layers) and pour ground.

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" route_board.py

Idempotent: every track, via and zone is removed and redrawn, so run it again
after any placement change. Coordinates are board mm (origin top-left, Y down)
and depend on the FIXED positions in placement_config.py -- move a part there
and its route here has to follow. The script checks that every pad it starts
or ends on is where it expects, and stops if not.

The plan, layer by layer:
  TOP     all signal and power routing; GND pour.
  BOTTOM  GND plane. Cut only by: the SW link U2 -> L1 and boot cap (TI Figure
          36 puts SW on the far layer), the VBUS feed up the left edge, and
          two 1.3 mm straps at the USB-C connector. Nothing crosses under D+/D-.

USB 2.0 pair:
  * J1's pads go B7(N) A6(P) A7(N) B6(P), so joining the flip pairs needs one
    crossing per net: a 1.3 mm bottom strap each (N at x = 8.6, P at x = 9.6),
    both outside the signal's main path.
  * U1 is flow-through: D+ crosses under it on the top row, D- on the bottom.
  * After U1 the pair is coupled at JLCPCB's geometry (0.3345 / 0.1524 mm,
    ground pour at 0.2032 mm from the .kicad_dru rule), goes through the M.2
    key gap between pins 23 and 33, runs down under the socket housing and
    turns back west into pins 3 (D+) and 5 (D-). Two right turns put D+ on the
    south side, which is where pin 3 is -- so no layer change and no swap.
  * It necks to 0.25 mm for the last 1.4 mm into the 0.3 mm socket pads.
"""
import math
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import placement_config as cfg      # noqa: E402
import setup_fab as fab             # noqa: E402

OX, OY = cfg.ORIGIN
W, H = cfg.BOARD_SIZE
MID = cfg.MID_Y
SX = cfg.SOCKET_X
F, B = pcbnew.F_Cu, pcbnew.B_Cu
mm = pcbnew.FromMM

board = pcbnew.LoadBoard(os.path.join(HERE, cfg.NAME + ".kicad_pcb"))
NETS = {n.GetNetname(): n for n in board.GetNetInfo().NetsByNetcode().values()}
FPS = {f.GetReference(): f for f in board.GetFootprints()}


def pt(x, y):
    return pcbnew.VECTOR2I(mm(OX + x), mm(OY + y))


def pad(ref, num, expect=None):
    """Board-mm centre of a pad; with `expect`, prove the placement is the one
    this script was written for."""
    for p in FPS[ref].Pads():
        if p.GetNumber() == str(num):
            q = p.GetPosition()
            xy = (round(pcbnew.ToMM(q.x) - OX, 3), round(pcbnew.ToMM(q.y) - OY, 3))
            if expect and (abs(xy[0] - expect[0]) > 0.02 or abs(xy[1] - expect[1]) > 0.02):
                raise SystemExit("%s.%s is at %s, route_board.py expects %s -- placement changed"
                                 % (ref, num, xy, expect))
            return xy
    raise KeyError((ref, num))


def track(net, pts, width, layer=F):
    for a, b in zip(pts, pts[1:]):
        if a == b:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pt(*a))
        t.SetEnd(pt(*b))
        t.SetWidth(mm(width))
        t.SetLayer(layer)
        t.SetNet(NETS[net])
        board.Add(t)


def via(net, xy, dia=0.6, drill=0.3):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pt(*xy))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetWidth(mm(dia))
    v.SetDrill(mm(drill))
    v.SetLayerPair(F, B)
    v.SetNet(NETS[net])
    board.Add(v)
    return xy


def offset(points, d):
    """Parallel polyline `d` mm to the LEFT of travel (Y is down, so left of
    east is north). Mitred joins, which is what keeps a pair's gap constant
    through 45 degree corners."""
    out = []
    n = len(points)
    for i, (x, y) in enumerate(points):
        dirs = []
        if i > 0:
            dirs.append((x - points[i - 1][0], y - points[i - 1][1]))
        if i < n - 1:
            dirs.append((points[i + 1][0] - x, points[i + 1][1] - y))
        norms = []
        for dx, dy in dirs:
            l = math.hypot(dx, dy)
            norms.append((dy / l, -dx / l))
        if len(norms) == 1:
            nx, ny = norms[0]
            out.append((x + nx * d, y + ny * d))
        else:
            (ax, ay), (bx, by) = norms
            sx, sy = ax + bx, ay + by
            k = d / (1 + ax * bx + ay * by)
            out.append((x + sx * k, y + sy * k))
    return [(round(px, 4), round(py, 4)) for px, py in out]


_REMOVED = []        # pcbnew: a removed item's proxy must never be garbage-collected


def clear():
    """Drop every track, via and copper zone. Zones are collected before anything
    is removed, and every removed proxy is kept alive: letting one die corrupts
    pcbnew's type table (GetArea() then returns bare SwigPyObjects)."""
    zones = [board.GetArea(i) for i in range(board.GetAreaCount())]
    doomed = list(board.GetTracks()) + [z for z in zones if not z.GetIsRuleArea()]
    for item in doomed:
        board.Remove(item)
        _REMOVED.append(item)


def zone(net, layer, name):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(NETS[net])
    z.SetZoneName(name)
    o = z.Outline()
    o.NewOutline()
    for x, y in ((0, 0), (W, 0), (W, H), (0, H)):
        o.Append(mm(OX + x), mm(OY + y))
    z.SetLocalClearance(mm(0.2))
    z.SetMinThickness(mm(0.25))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetThermalReliefGap(mm(0.25))
    z.SetThermalReliefSpokeWidth(mm(0.3))
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    z.SetAssignedPriority(0)
    board.Add(z)
    return z


P, N = "/USB_D_P", "/USB_D_N"
UW, UG = fab.USB_W, fab.USB_GAP
HALF = (UW + UG) / 2


def route_usb():
    y = MID                                     # J1's D+/D- pads straddle the board's mid line
    # --- connector fan-out: inner pair A6/A7 is the main path, B7/B6 join by bottom straps
    a6, a7 = pad("J1", "A6", (7.19, y - 0.25)), pad("J1", "A7", (7.19, y + 0.25))
    b7, b6 = pad("J1", "B7", (7.19, y - 0.75)), pad("J1", "B6", (7.19, y + 0.75))
    u4, u3 = pad("U1", 4, (12.162, y - 0.95)), pad("U1", 3, (14.438, y - 0.95))
    u6, u1 = pad("U1", 6, (12.162, y + 0.95)), pad("U1", 1, (14.438, y + 0.95))
    w = 0.25
    vn1 = via(N, (8.6, y - 0.90))
    track(N, [b7, (8.2, b7[1]), (8.35, y - 0.90), vn1], w)
    vn2 = via(N, (8.6, y + 0.45))
    track(N, [a7, (8.2, a7[1]), (8.4, y + 0.45), vn2, (10.1, y + 0.45), (10.6, u6[1]), u6, u1], w)
    track(N, [vn1, vn2], w, B)
    vp1 = via(P, (9.6, y - 0.35))
    track(P, [a6, (9.0, a6[1]), (9.1, y - 0.35), vp1, (10.0, y - 0.35), (10.6, u4[1]), u4, u3], w)
    vp2 = via(P, (9.6, y + 1.15))
    track(P, [b6, (8.0, b6[1]), (8.4, y + 1.15), vp2], w)
    track(P, [vp1, vp2], w, B)

    # --- coupled pair from U1 to J2 pins 3 / 5, D+ on the left of travel
    j3 = pad("J2", 3, (SX - 3.77, y + 8.75))
    j5 = pad("J2", 5, (SX - 3.77, y + 8.25))
    gap_y = y + 2.5                              # centre of the key gap (pins 24-31 absent)
    xs = 16.3                                    # southbound leg, west of the socket's odd pads
    xe = SX - 1.1                                # southbound leg under the housing
    yc = (j3[1] + j5[1]) / 2
    centre = [(xs, y + 1.25), (xs, gap_y - 0.5), (xs + 0.5, gap_y), (xe - 0.5, gap_y),
              (xe, gap_y + 0.5), (xe, yc - 0.5), (xe - 0.5, yc), (xe - 0.9, yc)]
    pp, nn = offset(centre, HALF), offset(centre, -HALF)
    # lead-ins from U1's east pads to the start of the coupled section
    track(P, [u3, (pp[0][0] - 0.543, u3[1]), (pp[0][0], u3[1] + 0.543), pp[0]], w)
    track(N, [u1, (nn[0][0] - 0.3, u1[1]), nn[0]], w)
    track(P, pp, UW)
    track(N, nn, UW)
    # neck down into the 0.3 mm pads, on the pads' own centre lines
    track(P, [pp[-1], (pp[-1][0] - 0.15, j3[1]), (j3[0] + 0.3, j3[1])], w)
    track(N, [nn[-1], (nn[-1][0] - 0.15, j5[1]), (j5[0] + 0.3, j5[1])], w)

    # --- U1 ground: a small via under the package. Pin 5 (VBUS) is open by design.
    g = via("GND", (13.3, y), 0.6, 0.3)      # 0.15 mm ring; 0.18 mm to U1's pads either side
    track("GND", [pad("U1", 2), g], 0.25)


def route_cc_vbus():
    y = MID
    a5, b5 = pad("J1", "A5", (7.19, y - 1.25)), pad("J1", "B5", (7.19, y + 1.75))
    r1, r2 = pad("R1", 1), pad("R2", 1)
    track("/CC1", [a5, (8.0, a5[1]), (8.4, r1[1]), r1], 0.2)
    track("/CC2", [b5, (8.0, b5[1]), (8.4, r2[1]), r2], 0.2)
    # J1's two GND signal pads are boxed in by their neighbours: tie each to the shell stake beside it
    for num, sy in (("A1", -1), ("A12", 1)):
        g = pad("J1", num)
        track("GND", [(g[0] - 0.3, g[1]), (g[0] - 0.75, g[1] + sy * 0.55)], 0.3)
    # VBUS: upper pads -> D1 -> vias -> bottom trunk up the left edge to the buck
    va, vb = pad("J1", "A4", (7.19, y - 2.45)), pad("J1", "A9", (7.19, y + 2.45))
    k = pad("D1", 1)
    track("VBUS", [va, (8.3, va[1]), (8.3 + (va[1] - k[1] - 0.35), k[1] + 0.35), (k[0], k[1] + 0.35), k], 0.5)
    vk = via("VBUS", (k[0], k[1] - 1.45), 0.8, 0.4)
    track("VBUS", [k, vk], 0.6)
    vk2 = via("VBUS", (k[0] + 0.95, vk[1]), 0.8, 0.4)          # second via: all of VBUS passes here
    track("VBUS", [(k[0], k[1] - 0.5), vk2], 0.6)
    track("VBUS", [vk, vk2], 0.8, B)
    tp2 = pad("TP2", 1)
    track("VBUS", [vk2, (vk2[0] + (vk2[1] - tp2[1]), tp2[1]), tp2], 0.4)
    # lower pads: via, then a bottom link round the BACK of the connector (under its
    # body, between the shell stakes) so that nothing on the bottom crosses under D+/D-
    vl = via("VBUS", (8.6, vb[1] + 0.55))
    track("VBUS", [vb, (7.95, vb[1]), (8.5, vb[1] + 0.55), vl], 0.3)
    yt = vk[1] - 1.75
    track("VBUS", [vl, (8.6, y + 5.1), (7.8, y + 5.9), (4.2, y + 5.9), (4.2, yt + 0.8), (5.0, yt), (8.2, yt)], 0.5, B)
    # trunk to the buck's VIN node
    vin = [(15.0, 2.2), (16.2, 2.2)]
    track("VBUS", [vk, (8.2, vk[1] - 1.2), (8.2, 2.2), vin[1]], 0.8, B)
    for v in vin:
        via("VBUS", v, 0.8, 0.4)
        track("VBUS", [v, (v[0], 3.45)], 0.6)
    return vk


def route_buck():
    u = {n: pad("U2", n) for n in range(1, 7)}
    pad("U2", 2, (14.038, 4.3))
    c1v, c2v = pad("C1", 1, (18.2, 2.825)), pad("C2", 1, (16.0, 3.82))
    # VIN node: U2.3 - C2.1 - C1.1 on the top layer, fed by the two VBUS vias
    track("VBUS", [u[3], (c2v[0], 3.45), (c1v[0], 3.45)], 0.5)
    # EN tied to VBUS: west between R4 and C3 to a via on the bottom trunk
    en = via("VBUS", (8.3, u[5][1]))
    track("VBUS", [u[5], en], 0.25)
    # SW: one via beside pin 2, one under the package; bottom link to L1 and to the boot cap
    s1, s2 = via("/SW", (15.15, 4.3)), via("/SW", (13.0, 4.3))
    track("/SW", [s2, u[2], s1], 0.5)
    l1 = pad("L1", 1, (21.3, 4.3))
    # C1 stands on end with 2.7 mm wide pads that reach x = 19.55, so nothing fits
    # between it and L1: one via above L1's pad and one below it instead
    lv = [via("/SW", (l1[0], yy), 0.8, 0.4) for yy in (1.75, 6.85)]
    for v in lv:
        track("/SW", [v, (l1[0], 4.3 + (-1.2 if v[1] < 4.3 else 1.2))], 0.8)
    track("/SW", [s2, (l1[0], 4.3)], 1.2, B)
    track("/SW", [lv[0], lv[1]], 1.0, B)
    c3s, c3b = pad("C3", 1), pad("C3", 2)
    bv = via("/SW", (c3s[0], 6.5))
    track("/SW", [c3s, bv], 0.3)
    track("/SW", [bv, (10.8, 6.5), s2], 0.3, B)
    track("/VBST", [u[6], (10.9, u[6][1]), (10.7, c3b[1]), c3b], 0.25)
    # feedback: divider node straight into pin 4; VOUT sensed on its own trace
    r3o, r3f, r4f = pad("R3", 1), pad("R3", 2), pad("R4", 1)
    track("/FB", [r3f, r4f, u[4]], 0.2)
    l2 = pad("L1", 2, (27.0, 4.3))
    track("+3V3", [r3o, (r3o[0], 0.7), (l2[0] + 0.1, 0.7), (l2[0] + 0.1, 2.9)], 0.2)
    # output: L1 -> C4/C5 -> down the east side of the socket to both 3.3 V pin pairs
    c4, c5 = pad("C4", 1), pad("C5", 1)
    track("+3V3", [l2, (c4[0], l2[1])], 1.5)
    xr = c4[0]
    c6, c7 = pad("C6", 1, (SX + 6.07, MID + 11.925)), pad("C7", 1, (SX + 3.77, MID + 12.125))
    track("+3V3", [c4, c5, (xr, c7[1])], 1.2)
    track("+3V3", [(xr, c7[1]), (c6[0], c7[1]), c7], 0.8)
    tp1 = pad("TP1", 1)
    track("+3V3", [(xr, c7[1]), (xr + (tp1[1] - c7[1]), tp1[1]), tp1], 0.5)
    xv = SX + 5.05                               # clear of the neighbouring socket pads' ends
    for pins, cap, yb in ((("74", "72"), "C9", -1), (("4", "2"), "C8", 1)):
        ys = [pad("J2", p)[1] for p in pins]
        cy = pad(cap, 1)[1]
        ybr = cy + yb * 1.2
        track("+3V3", [(xr, ybr), (xv, ybr), (xv, cy)], 0.6)
        for yy in ys:
            track("+3V3", [(xv, yy), (pad("J2", pins[0])[0], yy)], 0.25)
        track("+3V3", [(xv, cy), pad(cap, 1)], 0.4)


def ground_vias():
    """One via beside every ground pad that the top pour alone would leave on
    a long or thin path. (x, y) is the via; it is tracked to the named pad."""
    y = MID
    todo = [("C1", 2, (18.2, 8.0), 0.8, 0.4), ("C2", 2, (16.0, 6.0), 0.6, 0.3),
            ("U2", 1, (14.04, 6.4), 0.6, 0.3),
            ("C4", 2, (32.9, 3.2), 0.8, 0.4), ("C5", 2, (32.9, 5.4), 0.8, 0.4),
            ("C9", 2, (SX + 6.28, y - 7.7), 0.6, 0.3), ("C8", 2, (SX + 6.28, y + 7.7), 0.6, 0.3),
            ("C6", 2, (28.07, 38.1), 0.8, 0.4), ("C7", 2, (25.77, 36.4), 0.6, 0.3),
            ("D1", 2, (13.3, y - 3.9), 0.6, 0.3), ("R1", 2, (11.7, y - 2.4), 0.6, 0.3),
            ("R2", 2, (11.8, y + 2.15), 0.6, 0.3), ("R4", 2, None, 0, 0)]
    for ref, num, xy, dia, drill in todo:
        if xy is None:
            continue
        p = pad(ref, num)
        via("GND", xy, dia, drill)
        track("GND", [p, xy], 0.4 if dia > 0.6 else 0.3)
    # stitching along the USB pair and around the board so the two pours act as one
    for xy in [(15.2, y + 3.6), (17.0, y - 1.2), (SX - 0.2, y + 3.6), (SX - 0.2, y + 1.3),
               (SX + 0.2, y + 6.0), (SX - 2.6, y + 12.2), (14.5, y + 5.5),   # none inside J2's hold-down tabs
               (20.0, 9.0), (35.0, 9.0), (45.0, 9.0), (35.0, 34.5), (45.0, 34.5),
               (12.0, 30.0), (12.0, 12.0), (40.0, 21.5), (33.0, 21.5)]:
        via("GND", xy)


def solid_power_grounds():
    """The buck's loops close through these pads: no thermal spokes on them."""
    for ref, num in (("C1", 2), ("C2", 2), ("U2", 1), ("C4", 2), ("C5", 2), ("C6", 2), ("C7", 2), ("D1", 2)):
        for p in FPS[ref].Pads():
            if p.GetNumber() == str(num):
                p.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)


def silk_id():
    """Board name, revision and date on the top silkscreen (JLCPCB legend minimum: 1.0 / 0.15 mm)."""
    for d in list(board.GetDrawings()):
        if d.GetClass() == "PCB_TEXT" and d.GetText().startswith("HaLow USB-M.2E"):
            board.Remove(d)
            _REMOVED.append(d)
    t = pcbnew.PCB_TEXT(board)
    t.SetText("HaLow USB-M.2E  Rev A  2026-09")
    t.SetLayer(pcbnew.F_SilkS)
    t.SetPosition(pt(30.0, 38.9))
    t.SetTextSize(pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
    t.SetTextThickness(mm(0.15))
    board.Add(t)


def main():
    clear()
    silk_id()
    solid_power_grounds()
    route_usb()
    route_cc_vbus()
    route_buck()
    ground_vias()
    zone("GND", F, "GND top")
    zone("GND", B, "GND bottom")
    pcbnew.ZONE_FILLER(board).Fill([board.GetArea(i) for i in range(board.GetAreaCount())])
    pcbnew.SaveBoard(os.path.join(HERE, cfg.NAME + ".kicad_pcb"), board)
    n = len(list(board.GetTracks()))
    print("routed: %d track segments and vias, 2 ground zones filled" % n)


if __name__ == "__main__":
    main()
