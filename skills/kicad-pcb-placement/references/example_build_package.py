#!/usr/bin/env python3
"""Build the JLCPCB turnkey package into fab/:

    fab/gerbers/*            gerbers + Excellon drill (PTH and NPTH separate)
    fab/<name>_gerbers.zip   the file to upload
    fab/<name>_bom.csv       Comment, Designator, Footprint, LCSC Part #, MPN, Manufacturer, Quantity
    fab/<name>_cpl.csv       placement file (from jlc_cpl.py)
    fab/ORDER_NOTES.txt      the options to pick on JLCPCB's order form

It refuses to build unless KiCad's DRC is clean (no errors, nothing unconnected,
schematic parity zero). Gerbers, drill and placement all use the
origin set by pcblib (the aux / drill-place origin at the board corner), so they line up.

    "C:\Program Files\KiCad\10.0\bin\python.exe" build_package.py
"""
import collections
import csv
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import placement_config as cfg      # noqa: E402

CLI = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
PCB = os.path.join(HERE, cfg.NAME + ".kicad_pcb")
FAB = os.path.join(HERE, "fab")
GER = os.path.join(FAB, "gerbers")
LAYERS = "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"

ORDER_NOTES = """JLCPCB order settings for USBC_M2E_HaLow_Adapter (Rev A)

PCB
  Layers 2, 56.5 x 40.5 mm, thickness 1.6 mm, outer copper 1 oz, FR-4.
  Surface finish: LeadFree HASL is fine; ENIG is nicer for the 0.5 mm pitch M.2 socket.
  Via covering: tented. Min via hole 0.3 mm / diameter 0.6 mm (no surcharge tier).
  Impedance control: NO. JLCPCB does not offer it on 2 layers. The USB pair is drawn
  to their calculator's numbers for stackup JLC0216A: 0.3345 / 0.1524 mm, ground 0.2032 mm.
  Remove order number: optional ("Specify a location" is not drawn on this board).

PCB assembly
  Side: top only. Economic PCBA. JLCPCB's parts library lists J1 (C165948), J2 (C52766479)
  and H5 (C5301773) as "SMT Assembly, PCBA Type: Economic and Standard" (checked 2026-09-20).
  All three are Extended parts. J1 overhangs the board edge by 0.5 mm: if their engineer
  queries that at file review, it is intentional.
  Tooling holes: added by JLCPCB. Fiducials FID1-FID3 are on the board.
  Files: *_bom.csv and *_cpl.csv from this folder.
  Not populated by design: H1-H4 (holes), FID1-3, TP1-3. They have no LCSC number and
  are in neither file.
  Placement corrections are in jlc_rotations.json, each with its evidence. JLCPCB's zero
  orientation belongs to THEIR footprint for each LCSC part: U1 (C7519) needs +270, U2
  (C116592) needs +180 although both are SOT-23-6, and J1's origin is 1.57 mm nearer its pads.
  LOOK AT THESE AGAIN in the placement preview after uploading this CPL:
    U1  pin 1 dot bottom-right, on the silkscreen triangle   (was wrong, now corrected)
    U2  pin 1 dot bottom-right, on the silkscreen triangle   (corrected from their footprint, not yet seen)
    J1  contacts sitting on the pads, opening toward the left edge   (shifted 1.57 mm)
    D1  cathode band on the J1 side;  J2 slot opening toward H5
  H5 is an M2 x 2.5 mm SMT nut (C5301773) that seats in a 3.7 mm plated hole.

First board bring-up
  Measure VBUS at C1 during transmit (TI wants >= 4.43 V for 3.3 V out).
  Check 3.3 V load-step / ripple at TP1, and receive sensitivity with the buck loaded.
"""


def run(args):
    r = subprocess.run([CLI] + args, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("kicad-cli %s failed:\n%s%s" % (" ".join(args[:3]), r.stdout, r.stderr))
    return r.stdout


def gate():
    out = os.path.join(HERE, "drc.json")
    subprocess.run([CLI, "pcb", "drc", "--severity-error", "--schematic-parity", "--format", "json", "-o", out, PCB],
                   capture_output=True, text=True)
    d = json.load(open(out))
    errs = [v for v in d.get("violations", []) if v["severity"] == "error"]
    n = (len(errs), len(d.get("unconnected_items", [])), len(d.get("schematic_parity", [])))
    print("DRC gate: %d error(s), %d unconnected, %d parity" % n)
    if any(n):
        raise SystemExit("not building a package from a board that fails DRC")


def bom():
    board = pcbnew.LoadBoard(PCB)
    groups = collections.OrderedDict()
    skipped = []
    for fp in sorted(board.GetFootprints(), key=lambda f: (f.GetReference()[:1], int("".join(c for c in f.GetReference() if c.isdigit()) or 0))):
        get = lambda k: fp.GetFieldText(k) if fp.HasField(k) else ""
        lcsc = get("LCSC")
        if not lcsc:
            skipped.append(fp.GetReference())
            continue
        key = (fp.GetValue(), fp.GetFPID().GetLibItemName().wx_str() if hasattr(fp.GetFPID().GetLibItemName(), "wx_str") else str(fp.GetFPID().GetLibItemName()), lcsc)
        g = groups.setdefault(key, {"refs": [], "mpn": get("MPN"), "mfr": get("Manufacturer")})
        g["refs"].append(fp.GetReference())
    path = os.path.join(FAB, cfg.NAME + "_bom.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN", "Manufacturer", "Quantity"])
        for (value, fpname, lcsc), g in groups.items():
            w.writerow([value, ",".join(g["refs"]), fpname, lcsc, g["mpn"], g["mfr"], len(g["refs"])])
    print("BOM: %d line(s), %d part(s); not in BOM (no LCSC number): %s"
          % (len(groups), sum(len(g["refs"]) for g in groups.values()), ", ".join(skipped)))
    return {r for g in groups.values() for r in g["refs"]}


def main():
    gate()
    if os.path.isdir(FAB):
        shutil.rmtree(FAB)
    os.makedirs(GER)
    run(["pcb", "export", "gerbers", "-o", GER + os.sep, "--layers", LAYERS, "--subtract-soldermask",
         "--use-drill-file-origin", "--check-zones", PCB])
    run(["pcb", "export", "drill", "-o", GER + os.sep, "--format", "excellon", "--drill-origin", "plot",
         "--excellon-units", "mm", "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2", PCB])
    for f in os.listdir(GER):
        if f.endswith(".gbrjob"):
            os.remove(os.path.join(GER, f))
    z = os.path.join(FAB, cfg.NAME + "_gerbers.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(os.listdir(GER)):
            zf.write(os.path.join(GER, f), f)
    print("gerbers: %d files -> %s" % (len(os.listdir(GER)), os.path.relpath(z, HERE)))
    refs = bom()
    subprocess.run([sys.executable, os.path.join(HERE, "jlc_cpl.py"), PCB], capture_output=True, text=True)
    src = os.path.join(HERE, "jlc", cfg.NAME + "_cpl.csv")
    dst = os.path.join(FAB, cfg.NAME + "_cpl.csv")
    shutil.copy2(src, dst)
    cpl = {row[0] for row in list(csv.reader(open(dst, encoding="utf-8")))[1:]}
    print("CPL: %d placement(s); in CPL but not BOM: %s; in BOM but not CPL: %s"
          % (len(cpl), sorted(cpl - refs) or "none", sorted(refs - cpl) or "none"))
    if cpl != refs:
        raise SystemExit("BOM and CPL designators differ -- JLCPCB rejects that upload")
    with open(os.path.join(FAB, "ORDER_NOTES.txt"), "w", encoding="utf-8") as f:
        f.write(ORDER_NOTES)
    print("wrote fab/ORDER_NOTES.txt")


if __name__ == "__main__":
    main()
