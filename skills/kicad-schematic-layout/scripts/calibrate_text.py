#!/usr/bin/env python3
"""Measure KiCad's stroke-font glyph advances, exactly, from KiCad itself.

Every extent schlib computes for a string -- block outlines, clearance
checks, label lanes, stub lengths -- is only as good as its idea of how wide
text is. Guessing a single "0.85 x size per character" factor is wrong by up
to 40% per glyph ('I' is 0.39, 'm' is 1.01), which is the difference between
a label that clears its neighbour and one drawn on top of it.

So don't guess. This writes a throwaway schematic containing a run of each
character at two lengths, renders it with kicad-cli, and reads the
`textLength` KiCad puts on every <text> element. Two lengths separate the
per-character advance from any fixed per-string offset:

    advance = (w_long - w_short) / (n_long - n_short)

Run it after a KiCad major upgrade and paste the table into schlib.py:

    "C:/Program Files/KiCad/10.0/bin/python.exe" calibrate_text.py
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schlib import Design, find_kicad_cli  # noqa: E402

CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
         "abcdefghijklmnopqrstuvwxyz"
         "0123456789"
         " !\"#$%&'()*+,-./:;<=>?@[]^_`{|}~")
SHORT, LONG = 4, 12
SIZE = 1.27


def measure(svg):
    out = {}
    pat = re.compile(r"<text\b([^>]*)>([^<]*)</text>")
    for attrs, body in pat.findall(open(svg, encoding="utf-8").read()):
        g = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        try:
            w, fs = float(g["textLength"]), float(g["font-size"])
        except (KeyError, ValueError):
            continue
        out[html.unescape(body)] = w / fs
    return out


def main():
    cli = find_kicad_cli()
    tmp = tempfile.mkdtemp(prefix="calib-")
    try:
        d = Design(tmp, "calib", title="Calibration", rev="A")
        s = d.sheet("calib.kicad_sch", "Calibration", paper="A0")
        y = 10.0
        wanted = []
        for c in CHARS:
            for n in (SHORT, LONG):
                txt = c * n
                s.note(txt, 10.0, y, SIZE)
                wanted.append((c, n, txt))
                y += 5.08
        d.write(center=False, strict=False, native_check=False)
        subprocess.run([cli, "sch", "export", "svg", "--exclude-drawing-sheet",
                        "-o", tmp, os.path.join(tmp, "calib.kicad_sch")],
                       capture_output=True, text=True)
        svgs = [f for f in os.listdir(tmp) if f.endswith(".svg")]
        if not svgs:
            raise SystemExit("kicad-cli produced no SVG")
        seen = measure(os.path.join(tmp, svgs[0]))

        adv, miss, intercepts = {}, [], []
        for c in CHARS:
            a, b = seen.get(c * SHORT), seen.get(c * LONG)
            if a is None or b is None:
                miss.append(c)
                continue
            per = (b - a) / (LONG - SHORT)
            adv[c] = round(per, 4)
            intercepts.append(round(a - SHORT * per, 4))
        print(f"# measured with {cli}")
        print(f"# {len(adv)} glyphs, fixed per-string offset "
              f"{min(intercepts):.4f}..{max(intercepts):.4f} x size")
        print("STROKE_ADVANCE = {")
        line = "    "
        for c in sorted(adv):
            piece = f"{c!r}: {adv[c]:.4f}, "
            if len(line) + len(piece) > 76:
                print(line.rstrip())
                line = "    "
            line += piece
        print(line.rstrip().rstrip(","))
        print("}")
        if miss:
            print(f"# not measured (KiCad drew nothing): {''.join(miss)!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
