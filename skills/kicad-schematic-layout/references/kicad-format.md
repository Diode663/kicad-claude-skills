# KiCad schematic format — the facts that matter when generating it

Everything here was confirmed against KiCad 10's own loader, netlister or
plotter, not taken from memory. schlib implements all of it; read this when
you need to debug a file or go beyond what schlib covers.

## Contents
1. Version and file skeleton
2. Embedded library symbols
3. Pins, rotation and mirroring
4. Placed symbols and instance paths
5. Hierarchy: sheet symbols, pins, labels
6. Power symbols
7. Wires, junctions, no-connects
8. Graphics and text
9. Project file settings that affect drawing
10. kicad-cli as an oracle

---

## 1. Version and file skeleton

```
(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0")
  (uuid "...") (paper "A4")
  (title_block (title "...") (date "...") (rev "...") (comment 1 "..."))   ; company only if set
  (lib_symbols ...every symbol used, flattened, KiCad 10 library syntax...)
  ...items in KiCad's save order: rectangle, text, junction, no_connect, wire,
     label, global_label, hierarchical_label, symbol, sheet -- each type sorted by UUID...
  (sheet_instances (path "/" (page "1"))))    ; ROOT sheet only; no sheet-level embedded_fonts
```

- `20260306` is **KiCad 10.0.6's native format**. Files written in it open
  without the "created with an older version" prompt and KiCad saves them
  back unchanged. KiCad 9 cannot open them. (schlib wrote KiCad 9's
  `20250114` until 2026-09-14; KiCad 10 opened those with an upgrade prompt.)
- **Proof, not assumption:** `Design.check_native()` (run by `write()`)
  copies each sheet plus the `.kicad_pro` to a temp folder, lets
  `kicad-cli sch upgrade` rewrite the copies, and compares the parsed trees.
  Any difference is printed with its path. Copy the `.kicad_pro` too, or
  KiCad blanks the instance project names and every root sheet "differs".
- **Symbol libraries must be in the current format** (`20251024`), because
  the schematic embeds library symbols verbatim. Stock KiCad 10 libraries
  are. easyeda2kicad writes `20211014`; schlib converts such a library in
  memory and prints the `kicad-cli sym upgrade` command to fix it on disk.
- **UUIDs must be unique within a sheet.** KiCad silently renumbers a
  duplicate on save (last hex digit +1). Two power ports stacked on the same
  point at different rotations used to collide; `sid()` now includes rotation.
- Sub-sheet files have **no** `sheet_instances` block.
- When KiCad changes its format again, `check_native()` names the first
  construct that differs. Fix the writer, don't post-process with `upgrade`.

## 2. Embedded library symbols

- The schematic carries its own copy of every symbol in `lib_symbols`; KiCad
  renders from that copy. The top-level name must be the full lib_id
  (`"Device:R"`, `"myproj:ESP32-S3"`). Sub-unit names stay unprefixed
  (`"R_0_1"`, `"R_1_1"`).
  - If the top-level name is wrong, every pin resolves as unknown. ERC then
    reports `pin_not_connected` at the symbol's origin with nonsense
    positions.
- **`(extends "Parent")` must be flattened.** Many stock symbols are derived,
  for example `Regulator_Linear:AMS1117-3.3` and
  `Amplifier_Operational:LM358`. To flatten: take the parent's body, rename it
  and its sub-units to the child's name, and overlay the child's properties.
  schlib's `Library._flatten()` does this.
- Copy symbol text **verbatim**; don't re-serialise a parsed tree. KiCad's
  quoting is inconsistent (pin numbers are always quoted even when numeric,
  keywords are bare), so guessing it produces files that won't load.
- Sub-unit naming is `Name_U_S`. U is the unit (0 = common to all units) and
  S is the body style (1 = normal, 2 = De Morgan alternate).
  - **Which sub-unit holds the pins varies**: easyeda2kicad puts them in
    `_0_1`, while stock KiCad symbols such as `Device:R` use `_1_1`. Scan the
    whole symbol.

## 3. Pins, rotation and mirroring

- A library pin's `(at x y angle)` is its **electrical tip**. The pin line
  runs from there toward the body, in the direction of `angle`. No length
  offset is needed to find the connection point.
- Symbol-local coordinates are **Y-up**; the sheet is **Y-down**:
  `sheet = (X + rx, Y − ry)`, where `(rx, ry)` is the local point rotated.
- **Rotation** (placement angle, counter-clockwise on screen). This was
  verified by placing a `Device:R` at each angle and reading KiCad's netlist.
  Local +Y (pin 1 of a resistor) ends up:

  | rot | local +Y points | Device:R pin 1 |
  |-----|-----------------|----------------|
  | 0   | up              | top            |
  | 90  | left            | left           |
  | 180 | down            | bottom         |
  | 270 | right           | right          |

- **Mirroring**: KiCad rotates first, then mirrors in sheet space.
  `(mirror x)` flips top↔bottom and `(mirror y)` flips left↔right. All 12
  rotation × mirror combinations were verified with `scripts/probe_transforms.py`;
  rerun it after a KiCad major upgrade.
- An outward direction at a pin tip (for drawing stubs) is local
  `(−cos a, −sin a)`, run through the same transform.

## 4. Placed symbols and instance paths

```
(symbol (lib_id "Device:R") (at 50.8 50.8 90) (mirror y) (unit 1) (body_style 1)
  (exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes) (dnp no) (uuid "...")
  (property "Reference" "R1" (at ...) (show_name no) (do_not_autoplace no) (effects (font (size 1.27 1.27))))
  (property "Value" ...)
  (property "Footprint" "..." (at ...) (hide yes) (show_name no) (do_not_autoplace no) (effects ...))
  (property "Datasheet" ...) (property "Description" ...)    ; always present, in this order
  (property "MPN" ...)                                        ; user fields after the mandatory five
  (pin "1" (uuid "...")) (pin "2" (uuid "..."))
  (instances (project "myboard" (path "/<root-uuid>" (reference "R1") (unit 1)))))
```

- KiCad 10 order inside a property: `at`, `hide`, `show_name`,
  `do_not_autoplace`, `effects`. On power symbols Footprint/Datasheet/
  Description are empty and **not** hidden; on normal parts they are hidden.

- `(hide yes)` is a **sibling** of `(effects …)` in KiCad 9+. A bare `hide`
  inside effects makes the file fail to load.
- Field (property) positions are absolute sheet coordinates, but the text's
  **angle and justification are relative to the symbol's rotation and
  mirror**:
  - A field at angle 0 on a part placed at 90° or 270° renders sideways.
  - At a *net* 180° (symbol rotation + field angle), KiCad also swaps
    left/right justification. A "left"-justified field then runs back over
    the body; this is how LED1 and D2 got their text on top of themselves.
  - Rule: give fields angle `(-rotation) mod 360`. The text is then upright
    and justification means what it says.
  - Under `(mirror y)`, write left/right justification swapped.
  - All 24 rotation × mirror cases were verified with
    `scripts/probe_fields.py`.
- To decide whether a two-pin part is vertical, look at where its **pins**
  are, not at its body's shape. Some library bodies (EasyEDA diodes) are
  taller than they are wide while the part itself is drawn horizontally.
- **The `instances` block is mandatory.** Without it the file fails to load,
  or references don't bind.
- **Instance path on a sub-sheet**: `"/<root sheet uuid>/<sheet-symbol uuid>"`.
  The second UUID belongs to the `(sheet …)` symbol *on the root* that
  references this file, **not** the sub-sheet file's own `(uuid)`.
- Multi-unit parts: one `(symbol …)` per placed unit, all with the same
  reference, each with its own `(unit N)` in the header and in the instance.
- UUIDs: KiCad links PCB footprints to schematic symbols by UUID path.
  Generate them deterministically (uuid5 of sheet + reference) so
  regeneration never breaks the board.

## 5. Hierarchy: sheet symbols, pins, labels

```
(sheet (at 20.32 55.88) (size 63.5 40.64) ... (uuid "<sheet-symbol uuid>")
  (property "Sheetname" "USB-C Input" (at ...) (effects ... (justify left bottom)))
  (property "Sheetfile" "01-usb-power.kicad_sch" (at ...) ...)
  (pin "USB_DP" bidirectional (at 83.82 71.12 0) (uuid "...") (effects ... (justify right)))
  (instances (project "myboard" (path "/<root uuid>" (page "2")))))
```

- Sheet pins sit **exactly on the box edge**. Angle 0 is the right edge
  (justify right, so the text is inside); angle 180 is the left edge
  (justify left).
- A sheet pin pairs by name with a `(hierarchical_label "USB_DP" (shape
  bidirectional) …)` inside the sub-sheet. Shapes are input, output,
  bidirectional, tri_state and passive.
- A local `(label …)` and a hierarchical label with the same name on the same
  sheet are the same net.
- In an exported netlist, sheet-local net names are prefixed with the sheet
  path (`/USB-C Input/CC1`). Strip it before comparing.

## 6. Power symbols

- These come from the stock `power` library: `power:GND`, `power:+3V3`,
  `power:+5V`, `power:+24V`, `power:VBUS`, `power:PWR_FLAG`…
- The pin is at the symbol origin with length 0. **GND's graphic hangs below
  that point; supply symbols draw an arrow above it.** So placing the symbol
  at the end of a wire needs no offset.
- The reference is `#PWRnnn` (hidden) and the value is visible.
  - For GND, put the value text ~4 mm below the origin; for supplies, ~3.8 mm
    above.
- The **net name is the symbol's name**. Two `+3V3` symbols anywhere in the
  hierarchy are the same net.
- `#PWR…` and `#FLG…` nodes appear in exported netlists. Drop them before
  comparing against a spec.

## 7. Wires, junctions, no-connects

```
(wire (pts (xy 52.07 53.34) (xy 52.07 48.26)) (stroke (width 0) (type solid)) (uuid "..."))
(junction (at 27.94 53.34) (diameter 0) (color 0 0 0 0) (uuid "..."))
(no_connect (at 52.07 60.96) (uuid "..."))
```

- `width 0` means "use the netclass wire width" (see §9).
- **Connectivity rules**:
  - Wire endpoints that coincide are connected.
  - A wire endpoint on another wire's middle is connected (a T); draw a
    junction there.
  - A **pin touching a wire's middle is connected**. This is the usual way a
    generated layout shorts two nets by accident.
  - Two wires that **cross** without a junction are **not** connected.
  - Two **collinear, overlapping** wires are merged into one net. ERC reports
    it as `multiple_net_names` ("Both GND and MT_SW are attached to the same
    items").
- Draw a junction wherever three or more conductor directions meet.
- Every endpoint must be on the 1.27 mm grid, or KiCad will not join it
  (`endpoint_off_grid`).

## 8. Graphics and text

```
(rectangle (start x y) (end x y) (stroke (width 0.254) (type dash) (color 132 132 132 1))
  (fill (type none)) (uuid "..."))
(text "note\\nsecond line" (exclude_from_sim no) (at x y 0)
  (effects (font (size 1.0 1.0) (bold yes)) (justify left)) (uuid "..."))
```

Escape `\` and `"` in every string, and write newlines as `\n`.

## 9. Project file settings that affect drawing

- **Every netclass needs `wire_width` and `bus_width`** in `.kicad_pro`
  (`net_settings.classes[]`). If they're missing, KiCad plots every wire and
  junction with `stroke:none`. They disappear from SVG and PDF, yet stay
  electrically present, so netlist and ERC look fine. A controlled test
  confirmed this is the whole cause: stroke `type` (solid vs default) made no
  difference, and a project with no `.kicad_pro` at all renders fine.
  schlib's `Design.write()` patches the file.
- `kicad-cli` may rewrite `.kicad_pro` (normalising or adding keys) when it
  runs ERC. That's benign.

## 10. kicad-cli as an oracle

| Purpose | Command |
|---|---|
| Does it load? Is it clean? | `kicad-cli sch erc --format json -o erc.json root.kicad_sch` |
| Ground-truth connectivity | `kicad-cli sch export netlist --format kicadxml -o n.xml root.kicad_sch` |
| Look at it | `kicad-cli sch export svg -o outdir root.kicad_sch` (one SVG per page) |
| Deliverable | `kicad-cli sch export pdf -o schematic.pdf root.kicad_sch` |

- **Always run it on the root sheet**; sub-sheets come along with it.
- In the netlist, no-connect pins appear as single-pin nets named
  `unconnected-(REF-PIN-PadN)`.
- An empty netlist, or an export that fails, means the file didn't load.
  Bisect by deleting one element type at a time until it loads.
