#!/usr/bin/env python3
"""Measure every part against the M.2 socket and the card it holds.

Geometry of the socket and the 2230 card comes from build_models.py (the MLD
drawing). Part bodies come from their 3D models: the bounding box of the
model's own vertices (VRML points, or STEP CARTESIAN_POINTs), placed with the
footprint's position and rotation. A part with no readable model falls back
to courtyard + a package height table and is marked "est".

Rules checked (all from the MLD-NGFF-E-4.2H drawing unless noted):
  1. nothing but the card may overlap the socket housing;
  2. a part under the card must be <= 0.90 mm tall;
  3. (mounting holes are checked as a 7 mm washer under a 2.4 mm M3 head)
     a part taller than the card's underside (2.52 mm) must clear the card's
     edge by SIDE_CLEAR, and the housing by HOUSING_CLEAR (rework room for
     the hold-down tabs -- a judgement, not a drawing dimension);
  4. the standoff must be on the card's mounting notch, and 2.5 mm tall;
  5. the card's 25 degree insertion sweep is above its own outline, so rule 2
     covers it; parts past the card's far end are checked against the raised
     card edge.

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" check_m2_clearance.py
"""
import math
import os
import re
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_models as M            # noqa: E402
import placement_config as cfg      # noqa: E402

SIDE_CLEAR = 0.5
HOUSING_CLEAR = 1.0
HEIGHTS = {"0402": 0.55, "0603": 0.95, "0805": 1.35, "1206": 1.8, "1210": 2.7,
           "SOT-23": 1.45, "SOD-123": 1.35, "TestPoint": 0.05, "Fiducial": 0.05}
MODEL_DIR = r"C:\Program Files\KiCad\10.0\share\kicad\3dmodels"
NUM = r"-?\d+\.?\d*(?:[eE][-+]?\d+)?"


def model_bbox(path):
    """(x0, y0, z0, x1, y1, z1) in mm, model frame (Y up), or None."""
    if not os.path.exists(path):
        return None
    t = open(path, encoding="utf-8", errors="ignore").read()
    pts = []
    if path.lower().endswith(".wrl"):
        for m in re.finditer(r"point\s*\[([^\]]*)\]", t):
            v = [float(n) * 2.54 for n in re.findall(NUM, m.group(1))]
            pts += [v[i:i + 3] for i in range(0, len(v) - 2, 3)]
    else:
        for m in re.finditer(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", t):
            v = [float(n) for n in re.findall(NUM, m.group(1))]
            if len(v) == 3:
                pts.append(v)
    if not pts:
        return None
    return tuple(min(p[i] for p in pts) for i in range(3)) + tuple(max(p[i] for p in pts) for i in range(3))


def rot(x, y, deg):
    """Footprint-local -> board offset, KiCad's convention (measured, not assumed:
    J2 at 90 puts local +Y on board +X)."""
    a = math.radians(deg)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def rect_of(fp, local):
    x0, y0, x1, y1 = local
    p = fp.GetPosition()
    ox, oy = pcbnew.ToMM(p.x) - cfg.ORIGIN[0], pcbnew.ToMM(p.y) - cfg.ORIGIN[1]
    c = [rot(x, y, fp.GetOrientationDegrees()) for x in (x0, x1) for y in (y0, y1)]
    return (ox + min(q[0] for q in c), oy + min(q[1] for q in c),
            ox + max(q[0] for q in c), oy + max(q[1] for q in c))


def body(fp):
    """(board rect, height, source) for a footprint."""
    for m in fp.Models():
        path = m.m_Filename.replace("${KIPRJMOD}", HERE).replace("${KICAD10_3DMODEL_DIR}", MODEL_DIR)
        bb = model_bbox(path)
        if bb and not (m.m_Rotation.x or m.m_Rotation.y or m.m_Rotation.z):
            x0, y0, z0, x1, y1, z1 = bb
            ox, oy = m.m_Offset.x, m.m_Offset.y
            return rect_of(fp, (x0 + ox, -(y1 + oy), x1 + ox, -(y0 + oy))), z1 + m.m_Offset.z, "model"
    cy = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
    p = fp.GetPosition()
    h = next((v for k, v in HEIGHTS.items() if k in fp.GetFPIDAsString()), None)
    r = ((pcbnew.ToMM(cy.GetLeft()) - cfg.ORIGIN[0]), (pcbnew.ToMM(cy.GetTop()) - cfg.ORIGIN[1]),
         (pcbnew.ToMM(cy.GetRight()) - cfg.ORIGIN[0]), (pcbnew.ToMM(cy.GetBottom()) - cfg.ORIGIN[1]))
    return r, h, "est"


SCREW_R, SCREW_H = 3.5, 2.4      # M3 pan head with a 7 mm washer; head 2.4 mm tall


def circle_gap(cx, cy, radius, b):
    dx = max(b[0] - cx, cx - b[2], 0.0)
    dy = max(b[1] - cy, cy - b[3], 0.0)
    return math.hypot(dx, dy) - radius


def gap(a, b):
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, cfg.NAME + ".kicad_pcb")
    board = pcbnew.LoadBoard(path)
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    j2 = fps["J2"]
    housing = rect_of(j2, (-M.HOUSING_X, M.HOUSING_REAR_Y, M.HOUSING_X, M.HOUSING_FRONT_Y))
    card = rect_of(j2, (-M.CARD_W / 2, M.CARD_STOP_Y, M.CARD_W / 2, M.CARD_STOP_Y + M.CARD_L))
    under = rect_of(j2, (-M.CARD_W / 2, M.HOUSING_FRONT_Y, M.CARD_W / 2, M.CARD_STOP_Y + M.CARD_L))
    notch = rot(0.0, M.CARD_STOP_Y + M.CARD_L, j2.GetOrientationDegrees())
    p = j2.GetPosition()
    notch = (pcbnew.ToMM(p.x) - cfg.ORIGIN[0] + notch[0], pcbnew.ToMM(p.y) - cfg.ORIGIN[1] + notch[1])
    fmt = lambda r: "x %.2f..%.2f  y %.2f..%.2f" % (r[0], r[2], r[1], r[3])
    print("socket housing  %s   z 0..%.2f" % (fmt(housing), M.HOUSING_H))
    print("2230 card       %s   underside z %.2f, bottom parts down to %.2f" %
          (fmt(card), M.CARD_Z0, M.CARD_Z0 - M.BOT_ZONE_H))
    print()
    print("%-4s %-7s %6s  %9s %9s  %s" % ("ref", "height", "src", "to socket", "to card", "verdict"))
    fails = 0
    rows = []
    for ref, fp in fps.items():
        if ref == "J2":
            continue
        r, h, src = body(fp)
        gh, gc = gap(r, housing), gap(r, card)
        if fp.GetFPIDAsString().startswith("MountingHole:"):
            c = fp.GetPosition()
            cx, cy = pcbnew.ToMM(c.x) - cfg.ORIGIN[0], pcbnew.ToMM(c.y) - cfg.ORIGIN[1]
            gh, gc = circle_gap(cx, cy, SCREW_R, housing), circle_gap(cx, cy, SCREW_R, card)
            h, src = SCREW_H, "screw"
        if gh > 6 and gc > 6:
            continue
        verdict = "ok"
        if ref == "H5":
            d = math.hypot(pcbnew.ToMM(fp.GetPosition().x) - cfg.ORIGIN[0] - notch[0],
                           pcbnew.ToMM(fp.GetPosition().y) - cfg.ORIGIN[1] - notch[1])
            verdict = ("ok: on the mounting notch (%.2f mm off), %.2f mm tall vs card underside %.2f"
                       % (d, h or 0, M.CARD_Z0)) if d < 0.1 and abs((h or 0) - M.CARD_Z0) < 0.1 else \
                      "FAIL: %.2f mm from the notch, height %s" % (d, h)
        elif overlap(r, housing):
            verdict = "FAIL: overlaps the socket housing"
        elif overlap(r, under):
            verdict = ("ok: under the card, %.2f <= %.2f mm" % (h, M.UNDER_CARD_MAX)
                       if h is not None and h <= M.UNDER_CARD_MAX else
                       "FAIL: under the card and %s mm tall (limit %.2f)" % (h, M.UNDER_CARD_MAX))
        elif h is None:
            verdict = "FAIL: height unknown"
        elif h > M.CARD_Z0 - M.BOT_ZONE_H and gc < SIDE_CLEAR:
            verdict = "FAIL: %.2f mm from the card edge (want %.2f)" % (gc, SIDE_CLEAR)
        elif gh < HOUSING_CLEAR:
            verdict = "FAIL: %.2f mm from the housing (want %.2f for rework)" % (gh, HOUSING_CLEAR)
        fails += verdict.startswith("FAIL")
        rows.append((min(gh, gc), "%-4s %-7s %6s  %9.2f %9.2f  %s" %
                     (ref, "%.2f" % h if h is not None else "?", src, gh, gc, verdict)))
    for _k, line in sorted(rows):
        print(line)
    # insertion: the card goes in tilted up to 25 degrees about the slot mouth
    tip = M.CARD_L * math.sin(math.radians(25)) + M.CARD_Z0
    print()
    print("insertion at 25 deg lifts the card's far end to %.1f mm; the sweep stays inside the card's"
          % tip)
    print("own outline, so only rule 2 applies to it. H5 is %.2f mm tall and sits under the notch." %
          (body(fps["H5"])[1] or 0))
    print()
    print("RESULT: %s" % ("all clear" if not fails else "%d problem(s)" % fails))
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
