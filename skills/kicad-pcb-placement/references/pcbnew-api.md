# pcbnew from Python — verified facts

Everything here was checked against **pcbnew 10.0.6** by running it, not by
reading documentation. Re-run the checks after a KiCad major upgrade.

## Running

`pcbnew` ships only inside KiCad. Use KiCad's interpreter:

```bash
"C:\Program Files\KiCad\10.0\bin\python.exe" place_components.py     # Windows
/usr/lib/kicad/bin/python3 place_components.py                       # Linux
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 place_components.py
```

A `pip install pcbnew` does not exist. If `import pcbnew` fails, you are on
the wrong interpreter.

`kicad-cli` lives next to that Python and does the jobs the API can't:
netlist export, DRC, renders, gerbers.

### Don't shadow the standard library
A file called `bisect.py`, `code.py`, `types.py`, `parser.py` or similar in
the project folder shadows the real module for pcbnew's own imports too. The
symptom is a script that appears to run twice, or an import error deep in
someone else's code. Name scripts `place_components.py`, `netlist_report.py`
and so on.

## The ownership rule — read this one

**After `BOARD.Remove(item)`, the Python proxy must not be garbage
collected.**

`Remove()` detaches the item and hands ownership back to Python. When the
proxy's refcount hits zero, SWIG deletes an object the board still has
pointers to. The heap corruption that follows surfaces nowhere near the
cause:

- a later `board.Zones()` yields `SwigPyObject` with no methods —
  `AttributeError: 'SwigPyObject' object has no attribute 'GetIsRuleArea'`;
- `board.GetDesignSettings()` does the same;
- or the interpreter segfaults during shutdown, *after* the script finished,
  taking any buffered output with it.

Verified by experiment — dropping the proxy crashes, keeping it or disowning
it does not:

```python
for d in list(board.GetDrawings()):
    if d.GetLayer() == pcbnew.Edge_Cuts:
        board.Remove(d)
        d.thisown = False        # SWIG must not free it
        kept.append(d)           # and something must hold the reference
```

`Board._remove()` in `pcblib.py` does both. Route every removal through it.

This is worth internalising because the first two theories are both wrong
and both plausible: it is *not* "GetDesignSettings is broken", and it is
*not* "hold a reference to design settings" — holding that particular
reference makes things worse. The cause is always the earlier `Remove()`.

## Coordinates

- Internal units are nanometres. `pcbnew.FromMM(x)` / `pcbnew.ToMM(v)`.
- `VECTOR2I(x, y)` in internal units. Page Y points down.
- An **area** in internal units is nm², so converting to mm² means calling
  `ToMM` twice:
  ```python
  def area_mm2(poly):
      return pcbnew.ToMM(pcbnew.ToMM(poly.Area()))
  ```
- Rotation is degrees counter-clockwise: `fp.SetOrientationDegrees(90)`.
- Flip with `fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)`.
  Check `fp.IsFlipped()` first; flipping twice puts it back.

## Footprints

| What | How |
|---|---|
| all footprints | `board.GetFootprints()` |
| by reference | build `{f.GetReference(): f for f in ...}` once |
| position | `fp.SetPosition(VECTOR2I)` / `GetPosition()` |
| library id | `fp.GetFPID().GetUniStringLibId()` |
| pads | `fp.Pads()`, then `pad.GetNumber()`, `GetNetname()`, `SetNet(net)` |
| silkscreen and outline | `fp.GraphicalItems()` |
| reference text | `fp.Reference()` — a text object with position, size, angle |

**`GraphicalItems()` includes the reference and value.** They come back as
`PCB_TEXT` whose `GetText()` is usually the literal `${REFERENCE}` or
`${VALUE}`. Filter them out of any "what artwork does this footprint have"
computation, or a reference designator collides with itself and the placer
pushes it somewhere worse.

## Reloading footprints from a library

`FootprintLoad(directory, name)` takes a **directory**, not a library
nickname, so you resolve nicknames yourself from `fp-lib-table` (project
first, then the user's global one) and expand `${KIPRJMOD}` and
`${KICADn_FOOTPRINT_DIR}`.

Two things bite:

- the loaded footprint has **no library nickname**, so unless you restore it
  the board forgets where each part came from and KiCad's own library tools
  stop working on it;
- `LIB_ID.SetLibNickname()` / `SetLibItemName()` want a `pcbnew.UTF8`, not a
  Python `str`, and `LIB_ID.Parse()` can take the interpreter down.

```python
lid = pcbnew.LIB_ID()
lid.SetLibNickname(pcbnew.UTF8(nickname))
lid.SetLibItemName(pcbnew.UTF8(name))
new.SetFPID(lid)
```

`Board.refresh_footprints()` does all of this, carrying over position,
rotation, side, reference, value and the schematic link.

## Courtyards — the overlap oracle

```python
cy = fp.GetCourtyard(pcbnew.F_CrtYd)     # SHAPE_POLY_SET
if cy.OutlineCount():                    # 0 means the footprint has none
    sp = pcbnew.SHAPE_POLY_SET(cy)       # copy: the boolean ops mutate
    sp.BooleanIntersection(other_cy)     # overlap
    sp.BooleanSubtract(board_poly)       # what sticks out past the edge
```

One-argument booleans (no `PM_FAST`) in KiCad 9/10. A footprint with no
courtyard is not checked by this and not by DRC's courtyard test either —
list them for the user.

## Pads

- `pad.GetEffectivePolygon(layer)` — **the layer argument is required** in
  KiCad 10. Pick `F_Cu` or `B_Cu` from `pad.IsOnLayer(...)`.
- `pad.IsOnCopperLayer()` to skip mechanical pads.
- Pad numbers are strings, and not always numeric: a USB-C footprint has
  `A1B12`, `B4A9`.

## Zones and rule areas

```python
z = pcbnew.ZONE(board)
z.SetIsRuleArea(True)
z.SetZoneName("ANTENNA")
z.SetDoNotAllowTracks(True)
z.SetDoNotAllowVias(True)
z.SetDoNotAllowPads(True)
z.SetDoNotAllowFootprints(False)     # let the module overhang its keep-out
for s in ("SetDoNotAllowZoneFills", "SetDoNotAllowCopperPour"):
    if hasattr(z, s):                # the name changed between versions
        getattr(z, s)(True)
ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu)
z.SetLayerSet(ls)
z.Outline().NewOutline()
for x, y in points:
    z.Outline().Append(x, y)         # internal units
board.Add(z)
```

## Board edge

`PCB_SHAPE` with `SHAPE_T_SEGMENT`, `SHAPE_T_RECT` or `SHAPE_T_ARC` on
`pcbnew.Edge_Cuts`. For an arc, set `SetCenter`, `SetStart`, `SetEnd`.
Rounded corners are four arcs plus four segments; clamp the radius to half
the shorter adjacent edge or the geometry crosses itself.

## Holes

KiCad has no standalone hole object — a hole is a pad, and a pad needs a
footprint. Build a one-pad footprint:

```python
pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)     # or PAD_ATTRIB_PTH
pad.SetLayerSet(pad.UnplatedHoleMask())      # or pad.PTHMask()
pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_CIRCLE)
```

For NPTH the pad size equals the drill size and the number is empty.

## Fiducials

Optical targets for the assembler's pick-and-place. `Board.fiducial(x, y)`
loads `Fiducial:Fiducial_1mm_Mask2mm` from KiCad's stock library, so the
project's `fp-lib-table` needs a `Fiducial` entry:

```
(lib (name "Fiducial")(type "KiCad")(uri "${KICAD10_FOOTPRINT_DIR}/Fiducial.pretty")(options "")(descr "KiCad fiducial markers"))
```

Like a hole, a fiducial has no schematic symbol, so it is marked board-only
and kept out of the BOM and the position file — otherwise it turns up as an
orphan designator and JLCPCB's PCBA upload rejects the CPL.

Placement, from the assemblers' own guidance: **three** in an **L** (two is
the minimum), as far apart as the layout allows, and at least **3.85 mm**
from the board edge for a 1 mm fiducial. On a crowded board the constraint
that actually binds is the 1.25 mm courtyard against neighbouring parts —
search for spots rather than assuming the corners are free.

## Symbol links and nets

A footprint with no `path` is a stranger to the schematic: *Update PCB from
Schematic* treats it as new and can move or duplicate it.

```python
fp.SetPath(pcbnew.KIID_PATH("/" + symbol_uuid))
fp.SetSheetfile("board.kicad_sch"); fp.SetSheetname("/")
net = board.FindNet(name) or pcbnew.NETINFO_ITEM(board, name)   # Add() if new
pad.SetNet(net)
```

Get the UUIDs and the net map from KiCad itself, never by parsing the
schematic yourself:

```bash
kicad-cli sch export netlist --format kicadxml -o out.xml board.kicad_sch
```

`<comp ref="U1"><tstamps>…` is the symbol UUID; `<net name=…><node ref= pin=…>`
is the connectivity.

## DRC output

```bash
kicad-cli pcb drc --format json -o drc.json board.kicad_pcb
```

Top-level keys: `violations`, `unconnected_items`, `schematic_parity`,
`ignored_checks`, `included_severities`.

Each violation has `type`, `severity`, `description` and `items`; each item
has a `description`, a `pos` and a `uuid`. Item descriptions name their
owner after `" of "`:

```
"PTH pad [<no net>] of U1"
"Pad A1B12 [GND] of J1 on top_cu"
"Segment of U1 on F.Silkscreen"
"Rectangle on Edge.Cuts"          <- no owner: a board-level object
```

That is enough to classify: **all items owned by one footprint and none
board-level ⇒ the violation is inside that library footprint**, and moving
the part cannot fix it. Anything else is placement.

`schematic_parity` must be empty; it is the check that your symbol links
worked.

## Renders

```bash
kicad-cli pcb render -o top.png --quality high --side top BOARD.kicad_pcb
kicad-cli pcb render -o iso.png --rotate -30,0,25 --perspective BOARD.kicad_pcb
kicad-cli pcb export svg -o flat.svg --page-size-mode 2 --exclude-drawing-sheet \
    --layers F.Cu,F.Silkscreen,Edge.Cuts BOARD.kicad_pcb
```

The board file is a **positional** argument and must come last. Passing it
with a flag, or omitting it, fails with
`input: 1 argument(s) expected. 0 provided.`

## Native file format

`BOARD.Save()` from pcbnew 10.0.6 writes the current board format
(`20260206`), but a file saved from Python can still differ from what KiCad
would save:

- **Fields added from Python have no stroke width.** `fp.SetField(name, value)`
  creates a field whose text thickness is 0; KiCad 10 writes `(thickness 0.15)`
  on its next save, so the board shows as changed although nobody edited it.
  Set `fld.SetTextThickness(pcbnew.FromMM(0.15))` for every field whose
  thickness is ≤ 0. `Board.save()` does this for all footprints.
- **Check, don't assume.** `Board.check_native()` (run by `save()`) lets
  `kicad-cli pcb upgrade --force` rewrite a temp copy and diffs it against the
  saved file. 0 lines means KiCad opens and saves the board unchanged.

## Output hygiene

pcbnew prints `swig/python detected a memory leak of type 'PCB_SHAPE *'` and
similar at interpreter exit. They are harmless, they go to **stdout**, and
they will corrupt any report you capture. Filter them (`grep -v "memory
leak"`), and print with `flush=True` so a shutdown-time crash cannot swallow
output the script already produced.
