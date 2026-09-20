#!/usr/bin/env python3
"""jlc_cpl -- JLCPCB placement (CPL) file from a KiCad board, with rotation
corrections that are written down, sourced and applied every time.

KiCad's zero-degree orientation and JLCPCB's are not the same for every
package, so a CPL can pass the upload checks with an IC or connector turned
the wrong way in the Component Placements preview. The usual fix is to edit
the Rotation column by hand after each export -- and forget one next time.
This script keeps the fixes in a JSON table instead:

    references/jlc_rotations.json    skill defaults (few, each with a source)
    <project>/jlc_rotations.json     this project's rules and per-part overrides

Precedence: project override for the designator > project rule > default
rule > nothing. Rules match the footprint name (library prefix stripped) as a
regular expression.

    python jlc_cpl.py board.kicad_pcb                # -> jlc/<board>_cpl.csv
    python jlc_cpl.py board.kicad_pcb --all          # include parts with no LCSC field

Rows: only footprints with an LCSC field (what JLCPCB will actually place),
never DNP or "exclude from position files" ones. The report lists every
correction applied and every polarised or multi-pin part that has no rule --
those are the ones to check in JLCPCB's preview before paying. When a
correction is confirmed on a real order, set "verified": true on it.

Bottom side: KiCad reports a bottom part's rotation as seen from the top, and
JLCPCB views the bottom from below, so a correction is applied with its sign
reversed there. That convention is not yet confirmed on an order of this
user's -- every corrected bottom-side row is flagged for the preview.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# beside the skill, or -- when this file was copied into a project -- in the
# installed skill, so the sourced defaults still apply
DEFAULTS = next((p for p in (
    os.path.join(HERE, "..", "references", "jlc_rotations.json"),
    os.path.join(os.path.expanduser("~"), ".claude", "skills", "kicad-pcb-placement",
                 "references", "jlc_rotations.json")) if os.path.exists(p)),
    os.path.join(HERE, "..", "references", "jlc_rotations.json"))

# reference prefixes whose orientation matters: the preview is where to look
POLARISED = re.compile(r"^(U|IC|Q|D|LED|J|P|CN|SW|Y|X|BT|K|RN|AR|T)\d")


def find_cli():
    env = os.environ.get("KICAD_CLI")
    for c in (env, shutil.which("kicad-cli"),
              r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
              r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
              "/usr/bin/kicad-cli", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if c and os.path.exists(c):
            return c
    raise SystemExit("kicad-cli not found; set KICAD_CLI")


def board_parts(pcb):
    """{ref: {lib, name, lcsc, dnp, no_pos, pads}} read from the board file."""
    text = open(pcb, encoding="utf-8").read()
    starts = [m.start() for m in re.finditer(r'\n\t\(footprint "', text)] + [len(text)]
    out = {}
    for a, b in zip(starts, starts[1:]):
        blk = text[a:b]
        lib_id = re.match(r'\n\t\(footprint "([^"]*)"', blk).group(1)   # "" for pcblib's holes
        ref = re.search(r'\(property "Reference" "([^"]*)"', blk)
        if not ref:
            continue
        lcsc = re.search(r'\(property "(?:LCSC|LCSC Part|JLCPCB|JLC)" "([^"]*)"', blk, re.I)
        attr = re.search(r"\(attr ([^)]*)\)", blk)
        attrs = attr.group(1).split() if attr else []
        lib, _, name = lib_id.rpartition(":")
        out[ref.group(1)] = dict(lib=lib, name=name, lcsc=(lcsc.group(1).strip() if lcsc else ""),
                                 dnp="dnp" in attrs or re.search(r"\(dnp yes\)", blk) is not None,
                                 no_pos="exclude_from_pos_files" in attrs,
                                 pads=len(re.findall(r'\n\t\t\(pad "', blk)))
    return out


def load_rules(paths):
    """Merged rule set: overrides {ref: rule}, rules [rule...] in precedence order."""
    overrides, rules = {}, []
    for path, origin in paths:
        if not path or not os.path.exists(path):
            continue
        data = json.load(open(path, encoding="utf-8"))
        if data.get("schema") != 1:
            raise SystemExit(f"{path}: unsupported schema {data.get('schema')!r} (want 1)")
        ids = set()
        for r in data.get("rules", []):
            for k in ("id", "match", "rotation"):
                if k not in r:
                    raise SystemExit(f"{path}: rule {r} has no {k!r}")
            if r["id"] in ids:
                raise SystemExit(f"{path}: duplicate rule id {r['id']!r}")
            ids.add(r["id"])
            re.compile(r["match"])
            rules.append(dict(r, origin=origin))
        for ref, r in data.get("overrides", {}).items():
            overrides.setdefault(ref, dict(r, id=f"override:{ref}", origin=origin))
    return overrides, rules


def export(pcb, out_csv, rules_paths, include_all=False, quiet=False):
    cli = find_cli()
    parts = board_parts(pcb)
    overrides, rules = load_rules(rules_paths)
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        r = subprocess.run([cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm",
                            "--side", "both", "--use-drill-file-origin", "--exclude-dnp",
                            "-o", tmp, pcb], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"kicad-cli pos export failed:\n{r.stdout}\n{r.stderr}")
        rows = list(csv.DictReader(open(tmp, encoding="utf-8")))
    finally:
        os.remove(tmp)

    out, applied, unchecked, skipped = [], [], [], []
    for row in rows:
        ref = row["Ref"]
        info = parts.get(ref, {})
        if info.get("no_pos") or info.get("dnp"):
            skipped.append((ref, "DNP / excluded from position files"))
            continue
        if not include_all and not info.get("lcsc"):
            skipped.append((ref, "no LCSC field"))
            continue
        side = row["Side"].strip().lower()
        rot = float(row["Rot"])
        name = info.get("name") or row["Package"]
        rule = overrides.get(ref)
        if rule is None:
            rule = next((r for r in rules if re.search(r["match"], name)), None)
        x, y = float(row["PosX"]), float(row["PosY"])
        if rule:
            corr = float(rule["rotation"])
            new = (rot + corr) % 360 if side == "top" else (rot - corr) % 360
            ox, oy = float(rule.get("offset_x", 0)), float(rule.get("offset_y", 0))
            x, y = x + ox, y + oy
            applied.append((ref, name, side, rot, new, rule))
            rot = new
        elif POLARISED.match(ref) or info.get("pads", 2) > 2:
            unchecked.append((ref, name, side, info.get("lib", "")))
        out.append({"Designator": ref, "Mid X": f"{x:.4f}", "Mid Y": f"{y:.4f}",
                    "Layer": "Top" if side == "top" else "Bottom", "Rotation": f"{rot:g}"})

    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writeheader()
        w.writerows(sorted(out, key=lambda r: _natural(r["Designator"])))

    if not quiet:
        print(f"wrote {out_csv}: {len(out)} placement(s), {len(skipped)} skipped")
        if applied:
            print(f"  corrections applied ({len(applied)}):")
            for ref, name, side, a, b, rule in applied:
                flag = "" if rule.get("verified") else "  [unverified -- check preview]"
                if side == "bottom":
                    flag = "  [bottom side -- check preview]"
                print(f"    {ref:6s} {name[:38]:38s} {a:6g} -> {b:6g}  ({rule['id']}, "
                      f"{rule['origin']}){flag}")
        if unchecked:
            easy = [u for u in unchecked if re.search(r"lcsc|easyeda|jlc", u[3], re.I)
                    or re.search(r"_lib$", u[3])]
            print(f"  no rule, orientation matters -- check these in JLCPCB's preview "
                  f"({len(unchecked)}):")
            for ref, name, side, lib in unchecked:
                note = "  (project/EasyEDA library: usually already JLC's zero)" \
                    if (ref, name, side, lib) in easy else ""
                print(f"    {ref:6s} {side:6s} {lib + ':' if lib else ''}{name}{note}")
        by = {}
        for ref, why in skipped:
            by.setdefault(why, []).append(ref)
        for why, refs in by.items():
            print(f"  skipped, {why}: {', '.join(sorted(refs, key=_natural))}")
    return out, applied, unchecked, skipped


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcb")
    ap.add_argument("-o", "--output", help="CSV path (default: <project>/jlc/<board>_cpl.csv)")
    ap.add_argument("--rules", help="project rules (default: <project>/jlc_rotations.json)")
    ap.add_argument("--all", action="store_true", help="include parts without an LCSC field")
    a = ap.parse_args()
    pcb = os.path.abspath(a.pcb)
    proj = os.path.dirname(pcb)
    base = os.path.splitext(os.path.basename(pcb))[0]
    out = a.output or os.path.join(proj, "jlc", f"{base}_cpl.csv")
    rules = [(a.rules or os.path.join(proj, "jlc_rotations.json"), "project"),
             (DEFAULTS, "default")]
    export(pcb, out, rules, include_all=a.all)


if __name__ == "__main__":
    sys.exit(main())
