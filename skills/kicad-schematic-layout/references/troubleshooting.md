# Troubleshooting — symptom → cause → fix

Every entry here actually happened while building a real board. When
something odd shows up, find the symptom here before you theorise.

## File won't load / kicad-cli fails

| Symptom | Cause | Fix |
|---|---|---|
| "Failed to load schematic", empty netlist | `hide` placed inside `(effects …)` | KiCad 9+: `(hide yes)` as a sibling of `effects` |
| same | a placed symbol has no `(instances …)` block | add it (schlib always does) |
| same | a derived `(extends …)` symbol was embedded unflattened | flatten it (`Library._flatten`) |
| same | an unescaped `"` in a note or value | escape strings (`q()` in schlib) |
| same, cause unknown | — | **bisect**: copy the file, delete one element type (rectangles, texts, labels, junctions…) at a time and retry until it loads |
| KiCad 10 says the file "was created with an older version" and will be converted on save | the writer's format version (was KiCad 9's `20250114`) is older than the running KiCad's | schlib now writes KiCad 10's `20260306`; `write()` runs `check_native()`, which names the first construct that still differs from KiCad's own save — fix the writer there, don't post-process with `kicad-cli sch upgrade` |
| `check_native()` says every root sheet differs in `instances/project` | the temp copy was upgraded without the `.kicad_pro`, so KiCad blanked the project name | copy the project file into the temp folder (schlib does) |
| a UUID's last hex digit changes by one after KiCad saves | two items shared a UUID and KiCad renumbered one | make the `sid()` key unique (power ports now include rotation) |
| every sheet embeds old-syntax symbols (`(id N)`, `hide` in effects) | the symbol library itself is an old format (easyeda2kicad writes `20211014`) | `kicad-cli sym upgrade library.kicad_sym` once; schlib converts old libraries in memory meanwhile |

## Connectivity wrong (the netlist verifier fails)

| Symptom | Cause | Fix |
|---|---|---|
| ERC `pin_not_connected` for every pin, at the symbol origin | the embedded symbol name doesn't match the lib_id (e.g. `"R"` vs `"Device:R"`) | rename the top-level `(symbol "…")` in lib_symbols to the full lib_id |
| two nets merged; ERC `multiple_net_names` | collinear overlapping wires on different nets, or a stub/label end landing on another net's wire | move one part; the verifier names both nets |
| a pin in the wrong net | a pin of another part lands on the *middle* of a wire (KiCad connects it) | `check()` warns "lands mid-wire"; reroute |
| a two-pin part's pins swapped vs spec | rotation chosen wrongly (90 vs 270) | see the rotation table in `kicad-format.md`; flip the angle |
| everything flipped on rotated parts | rotation sign inverted in your own math | rerun `probe_transforms.py`; trust KiCad, not reasoning |
| a net named `Net-(U1-EN)` instead of `EN` | the net is wired but never labelled | add a label on the wire; renames are informational only |
| `unconnected-(…)` nets that aren't in NO_CONNECT | a pin was never wired | wire it, or add it to NO_CONNECT on purpose |

## ERC warnings

| Warning | Meaning | Action |
|---|---|---|
| `power_pin_not_driven` (error) | nothing on a rail is typed as a power output (common with EasyEDA symbols) | add `PWR_FLAG` where the rail enters the board |
| `pin_to_pin` | EasyEDA/LCSC symbols type pins as "unspecified" | cosmetic; leave it, or fix pin types in the library |
| `lib_symbol_mismatch` | embedded copy differs from the library file (renamed or flattened) | expected for generated files |
| `endpoint_off_grid` | a coordinate that isn't a multiple of 1.27 mm (for example 183.05 instead of 182.88) | `check()` lists every one |
| `unconnected_wire_endpoint` | a dangling stub, e.g. a rail extended past the last tap | `check()` lists every one; shorten it |

## Placement and mirroring

| Symptom | Cause | Fix |
|---|---|---|
| `check_native()` reports `at[3]: ours '180', KiCad '0'` on a mirrored part | `rot 180` + `mirror y` is not KiCad's canonical spelling; it normalises the pair to `rot 0` + `mirror x` | write the canonical form. The two are geometrically identical -- verify by checking the pin positions, not by trusting either spelling |

## Text orientation

| Symptom | Cause | Fix |
|---|---|---|
| a reference or value reads upside down | the part is rotated 180 and the field angle was set to `(-rot) % 360` = 180, which KiCad draws literally | write field angle 0 at rot 180, and swap left/right justification to match |
| a reference or value reads top-to-bottom | field angle left at 0 on a part rotated 90 or 270 | angle = `(-rot) % 360`; KiCad normalises the result to bottom-to-top |
| text runs back over the part body | justification not compensated for the part's frame | `mirror="y"` flips left/right, and so does rot 180; both together cancel |
| a vertical label runs through a neighbouring part | the label was given angle 90 on a short wire between two parts | make it horizontal, or lengthen the wire |
| a reference sits hard against one of its own pin numbers | the part is boxed in and every candidate spot scored badly; the placer took the least bad | open space around it -- mirroring a 3-pin part so its odd pin leaves the other way frees the flank the reference wants |

## Rendering

| Symptom | Cause | Fix |
|---|---|---|
| **wires and junctions missing from SVG/PDF, but netlist fine** | a netclass in `.kicad_pro` has no `wire_width` | add `wire_width`/`bus_width` (`Design.write()` does it). It's **not** the stroke type — tested. |
| counting `stroke:none` in the SVG suggests wires are still hidden | junction dots are fill-only groups, so the count misleads | **look at the render**; don't grep the SVG |
| browser shows an old render | cache | the bundled viewer adds `?t=`; otherwise cache-bust by hand |
| `file://` SVG won't open in the Browser pane | local files blocked | serve the directory: `python -m http.server 8731 --bind 127.0.0.1` |
| Browser screenshot "timed out" | the page hadn't painted yet | take the screenshot again on its own |
| Read tool can't show the PDF | poppler not installed | use the SVGs in the browser |
| ref/value text on top of a connector or IC | side placement used for a tall, many-pin part | above/below placement for anything that isn't a 2-pin passive (schlib's rule) |
| ref/value text drawn **sideways** on parts rotated 90°/270° (horizontal resistors, a vertical TVS) | field angles are relative to the symbol's own rotation, so angle 0 on a rot-90 part reads vertically | field angle = `(-rotation) mod 360` (schlib does it) |
| ref/value text runs **back over its own part** on parts rotated 90° or 180° | at a net 180°, KiCad swaps left/right justification | same fix — angle `-rotation` keeps the justification literal; swap left/right under `mirror y`. Check with `probe_fields.py` |
| a horizontal two-pin part gets its text stacked *beside* it, on the wire | orientation was judged by the body's shape (EasyEDA diode bodies are tall) | judge by pin positions (schlib does) |
| text lies on a wire, a part or another label | the spot collided with something | `d.check_text()` finds these from KiCad's SVG. The field placer avoids them automatically; if a part is boxed in on every side, move wiring to open a gap |
| `write()` warns the drawing reaches the title-block corner | usually real — a note or block ended up under the title block | look at the render; move notes to a free column. The bottom-right corner belongs to the title block |
| page looks empty | paper too large for the drawing | `paper="auto"` or `suggest_paper()`; A5 for small sheets |
| a note runs through a part | text is wider than you expect (KiCad's stroke font advances ~0.85 × size per character) | break it with `\n` or move it |
| drawing overlaps the title block | too tall or too wide for the page | `write()` warns; bigger paper or two columns of blocks |

## Blocks and page composition

| Symptom | Cause | Fix |
|---|---|---|
| two block outlines overlap, or one clips the other's title | the rectangles were placed by hand with `box()` | declare blocks (`block_start`/`block_end`) and call `arrange()`; `check_blocks()` reports any that still overlap |
| labels, pin names, port names or notes lie across a dashed outline | hand-typed `box()` rectangles, usually one per sub-function, so a circuit's own wires cross them | one block per *circuit*, outlines derived from contents, notes inside their block. `check_text()` lists every offender (`outline_faults()`): ~100 on a 12-box, 50-part sheet, 0 after regrouping into 7 blocks |
| `outline_faults` fires on a *derived* outline (a value like `10uF/50V` on a block's flank) | the extent model missed something; it once guessed field text as body + 4 mm and counted characters for notes | fix `Sheet._bbox()` -- it now reads the real field spots from `layout_fields()` and measures with `text_width()` -- never nudge the part or pad the block to hide it |
| the user says the sheet is "too granular" / boxes everywhere | blocks were declared per sub-function (input / controller / output) | merge to what a reference design would give its own heading; structure a big block with a bold 1.4 mm sub-heading note, not another box. See layout-conventions.md, "How big is a block?" |
| converting an old multi-sheet generator whose builders each made a scratch sheet and were tiled by hand | `merge()` of scratch sheets copies lists, which breaks block slices | pass the ONE real sheet to every builder, let each wrap itself in a block in its own working coordinates (they may overlap: junctions are computed at emit time, after `arrange()`), turn `hlabel` into `label`, and call `arrange(relieve=False)` + `write(relieve=False)` if the wiring inside the blocks is hand-tuned |
| a block's outline runs through a note or a reference | the hand-placed rectangle predates the note | same fix: a declared block derives its outline from its contents |
| a narrow block's title runs into the next block along | titles are drawn from the top-left corner and are often wider than the block | `block_extent()` reserves the title's width; if a title is very long, shorten it |
| a note stays put while the blocks move around it | the note is a loose item in a *container* block, so it is not part of any child's flow | give it a block of its own: `s.block_start(None, pad=None)` |
| a wire is left behind when the blocks are arranged | it was added after its block closed, so it sits outside every block's index range | add it with `_insert_item(..., block)`; relief does |
| a net splits or merges after a layout change | a group boundary was wrong, or a move created a contact | see conventions 6.4: the three rules, and `verify()` names the net |
| `block_end() with no block open`, or `N block(s) never closed` | a `block_start` without its `block_end` (or the reverse) on the same sheet | pair them, or use `with s.block(...)` |
| a net breaks the moment blocks are arranged | a wire crossed a block boundary; blocks move as a unit | `verify()` catches it -- replace that wire with a label at each end |
| the sheet is mostly empty on one side | a tall block forced a tall row | stack the small blocks in a `pad=None, width=0` container so they form a column beside the tall one |
| the paper keeps growing to A1 | `arrange(grow=True)` steps up until the drawing fits | reduce `BLOCK_GUTTER`, split the sheet, or accept the larger paper |

## Crowding

Run `Design.check_text()` first: it measures KiCad's own render, names both
strings and gives the gap. Everything below assumes you have that list.

| Symptom | Cause | Fix |
|---|---|---|
| the checker says a sheet is clean and the render disagrees | the model's text metrics are wrong | `scripts/calibrate_text.py` re-measures the glyph advances from KiCad; remember the SVG font-size is 4/3 x the text size |
| a whole column of labels is reported as crowded | they are stacked on the 2.54 mm pin grid, which leaves 1.0 mm -- normal | that is what `CLEAR_STACK` is for; only side-by-side text needs 1.27 mm |
| a pin name is reported crowding its own pin number | attribution went by text instead of position | fixed: strings are matched to the model's boxes; pairs inside one symbol are excluded |
| a paragraph is reported as crowding itself, or its lines drift apart | consecutive `note()` calls were treated as separate items | `note_groups()` makes a paragraph one item; keep the lines at one line pitch and the same x |
| relief moved nothing and the crowding stayed | the item is not at a free wire end (a junction, or a label mid-wire) | look at the pair: usually the *parts* need room, which is `spread()`'s job, or the group is frozen because a wire ends on a sheet pin |
| a port's name sits on the pin it labels | the port's field offset was applied as a fixed "below", not turned with the symbol | fixed: the offset comes from the library symbol and is transformed like a pin |


| Symptom | Cause | Fix |
|---|---|---|
| a dense IC flank renders as a smear of overlapping text | power ports at the end of horizontal stubs are rotated to face the wire, so their names are drawn vertically | `upright_power_ports()` stands them up and staggers them; if you are drawing by hand, use horizontal net labels and keep the power port on that rail's decoupling cap |
| several GND flags in a row collide | one flag per pin on a 2.54 mm pitch | `bundle_ports()` joins them onto one bar with a single symbol, stepped out past the label lane |
| two notes look merged in the render although neither `check()` nor ERC complains | their text boxes are adjacent, not overlapping | `check_spacing()` flags anything closer than 1.27 mm; move one, or put them in different blocks |
| every line of a multi-line note "crowds" the next at 0.41 mm | `_attribute()` matched one line of the note to the note and left the rest as strangers | fixed 2026-09-20: a multi-line note owns every string inside its box. If it comes back, that is where to look -- KiCad's own line pitch is not a layout fault |
| the browser shows an old drawing after a re-render | an `http.server` from an earlier session still owns the port and serves its old folder; the new server failed to bind, silently (output went to /dev/null) | use a fresh port; check the render time in the SVG `<title>` |
| a PDF reference schematic cannot be read (no `pdftoppm`, text extraction returns nothing) | vendor schematics embed fonts with private encodings | find the same design as an image (Adafruit's learn pages publish `*_sch.png`; open the `original/` URL in the Browser pane) rather than navigating to the PDF, which only triggers a download dialog on the user's side |

## Workflow

| Symptom | Cause | Fix |
|---|---|---|
| `write()` refuses: "modified outside the generator" | the file was opened and saved in KiCad since the last generate (even just moving parts) | open it, **port the user's edits into the generator**, then `write(force=True)`. Backups go to `.schgen-backup/` automatically |
| a user says they rearranged things in KiCad | their edits live only in the .kicad_sch | never regenerate over it without asking; KiCad also keeps snapshots in `.history/` (File → Local History) |
| PCB lost its footprint links after regenerating | random UUIDs | schlib's UUIDs are deterministic; don't swap in `uuid4()` |
| net renames after switching labels to power ports (e.g. `VUSB_5V` → `+5V`) | power-port names are the net names | tell the user to run *Tools → Update PCB from Schematic* |

## Learned on the USB-C to M.2 adapter (2026-09-20)
| Symptom | Cause | Fix |
|---|---|---|
| `kicad-cli could not upgrade <project>.kicad_sym` | a hand-built library whose header `(version …)` is older than the symbol text pasted into it | copy the header from the stock library the symbol came from (`20251024`, generator_version "10.0") |
| pins added to a copied stock symbol do not appear | they were appended after the `_1_1` unit, inside `(embedded_fonts no …)` | insert them before the `)` that closes the `<name>_1_1` sub-symbol; check with `Library.get(id).pins()` |
| DRC parity: "'Exclude from bill of materials' settings differ" | mounting-hole, fiducial and test-point footprints carry that attribute; the symbols did not | `place(..., in_bom=False)` for parts whose footprint library is `MountingHole:`, `Fiducial:` or `TestPoint:` |
| KiCad does not offer the pair to the differential router / netclass pair rules ignore it | net names `USB_DP` / `USB_DM` | a pair must end `_P`/`_N` or `+`/`-`: `USB_D_P`, `USB_D_N` |
| `paper="auto"` put every block in one row and ran off the page (121 % wide) | the blocks were flowed once at the widest page, and the page chosen afterwards | fixed: `arrange()` now flows at each page width in turn and keeps the first page that fits |
| a stock symbol stacks many hidden pins on one point (`Bus_M.2_Socket_E`: 11 GND, 4 × 3.3 V) | intended | one `stub()` on the visible pin connects them all; list every stacked pin in `NETS` and `verify()` confirms it |
| connector hold-down tabs float | the stock symbol has no pins for them | copy the symbol into the project library and add passive pins (one visible, the rest hidden at the same point) numbered like the footprint's tab pads |

## Learned on a USB-C PD battery charger (2026-10-22)
| Symptom | Cause | Fix |
|---|---|---|
| ERC `multiple_net_names`: "Both GND and PGND" (or "GND and EP") on one item | a hidden pin typed **power input** is an implicit global net named after the pin, so a stacked hidden `PGND`/`EP` power-in pin creates its own net | type stacked hidden pins `passive`; keep power input on the one visible pin |
| an EasyEDA IC symbol lacks pin numbers the datasheet lists (TPS25751D: 20, 22, 23, 25, 33, 35) | the footprint fuses those leads into one custom pad (21, 24, 32, 34) | match the symbol to the footprint's pads, not the datasheet's pin count, and check the land pattern before layout |
| `check_text()` reports a value overlapping the *next* capacitor at 15.24 mm pitch | values like `10uF/50V` plus the reference need about 17.8 mm | space hanging parts by their text (`part_pitch()`), or stagger rows |
| ERC `pin_to_pin` "Bidirectional and Power output" on GPIOs tied to GND | unused GPIOs tied low as the datasheet asks; GND is driven by a PWR_FLAG | intentional; note it on the sheet |
| a power port at the end of a horizontal stub stays rotated on a dense IC flank | standing it up would collide | run each supply pin out and up to an upright port in its own column (2.54, 10.16, 17.78 mm out) |

## Learned redrawing a 52-part sensor board (2026-11-11)
| Symptom | Cause | Fix |
|---|---|---|
| a block reads as scattered parts joined by names | connections inside the block were made by label | wire them; `label_joins()` counts them. If a wire cannot be drawn, the symbol is usually at fault: `Design.functional_symbol` |
| a functional symbol's top or bottom pin names print across the side names | the body was sized for the flanks only | fixed in `functional_symbol`: the vertical names get a corridor between the flanks and the body grows until a top and a bottom name cannot meet. If it happens with a hand-made symbol, widen the body |
| wires stranded after a symbol was resized | the block was written against typed coordinates | anchor on `s.pin(ref, n)` and offsets from it |
| one row of blocks starts 18 mm further in than the next | a block was measured while others lay on top of it in local coordinates; reference and value text had moved out of their way, widening it | fixed: `arrange()` stands the blocks clear of each other before measuring (`_stage`) and re-measures after each move (`_land`) |
| a part's value lands on a wire that the block drew past the part | `place()` puts fields on the side of the part with no pins, which for a four-sided part is beside a pin | route that wire one text-width further out (`text_width(value)`), or the checks will report it |
| a legend written as one multi-line `note()` looks cramped | KiCad's line pitch for multi-line text is tighter than the 2.54 mm used everywhere else | one `note()` per line |
| the sheet needs A2 though the blocks' area fits A3 | row flow leaves the space under every short block empty | `arrange(layout=[...])`, `pad=3.81`, `gutter=10.16`, shorter block titles |
| `SPICE` reports a source where a fuse was meant | an element whose name starts with F is a current-controlled source | call it `Rptc` |
