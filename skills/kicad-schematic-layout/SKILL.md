---
name: kicad-schematic-layout
description: Generate or redraw KiCad schematics (.kicad_sch) from Python so they look professionally drafted — hierarchical sheets, power-port symbols, real wires and junctions, buses, titled functional blocks (one per circuit, outlines derived from their contents so nothing lies across a box line), on-sheet design notes, ready-made subcircuits (LDO, USB-C sink, ESP32-S3 core, MT3608 boost, INA226 4-20 mA input, I2C pull-ups, LED) — lint the spec against design rules before drawing, and prove the result against KiCad's own netlist. Use this whenever the user wants a KiCad schematic created from a circuit description, BOM or netlist; wants an existing generated or netlist-style schematic made readable ("fix the stacked labels", "clean up", "relayout", "make it look professional", "split into sheets", "too many group boxes", "text is on the box lines"); or wants code that writes .kicad_sch files — even if they never say "generate". Not for PCB layout or for judging whether a circuit is electrically sound (the kicad review skill does that); part sourcing belongs to the lcsc/bom skills.
---

# KiCad schematic layout

Drawing a schematic in code is easy. Drawing one that reads like a
professional's is not. The usual failure is a *netlist-style* sheet:
floating symbols, every pin a stub with a label stacked on it, rails
spelled out as text, a small drawing lost on a big page. It is electrically
correct and unreadable.

This skill gives you six things:

- **`scripts/schlib.py`** — a builder. You place parts, draw wires, power
  ports, labels and notes in sheet coordinates, and declare which functional
  block each belongs to; it draws the outlines, arranges the blocks, and
  **spaces the drawing out for you**. It emits **native KiCad 10 files**
  (format `20260306`, no "older version" prompt), including hierarchy, checks
  the geometry as it goes, and after every write proves each sheet is exactly
  what KiCad 10 itself would save (`Design.check_native()`). KiCad 9 cannot
  open the output.
- **A spacing engine that measures instead of guessing.** Glyph advances come
  from KiCad itself (`scripts/calibrate_text.py`), so schlib knows the exact
  width of every string it draws. It then bundles repeated power pins onto one
  bar, stands rotated port names upright, pushes wire-connected groups apart
  and slides labels along their own stubs until nothing crowds -- without
  changing a single connection.
- **A method** in which correctness is never taken on trust: write the
  netlist spec first, draw to match it, then let KiCad's own connectivity
  engine prove the drawing matches, and KiCad's own render prove the spacing.
  Where schlib has to reimplement a KiCad rule, a probe script proves the
  copy is the same rule: `scripts/probe_diffpair.py` checks the
  differential-pair name matcher against pcbnew's `DpCoupledNet()` over
  2317 names, `scripts/probe_fields.py` and `scripts/probe_transforms.py`
  do the same for field rotation.
- **`scripts/block_crops.py`** — one full-size picture per block for the
  visual pass, because a whole sheet in one screenshot shows nothing.
- **`scripts/subcircuits.py`** — proven blocks (regulator, USB-C, ESP32-S3,
  boost, loop input...) that each carry their PARTS, NETS and drawing, so
  the spec and the sheet come from one place.
- **The drafting rules and file-format facts**, learned the hard way on real
  boards, in `references/`.

## Workflow

### 0. Don't clobber the user's work
Before you write over an existing `.kicad_sch`, check whether it was edited
by hand since it was last generated. Signs of that:
- a newer modification time than the generator script;
- `(generator "eeschema")` in the file where the generator emits something else;
- a `.history/` folder with recent "SCH Save" entries.

If it has been edited, **ask** before regenerating, and back the file up.
schlib enforces this: `Design.write()` refuses to overwrite files whose
hash differs from what it last wrote, and backs them up to
`.schgen-backup/`. Port the user's edits into the generator, then pass
`force=True`.

### 1. Write the spec first
In the generator script, before any geometry:

```python
PARTS = {"R1": ("Device:R", "10k", "Resistor_SMD:R_0402_1005Metric", {"LCSC": "C25744", ...}), ...}
NETS  = {"GND": [("U1", "1"), ("C1", "2")], "+3V3": [...], "EN": [...], ...}
NO_CONNECT = [("U1", "4"), ...]
```

Take pin numbers from the actual symbol. Never guess them:
`Library(project_dir).get("Device:LED").pins()` lists number, name and
position. `NETS` is the source of truth. The drawing is an *implementation*
of it, and step 5 checks that it matches.

Power nets are named by their power symbol (`+3V3`, `GND`). If a rail
used to be a label such as `VUSB_5V`, it becomes `+5V`; tell the user,
because the PCB needs *Update PCB from Schematic*.

**Name every differential pair `<BASE>_P` / `<BASE>_N`.** KiCad stores no
list of pairs — it infers one from the two *names*, and it only accepts `P`,
`N`, `+` or `-`, upper case, at the end (trailing digits and underscores are
skipped). `USB_DP`/`USB_DM` is **not** a pair to KiCad, and neither is
`TD1P`/`TD1M` or `USB_p`/`USB_n`, however obvious it looks to a human. The
cost is silent: no differential-pair router, no length tuner, no skew or
coupling DRC, and no `diff_pair_width` — that netclass constraint is gated on
`A.inDiffPair()`, so both legs route at the ordinary track width at whatever
impedance that gives. `lint_spec` catches it (rule `dp-name`) and prints the
names to use. Fix it in the spec: renaming later means the schematic, the
netlist, the netclass patterns and any cable pinout already written down.

### 1b. Lint the spec before drawing
`verify()` later proves the drawing matches `NETS`; nothing proves `NETS` is
a sensible circuit. Run the design-rule lint on the spec first, while a fix
costs one line and no geometry:

```python
from schlib import lint_spec
lint_spec(PARTS, NETS, NO_CONNECT, project_dir=project_dir)   # or d.lint(PARTS, NETS, NO_CONNECT)
```

- **Errors** (fix before drawing): unknown refs, pin numbers the symbol does
  not have (it lists the real ones), a pin in two nets or in a net and
  `NO_CONNECT`, a power-input pin left open.
- **Warnings**: an IC rail with no capacitor to ground, a rail with no
  >= 10 uF bulk cap, SDA/SCL with no pull-up, a reset/enable input with no
  pull-up and nothing driving it, a net of inputs only, a single-pin net (the
  usual sign of a typo in a net name), pins in no net and not no-connect.
- **Warnings, naming**: `dp-name` — two nets that are plainly a pair but
  that KiCad will not couple, with the `<BASE>_P` / `<BASE>_N` replacements
  spelled out.
- **Notes**: fewer small caps than ICs on a rail; `test_points=True` adds
  rails without a TP; `dp-suffix` — single-ended nets that KiCad reads as
  half a pair (usually active-low `..._N` signals), which couple silently the
  day a net with the complement name appears.

Pin roles come from the symbol's electrical types, falling back to pin names
for LCSC/EasyEDA symbols whose pins are all "unspecified". It is a
heuristic: keep a warning you have a reason to keep, and say why in a sheet
note. The **kicad** review skill still does the real review of the finished
sheets.

### 2. Plan sheets and blocks before coordinates
- Past ~25 parts or ~3 functions, go hierarchical: one sheet per function
  plus a root block diagram.
- Decide which nets cross sheets. Only **signals** get hierarchical labels
  and sheet pins; rails are global power ports.
- On each sheet, lay out left→right signal flow in labelled blocks: input,
  processing, output. **Declare the blocks in that order** — `arrange()`
  places them in declaration order, left to right and wrapping top to
  bottom, which is how a reader scans the page.
- **Aim for A3 or smaller.** Most people who are handed a schematic print it
  on A4 or Letter or read it on a laptop. A3 on A4 is 71 %: 1.27 mm text
  comes out 0.9 mm, still readable. A2 on A4 is 50 %: 0.64 mm text, and the
  1.0 mm notes are gone. `write()` prints a note when a sheet is larger than
  A3. The fix is, in order: tighten the blocks (notes into the dead corners,
  ground ports at the part's pin), compose the page by hand (below), and only
  then split into more sheets.

Read **`references/layout-conventions.md`** now if you haven't this session.
It has the decision rules and the routing recipes for the hard cases.

### 3. Draw with schlib
Copy `scripts/schlib.py` into the user's project, next to their generator,
so the project stays self-contained and reproducible. Then:

```python
from schlib import Design
d = Design(project_dir, "myboard", title="My Board", rev="A", company="...")

s = d.sheet("01-power.kicad_sch", "Power Input & 3.3 V", paper="auto",
            comment="USB-C 5 V in, 3.3 V LDO")
s.place("U1", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", 71.12, 50.8,
        props={"Footprint": "...", "LCSC": "C6186"})
vi, vo = s.pin("U1", "VI"), s.pin("U1", "VO")          # by number or unique name
s.wire(vi, (vi[0] - 12.7, vi[1]))                        # orthogonal polylines
s.power("+5V", vi[0] - 12.7, vi[1])                      # rail = power port
s.stub("U1", "GND", 2.54, power="GND")                   # pin -> short wire -> port
s.stub("U2", 5, 2.54, label="SDA")                       # ... or -> net label
s.place("J1", "Connector_Generic:Conn_01x03", "IN", 25.4, 50.8, mirror="y")
s.place("U3", "Amplifier_Operational:LM358", "LM358", 132.08, 55.88, unit=1)  # multi-unit
s.nc("U1", "4")
s.note("Vout = 0.6 x (1 + R1/R2) = 24.0 V", 20, 90, 1.0)

root = d.root_sheet(title="My Board")                    # hierarchical designs
d.link(s, 20.32, 55.88, 63.5, 40.64, pins=[("SDA", "right", 71.12)])
root.wire((83.82, 71.12), (111.76, 71.12))               # wires between sheet pins

s.arrange()                # flow the blocks: left to right, top to bottom
d.write()                  # relieves crowding, checks, centres, writes, patches .kicad_pro
d.verify(NETS, NO_CONNECT) # KiCad netlist vs spec -- must match exactly
d.erc()                    # ERC by category
d.check_text()             # KiCad's own render: collisions and crowding
d.render(outdir)           # p1.svg... + schematic.pdf + view.html
```

**Every part of a sheet belongs to a block.** Wrap each function as you
draw it and never compute a rectangle by hand:

```python
with s.block("3.3 V REGULATION"):        # or block_start()/block_end()
    s.place("U1", ...)                   # everything drawn in here is in it
    s.note("100 nF at pin 8", ...)
```

- The outline is derived from the contents, so it is never too tight and
  never clips a note. `BLOCK_PAD` (5.08 mm) is the clearance inside it,
  `BLOCK_GUTTER` (12.7 mm) the space between neighbours.
- Blocks nest. A big IC's core and each of its support groups are children
  of one container block; `arrange()` flows the children inside the parent,
  then places the parent on the page. `pad=None` makes a container a layout
  group with no outline of its own, `width=0` stacks its children in one
  column, and an explicit `width=` sets their wrap width.
- `arrange()` also grows the paper (A4 → A3 → A2 → A1) until the sheet fits.
- **Compose the page when the flow wastes it.** The flow fills each row to
  the height of its tallest block and leaves the space under every shorter
  one empty; on a sheet of mixed block sizes that is often the difference
  between A3 and A2. `arrange(layout=[...])` places the blocks where you say:
  a list of rows, each a list of columns, each a block title (any unique
  leading part of it) or a list stacked top to bottom, nesting as deep as
  needed:

  ```python
  s.arrange(gutter=10.16, layout=[
      ["USB-C", ["3.3 V", "TEST POINTS"], "MCU"],           # three columns
      [["BOOST", ["LIMITER", "LEGEND"]], "LOOP TERMINAL"],  # boost over a row of two
  ])
  ```

  Print the block sizes first (`s.block_extent(b)` after a plain
  `arrange()`), do the sums against `s.usable_area("A3")`, and keep the
  signal order: supply side along the top, the output side along the bottom.
  A block's title counts toward its width; shorten a title that is wider
  than its circuit. `block_start(title, pad=3.81)` instead of the default
  5.08 buys 2.5 mm per block in each direction.
- A note that sits loose inside a *container* keeps its old coordinates
  while the children move around it. Give it a block of its own
  (`s.block_start(None, pad=None)`) so it flows too.

`box()` still exists for a one-off rectangle that is not a block, but a
hand-placed outline is what produces overlapping boxes, clipped titles and
labels lying across a dashed line.

**One block per circuit, not per sub-function.** A boost converter is one
block -- load switch, input cap, controller, rectifier, output cap and
feedback divider together -- not "BOOST INPUT", "BOOST CONTROLLER" and
"24 V OUTPUT & FEEDBACK". A module is one block with its reset RC, boot
button, pull-ups, decoupling and strapping notes, not "MODULE" beside
"SUPPORT CIRCUITRY". The test: *would this box get its own heading in the
part's reference design?* Vendors' sheets for the same parts (Adafruit's
ESP32-S3 Feather: "POWER AND FILTERING", "USB TO SERIAL CONVERTER", "LIPO
CHARGING", the module and all that serves it in one region) run to five to
eight regions for a 50-part board. Splitting finer scatters one circuit's
wires across box lines, which is exactly where labels and outlines collide,
and it makes the reader reassemble the circuit. Twelve boxes on a 50-part
sheet was too many; the owner asked for them to be merged.

**Inside a block, wire it. Between blocks, label it.** A label joins things
a wire cannot sensibly reach: another block, another sheet. Inside a block the
reader expects to follow a line, and two labels with the same name in one
block mean two halves of one small circuit are joined only by name. A
two-transistor current limiter drawn as five labelled stubs is a netlist with
pictures; the same six parts wired are a circuit anyone recognises.
`check()` counts these (`Sheet.label_joins()`) and `write()` prints the count:
`0 label joins inside a block` is the target, and each one that remains should
be a wire that would otherwise have to cross another. One label on a wire is a
net *name* and is not counted; keep those, they are what the netlist and the
board call the net.

**Draw each circuit in the shape its datasheet draws it.** Power along one
rail left to right, ground returns hanging down from it, the controller under
the path it controls, feedback wired back from the output, inputs on the
block's left edge and outputs on its right. A boost converter is input cap,
inductor, switch node, rectifier, output cap along one line with the IC
underneath; a current-sense front end is terminal, clamp, fuse, shunt, filter,
amplifier in the order the current meets them. If the drawing cannot take that
shape because the symbol's pins are in the wrong places, change the symbol
(next paragraph), not the circuit's shape.

**Redraw a symbol whose pins fight the drawing.** Vendor and EasyEDA symbols
put pins in *package* order: SW, GND, FB down one flank of a boost controller,
or sixteen separate pins on a USB-C receptacle. `d.functional_symbol(name,
left=[...], right=[...], top=[...], bottom=[...])` writes a symbol with the
pins where a reader expects them and returns its lib_id: inputs left, outputs
right, supply on top, ground underneath, in the order the circuit flows.
`("A6,B6", "D+")` stacks several pads on one drawn pin (a reversible plug's
duplicate contacts, an IC's two ground pins). The body is sized so the names
never collide, pin lengths fit their numbers, and real pin types quiet the
`pin_to_pin` ERC noise that all-passive vendor symbols make. The footprint is
untouched: only the picture changes, and `verify()` still proves the netlist.
Anchor the block's wiring on `s.pin()` afterwards, never on typed
coordinates, so a resized symbol does not strand its wires.

**Nothing may touch an outline.** `check_text()` reads KiCad's render and
reports every string and part body within `OUTLINE_CLEAR` (0.8 mm) of any
rectangle on the sheet -- block outlines and `box()` alike -- counted with
the collisions; `write()` warns about the same from the model. A sheet of
content-derived blocks passes by construction; a sheet of hand-typed
rectangles does not (about a hundred faults on the 50-part sheet this was
written for). A fault on a derived outline means the extent model missed
something: fix `_bbox`, don't nudge the part.

### Spacing is computed, not typed

`write()` runs a relief pass over every sheet first (`Sheet.relieve()`, also
called by `arrange()`). You do not call these by hand; know what they do,
because they are why the output is not what your coordinates literally said:

| Pass | What it does | Why |
|---|---|---|
| `bundle_ports()` | joins a part's repeated power pins on one flank onto a single bar with one symbol | twenty GND flags on a connector is twenty names fighting for a 2.54 mm pitch, and the twentieth tells the reader nothing |
| `upright_power_ports()` | stands a rotated port back up -- rails above the wire, grounds below -- and picks how far along the stub it sits | a port on a horizontal stub is rotated to face the wire, which turns its name through 90 degrees; a column of those is an unreadable smear |
| `spread()` | pushes wire-connected groups apart to `CLEAR_GROUP` | parts drawn at a hand-typed pitch crowd once their text is real |
| label / note / port relief | slides a label or port further along its own stub, moves a note, slides an elbow | this is also how a dense flank gets its stagger |
| `_nudge_crowded()` | moves a whole group one grid step when nothing else worked | last resort, still connectivity-safe |

**None of it can change a connection.** Every move is checked against the
wires, pins and bodies first (`_safe_wire`, `_move_is_safe`), groups are the
pieces a *wire* holds together, and anything that only a label joins is free
to move. `verify()` proves it afterwards on every run.

Pass `relieve=False` to `write()` (or `arrange()`) if you want your
coordinates taken literally.

The full API is documented in docstrings: `place`, `power`, `gnd`, `pin`,
`pin_dir`, `pins_of`, `wire`, `route`, `stub`, `label`, `hlabel`, `nc`,
`bus`, `bus_entry`, `bus_label`, `pins_to_bus`, `lint_spec`/`Design.lint`,
`note`, `block`/`block_start`/`block_end`, `arrange`, `box`, `check`,
`check_blocks`, `check_spacing`, `label_joins`, `suggest_paper`,
`center_on_page`, and `Design.functional_symbol`. A single
sheet with no `root_sheet()` becomes a flat design automatically.

Coordinates are millimetres with Y pointing down. Keep **every** pin, wire
end and label on the **1.27 mm grid** (place parts on 2.54 mm). Don't bother
centring by hand: `write()` re-centres each drawing on its page.

### 3a. Start from a subcircuit when one fits
`scripts/subcircuits.py` has blocks the user's boards already use, built on
stock KiCad symbols, with LCSC numbers only where they come from a verified
order (blank otherwise -- fill them with the lcsc skill, never from memory):

| Block | What it draws |
|---|---|
| `Ldo` | AP2112K-3.3 (EN tied or brought out) or AMS1117-3.3, in/out caps |
| `UsbCSink` | USB-C 2.0 receptacle, 5.1k on each CC, USBLC6 ESD, PWR_FLAGs, drawn fully wired on two functional symbols it writes into the project (duplicate contacts stacked; the clamp between the two data lines), so the sheet must come from `Design.sheet()`. Pass `dp="USB_D_P", dm="USB_D_N"` so KiCad sees a differential pair; `esd_vbus=False` leaves the array's VBUS pin open for 2-layer flow-through layouts |
| `Esp32S3Core` | WROOM-1, 10 uF + 100 nF, EN RC + RESET, BOOT on IO0, native USB, GPIOs on labels, the rest NC |
| `Mt3608Boost` | 5 V -> 24 V (Vout = 0.6 x (1 + Rtop/Rbot)), EN pull-up when EN is a GPIO, PWR_FLAG |
| `Ina226LoopSense` | 4-20 mA terminal, TVS, low-side shunt, Kelvin RC filter, INA226 at 0x40 |
| `I2cPullups`, `LedIndicator` | one pair per bus; rail -> R -> LED -> GND |

```python
from subcircuits import Refs, Spec, UsbCSink, Esp32S3Core
refs, spec = Refs(), Spec()
usb = spec.add(UsbCSink(refs, vbus="+5V"))
mcu = spec.add(Esp32S3Core(refs, gpio={"IO8": "I2C_SDA", "IO9": "I2C_SCL"}))
spec.lint(lib=d.lib)                      # step 1b, on the assembled spec
for blk in (usb, mcu):
    blk.draw(s)                           # each in its own titled block, notes included
s.arrange(); d.write(); d.verify(dict(spec.NETS), spec.NO_CONNECT)
```

Each block draws into its own titled block and starts clear of whatever is
already on the sheet: relief runs before `arrange()`, and two blocks drawn
over each other look connected to it. Hand-drawn parts mix freely: add them
to `spec.PARTS`/`spec.NETS` and `refs.reserve()` their designators. Change a
part by editing `block.parts[ref]` before drawing. A new block is a `Block`
subclass -- parts and `conn()` in `__init__`, geometry in `_draw()` -- and it
is not done until `verify()`, `erc()` and `check_text()` are clean on a test
sheet and `label_joins()` is empty or every entry has a reason (the ESP32
core keeps one: IO0 to the BOOT button, whose wire would cross the reset
network). Copy `subcircuits.py` into the project next to `schlib.py`.

### 3b. Draw the conventions, don't reinvent them
`s.rail_bank(refs, x, y, rail, place=...)` draws a decoupling or bulk bank the
way a schematic draws one: the parts side by side between a rail bar and a
ground bar, one rail symbol and one ground symbol, each on its own drop. The
pitch comes from the parts' own text (`part_pitch()`), not from a number you
liked. `flag=True` adds the rail's PWR_FLAG on the far end of the bar;
`rail_kind="label"` names a local rail instead of a global one.

Repeating a rail port and a GND flag beside every capacitor is the single
most common way a generated sheet reads as a crowd.

### 3c. Buses
A bus is drawing, not connectivity: KiCad joins the member nets by their
labels whether or not the bus is there. It earns its place where eight or
sixteen parallel lines would otherwise be as many wires or loose labels -- a
data/address bus, a parallel display, a row of GPIOs to a header.

```python
end, start = s.pins_to_bus("U1", range(20, 28), "D[0..7]")  # stubs, member labels, entries, trunk, name
s.bus(end, (end[0], 120.65), (200.66, 120.65))             # carry the trunk on
s.bus_entry(x, y, 2.54, 2.54)                              # the primitives, when drawing by hand
s.bus_label("D[0..7]", x, y)
```

- Names: `D[0..7]` (vector), `{SDA SCL}` (group), `I2C{SDA SCL}` (named
  group: members are `I2C.SDA`, `I2C.SCL`). `bus_members()` expands them. The
  member labels are what actually connect, so name those nets in `NETS`
  exactly that way.
- `pins_to_bus` takes a row of pins on one flank of one part; `toward=`
  picks which way the trunk leaves (default down or right), and the tail is
  long enough for the bus name.
- **A bus run stays inside one block.** `arrange()` moves blocks
  independently, so a trunk drawn between two blocks is cut when they move.
  Between blocks, give each side its own labelled bus stub with the same
  name -- KiCad joins same-named buses on a sheet as it joins labels.
- `check()` errors on an entry that does not reach a bus, an entry with no
  wire on its free end, and a wire ending on a bus without an entry.
- Pins on a top or bottom flank put their member labels side by side at the
  2.54 mm pin pitch; `check_text()` reports those pairs as 1.02 mm apart.
  That is the pitch, not a layout fault.

### 4. Fix what `write()` reports
`check()` runs inside `write()`. It aborts on **errors**:
- off-grid points, which KiCad won't join;
- dangling wire ends;
- two blocks overlapping each other (`check_blocks`);
- two part bodies drawn on top of each other (`check_spacing`).

It also prints **warnings**, plus a crossing count:
- a pin landing mid-wire (KiCad connects it — usually an accidental short);
- a wire running through a part's body;
- **a wire that leaves a pin back across its own part** -- the fault that
  draws a connection straight through a resistor;
- a note written over a part or over another note (`check_clearance`);
- a drawing that reaches into the title block.

Aim for zero crossings. Every one is a place a reader has to stop and check
for a dot.

### 5. Verify connectivity against KiCad — non-negotiable
`d.verify(NETS, NO_CONNECT)` exports the netlist with `kicad-cli` and
compares nets **by their pin sets**. Hand-authored coordinates fail
silently: one mistyped number can short two nets or orphan a pin, and the
drawing still looks fine. A *rename* (for example `EN -> Net-(U1-EN)`) is
only informational; add a label if the name matters. Any *mismatch* is a
bug — fix the geometry, not the spec.

### 6. ERC
Target **0 errors**:
- `power_pin_not_driven` → add a `PWR_FLAG` where that rail enters the
  board.
- `pin_to_pin` warnings on LCSC/EasyEDA symbols are expected, because their
  pins are typed "unspecified". A part redrawn with
  `Design.functional_symbol` has real pin types and stops producing them.
- `lib_symbol_mismatch` is expected for generated files.

### 7. Render it and look — then iterate
A netlist match says nothing about appearance, so look at every sheet.

1. Render: `d.render(<scratch dir>)`.
2. Serve that directory in the background:
   `python -m http.server 8731 --bind 127.0.0.1 --directory <dir>`.
   The Browser pane blocks `file://`.
3. Open `http://127.0.0.1:8731/view.html` for the page list.
   - `view.html?f=p1.svg` fits a whole page.
   - `view.html?f=p1.svg&box=120,90,60,34` zooms to a region given in
     **sheet millimetres** (x, y, width, height). That's the same coordinate
     system as your generator and the part's `(at x y)`, so you can aim
     straight at a component. Keep the box roughly 16:9.

   If a screenshot times out, retry it on its own. If screenshots keep failing
   or look stale, the pane may not be painting. Don't guess: rely on
   `check_text()` (below), and use the browser's JavaScript tool to query
   element positions.

4. **Look at every block at full size**:
   `python scripts/block_crops.py <root.kicad_sch> <dir> --serve 8760` writes
   one cropped SVG per block (`p1_b0.svg`, ...) and an `index.html`. A static
   crop paints at once where the pan/zoom viewer times out, and it is cut from
   the rectangle KiCad saved, so it cannot be aimed wrong. A whole A2 page in
   one screenshot only proves the blocks are in the right places.
   **Use a port nobody else is on.** A server left over from an earlier
   session keeps answering on 8731 from its old folder, and you review last
   week's drawing with no error anywhere; the SVG's `<title>` carries the
   render time -- check it when something looks familiar.

5. **No browser pane, or it will not paint?** Rasterise the SVGs yourself and
   read the PNGs. On Windows, headless Edge does it with nothing to install:
   `msedge.exe --headless --disable-gpu --hide-scrollbars
   --window-size=1900,1300 --screenshot=out.png file:///C:/.../p1_b3.svg`.
   Run them one at a time (parallel runs drop outputs) and give the whole-page
   SVG a pixel `width`/`height` first, or the window shows only its corner.

Before the visual pass, run **`d.check_text()`**. It renders every sheet and
reads KiCad's own SVG, where every string is an element with an exact
position and length. This is the authority on spacing -- not the model, not
your reading of the code -- and it reports two things:

- **collisions**: text drawn sideways, on a wire, or over a part body -- and
  any string or part body touching a block outline;
- **crowding**: any two strings closer than the clearance, with the pair
  named and the gap measured. Strings belonging to one library symbol (a pin
  name against its own pin number) are excluded: that spacing is the
  symbol's, not the layout's.

The two clearances are deliberately different, and the difference matters:

| | clearance | why |
|---|---|---|
| side by side (`CLEAR_ROW`) | 1.27 mm | one string ending where the next begins reads as a single word |
| stacked (`CLEAR_STACK`) | 0.85 mm | a column of labels on the 2.54 mm pin grid leaves 1.0 mm and is exactly how a schematic is supposed to look |

Target zero. A real 200-part, 7-sheet design finishes at **2 collisions and 6
pairs within 0.6 mm of target**, and every one of those is named.
- Reference and value placement is automatic: a collision-aware placer tries
  the conventional spot first, then the alternatives.
- If a part is boxed in by wires on every side and even the best spot
  collides, move the wiring to open a gap.

Check at page scale (does it fill the page? do blocks read left→right?) and
zoomed in:
- text overlapping parts or wires;
- labels crowding each other;
- ground symbols pointing up;
- wires hugging bodies.

Fix the problems, then regenerate, verify and render again. Expect two or
three passes. Stop the server when you're done.

### 8. Deliver
- Export a PDF (`render()` writes `schematic.pdf`) and send it to the user.
- Leave the generator plus `schlib.py` in the project, and document how to
  regenerate:
  ```
  python generate_schematic.py
  ```
  Note that the `.kicad_sch` files are generated, so edits belong in the
  script.
- Mention any net renames, and remind the user to run *Update PCB from
  Schematic*.

UUIDs are deterministic, so regenerating never breaks PCB footprint links.

## Drafting rules in brief
Details and the reasons behind each rule are in `references/layout-conventions.md`.

1. Signals flow left → right. Supplies enter at the top and grounds leave
   at the bottom.
0. Never type a spacing number you could measure. `text_width()` knows how
   wide a string is, `part_pitch()` how far apart two parts have to stand,
   and the relief pass fixes what is left. A pitch that "looked right" at
   design time is the reason generated sheets read as crowded.
2. Every rail is a **power port**, never a text label. Give each supply pin
   its own port at the end of a short stub, rather than running wires to one
   shared symbol.
3. **Wire** local connections that take one or two segments. Use a **label**
   where a wire would cross another net, run through a body or detour.
4. A label always sits at the end of a ≥ 2.54 mm stub, never on the pin.
   This is what cures the "stacked labels" look.
5. Where pin order interleaves nets (A-B-A-B), a crossing is unavoidable,
   so label those nets instead.
6. When adjacent pins must pass each other's rows, one turns early and the
   other ends in a port or label.
7. Orient parts so their pins face what they connect to: mirror an
   input-side connector, and rotate an IC whose inputs are on the wrong side.
8. Decoupling caps hang below their rail or sit in a cap bank, with a
   placement note.
9. Put a no-connect flag on every unused pin, and a `PWR_FLAG` where each
   rail enters the board.
10. Put design maths and constraints on the sheet as notes: divider values,
    shunt sizing, strapping-pin rules, "place near pin X".
11. Group functional blocks in dashed outlines with bold titles, declared in
    signal order and arranged left→right, top→bottom. **One block per
    circuit** (what a reference design would give its own heading), notes
    inside the block they explain. Let `arrange()` place them and never type
    a rectangle: a hand-placed outline is how boxes end up overlapping and
    how labels end up lying across the dashed line.
12. Size the paper to the content (`paper="auto"`, fill 70–100%). Fill in
    the title block on every sheet.
13. Differential pairs are `<BASE>_P` / `<BASE>_N`, never `DP`/`DM`, `P`/`M`
    or lower case — that is the only spelling KiCad recognises, and every
    pair-aware tool and the impedance netclass depend on it.

## Geometry facts you will need
The full file-format notes are in `references/kicad-format.md`.

- A library pin's `(at …)` is its **tip**, the connection point. The symbol's
  local frame is Y-up; the sheet is Y-down.
- Rotation moves local +Y (a resistor's pin 1) as follows:
  `0 → up, 90 → left, 180 → down, 270 → right`. A two-pin part's rotation
  therefore decides *which pin number* faces which way.
- Mirroring: KiCad rotates first, then mirrors in sheet space. `mirror="x"`
  flips top↔bottom and `mirror="y"` flips left↔right. This was verified for
  all 12 combinations by `scripts/probe_transforms.py`; rerun the probe after
  a KiCad major upgrade.
- Connectivity rules:
  - Wires that cross without a junction are **not** connected.
  - A pin touching a wire's middle **is** connected.
  - Overlapping collinear wires **merge** nets.

## Gotchas that cost hours
Each is explained in `references/troubleshooting.md`.

- **Invisible wires in SVG/PDF**, even though the netlist is fine: the
  `.kicad_pro` netclass is missing `wire_width`. `write()` patches it. The
  stroke type is irrelevant; this was tested.
- Embedded symbols must be named with the full lib_id (`"Device:R"`), and
  derived `(extends …)` symbols must be flattened. schlib does both.
- A sub-sheet instance path is `/<root uuid>/<uuid of the sheet symbol on
  the root>`, not the sub-sheet file's own UUID.
- Symbols from easyeda2kicad keep their pins in sub-unit `_0_1`; stock
  symbols keep them in `_1_1`.
- Field text angle *and* justification are relative to the part's rotation,
  and KiCad does **not** rescue you the way it does in the editor.
  - At 90° or 270°, text at angle 0 draws sideways. Cancelling the rotation
    (angle = `-rotation`) fixes those two: KiCad then normalises the result
    to read bottom-to-top.
  - **At 180° that same formula gives angle 180, which KiCad draws literally
    — upside down.** 180 takes angle **0** instead, and because justification
    is read in the symbol's frame, left and right swap with it. A `mirror="y"`
    swaps them too, so the two together cancel.
  - schlib handles all of it; all 24 rotation × mirror cases are verified by
    `scripts/probe_fields.py`, and `check_text()` now fails on upside-down
    text as well as sideways text, so a regression here cannot pass silently.
  - Decide whether a two-pin part is vertical from its pin positions, not
    from its body's shape.
- **A power port at the end of a horizontal stub is rotated to face the
  wire, which turns its name through 90°.** A name is longer than a 2.54 mm
  pin pitch, so on a dense IC flank the rotated names overlap into an
  unreadable smear. Use horizontal net labels on the flank (the rail keeps
  its identity from the power port on its decoupling cap), and bundle the
  ground pins onto one bar with a single GND symbol clear of the label lane.
- **A sheet label directly above a net label, on adjacent 2.54 mm pins, is
  0.67 mm apart** -- a hierarchical label is centred on its wire, a net label
  sits above its own. The other order is fine. Stagger the stubs so the two
  strings share no x (the shorter string keeps the short stub). The same goes
  for two sideways power ports two pins apart: equal stubs stand their names
  end to end, unequal stubs give each its own column.
- **Two parts whose pins face each other 5.08 mm apart must be wired, not
  given a 2.54 mm port stub each.** The stubs meet end to end, so the net is
  right and nothing complains -- but two ports are stacked on one point and
  the rail name lands on the neighbouring value. Seen between an inductor and
  its output cap, and between the two resistors of a divider.
- **A net name ending in `N` is half a differential pair as far as KiCad is
  concerned.** `GPS_RF_IN` sent it looking for `GPS_RF_IP`; `RESET_N` will
  couple to `RESET_P` the moment such a net exists. For active-low signals
  prefer `nRESET` or `~{RESET}` over `RESET_N`, and check the `dp-suffix`
  note before shipping.
- Take the title-block warning seriously until the render proves otherwise.
  Notes drift under the title block easily.
- Measure; don't reason. When a result looks contradictory (a rotation
  "works" for one part and not another), build a tiny probe schematic and ask
  KiCad. That is how the rotation table was settled.

## Worked examples
- **`references/example_minimal.py`**: one A5 sheet using stock libraries
  only, so it runs in any empty folder. It shows the core idioms: a mirrored
  connector, derived symbols, a multi-unit op-amp including its power unit,
  `stub()`, rails with caps, a feedback network and `PWR_FLAG`s. Start here
  for small designs.
- **`scripts/subcircuits.py`**: read the blocks themselves for real-world
  patterns:
  - the USB-C flip pairing done with labels;
  - a reset RC with button;
  - staggered I²C pull-ups;
  - a boost converter whose interleaved pins use labelled stubs;
  - a Kelvin-sensed shunt.
- A complete generated board built with this skill is public:
  [USBC_M2E_HaLow_Adapter](https://github.com/Diode663/USBC_M2E_HaLow_Adapter)
  (`generate_schematic.py`, 30 parts, fabricated).

## Related skills
Use the **lcsc** / **bom** skills to source parts. Use `easyeda2kicad` to
fetch LCSC symbols and footprints into a project library, then resolve them
through the project's `sym-lib-table`. schlib reads that table
automatically, as well as the global one and KiCad's stock libraries. Use
the **kicad** skill for an electrical design review once the sheets are
drawn, and the **kicad-pcb-placement** skill to lay the board out afterwards
— it consumes the netlist this skill verifies.
