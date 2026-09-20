# Layout conventions and routing recipes

What separates a schematic that reads like a professional drawing from one
that is merely electrically correct. Each rule says *why*, so you can judge
the exceptions.

## Contents
1. Sheet structure
2. Flow and orientation
3. Power ports
4. Wire or label? — the decision rule
5. Routing recipes (the hard cases)
6. Text, notes and blocks (6.1 blocks are declared, 6.2 reading order,
   6.3 measured spacing, 6.4 the relief pass)
7. Page composition
8. A pre-flight checklist

---

## 1. Sheet structure

**Split by function once a design passes ~25 parts or ~3 functional blocks.**
One sheet per function (input power, regulation, MCU, analog front end,
connectors/IO). A reader looking for the boost converter should find a sheet
called that, not hunt across a crowded A2.

**The root sheet is a block diagram.** Sheet boxes laid out in signal-flow
order, wires between them for the inter-sheet *signals*, a title, a
two-line description, and a short power-tree note. It is the page someone
reads first.

**Power rails don't need sheet pins.** `GND`, `+3V3`, `+5V`… drawn as
power-port symbols are global nets — they connect across every sheet by
name. Only signals (USB D±, I²C, SPI, enables) get hierarchical labels and
sheet pins. This keeps the root diagram about data flow, not plumbing.

**Name what matters.** A net that is wired but unlabelled gets a KiCad name
like `Net-(U1-EN)`. Put a label on nets people will talk about (EN, IO0,
BOOT, FB) so the name survives into the PCB, the BOM tools and conversations.

## 2. Flow and orientation

- **Signals flow left → right.** Inputs and connectors on the left,
  processing in the middle, outputs on the right. Feedback paths run
  right → left; that's expected.
- **Supplies at the top, grounds at the bottom.** Supply arrows point up
  above the thing they feed; ground symbols hang below. Higher voltages sit
  higher on the page. "Water flows downhill" is how people read schematics.
- **Orient parts so their pins face the thing they connect to.** A connector
  on the left edge should have its pins pointing right into the circuit —
  mirror or rotate it (`mirror="y"` on a one-sided connector). An IC whose
  input pins are on its right-hand side gets rotated 180°.
- **Vertical passives for anything to a rail**: pull-ups, decoupling,
  dividers to ground. Horizontal passives for things in series with a signal
  (series resistors, filter elements, inductors in a power path).

## 3. Power ports

Power-port symbols are the single biggest readability win. Rules:

- **Every rail is a port, never a text label.** A label that says `GND` is
  legal but reads as "a signal called GND" and clutters. Ports are
  recognisable at a glance.
- **Give each supply pin its own port.** Don't route five ground pins to one
  shared ground symbol across the sheet; put a ground symbol at the end of a
  short stub from each. Long rails between distant parts are what make
  schematics look like wiring diagrams.
- **A port can sit at the end of a horizontal stub.** It needn't be at the end
  of a vertical wire. The ground graphic hangs below the stub end; a supply
  arrow sits above it. This is the move that removes most routing conflicts
  (see recipe 5.1).
- **PWR_FLAG where a rail enters the board** — on the connector's VBUS/GND
  and at a regulator output. ERC otherwise complains "power pin not driven"
  because LCSC/EasyEDA-imported symbols type their pins as passive or
  unspecified, so nothing on the net is marked as a source.

## 4. Wire or label? — the decision rule

**Wire it** when the connection is local and takes one or two orthogonal
segments without crossing another net, entering a part's body, or making a
detour.

**Label it** when a wire would have to:
- cross a different net,
- run through or hug a component body,
- detour around the page, or
- connect pins whose order interleaves with another net's (recipe 5.3).

Two rules that matter as much as the choice itself:

- **A label always sits at the end of a short stub (≥ 2.54 mm), never on the
  pin itself.** Labels stacked directly on pins overlap each other and the
  body — the classic "generated schematic" look.
- **Local labels for connections within a sheet; hierarchical labels only for
  signals that leave the sheet.** A local label and a hierarchical label
  with the same name on the same sheet are one net.

A real professional sheet is mostly wires with a handful of labels — the
KiCad demo *StickHub* has 470 wires to 26 labels. A generated sheet that is
all labels and no wires reads as a netlist, not a drawing.

## 5. Routing recipes (the hard cases)

### 5.1 End in a port instead of reaching a rail
Pin needs +5V but the +5V rail is on the other side of three other nets?
Stub out of the pin and put a `+5V` port at the stub end. No crossing, and
the reader sees "+5V" right at the pin.

### 5.2 Stagger the turn points of adjacent pins
Two pins on the same flank both leave outward, and one must turn to go
past the other's row. The one that turns must turn **closer to the body**;
the other must **end** (in a port or label) before reaching that column.

    GND pin (upper) --+            turns up early, at x = body - 3.81
                      |
    3V3 pin (lower) ------------ [+3V3]   runs further out, ends in a port

If both turn, one crosses the other. Choose which one ends.

### 5.3 Interleaved pin orders — use labels
If nets alternate along a flank — A B A B (USB-C: D+, D−, D+, D−) or a
converter whose FB / GND / SW leave in an order that doesn't match where
they're going — then joining like to like with straight wires needs **at
least one crossing**. It's topological, and no amount of tweaking avoids it.
Use short labelled stubs for the nets that would cross and wire only the
remaining one.

### 5.4 Grounds that sit at the top of a module
Wi-Fi modules often put GND pins and the exposed pad at the top. Join them
with a short bus above the body, run it down an unused flank (usually the
side whose GPIOs are all NC), and end in one ground port at the bottom. It
keeps "ground goes down" true without a symbol pointing the wrong way.

### 5.5 Decoupling
Either hang each cap below a horizontal supply rail (short vertical wire up
to a T on the rail, ground port below), or draw a small cap bank off to the
side with a port above and a ground below each, plus a note: *"place C3
within 5 mm of U1 pin 2"*. The note matters more than where the cap sits on
the page.

### 5.6 Pull-ups onto a bus of signal lines
A vertical resistor drops from a supply port to its signal line. When several
lines run out beneath it, put its column *outside* the lines below, i.e. the
pull-up for the lower line goes further left, and extend that line's wire
further to meet it. Otherwise the resistor's wire crosses the lines in
between.

### 5.7 Tying a pin to a rail (EN → VIN)
Don't loop a wire from EN back round to VIN; give EN its own supply port at a
stub end. It reads as "EN is tied high", which is the actual intent.

### 5.8 Two-pin parts: orientation is electrical
Rotating a resistor 90° vs 270° swaps which pin faces which way. That's
invisible for the circuit function but it changes the *netlist* (R13 pin 1
vs pin 2), which matters for back-annotation, polarised parts (diodes,
LEDs, electrolytics) and anything with a spec that names pins. Choose the
rotation from the rotation table in `kicad-format.md` and let the netlist
verifier confirm it.

### 5.9 Unused pins and unused sections
Put a no-connect flag on every unused pin; it records intent and silences
ERC. For unused op-amp or comparator sections, wire them as followers with the
input inside the common-mode range, and say so in a note.

## 6. Text, notes and blocks

- **Reference and value stay off the body.** Vertical two-pin parts get them
  stacked to the right; ICs and connectors get the reference above and the
  value below. schlib does this automatically from the body's bounding box.
- **Notes carry the engineering.** Put calculations and constraints on the
  sheet, next to the circuit they explain: divider maths, shunt sizing,
  current-limit resistor values, strapping-pin rules, "don't add a pull-up
  here", "place close to pin X", why a part was chosen. This is what makes a
  schematic reviewable months later.
- **Sizes**: 1.0 mm for notes, 1.27 mm default, 1.6 mm bold for block titles,
  2.5 mm bold for the root sheet's title.
- **Keep notes clear of wires and symbols.** Break long notes into two lines
  (`\n` in the string) instead of letting them run through a part.
- **Functional blocks**: a dashed grey rectangle around each function with a
  bold title just above its top-left corner ("USB-C INPUT & ESD", "3.3 V
  REGULATION").

### 6.1 Blocks are declared, not drawn

Never compute a block rectangle by hand. Declare the block and let schlib
derive the outline from what is inside it:

```python
with s.block("USB-C INPUT & PD NEGOTIATION (J1/U1)"):
    ...
```

A hand-placed rectangle goes wrong in three ways at once, all of which were
on a real sheet before this existed: it clips the note that was added after
it, it overlaps the block next door (5 mm here, 60 mm there), and it has to
be re-measured every time a part moves. A derived outline cannot do any of
those.

Sizing, and why:

| Quantity | Default | Reason |
|---|---|---|
| `BLOCK_PAD` | 5.08 mm | clearance from the contents, including field text and notes, so nothing touches the outline |
| `BLOCK_GUTTER` | 12.7 mm | white space between neighbours -- below about 8 mm two blocks read as one |
| `BLOCK_TITLE_H` | 5.08 mm | band above the box for the 1.6 mm bold title |
| title width | reserved | a narrow block's title is wider than the block; without reserving it, it runs into the next block's title |

### 6.2 Reading order

`arrange()` places blocks **in the order they were declared**, left to right,
wrapping to a new row when the width runs out -- the order a reader scans a
page, and the order the standards assume: inputs on the left, processing in
the middle, outputs and connectors on the right; supplies at the top,
grounds at the bottom. So declare input first and the external connector
last, and if the code builds them in a different order, move the code.

Nest blocks when one function is large: an IC's core and each support group
(decoupling, feedback, pull-ups) are children of one container. The children
are flowed inside the container first, then the container is placed on the
page, so a 29-pin charger and its seven support groups occupy one labelled
region instead of scattering. A container with `pad=None` draws no outline
of its own -- use it to stack a few small blocks into a column beside a tall
one, with `width=0`.

Blocks only move as a unit, so **every wire must stay inside one block**.
Anything crossing a block boundary is a label -- which is the rule anyway
for anything that isn't a one- or two-segment local connection (section 4).
`Design.verify()` catches a wire you left spanning two blocks, because the
net breaks the moment they are flowed apart.

## 6.3 Spacing: measure it

Every crowded generated schematic has the same cause -- a spacing number
someone typed before the text existed. A 2.54 mm stub was fine until the net
was called `MODULE_ETH_TD3M`; a 15.24 mm column pitch was fine until the values
became `100nF` and `TPS563201DDCR`.

So the numbers come from measurement:

- `text_width(s, size)` returns what KiCad will actually draw, from glyph
  advances measured out of KiCad's own SVG by `scripts/calibrate_text.py`.
  The advances vary by a factor of nearly three (`I` 0.36, `m` 1.00, in units
  of the font size), so any single average factor is wrong by tens of percent
  on a real string. **The advances are relative to KiCad's SVG font-size,
  which is 4/3 x the text size** -- the factor that, when it was missing, made
  every string 25% narrower than reality and let a checker call a crowded
  sheet clean.
- `part_pitch(refs)` returns how far apart a row of two-pin parts has to
  stand for their reference and value to fit between them.
- `CLEAR_ROW` (1.27 mm) is the gap two strings need **side by side**;
  `CLEAR_STACK` (0.85 mm) is what they need **stacked**. They are different
  on purpose: a column of labels on the 2.54 mm pin grid leaves 1.0 mm and is
  correct, while 1.0 mm between the *end* of one string and the *start* of
  the next reads as one word.
- `CLEAR_GROUP` (7.62 mm) separates two wire-connected groups, and
  `CLEAR_PART` (2.54 mm) two part bodies.

What the layout cannot see, it cannot avoid: the model also includes each
symbol's own pin names and numbers (`pin_text_boxes`), which is where a label
on a short stub actually collides.

## 6.4 The relief pass, and what it may not do

`Sheet.relieve()` (run by `write()`) fixes crowding after the fact, because
no set of hand-chosen coordinates survives contact with real text. In order:
bundle repeated power pins onto one bar, stand rotated ports upright and
choose how far out they sit, push wire-connected groups apart, slide labels
and ports along their own stubs, move notes, and as a last resort nudge a
whole group one grid step.

The invariant is absolute: **relief never changes a connection.** It holds
because of three rules, each of which was learned by breaking it:

1. A *group* is what a wire holds together -- including a pin that lands on a
   wire's middle, and a label dropped on a wire (both are connections KiCad
   makes and a naive grouping misses, which splits the net when the group
   moves).
2. A move is rejected unless the end being moved lands on empty space. The
   end that stays put is a corner of its own net and is not a fault; the end
   that moves may not touch another wire, another wire's end, or a pin.
3. Groups may only be moved when no contact is created
   (`_move_is_safe`): a group that lands with a wire end on another group's
   wire *is* connected to it, and KiCad merges the nets silently.

`Design.verify()` checks all of this against KiCad's own netlist on every
run. If it ever fails after a layout change, the bug is in one of those three
rules, not in the drawing.

## 7. Page composition

- **Match paper to content.** A big page with a small drawing in one corner
  looks unfinished. Aim for the drawing to fill roughly 70–100% of the
  usable area. `paper="auto"` or `Sheet.suggest_paper()` picks the smallest
  of A5/A4/A3 that fits; `Design.write()` prints the fill ratio per sheet.
- Sub-sheets on A5 with the root on A4 is fine and common. Change `paper=`
  if the user wants one size throughout.
- **Centre the drawing** in the frame, clear of the title block
  (`write()` does this by default, snapped to grid).
- **Fill in the title block** on every sheet — title, date, revision, and a
  one-line comment saying what the sheet is.

## 8. Pre-flight checklist

Before calling a sheet done:

- [ ] `Design.verify()` → netlist matches the spec exactly
- [ ] `Design.erc()` → 0 errors (warnings understood — see `troubleshooting.md`)
- [ ] `check()` → no errors, warnings reviewed, crossings at or near 0
- [ ] rendered and **looked at**, at full-page and zoomed in
- [ ] no text overlapping symbols or wires
- [ ] every rail is a power port; PWR_FLAG where rails enter the board
- [ ] every unused pin has a no-connect flag
- [ ] every part is inside a declared block; `check_blocks()` clean
- [ ] `check_spacing()` clean -- no part or note drawn on another
- [ ] blocks read left to right, top to bottom, in signal order
- [ ] `check_text()` -> 0 collisions, crowding list empty or every entry
      explained
- [ ] drawing fills the page; title block filled in


## Net naming: differential pairs

KiCad has no pair list. `BOARD::MatchDpSuffix` (pcbnew/board.cpp) infers a
pair from the two names, every time it needs one:

> read the name backwards; skip any run of digits and underscores; the next
> character must be `P`, `N`, `+` or `-`, and the letters must be upper case.
> The complement is the same name with that one character swapped, and the
> pair exists only if a net with exactly that name exists.

That is the whole rule. `schlib.match_dp_suffix()` is a reimplementation of
it, checked name-for-name against pcbnew's own `DpCoupledNet()` over 2315
names with no disagreement, so the table below is KiCad's behaviour and not
an opinion:

| pair | recognised? | why |
|---|---|---|
| `USB_P` / `USB_N` | yes | the canonical form |
| `USBP` / `USBN` | yes | the separator is optional |
| `USB+` / `USB-` | yes | `+`/`-` count too |
| `TD1_P` / `TD1_N` | yes | |
| `USB_P12` / `USB_N12` | yes | trailing digits are skipped |
| `TD_P_1` / `TD_N_1` | yes | trailing underscores are skipped too |
| `USB_DP` / `USB_DM` | **no** | reads the `P`, looks for `USB_DN` |
| `TD1P` / `TD1M` | **no** | same: it wants `TD1N` |
| `MX1P_INT` / `MX1M_INT` | **no** | the marker is not at the end |
| `USB_p` / `USB_n` | **no** | lower case |
| `CAN_H` / `CAN_L` | **no** | `H` is not a polarity character |

`DP`/`DM` is the commonest way to lose this, because it is what the USB spec
calls the two wires and what most datasheets print on the pins.

### What an unrecognised pair costs

Nothing warns you. Not ERC, not DRC, not the netlist. You lose:

- the differential-pair router and its length/skew tuner;
- the pair DRC tests (skew, uncoupled length, gap);
- **`diff_pair_width` and `diff_pair_gap` in the netclass.** In
  `drc_engine.cpp` that constraint is gated on `A.inDiffPair( '*' )`, so on
  an unrecognised pair it never fires: both legs route at the class's
  ordinary `track_width`, at whatever impedance that happens to give, and the
  board reads as "controlled impedance" in every report.

Found on a real board: 20 pair nets named `DP`/`DM`, `P`/`M` and `P_INT`/
`M_INT`, of which **0** were recognised. After renaming, 28 nets, all paired.

### Also worth knowing

- A single-ended net whose name ends in `N` is read as the *negative* half of
  a pair. `GPS_RF_IN` had KiCad hunting for `GPS_RF_IP`. It is harmless while
  no such net exists — and it couples silently the day one does. Prefer
  `nRESET` or `~{RESET}` to `RESET_N` for active-low signals.
- The hierarchical path in a board net name (`/Sheet Title/USB_P`) does not
  matter: the scan starts at the end.
- Bus members inherit the rule. `{USB_P USB_N}` works; `{USB_DP USB_DM}`
  does not.
- CAN and RS-485 (`CANH`/`CANL`, `A`/`B`) are differential but cannot be
  named to satisfy the rule without losing the standard names. If you want
  the router to couple them, name them `CAN_P`/`CAN_N` and say so in a sheet
  note; otherwise route them by hand and accept the loss.
- Fix it in the **schematic**. Renaming nets on the board alone is undone by
  the next *Update PCB from Schematic*.
