# kicad-claude-skills

Two [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) that
generate KiCad files from Python and then prove the result with KiCad's own tools.

| Skill | What it does | Oracle |
|---|---|---|
| [`kicad-schematic-layout`](skills/kicad-schematic-layout/SKILL.md) | Draws `.kicad_sch` files that read like a drafted schematic: hierarchical sheets, power ports, real wires and junctions, buses, titled functional blocks, on-sheet notes, ready-made subcircuits. Lints the spec before drawing. | `kicad-cli sch export netlist` must match the spec pin for pin; ERC; a text-collision check read from KiCad's own render |
| [`kicad-pcb-placement`](skills/kicad-pcb-placement/SKILL.md) | Places footprints on a `.kicad_pcb` from the netlist using datasheet-derived recipes (regulator loops, decoupling, charger priority order, M.2, modules), with a reviewable floor plan, keep-outs, double-sided boards, outline, holes, silkscreen tidy, BGA fan-out, stackup and net classes, and a JLCPCB placement file. | a recipe audit (every distance limit, pass/fail) and `kicad-cli pcb drc`, split into placement problems and library-footprint problems |

The idea behind both: **the generator is never trusted.** Every run ends by asking
KiCad what was actually drawn or placed, and a mismatch is an error, not a warning.
When KiCad behaves unexpectedly, the answer is a small probe script and a note in
`references/troubleshooting.md` — those files are the most valuable part of this repo.

The libraries also work without Claude: `schlib.py` and `pcblib.py` are plain Python
modules you can import from your own generator scripts.

## What's inside

```
skills/
  kicad-schematic-layout/
    SKILL.md                     workflow, API summary, gotchas
    scripts/schlib.py            the schematic writer and its checks
    scripts/subcircuits.py       LDO, USB-C sink, ESP32-S3 core, MT3608 boost,
                                 INA226 4-20 mA input, I2C pull-ups, LED
    scripts/calibrate_text.py    measures glyph widths from KiCad's SVG output
    scripts/probe_*.py           probes that settled rotation, field and
                                 differential-pair naming behaviour
    references/                  file-format notes, layout conventions,
                                 troubleshooting, a minimal worked example
    assets/view.html
  kicad-pcb-placement/
    SKILL.md
    scripts/autoplace.py         netlist + recipe driven placer, floor plan, audit
    scripts/recipes.py           the placement rules, each with its source
    scripts/pcblib.py            board builder: outline, holes, keep-outs, schematic
                                 linking, reference tidy, stackup, net classes,
                                 fan-out, DRC split, render
    scripts/jlc_cpl.py           JLCPCB CPL export with sourced rotation corrections
    references/                  placement conventions, recipe evidence, stackup and
                                 impedance, pcbnew API notes, troubleshooting, and six
                                 worked examples from a fabricated board
```

The worked placement/routing/fab examples come from the
[USB-C to M.2 Key-E adapter](https://github.com/Diode663/USBC_M2E_HaLow_Adapter),
a 30-part 2-layer board generated end to end with these skills and sent to JLCPCB.
That repo shows the skills' output in a complete project.

## Requirements

- **KiCad 10** (developed against 10.0.6). The writers emit KiCad 10's native file
  format and check it against `kicad-cli sch upgrade` / a pcbnew round trip.
- **KiCad's bundled Python** for anything that imports `pcbnew`
  (`autoplace.py`, `pcblib.py`, the probes):
  - Windows: `"C:\Program Files\KiCad\10.0\bin\python.exe"`
  - Linux: `/usr/lib/kicad/bin/python3` (or the system Python if `import pcbnew` works)
  - macOS: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3`
- `kicad-cli` on the path or in the default install location.

Developed and tested on Windows 11 only. The scripts also look for KiCad in the usual
Linux and macOS locations, but that has not been run there.

## Install

Copy (or symlink) the two skill folders into your Claude Code skills directory:

```bash
git clone https://github.com/Diode663/kicad-claude-skills
cp -r kicad-claude-skills/skills/* ~/.claude/skills/
```

On Windows PowerShell:

```powershell
git clone https://github.com/Diode663/kicad-claude-skills
Copy-Item -Recurse kicad-claude-skills\skills\* $env:USERPROFILE\.claude\skills\
```

For one project only, put them in `<project>/.claude/skills/` instead. Claude picks
them up when you ask for a schematic to be generated or cleaned up, or for parts to
be placed on a board.

## Try it without Claude

```bash
mkdir demo
python skills/kicad-schematic-layout/references/example_minimal.py --project demo
```

This writes a one-sheet schematic (connector, AMS1117, LM358 amplifier) from stock
libraries, exports KiCad's netlist, and reports whether it matches the spec, the ERC
result, and any crowded text.

## Status and caveats

- Personal tooling, shared as-is. It has produced one fabricated board and several
  in-progress ones; it has not been tested across a wide range of designs.
- Placement recipes cover the parts I have needed (buck/boost regulators, LDOs,
  BQ25792 charger, USB hub, codec, GNSS, M.2 sockets, compute modules on mezzanine connectors). A part
  without a recipe needs one written from its datasheet's layout section.
- Impedance widths in `references/stackup-impedance.md` are starting points — confirm
  with your fab's calculator.
- JLCPCB rotation corrections carry a `verified` flag and a source. Bottom-side
  correction sign is not yet confirmed on a real order; the exporter flags those rows.
- LCSC part numbers in `subcircuits.py` come from my own boards' BOMs; check stock and
  suitability yourself.

## Acknowledgements

Some checks and sourced facts (not code) were prompted by reading
[mixelpixx/Konnect](https://github.com/mixelpixx/Konnect); the individual sources are
cited where they are used. Built with [Claude Code](https://claude.com/claude-code).

## License

[MIT](LICENSE).
