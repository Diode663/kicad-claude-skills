#!/usr/bin/env python3
"""Check schlib's placement math against KiCad itself.

Places an asymmetric 3-pin symbol at every rotation x mirror combination, puts
a net label exactly where schlib predicts each pin tip lands, exports KiCad's
netlist, and reports whether each label actually touched the pin it should.
Also tests the alternative "mirror first, then rotate" convention, so a
failure tells you which way to fix _transform().

Run this after a KiCad major-version upgrade, or if a mirrored part ever wires
up wrong:  python probe_transforms.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schlib  # noqa: E402

CANDIDATES = ["Device:Q_NPN_BCE", "Device:Q_NPN", "Device:Q_NPN_EBC", "Device:Q_NMOS_GDS",
              "Device:Q_NMOS"]


def alt_transform(px, py, rot, mirror):
    """Hypothesis B: mirror in the symbol's local frame, then rotate."""
    if mirror == "x":
        py = -py
    elif mirror == "y":
        px = -px
    rx, ry = schlib._rot(px, py, rot)
    return rx, -ry


def main():
    work = tempfile.mkdtemp(prefix="schlib-probe-")
    d = schlib.Design(work, "probe", title="transform probe")
    lib_id = None
    for c in CANDIDATES:
        try:
            d.lib.get(c)
            lib_id = c
            break
        except KeyError:
            continue
    if not lib_id:
        sys.exit("no asymmetric 3-pin test symbol found in Device library")
    sym = d.lib.get(lib_id)
    pins = sym.pins(1)
    s = d.sheet("probe.kicad_sch", "probe", paper="A3")

    combos = [(r, m) for m in (None, "x", "y") for r in (0, 90, 180, 270)]
    placed = []
    for i, (rot, mir) in enumerate(combos):
        x = 38.1 + (i % 4) * 30.48
        y = 38.1 + (i // 4) * 30.48
        ref = f"Q{i + 1}"
        s.place(ref, lib_id, "probe", x, y, rot, mir)
        for p in pins:
            px, py = schlib.pin_position(sym, p, x, y, rot, mir)
            s.label(f"L{i + 1}_{p['num']}", px, py)
        placed.append((ref, rot, mir, x, y))
    d.write(center=False, force=True)
    nets = schlib.export_netlist(os.path.join(work, "probe.kicad_sch"))

    observed = {}   # (ref, label_pin) -> actual pin num at that spot, or None
    for name, nodes in nets.items():
        short = name.rsplit("/", 1)[-1]
        if not short.startswith("L"):
            continue
        idx, pnum = short[1:].split("_", 1)
        real = [pn for (r, pn) in nodes if r == f"Q{idx}"]
        observed[(f"Q{idx}", pnum)] = real[0] if real else None
    for i in range(len(combos)):
        for p in pins:
            observed.setdefault((f"Q{i + 1}", p["num"]), None)

    def hypothesis_ok(transform):
        for ref, rot, mir, x, y in placed:
            spots = {}
            for p in pins:
                sx, sy = transform(p["x"], p["y"], rot, mir)
                spots[(round(x + sx, 3), round(y + sy, 3))] = p["num"]
            for p in pins:
                # where schlib put label L_<p>, which pin does this hypothesis
                # say sits there?
                lx, ly = schlib.pin_position(sym, p, x, y, rot, mir)
                predicted = spots.get((round(lx, 3), round(ly, 3)))
                if predicted != observed[(ref, p["num"])]:
                    return False
        return True

    print(f"symbol: {lib_id}   work dir: {work}")
    bad = 0
    for ref, rot, mir, x, y in placed:
        results = [observed[(ref, p["num"])] == p["num"] for p in pins]
        ok = all(results)
        bad += not ok
        print(f"  rot {rot:3d} mirror {str(mir):4s}  {'ok' if ok else 'MISMATCH'}")
    print()
    print("schlib._transform (rotate, then mirror in sheet space):",
          "MATCHES KiCad" if hypothesis_ok(schlib._transform) else "wrong")
    print("alternative (mirror in local frame, then rotate):      ",
          "MATCHES KiCad" if hypothesis_ok(alt_transform) else "wrong")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
