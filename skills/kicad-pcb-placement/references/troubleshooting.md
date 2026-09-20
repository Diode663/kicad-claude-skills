# Troubleshooting — symptom → cause → fix

Every entry happened while placing a real board. Find the symptom here
before you theorise.

## The script crashes or behaves impossibly

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError: 'SwigPyObject' object has no attribute 'GetIsRuleArea'` (or `SetAuxOrigin`, or any method) | an earlier `board.Remove()` whose proxy was garbage collected corrupted the heap | route removals through `Board._remove()`: `thisown = False` **and** keep a reference. See `pcbnew-api.md` |
| segmentation fault at exit, after the script finished cleanly | same cause | same fix |
| the script printed nothing, but the exit code says it crashed | output was buffered and died with the process | print with `flush=True`; `pcblib.say()` does |
| output is full of `swig/python detected a memory leak of type …` | pcbnew says this at exit, on stdout | harmless; `grep -v "memory leak"` |
| the script seems to run twice, or imports fail oddly | a file in the project folder shadows a stdlib module (`bisect.py`, `code.py`, `types.py`) | rename it |
| `ModuleNotFoundError: pcbnew` | system Python | use KiCad's interpreter |
| `PAD.GetEffectivePolygon() missing 1 required positional argument: 'aLayer'` | KiCad 10 changed the signature | pass `F_Cu` or `B_Cu` |
| `input: 1 argument(s) expected. 0 provided.` from `kicad-cli pcb render` | the board path is positional and must be last | append it to the command |

## The placement isn't what the table says

| Symptom | Cause | Fix |
|---|---|---|
| `placement table does not match the board` | a part was added to the schematic and not the table, or vice versa | add it; the check exists so it can't silently sit at the origin |
| a part is still at the page origin | it was never placed and `check()` was skipped | run `check()` before `save()` |
| a two-pin part's ends are the wrong way round | rotation decides which pin faces where | check pad numbers in the render, not the body shape |
| a bottom-side part's angle looks mirrored | a flipped footprint reads its rotation from the bottom | look at the render; don't reason about it |
| parts move or duplicate after *Update PCB from Schematic* | footprints have no symbol UUID path | `link_schematic()` |
| the ratsnest is empty or wrong in KiCad | pad nets were never assigned | `link_schematic()` |
| `schematic_parity` is non-empty in the DRC report | the links didn't match the schematic | re-export the netlist; check refs match exactly |
| parity `net_conflict` on every net of one sheet, "/Power / Reset/X" vs "/Power {slash} Reset/X" | a sheet title contains `/`; the netlist export writes it raw, the board escapes it | `Board.netlist()` now escapes sheet titles (`_escape_net`); re-run `link_schematic()` and remove unused nets |
| parity `extra_footprint` for H1–H4 | mounting holes have no symbol | `hole()` marks them board-only (and out of BOM/position files) |
| parity `footprint_symbol_field_mismatch` on Datasheet or MPN | fields were never copied from the symbols to the footprints | copy Value, Datasheet and sourcing fields when syncing; `kicad-cli pcb drc --schematic-parity` is the check (plain `pcb drc` skips it) |
| KiCad reports a script-saved board as changed on its first save | footprint fields created from Python have no stroke width | `save()` sets 0.15 mm and runs `check_native()` |

## DRC

| Symptom | Cause | Action |
|---|---|---|
| dozens of `annular_width`, `drill_out_of_range`, `clearance` errors all naming one part | the library footprint is wrong, not your placement | fix the `.kicad_mod`, then `refresh_footprints()` — the board holds its own copy. Moving the part changes nothing |
| a fixed `.kicad_mod` changes nothing on the board | the board embeds a copy of every footprint | `refresh_footprints()`, or KiCad's *Update Footprints from Library* |
| clearance errors *between a pad and the vias inside it* | the vias have no pad number, so they count as a different net | number them like the pad |
| `hole_clearance` on a dense connector after making its pegs NPTH | the copper went away but the hole did not; this is the part's real geometry | ask the fab what it allows for NPTH-to-copper before shaving pads |
| `drill_out_of_range … actual 0.2500 mm` on a module's thermal vias | the footprint's vias are below the board's minimum drill, and often unnumbered so they connect to nothing | fix the footprint: give them the ground pad's number and a 0.3 mm drill |
| `padstack … PTH pad hole leaves no copper` on a connector | slot pads defined with no annular ring; locating pegs drawn as plated pads | the pegs should be NPTH |
| `items_not_allowed` for the module you placed deliberately | the keep-out forbids footprints | `SetDoNotAllowFootprints(False)` — forbid copper, allow parts |
| one `text_thickness` warning per part | silkscreen stroke below the fab minimum | set 0.15 mm; `tidy_references(thickness=0.15)` |
| `silk_edge_clearance` on a module or connector at the board edge | usually intentional | look at the render before "fixing" it |
| `silk_over_copper` naming one part twice | the footprint's own silkscreen crosses its own pad | library issue; ignore or fix the footprint |
| 96 `unconnected items` | nothing is routed | expected. Say so explicitly so nobody reads it as a fault |

## Silkscreen

| Symptom | Cause | Fix |
|---|---|---|
| `tidy_references` reports "tight, no fully clear spot" | the part is boxed in | it's a bounding-box estimate — check DRC; if real, move a neighbour or `hide=` the reference |
| a reference sits on top of its own part | the part's own field text was counted as an obstacle, so every candidate looked blocked | exclude `${REFERENCE}` / `${VALUE}` from the obstacle list |
| references drift far from their parts | the part's extent was measured including its text | measure extent from pads and artwork only |
| text is unreadable in the render | size below ~0.7 mm, or thickness below the fab minimum | 0.8 mm / 0.15 mm are sane defaults |

## Geometry

| Symptom | Cause | Fix |
|---|---|---|
| `check()` says two courtyards overlap by a tiny area | genuinely touching, or two footprints sharing an edge | move one; DRC will flag it too |
| `check()` says nothing but KiCad shows collisions | those footprints have no courtyard | `check()` lists them; add courtyards or verify by eye |
| parts look like they touch but every check passes | the footprints' pads reach outside their own courtyards, so courtyard clearance flatters them | `check()`'s pad-gap warning measures the pads; DRC's 0.2 mm electrical rule is not a spacing opinion |
| a part 0.3 mm from its neighbour and DRC is happy | 0.3 mm clears the electrical rule | that is not enough room to route between them or to assemble; raise `min_gap` and move one |
| "extends past the board edge" for a connector | expected for an edge-mounted part | confirm in the render, then ignore |
| rounded corners cross themselves | the radius exceeds half an edge | `outline_poly` clamps it; check the render if the outline is irregular |

## autoplace.py

| Symptom | Cause | Fix |
|---|---|---|
| `selftest FAILED` with a large error on connectors only | pad numbers repeat (shell, exposed pad), so comparing by number pairs the wrong pads | compare pads in `fp.Pads()` order — `selftest()` does |
| selftest passes but "no bottom-side parts" | the flip convention was not exercised | place one part on the bottom (or FIX one) and re-run before trusting double-sided output |
| a regulator's output caps are claimed by an M.2 socket or hub | rail caps match several ICs' power pins | designator affinity (`CU33O1`→`U331`) and the same-sheet rule resolve it; if the designers used neutral refs, add `EXTRA_RECIPES` or `NET_WEIGHTS`, and read the block list |
| an LDO claims the MCU's 3V3 decoupling caps (or the reverse) | on a single-sheet design with neutral designators, the regulator-output role and the MCU's decoupling role match the same caps, and specific recipes claim before the generic IC fallback | read the block list; pin the right owner with an `EXTRA_RECIPES` role for the MCU, or rename refs to carry the anchor (C3 → C_U1_3V3) |
| audit FAIL "other side" on a cap that is 0.02 mm away | the cap is directly behind its IC on the opposite side | support passives must share the anchor's side; fixed in split blocks — check `SIDE_RULES` if it reappears |
| `block X had no rigid fit -- split` | not enough contiguous room for the block's envelope | normal near capacity; look at the capacity line. `BLOCK_ENVELOPE = False` packs denser at the cost of interleaved blocks |
| `NO ROOM for …` | the board is full under the current rules | grow the board, relax a side rule or keep-out, or move a fixed module — report it, don't hide it |
| a block lands far from its REGION | REGIONS is a soft preference, and the region may be inside a keep-out or fully used | check the floor plan; move the region, or lock the anchor where you want it |
| run takes minutes | large board, fine grid | the 1 mm block grid and 0.5 mm member grid are the knobs; most time is spent on blocks that don't fit |

## Working with the user's board

| Symptom | Cause | Fix |
|---|---|---|
| `save()` refuses: "has been edited in KiCad" | someone moved parts or routed since the script last wrote it | port those changes into the table, then `save(force=True)`. The backup is in `.pcbgen-backup/` |
| the user's routing is dangling after a re-run | footprints moved under existing tracks | `check()` warns when tracks exist. Don't re-run over routed work |
| designator positions change between otherwise identical runs | the placement order tied, and the tie fell through to KiCad's footprint write order, which differs on every save | fixed: `tidy_references` orders by `_part_extent()` (pads/silk/courtyard, blind to the text) with `_natural_key(ref)` as tie-break. If you touch that ordering, re-check by running twice and diffing the reference positions |
| a designator still sits 0.05-0.15 mm from its neighbour, and moving the part does not help | the area really is full — parts crowded against a board edge all throw their references into the same strip | set that one by hand after the call: `b.fps["R20"].Reference().SetPosition(b.pt(x, y))` |
| mounting holes multiply or renumber every run | auto-named from the count of existing holes | number them in call order; `pcblib` does, and drops holes whose `hole()` call disappeared |

## When nothing here fits

Write a five-line script that isolates one call and run it. Vary one thing.
The `Remove()` ownership rule above was found that way, after two confident
and wrong theories about `GetDesignSettings()`. pcbnew's failures surface
far from their cause, so reasoning backwards from the symptom is usually
reasoning into a wall.

## Schematic parity: "pad net doesn't match" on unconnected pins only
An unused pin's net is named after the pin (`unconnected-(J2-PCM_CLK/I2S_SCK-Pad8)`), and
KiCad stores a `/` inside a pin name as `{slash}` on the board. `_escape_net()` used to
escape only sheet titles, so every unused pin whose name has a slash read as a net
mismatch (13 of them on an M.2 Key-E socket). Fixed 2026-09-20: `unconnected-(` names get
every `/` escaped. Related: `board.Zones()` returns untyped `SwigPyObject`s once a filled
zone has been loaded from disk -- use `board.GetArea(i)` for `i in range(GetAreaCount())`.

## JLCPCB preview shows an IC turned 90 or 180 degrees
JLCPCB places each part by the zero orientation and origin of *its own* footprint for that LCSC
number. Two parts in the same package can differ: USBLC6-2SC6 (C7519) uses a SOT-23-6 drawn with
pin 1 bottom-left (KiCad footprint at 90 deg, correction +270), TPS563201 (C116592) one with pin 1
bottom-right (KiCad at 180 deg, correction +180). A rule keyed on the footprint name cannot be right
for both. Measure instead of guessing: `uvx easyeda2kicad --lcsc_id Cxxxx --footprint --output tmp/x`,
read pad 1's position, find the KiCad rotation that puts KiCad's pad 1 there, and the correction is
minus that angle. Record it as an override for the designator in `<project>/jlc_rotations.json`.
The same comparison gives origin offsets for connectors (HRO TYPE-C-31-M-12: 1.57 mm toward the
signal pads). Also: a project copy of `jlc_cpl.py` looks for the default rules in
`../references/`, which does not exist beside a project, so only the project file applies there.

## Hybrid routing: scripted critical nets + Freerouting for the rest
`scripts/routelib.py` does everything in this section (`Router.autoroute`, `replay`, `normalise`,
`parse_ses`, `report`); the table is why it is written the way it is. Found on a 50-part
2-layer board, KiCad 10.0.6 + Freerouting 2.4.1, 2026-09-20.
Freerouting 2.4.1 needs **Java 25**; a portable Temurin JRE zip beside the jar is enough, nothing to
install. Headless run: `java -jar freerouting.jar --gui.enabled=false --router.fanout.enabled=false
--router.max_passes=30 --usage_and_diagnostic_data.disable_analytics=true --profile.allow_telemetry=false
--user_data_path=<work dir> -de in.dsn -do out.ses`. The old `-inc`, `-da`, `-dct` switches are gone in
2.4.x and `-l` is not what it was; settings are `--section.key=value`. Run it with `cwd` set to a work
folder: it drops a log directory wherever it starts. 45 s for a 50-part board.

| Symptom | Cause | Fix |
|---|---|---|
| the autorouter moves or rips up scripted tracks | `ExportSpecctraDSN` writes every existing track as `(type route)` | patch the DSN text: `(type route)` -> `(type protect)` |
| `ImportSpecctraSES` returns False, no message | KiCad's importer refuses a session that carries `(type protect)` | read the SES yourself: `(routes (network_out (net NAME (wire (path LAYER W x y ...)) (via "Via[0-1]_600:300_um" x y))))`, units from `(resolution um 10)`, Y negated, page coordinates. Items without `(type protect)` are the new ones |
| Freerouting lists scripted nets as unrouted and adds duplicate tracks | it does not count a track that ends inside a pad, off its centre, as connected (KiCad does); nor a T junction in mid-segment | split tracks at T junctions before export; keep a set of fully scripted nets and drop whatever it adds to them |
| netclass names come out as `GND,Default` | KiCad 10 exports the class list, comma-joined | strip `,Default ` from the DSN text |
| it routes ground as thin traces | ground is just another net to it | create the (unfilled) ground zones BEFORE the export: ground becomes a plane and is left alone. Fill after the autorouted tracks are in |
| it scatters vias under every SMD part | the fan-out pre-pass | `--router.fanout.enabled=false` |
| the result changes from run to run | the autorouter is not deterministic | store its new items as JSON in the project and replay them on ordinary runs; re-run it only when asked (`--autoroute`) |
| one pin is boxed in by its neighbour's track (SDA behind SCL at an MSOP) | the autorouter takes pins in its own order | script short stubs that bring the pair in in the right order; it picks the stubs up at their free ends |
| a signal takes a 25 mm detour under a module | a pull-up sits under the wrong pin and its track blocks the neighbour's exit | swap the two pull-ups; look at the picture after every autoroute, DRC will not complain |
| a run leaves a connection open that the last run made | the autorouter is not deterministic across invocations | `autoroute(tries=3)` draws each result on a trial board and lets **KiCad's DRC** count what is unconnected on the autorouter's own nets, re-running until zero; `best_of=N` keeps the best of N by (open, far-layer length, vias) |
| Freerouting's log lists connections as unrouted that KiCad shows connected | its log judges scripted copper by its own stricter rules | never use its log as the oracle; use the trial-board DRC |
| `track_dangling` on a scripted stub | the autorouter joined the stub part-way along and left the rest | end stubs where it must arrive (keep them short), or re-roll |
| far-layer ground comes out in two pieces, or a pad's thermal is starved, after a re-roll | a different run put a far-layer track or a top track somewhere new | `Router.report()` after every `--autoroute`; keep the stored run that was clean |
Order that works: scripted routes -> ground vias and any net-specific zones -> ground zones (unfilled)
-> split T junctions -> export -> Freerouting -> keep new items on non-scripted nets -> replay -> fill -> DRC.

## JLCPCB package
| Symptom | Cause | Fix |
|---|---|---|
| `easyeda2kicad` says "Can't find the folder" | it will not create the output folder | make it first (`jlc_compare.py` does) |
| `easyeda2kicad` / the EasyEDA API answers HTTP 403 for every part | rate limit after a burst of ~25 requests | stop; run again later. Do not work round it. What was fetched is cached |
| the assembly quote insists on Standard PCBA | one part is "Standard Only" (radio modules usually are) | check `jlcpcb.com/partdetail/Cxxxx` for every non-passive before promising Economic |
| the package builder's DRC gate fails on errors the project has accepted | a library footprint's own pegs/pads violate the board rule | whitelist exactly those (type + the footprint named on both items), never a whole type |
| two BOM lines for one LCSC number | the parts have different Values (SW1 "RESET", SW2 "BOOT") | group by (footprint, LCSC), use the MPN as the Comment when values differ |
| stock KiCad SOT-23 / SOT-223 turned 180 in the preview | KiCad draws pin 1 top-left, JLCPCB's footprints for these have it bottom-right | `jlc_compare.py` finds it per LCSC part; do not apply it by package name |

## Things DRC will not tell you
| Symptom | Cause | Fix |
|---|---|---|
| a connector tab or big pad solders badly | a same-net via sits inside the SMD pad; DRC only checks different nets | `Board.check_via_in_pad()` after any scripted via placement |
| tracks run through pads after adding an unrelated part | auto-placed parts moved; the routing script used old coordinates | put every routed part in `FIXED`; assert pad positions in the router (`pad(ref, n, expect=...)`) |
| `GetArea()` / `Zones()` return bare `SwigPyObject` | a removed track or zone proxy was garbage-collected | keep every removed item in a list for the life of the process |
| a "gap" between two parts is not there | a rotated 1210's pads are 2.7 mm wide | read pad sizes and positions from the board, never from the package name |
| hidden designators reappear at their library position | KiCad resets fields on Update PCB / Update Footprints | re-run the tidy step; `REF_HIDE` always wins, and a library-position designator is not treated as a user move |

## 3D models
- LCSC/EasyEDA has models for most parts: `uvx easyeda2kicad --lcsc_id Cxxxx --full --output dir/name`.
  Not all (the MLD M.2 socket has none) -- then build an envelope from the drawing
  (`references/example_build_models.py`): VRML boxes, 1 unit = 2.54 mm, model Y is up while
  footprint Y is down.
- KiCad's stock 3D library may lack a model its footprint names (HRO TYPE-C-31-M-12 here):
  the render silently shows bare pads.
- An EasyEDA model's origin is its own footprint's. On a KiCad stock footprint of the same
  part, offset it by the difference between the two footprints' pads or stakes.
- STEP heights without a CAD kernel: the extremes of `CARTESIAN_POINT` are good enough for a
  clearance check.
