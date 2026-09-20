#!/usr/bin/env python3
"""jlc_compare -- derive JLCPCB CPL corrections from the pads, instead of guessing.

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" jlc_compare.py board.kicad_pcb [--work DIR] [--all]

JLCPCB's zero orientation belongs to THEIR footprint for each LCSC part, not to
the package name. For every polarised or multi-pin part on the board that has
an LCSC field, this fetches JLCPCB/EasyEDA's footprint for that exact number
(easyeda2kicad), then finds the KiCad rotation -- 0, 90, 180 or 270 -- that
lays the board footprint's pads onto theirs, matched pad number to pad number:

    correction = -(that rotation)      what jlc_rotations.json wants
    offset     = where their origin sits, in CPL axes, at this part's placement
    fit        = worst pad after the match; large = different pad geometry, look

It prints a table and writes <work>/jlc_compare.json, plus ready-to-paste
"overrides" for every part whose correction is not zero. It never edits
jlc_rotations.json: a correction is evidence to be read, then recorded with
its source.

What it cannot see: POLARITY of two-pad parts. It matches pad 1 to pad 1, which
is right only if pad 1 means the same thing on both (KiCad: diode and LED pad 1
= cathode). Check their footprint's silk -- the cathode bar or the closed end
sits on their pad-1 side when it agrees -- or their symbol's pin names.

The EasyEDA API refuses requests (HTTP 403) after a burst of a couple of dozen.
Fetches are cached in the work folder and paced; if it starts refusing, stop
and run again later -- what was fetched is kept. Needs: pip install easyeda2kicad
(run under a Python that has it; pcbnew is only needed for reading the board,
so this script shells out for the fetch).
"""
import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
import time

import pcbnew

PAD = re.compile(r'\(pad\s+"?([^\s"]*)"?\s+(\w+)\s+\w+\s+\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)')
POLARISED = re.compile(r"^(U|IC|Q|D|LED|J|P|CN|SW|Y|X|BT|K|RN|AR|T|L)\d")


def rot(p, deg):
    """KiCad footprint rotation: counter-clockwise on screen, Y down."""
    a = math.radians(deg)
    return (p[0] * math.cos(a) + p[1] * math.sin(a), -p[0] * math.sin(a) + p[1] * math.cos(a))


def fetch(lcsc, work, python, pause):
    d = os.path.join(work, lcsc)
    hit = glob.glob(os.path.join(d, "lib.pretty", "*.kicad_mod"))
    if hit:
        return hit[0]
    os.makedirs(d, exist_ok=True)              # easyeda2kicad will not create it
    r = subprocess.run([python, "-m", "easyeda2kicad", "--lcsc_id", lcsc, "--footprint", "--overwrite",
                        "--output", os.path.join(d, "lib")], capture_output=True, text=True)
    time.sleep(pause)
    hit = glob.glob(os.path.join(d, "lib.pretty", "*.kicad_mod"))
    if not hit:
        msg = (r.stdout + r.stderr).strip().split("\n")
        print("  %s: fetch failed (%s)" % (lcsc, next((m for m in msg if "ERROR" in m), "no footprint")))
        return None
    return hit[0]


def mean_pads(pairs):
    out = {}
    for n, xy in pairs:
        if n:
            out.setdefault(n, []).append(xy)
    return {n: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for n, v in out.items()}


def fit(kp, jp, common, th):
    rk = {n: rot(kp[n], th) for n in common}
    tx = sum(jp[n][0] - rk[n][0] for n in common) / len(common)
    ty = sum(jp[n][1] - rk[n][1] for n in common) / len(common)
    err = max(math.hypot(jp[n][0] - rk[n][0] - tx, jp[n][1] - rk[n][1] - ty) for n in common)
    return err, tx, ty


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcb")
    ap.add_argument("--work", help="cache folder for fetched footprints (default: <project>/jlc/footprints)")
    ap.add_argument("--all", action="store_true", help="include two-pad passives (R, C) as well")
    ap.add_argument("--python", default="python", help="a Python that has easyeda2kicad (default: python)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between fetches (default 1.5)")
    a = ap.parse_args()
    pcb = os.path.abspath(a.pcb)
    work = a.work or os.path.join(os.path.dirname(pcb), "jlc", "footprints")
    os.makedirs(work, exist_ok=True)
    board = pcbnew.LoadBoard(pcb)
    result, overrides = {}, {}
    for fp in sorted(board.GetFootprints(), key=lambda f: [int(t) if t.isdigit() else t
                                                           for t in re.split(r"(\d+)", f.GetReference())]):
        ref = fp.GetReference()
        lcsc = fp.GetFieldText("LCSC") if fp.HasField("LCSC") else ""
        if not lcsc or fp.IsDNP() or not (a.all or POLARISED.match(ref) or len(list(fp.Pads())) > 2):
            continue
        mod = fetch(lcsc, work, a.python, a.pause)
        if not mod:
            continue
        r, o = fp.GetOrientationDegrees(), fp.GetPosition()
        kp = mean_pads((p.GetNumber(), rot((pcbnew.ToMM(p.GetPosition().x - o.x),
                                            pcbnew.ToMM(p.GetPosition().y - o.y)), -r)) for p in fp.Pads())
        text = open(mod, encoding="utf-8").read()
        jp = mean_pads((n, (float(x), float(y))) for n, kind, x, y, _r in PAD.findall(text)
                       if kind in ("smd", "thru_hole"))
        common = sorted(set(kp) & set(jp))
        if len(common) < 2:
            print("%-6s %-10s no pad numbers in common (%s vs %s)" % (ref, lcsc, sorted(kp)[:4], sorted(jp)[:4]))
            continue
        fits = {th: fit(kp, jp, common, th) for th in (0, 90, 180, 270)}
        th = min(fits, key=lambda t: fits[t][0])
        err, tx, ty = fits[th]
        also = [t for t in fits if t != th and fits[t][0] < err + 0.05]
        corr = (-th) % 360
        ob = rot(rot((-tx, -ty), -th), r)          # their origin, in board axes, at this placement
        side_bottom = fp.GetLayer() == pcbnew.B_Cu
        result[ref] = {"lcsc": lcsc, "jlc_footprint": os.path.basename(mod),
                       "kicad_footprint": str(fp.GetFPID().GetLibItemName()), "fit_mm": round(err, 3),
                       "rotation": corr, "offset_x": round(ob[0], 3), "offset_y": round(-ob[1], 3),
                       "pads_matched": len(common), "also_fits": also, "bottom": side_bottom}
        if corr or math.hypot(ob[0], ob[1]) > 0.2:
            overrides[ref] = {"rotation": corr, "verified": False,
                              "source": "%s: JLCPCB footprint %s vs KiCad %s, %d pads matched at KiCad %d deg, worst pad %.2f mm (jlc_compare.py)"
                                        % (lcsc, os.path.splitext(os.path.basename(mod))[0],
                                           str(fp.GetFPID().GetLibItemName()), len(common), th, err)}
            if math.hypot(ob[0], ob[1]) > 0.2:
                overrides[ref].update(offset_x=round(ob[0], 3), offset_y=round(-ob[1], 3))
        print("%-6s %-10s fit %.3f mm  correction %3d  origin offset %+.3f %+.3f  pads %2d%s%s%s"
              % (ref, lcsc, err, corr, ob[0], -ob[1], len(common),
                 "  <- pad geometry differs, look" if err > 0.15 else "",
                 "  (symmetric: also fits %s)" % also if also else "",
                 "  [bottom side: sign convention unconfirmed]" if side_bottom and corr else ""))
    json.dump(result, open(os.path.join(work, "jlc_compare.json"), "w"), indent=1)
    print("\n%d part(s) compared; %d need an entry in jlc_rotations.json:" % (len(result), len(overrides)))
    print(json.dumps({"overrides": overrides}, indent=2))
    print("\nTwo-pad parts: pad 1 was matched to pad 1. Confirm polarity from their silk or symbol.")


if __name__ == "__main__":
    sys.exit(main())
