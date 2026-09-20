#!/usr/bin/env python3
"""Visual probe for reference/value text placement.

Places a resistor (vertical by nature) and a diode (horizontal by nature) at
every rotation x mirror combination, each captioned with its combination, and
renders the sheet. Every part's reference and value should read upright and
horizontal, sit clear of the body, and never lie on a pin or wire.

Text placement can't be checked through the netlist, so this one is judged
by eye. Rerun it after a KiCad major upgrade, or if text ever lands on a part:

    python probe_fields.py <output dir>     # then view <dir>/p1.svg
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schlib  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="schlib-fields-")
    work = tempfile.mkdtemp(prefix="schlib-fields-proj-")
    d = schlib.Design(work, "fields", title="field placement probe")
    s = d.sheet("fields.kicad_sch", "Field placement probe", paper="A4")
    n = 0
    for row, lib_id in enumerate(("Device:R", "Device:D")):
        for col, (rot, mir) in enumerate([(r, m) for m in (None, "x", "y")
                                          for r in (0, 90, 180, 270)]):
            n += 1
            x = 25.4 + (col % 6) * 38.1
            y = 38.1 + (row * 2 + col // 6) * 33.02
            ref = f"{'R' if lib_id.endswith('R') else 'D'}{n}"
            s.place(ref, lib_id, "VALUE", x, y, rot, mir)
            for num, _name, (px, py) in s.pins_of(ref):
                # short wire off each pin so text sitting on a wire is obvious
                dx, dy = s.pin_dir(ref, num)
                s.wire((px, py), (px + dx * 5.08, py + dy * 5.08))
                s.label("N", px + dx * 5.08, py + dy * 5.08)
            s.note(f"rot {rot} {mir or '-'}", x - 7.62, y + 12.7, 1.0)
    d.write(center=False, force=True, strict=False)
    pages = schlib.render(os.path.join(work, "fields.kicad_sch"), out)
    problems = schlib.text_collisions(s, pages[0][0])
    print(f"text problems: {len(problems)}")
    for p in problems:
        print("   ", p)
    print("view:", os.path.join(out, "p1.svg"))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
