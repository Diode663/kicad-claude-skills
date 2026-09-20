#!/usr/bin/env python3
"""block_crops.py -- one full-size picture per block, for the visual pass.

    python block_crops.py <root.kicad_sch> <outdir> [--serve 8760]

A whole A2 sheet in an 800-pixel screenshot shows that the blocks are in the
right places and nothing else. This renders the design with kicad-cli, then
writes, for every sheet, one cropped SVG per rectangle on it (block outlines
and box() alike): the block plus a margin, scaled to 1560 px wide. It also
writes index.html listing them. Open a crop directly in the Browser pane
(http://127.0.0.1:<port>/p1_b3.svg) and one screenshot shows one circuit at a
size where a label on a dashed line, a crowded port name or a clipped note is
obvious.

Why a crop instead of zooming the page viewer: a static SVG paints at once,
where the pan/zoom viewer often times a screenshot out; and the crop is cut in
sheet millimetres from the rectangle KiCad actually saved, so it cannot be
aimed wrong.

--serve starts `python -m http.server` on that port for the output folder.
Pick a port nobody is using: an old server left on the usual port goes on
serving its old folder, and you will be looking at last week's drawing with
no error anywhere. The page title carries the render's timestamp -- check it.

Crops are ordered the way a reader meets the blocks: by row, then left to
right. kicad-cli is found the same way schlib finds it.
"""
import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# sheet-level rectangles only: one tab deep. The symbols embedded in (lib_symbols ...)
# have rectangles of their own, in symbol coordinates, several tabs deep.
RECT = re.compile(r"^\t\(rectangle\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)", re.M)
SHEETFILE = re.compile(r'\(property "Sheetfile" "([^"]+)"')


def sheet_files(root):
    """The root file and every sub-sheet file it references, in order."""
    out, todo = [], [os.path.abspath(root)]
    while todo:
        f = todo.pop(0)
        if f in out or not os.path.exists(f):
            continue
        out.append(f)
        text = open(f, encoding="utf-8").read()
        todo += [os.path.join(os.path.dirname(f), n) for n in SHEETFILE.findall(text)]
    return out


def crop(svg_text, x, y, w, h, px=1560):
    u = re.sub(r'viewBox="[^"]+"', 'viewBox="%g %g %g %g"' % (x, y, w, h), svg_text, count=1)
    u = re.sub(r'width="[^"]+"', 'width="%d"' % px, u, count=1)
    return re.sub(r'height="[^"]+"', 'height="%d"' % int(px * h / w), u, count=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root")
    ap.add_argument("outdir")
    ap.add_argument("--margin", type=float, default=4.0, help="mm around each block (default 4)")
    ap.add_argument("--serve", type=int, metavar="PORT")
    a = ap.parse_args()

    from schlib import render
    os.makedirs(a.outdir, exist_ok=True)
    for f in os.listdir(a.outdir):
        if re.fullmatch(r"p\d+(_b\d+)?\.svg", f):
            os.remove(os.path.join(a.outdir, f))
    pages = render(a.root, a.outdir)                 # [(svg path, sheet label)], root first
    files = sheet_files(a.root)
    rows = []
    for n, (svg, label) in enumerate(pages):
        text = open(svg, encoding="utf-8").read()
        src = files[n] if n < len(files) else files[0]
        rects = [tuple(float(v) for v in r) for r in RECT.findall(open(src, encoding="utf-8").read())]
        rects = [(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)) for x1, y1, x2, y2 in rects]
        rects.sort(key=lambda r: (round(r[1] / 40.0), r[0]))
        base = os.path.splitext(os.path.basename(svg))[0]
        rows.append("<h2>%s &mdash; <a href='%s'>whole page</a></h2>" % (label, os.path.basename(svg)))
        for i, (x1, y1, x2, y2) in enumerate(rects):
            m = a.margin
            name = "%s_b%d.svg" % (base, i)
            # the title sits in a band above the outline: leave room for it
            open(os.path.join(a.outdir, name), "w", encoding="utf-8").write(
                crop(text, x1 - m, y1 - m - 5.0, (x2 - x1) + 2 * m, (y2 - y1) + 2 * m + 5.0))
            rows.append("<p><a href='%s'>%s</a> &nbsp; %.0f x %.0f mm at (%.0f, %.0f)</p>"
                        % (name, name, x2 - x1, y2 - y1, x1, y1))
            print("  %s  %.0f x %.0f mm" % (name, x2 - x1, y2 - y1))
        if not rects:
            print("  %s: no rectangles on this sheet" % label)
    open(os.path.join(a.outdir, "index.html"), "w", encoding="utf-8").write(
        "<html><body style='font-family:sans-serif'><h1>Block crops</h1>%s</body></html>" % "\n".join(rows))
    print("wrote %s" % os.path.join(a.outdir, "index.html"))
    if a.serve:
        print("serving on http://127.0.0.1:%d/  (Ctrl-C to stop)" % a.serve)
        subprocess.run([sys.executable, "-m", "http.server", str(a.serve), "--bind", "127.0.0.1",
                        "--directory", a.outdir])


if __name__ == "__main__":
    main()
