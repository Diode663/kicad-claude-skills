# autoplace.py — configuration and behaviour

`scripts/autoplace.py` places a whole board from a short brief. The brief is a
`placement_config.py` in the project folder. Everything else — which parts
belong together, how close, in what order — comes from the netlist and the
recipes.

## Commands

Run with KiCad's Python, from the skill's `scripts/` folder or a copy of it
in the project:

```bash
python autoplace.py PROJECT --selftest     # prove the geometry model on this board
python autoplace.py PROJECT --audit        # score the board's current placement against the recipes
python autoplace.py PROJECT --floorplan    # solve and draw floorplan.svg + floorplan.json; board untouched
python autoplace.py PROJECT --place        # solve, write the board, link, check, save, DRC, render
```

`--place` refuses to overwrite a board edited in KiCad since the last write
(pcblib's manifest). Port the edits (or lock the parts) and add `--force`.

## placement_config.py

| Key | Type | Meaning |
|---|---|---|
| `NAME` | str | board/schematic basename |
| `BOARD_SIZE` | (w, h) mm | rectangular outline |
| `ORIGIN` | (x, y) mm | where the board's top-left sits on the page (default 100, 50) |
| `CORNER_RADIUS` | mm | outline corner radius |
| `HOLES` | [(x, y, drill)] | mounting holes |
| `HOLE_KEEPOUT` | mm | clear diameter around each hole, both sides (screw head / standoff) |
| `FIXED` | {ref: (x, y, rot, side)} | parts that never move: connectors on edges, modules |
| `DEFAULT_SIDES` | ["bottom", "top"] | sides a block may use, in no particular order |
| `SIDE_COST` | {"top": 40.0} | per-block penalty for using a side (scaled by √parts); makes one side preferred without forbidding the other |
| `SIDE_RULES` | {ref: [sides]} | per-part override; the block containing that part obeys it |
| `REGIONS` | {anchor ref: (x0, y0, x1, y1)} | soft preference for where a block's anchor lands |
| `KEEPOUTS` | [dict(name, side, rect, allow)] | `allow`: `"none"`, `"low_passives"` (C/R/FB under 4 mm), a list of refs, or a function(part) |
| `PART_GAP` | mm | clearance between parts inside a block (default 0.25) |
| `BLOCK_GAP` | mm | clearance between blocks — routing room (default 0.6) |
| `BLOCK_ENVELOPE` | bool | reserve each block's whole bounding box (default True: blocks never interleave, reads as a floor plan; False packs denser) |
| `EDGE_MARGIN` | mm | keep parts this far inside the outline (default 0.5) |
| `NET_WEIGHTS` | {regex: weight} | override net weights (ground 0, rails 0.3, signals 1, diff pairs 3, switch nodes 4) |
| `EXTRA_RECIPES` | [Recipe] | project-specific recipes, tried before the built-ins |
| `REF_SIZE`, `REF_THICKNESS` | mm | designator text size and line width (default 0.8 / 0.15; JLCPCB's legend minimum is 1.0 / 0.15) |
| `REF_HIDE` | (refs) | designators taken off the silkscreen where no legal spot exists; the config always wins |
| `REF_RETIDY` | (refs) or `"all"` | hand these designators back to the tidy step for one run (see below) |

Module keep-outs are automatic: when two 100-pin DF40 connectors are FIXED
on the same side, the module outline under them (from the module recipe in
`recipes.py`) is forbidden to every other footprint.

## Locking and re-running

1. Run `--floorplan`, open `floorplan.svg`.
2. Anything you want pinned: either add it to `FIXED`, or place it in KiCad
   and **lock** it (right-click → Lock). Locked footprints are read from the
   board and never moved.
3. Re-run. Blocks whose anchor is fixed or locked keep the anchor where it
   is and re-solve their support parts around it.
4. Repeat until the floor plan is right, then `--place`.

Re-running `--place` resets every unlocked, unfixed part. Lock what you have
tuned by hand before you run it again.

**Designators you move in KiCad are kept.** `tidy_references()` records what it wrote for
each designator in `.pcbgen-refs.json`, in the footprint's own frame. On the next run a
designator whose state differs from that record was edited by hand: it is pinned, left
exactly as it is, and the others are placed clear of it. Two things are not edits: a
designator sitting at its library position (KiCad resets them on Update PCB / Update
Footprints) and anything in `REF_HIDE`. On a board tidied before the ledger existed, call
`Board.adopt_moved_references(size, thickness, hide)` once: tidy is deterministic, so whatever
differs from a dry run of it was moved by a person. `REF_RETIDY` releases a pin.

## What the solver does

1. **Geometry model.** For each footprint: pads and courtyard in its own
   unrotated, unflipped frame, read by temporarily resetting the footprint
   on the loaded board (restored before anything else happens).
   `--selftest` checks the transform against every real pad position and
   picks the flip convention that matches (worst error must be ≤ 0.01 mm).
   Part extent = courtyard ∪ pads, because many library footprints leave
   pads outside the courtyard.
2. **Blocks.** Anchors are every non-passive part. Recipes match anchors by
   pin names. Candidates are claimed in this order: designator affinity,
   then role priority board-wide, then fallback recipes, then leftovers by
   local net (see recipes.md, "How parts get claimed").
3. **Block solve.** Anchor at the origin. Each member, in priority order, is
   searched on a 0.5 mm grid then refined at 0.1 mm, four rotations, around
   the pads it serves. Cost is pad distance to its target nets (ground
   weighted half), a pull toward the anchor, and a small penalty for 90°
   rotations so passives line up.
4. **Board solve.** Holes, FIXED/locked parts (through-hole pads block both
   sides) and keep-outs go in first. Support parts of fixed anchors are
   placed around them in board space. Free blocks are placed rigidly, most
   connected first: every position on a 1 mm grid × 4 rotations × allowed
   sides is costed by weighted half-perimeter wirelength to what is already
   placed, plus side cost, region preference and RF/switcher separation.
   Candidates are checked for room cheapest-first, and the winner is refined
   on a 0.25 mm grid. Then each block is lifted and re-placed against all
   the others until nothing improves.
5. **No rigid fit** → the block is split: the anchor is placed alone and its
   parts are placed around it one by one, where they can use fragmented space.
   Parts that still don't fit are reported, never silently dropped.
6. **Capacity report.** Free area per side against the courtyard area the
   unplaced parts need. Above roughly 50% there is too little routing room.
7. **Audit.** Every role is measured on the result and listed pass/fail.

## Outputs

- `floorplan.svg` — both sides, blocks outlined and labelled, fixed parts
  grey, keep-outs red. Convert for viewing:
  `uvx --with pymupdf python -c "import pymupdf; pymupdf.open('floorplan.svg')[0].get_pixmap(dpi=96).save('floorplan.png')"`
- `floorplan.json` — positions, block membership, audit rows.
- With `--place`: the board file, DRC split into placement and
  library-footprint problems, net spans for weight ≥ 3 nets, renders.

## Limits (be honest about them)

- Rectangular outlines only (a custom Edge.Cuts polygon is treated as its bounding box).
- Rotations are multiples of 90°.
- Heights are not modelled: "low passives" is a size proxy, not a 3D check.
- It places; it does not route. Critical routing is a separate step (see
  stackup-impedance.md).
- The search is greedy with improvement passes, not a global optimum. Review
  the floor plan; lock what is right; re-run.

### `max_height` and where the height comes from

`allow="by_height", max_height=X` admits a part only when `part_height()`
returns a number **and** that number fits. Unknown height is refused, never
assumed to fit -- the zone under an M.2 card or a module is not the place to
guess.

The height comes from the 3D model or footprint name first (easyeda2kicad
writes `-H0.6-`), then from `PACKAGE_HEIGHT` by package name. The name match
requires the number to be a whole token and to be under 25 mm, because a
manufacturer part number looks exactly like a height: `XFMR-SMD_H5007NLT` is
a Pulse H5007 transformer, and an earlier regex read it as 5007 mm tall. That
one erred safe, but the same pattern could as easily have produced `0.5` for
a part that is 3 mm tall and quietly allowed it under a card. Anything
ambiguous now returns None.

If a part you need under a card is being refused, add its package to
`PACKAGE_HEIGHT` with a *maximum* over the parts in that package, or put the
height in the footprint name. Do not raise `max_height` to make it fit.
