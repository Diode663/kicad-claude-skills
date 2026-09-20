---
name: kicad-pcb-placement
description: Lay out a KiCad PCB (.kicad_pcb) from Python — netlist-driven automatic placement using datasheet-derived recipes (regulator loops, decoupling, charger priority order, hub/codec/GNSS/M.2 rules), a reviewable floor plan with lock-and-rerun, module keep-outs (compute modules / SoMs on mezzanine connectors), double-sided boards, board outline, mounting holes and tidy silkscreen, decoupling rows for hand-written tables, BGA dog-bone fan-out, scripted and hybrid routing (critical nets from a script, Freerouting for the rest, ground pours, a reproducible result), and a JLCPCB placement (CPL) file with sourced rotation corrections — and prove it with a recipe audit and KiCad's own DRC. Use this whenever the user wants parts placed or arranged on a board ("come up with a sensible PCB layout", "place the components", "floorplan this board", "where should the regulator go", "make the board smaller", "score this placement"), wants placement copied from reference designs or datasheet layout guidance, asks to route a board or whether an autorouter is available ("route the board", "can you autoroute this", "pour ground"), or wants code that writes .kicad_pcb files — even if they never say "placement". Also use it to fix an existing layout's overlaps, silkscreen or board size, or to plan stackup and controlled-impedance widths before routing. Not for drawing schematics (the kicad schematic layout skill does that) and not for judging whether a circuit is electrically sound (the kicad review skill does that).
---

# KiCad PCB placement

Placement is where a board is won or lost. Routing can only connect what
placement already put within reach: a buck converter whose input cap is
30 mm from its ground pin cannot be saved by clever traces.

The usual failure is a *pile*: parts dropped by a packer into whatever gap
fits, a regulator 50 mm from its own inductor, and a board that passes DRC
and is unroutable. A packer-style version of this skill did exactly that on a
126-part double-sided module carrier: **0 of 76** datasheet placement rules
met. The recipe-driven placer below meets **67 of 76** on the same board, and
on an unrelated 34-part single-sided board it meets 18 of 20 where a
hand-written table met 3.

What you get:

- **`scripts/autoplace.py`** — the placer. Reads the netlist, groups parts
  into functional blocks with recipes, solves each block, places blocks on
  the board, audits every rule, draws a floor plan, writes the board.
- **`scripts/recipes.py`** — the rules: which support parts belong to which
  IC (found by pin names, not designators), how close, in what order, with
  the datasheet or measured reference board behind each number.
- **`scripts/pcblib.py`** — the board builder underneath: outline, holes,
  keep-outs, schematic linking, reference tidying, geometry checks, save
  guard, DRC split, net report, render, `decoupling_row()` and `fanout()`.
- **`scripts/jlc_cpl.py`** — the JLCPCB placement file, with rotation
  corrections kept in a sourced JSON table instead of hand edits.
- **References** for the conventions, the recipes' evidence, stackup and
  impedance, the pcbnew API, and troubleshooting.

You place, and on request route the critical nets. Unless the user asks,
you do not route the rest.

## Workflow

### 0. Don't clobber the user's work
`--place` rewrites every unlocked footprint position. Before running it:
- does the board have tracks? `check()` warns;
- has the `.kicad_pcb` changed since the script last wrote it? `save()`
  refuses and backs it up to `.pcbgen-backup/`.

Ask the user to **lock** hand-placed parts in KiCad (they are then never
moved) or port them into `FIXED`, then use `--force`. Say in the handover:
re-running resets every unlocked position.

### 1. Make sure the board matches the schematic
No board yet? `pcblib.create_board(dir, name, layers=2)` writes an empty one, and
`sync_footprints()` fills it; stock KiCad footprint libraries resolve by nickname without
being listed in the project's `fp-lib-table`.

Footprints must be on the board and current. If symbols were added, removed
or had their footprint changed, sync first (delete orphans, swap changed
footprints keeping position, add new ones parked off-board, link symbols).
`Board.sync_footprints()` adds; swapping and deleting are in the pattern of
a project `sync_pcb.py` — never place onto a stale board.

### 2. Read the datasheets and settle the brief
Before coordinates, collect what the netlist cannot tell you. Ask the user
when you don't know:

- outline and holes (enclosure? Pi-HAT? can it grow?);
- which side(s) — single-sided is cheaper, but a module carrier is usually
  double-sided;
- what must sit on an edge (connectors, antenna feeds, buttons);
- modules with no clearance underneath (a compute module on 1.5 mm mezzanine
  connectors leaves none);
- card areas (M.2 on a 4.2 mm socket: only **0.90 mm** parts under the card -- the card's underside
  is 2.52 mm up but its own bottom parts take most of that; which side of the socket the card leaves
  from, and where its standoff goes, are in `references/recipes.md`);
- stackup and which nets are controlled-impedance
  (`references/stackup-impedance.md`).

Check that every major IC has a recipe (`references/recipes.md`). If one
doesn't, read its datasheet's layout section and add a recipe — match on pin
names, cite the section, and measure a reference board if one exists.

### 3. Write placement_config.py
A short, commented brief in the project folder. Every key is documented in
`references/autoplace.md`; `references/example_placement_config_m2_adapter.py`
is a real one (USB-C to M.2 adapter: fixed regulator block, card keep-out by
height, edge connectors).

Copy `scripts/autoplace.py`, `recipes.py` and `pcblib.py` into the project
so it stays self-contained.

### 4. Prove the model, score what exists
```bash
"C:\Program Files\KiCad\10.0\bin\python.exe" autoplace.py PROJECT --selftest
"C:\Program Files\KiCad\10.0\bin\python.exe" autoplace.py PROJECT --audit
```
`--selftest` must report a worst pad error ≤ 0.01 mm, and must have seen at
least one bottom-side part before you trust double-sided output. `--audit`
prints the block list (check the claims make sense — a GNSS cap claimed by
an M.2 socket means a recipe or the sheet rule needs attention) and scores
the current placement: that number is the baseline you have to beat.

### 5. Floor plan — the first review stop
```bash
... autoplace.py PROJECT --floorplan
```
Writes `floorplan.svg` and `floorplan.json` and leaves the board untouched.
Convert the SVG to PNG and **look at it**, then send it to the user with:
- the capacity line (free area per side vs part area — above ~50% there is
  no routing room, and no solver fixes that);
- the audit result and every failing rule;
- blocks that landed somewhere the brief did not intend (wrong side, far
  from their connector, RF next to a switcher);
- the decisions only the user can make (grow the board, move a module,
  allow the other side).

Adjust the config or lock parts in KiCad, re-run, until the user approves.

### 6. Place — the second review stop
```bash
... autoplace.py PROJECT --place
```
Writes positions, links symbols and nets, tidies references, runs
`check()`, saves, runs DRC and renders. Then:

- **Recipe audit**: every limit, pass/fail. Report failures by name.
- **DRC, two halves**: placement/board-level (yours — target zero) versus
  inside one footprint (library defects: fix the `.kicad_mod`, then
  `refresh_footprints()`). `unconnected items` equals the pin count until
  routed — say so. `schematic parity` must be zero.
- **Net spans**: switch nodes a few mm, diff pairs short and direct.
- **Render and look**: blocks read as blocks, connectors reachable, RF clear,
  references readable.

### 6b. Hand-written tables: decoupling rows
On a small board placed from a hand-written table (a project
`place_components.py`), `Board.decoupling_row(ic)` does what the recipes do
for autoplace: each capacitor goes just outside the IC's courtyard beside
the pin it serves, rail pad toward the pin, ground pad away, same side.
A rail's capacitors are dealt out over its pins smallest first, so every
pin gets a small one before any pin gets a second; extras stack outward.
Call it after `place_all()` (it overrides the caps' table entries), pass
`caps=[...]` to name them, and read the printed plan -- then `check()`.

### 7. Critical routing (only when asked)
Placement and critical routing overlap: a regulator loop is placement *and*
copper. When the user asks for it:
- confirm impedance widths with the fab's calculator first
  (`references/stackup-impedance.md` — the listed widths are unconfirmed);
- set net classes: 90 Ω USB/PCIe, 100 Ω Ethernet, power widths by current;
- **check that KiCad can see the pairs**: `b.check_diff_pairs()` asks
  pcbnew's own `DpCoupledNet()` which nets it has coupled. KiCad infers pairs
  from the two names — only `P`/`N` or `+`/`-`, upper case, at the end — so
  `USB_DP`/`USB_DM` and `TD1P`/`TD1M` are not pairs, and on an unrecognised
  pair `diff_pair_width` never fires (it is gated on `A.inDiffPair()` in
  `drc_engine.cpp`). Both legs then route at the class's ordinary
  `track_width` and nothing complains. Fix the names in the schematic and
  re-sync; renaming on the board is undone by the next netlist import;
- route differential pairs as coupled traces, match within the pair, add a
  ground via beside every signal via;
- give switching loops and battery paths pours, not thin traces;
- run DRC and report lengths. Leave the rest to the user or an autorouter
  (Freerouting has no differential-pair support).
- **Routing a whole board: `scripts/routelib.py`** (copy it beside the project's `pcblib.py`).
  `Router.pad(ref, n, expect=, net=)` asserts placement and netlist, `track` / `via` / `zone` /
  `zone_connection` draw, `clear()` makes the script idempotent, `drc_report()` lists every DRC item
  with its board-mm position, and `report()` says what DRC does not: how many pieces each pour is
  in, every far-layer track (each one cuts that pour), via-in-pad, pair skew.
- **Hybrid routing** is the default offer when the user asks for an autorouter: script the nets
  where geometry *is* the design (switching loops, Kelvin taps, USB entry, power trunks, thermal
  copper, every ground via and the pours) and give the rest to Freerouting with
  `Router.autoroute(scripted_nets)`. Its result is stored in `routing/autoroute.json` and drawn by
  `Router.replay()`, so ordinary runs are byte-identical and need no Java; `--autoroute` re-rolls it.
  Freerouting is not bundled: it needs
  the jar and a Java 25 runtime in `~/tools/freerouting` (or `FREEROUTING_HOME`) -- downloading them
  is the user's call, ask first. The traps between KiCad 10 and Freerouting 2.4 are handled inside
  `autoroute()` and listed in `references/troubleshooting.md`, "Hybrid routing".
  **Always look at a picture of an autorouted result.** DRC passed a 25 mm detour under a module, a
  dangling stub and a far-layer pour cut in two; `report()` and your eyes catch those.
- **Routing changes placement.** Expect to move parts once copper shows the problem, and do it in
  the placement table, not by bending tracks: see "Placing for routability" in
  `references/placement-conventions.md`.
- **A whole small board can be routed from a script**: `references/example_route_board.py`
  (2-layer, USB 2.0 pair, SOT-23-6 buck, ground pours; fabricated). The pattern that made it
  safe: every pad the router starts or ends on is asserted to be where the script expects,
  and **every part it touches is in `FIXED`** -- adding a test pad moved two auto-placed
  capacitors and the 3.3 V track went straight through their new pads. It is idempotent
  (removes and redraws tracks, vias and zones, keeping removed proxies alive), offsets a
  centre line into a constant-gap pair with mitred corners, sets solid zone connection on
  the regulator's ground pads, and is followed by `Board.check_via_in_pad()`: DRC is silent
  about a same-net via inside an SMD pad, and a stitching via landed in a connector's
  hold-down tab.
- USB-C receptacle pads run B7(N) A6(P) A7(N) B6(P): joining the flip pairs costs one
  crossing per net. Two 1.3 mm far-layer straps as side branches, not in the signal path,
  keep the ground under the pair whole. A USBLC6-2 at 180° is flow-through (D+ over the top
  row, D− over the bottom); its VBUS pin then sits between the lines -- see the schematic
  skill's `UsbCSink(esd_vbus=False)`.
- **BGA escape**: `Board.fanout("U1")` puts a dog-bone on every inner ball:
  a short track to a via in the gap diagonally outward, quadrant by
  quadrant so no two balls want one site. The outer `rings=1` ring is left
  for top-layer escape. Via, drill, track and clearance default to the
  board's own minimums (Board Setup > Constraints, floored at
  0.45/0.2/0.1/0.1 mm), and it refuses when the via cannot fit between four
  balls -- 0.5 mm pitch and below needs via-in-pad, a fab option. Re-running
  replaces its earlier fan-out. DRC then shows one `via_dangling` per via
  until the board is routed; that is expected, say so.

### 7b. JLCPCB placement file
`references/example_build_package.py` builds the whole upload (gerbers + drill on the same
origin as the CPL, BOM, CPL, order notes) behind a DRC gate, and refuses when the BOM and
CPL designators differ, which JLCPCB rejects. Parts with no LCSC number (holes, fiducials,
test pads) stay out of both.

```bash
python jlc_cpl.py board.kicad_pcb            # -> jlc/<board>_cpl.csv
```
Rows are the footprints with an LCSC field, never DNP ones, positions from
the board corner (`pcblib` sets the aux origin there). Rotation corrections
come from `references/jlc_rotations.json` (skill defaults: two, each with its
source) and `<project>/jlc_rotations.json` (project rules and per-designator
`overrides`); a designator override beats a project rule beats a default.
The report lists every correction applied and every polarised or multi-pin
part with no rule -- those are the ones to look at in JLCPCB's Component
Placements preview before paying. Parts from an EasyEDA/LCSC-derived
library are usually already at JLC's zero; stock KiCad footprints are
where corrections are needed.

- **Derive corrections with `scripts/jlc_compare.py board.kicad_pcb`**: it fetches JLCPCB's
  footprint for every polarised part's LCSC number, matches it to the board's pad number by
  pad number at 0/90/180/270, and prints the correction, the origin offset, the worst-pad
  fit and ready-to-paste `overrides`. On a 47-part board: every EasyEDA-derived footprint at
  zero, the four stock SOT-23 / SOT-223 at +180. It cannot see the polarity of two-pad
  parts (it matches pad 1 to pad 1): confirm from their silk or symbol. The EasyEDA API
  refuses requests after a burst of ~25; the tool caches and paces, so just run it again.
- **Check each part's "PCBA Type" on jlcpcb.com/partdetail/Cxxxx before choosing Economic.**
  Radio modules are often "Standard Only" (ESP32-S3-WROOM-1U-N4 is); one such part moves the
  whole order to Standard. Through-hole parts show "Wave Soldering".
- **Check manufacturer names against LCSC, not memory** (`wmsc.lcsc.com/ftps/wm/product/detail?
  productCode=Cxxxx` returns `brandNameEn`, `productModel`, `stockNumber`): a BOM whose LCSC
  numbers were all right still had 9 of 34 manufacturers wrong.
- **JLCPCB's zero orientation belongs to their footprint for each LCSC part, not to the
  package name.** Two SOT-23-6 parts on one board needed +270 and +180. Derive it: fetch
  their footprint (`uvx easyeda2kicad --lcsc_id Cxxxx --footprint`), find the KiCad rotation
  that puts KiCad's pad 1 where theirs is, and the correction is minus that; write it as a
  per-designator override. The same comparison gives a connector's origin offset.
- Add a rule only from evidence -- a preview that showed the part turned, or
  a published comparison -- and record the `source`. A wrong rule rotates a
  part that was right.
- Set `"verified": true` once a correction was confirmed on one of the
  user's own orders; unverified ones are flagged on every export.
- Bottom-side corrections are applied with the sign reversed (KiCad reports
  rotation seen from the top, JLCPCB looks from below). That is not yet
  confirmed on an order, so each corrected bottom row is flagged -- check
  it on the first double-sided order and write down what the preview showed.

### 8. Deliver
- The floor plan and the top/bottom renders.
- The audit table and the DRC summary, library problems listed separately.
- What was fixed, locked, and assumed.
- The re-run command, and the warning that it resets unlocked parts.
- What you did not do.

## Placement rules in brief
Details, reasons and sources: `references/placement-conventions.md` and
`references/recipes.md`.

1. One function, one patch of board. The recipes define the functions.
2. Switching loop first: input cap → switch → inductor → output cap, as
   small as the footprints allow. Feedback on the quiet side.
3. Decoupling at the pin it serves, same side, smallest value nearest.
4. Follow a datasheet's layout priority list literally when it has one
   (BQ25792 does).
5. Connectors on edges with their mating face flush; ESD arrays beside them,
   in line.
6. RF (GNSS, radios) away from switchers; antenna feeds short, 50 Ω, ground
   fenced.
7. Nothing under a module that sits flush; only low passives under cards.
8. Through-hole pads occupy both sides — never put a bottom part under one.
9. Leave routing channels. Capacity above ~50% is a board-size problem.
10. Like parts aligned, references readable, polarity marks visible.

## pcbnew facts you will need
Full notes in `references/pcbnew-api.md`.

- Use **KiCad's own Python**: `"C:\Program Files\KiCad\10.0\bin\python.exe"`.
- **After `board.Remove(item)`, never let the proxy be collected** —
  `Board._remove()` disowns and keeps it.
- A bottom footprint's geometry is mirrored then rotated; which axis depends
  on the flip call. **Don't reason about it — `--selftest` measures it.**
- Pad numbers repeat (connector shells, EP) — compare pads by order, not by
  number, when checking geometry.
- `GetCourtyard(layer)` is a real polygon; many footprints leave pads outside
  it, so extent = courtyard ∪ pads.
- The netlist XML (`kicad-cli sch export netlist --format kicadxml`) carries
  pin names (`libparts`) and each symbol's sheet (`sheetpath`) — both drive
  the recipes.
- `kicad-cli pcb render` takes the board as the **last positional** argument.
- pcbnew prints `swig/python detected a memory leak` at exit: harmless, on
  stdout — filter it.

## Gotchas that cost hours
Each is explained in `references/troubleshooting.md`.

- Placing onto a board whose footprints are out of date with the schematic.
- A packer "with no overlaps" still loses every loop — measure with `--audit`.
- Rail capacitors are ambiguous; designator affinity and schematic sheet are
  the tie-breakers, and the block list must be read, not assumed.
- A keep-out that forbids footprints flags the part it protects: forbid
  copper, allow the module.
- Silkscreen below 0.15 mm gives one DRC warning per part.
- Adding one part can move every auto-placed part. Anything a later script depends on
  (a router, a clearance check) belongs in `FIXED`, with its pad positions asserted.
- Designators the user moves in KiCad are kept by `tidy_references()` (ledger
  `.pcbgen-refs.json`); never edit a board file while KiCad has it open (`~*.lck`).
- Measure, don't reason: when pcbnew behaves oddly, isolate one call.

## Worked examples
- `references/example_placement_config_m2_adapter.py`, `example_route_board.py`,
  `example_setup_fab_jlc_2layer.py`, `example_m2_clearance.py`, `example_build_models.py`,
  `example_build_package.py` — the USB-C to M.2 Key-E adapter, 30 parts, 2 layers,
  generated end to end and sent to JLCPCB: hand-placed Figure 36 buck, card keep-out by
  height, scripted routing, JLCPCB rules and package. They import that project's
  `placement_config`; read them as patterns, not as a library.
