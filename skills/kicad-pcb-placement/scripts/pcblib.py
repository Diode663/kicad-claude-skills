#!/usr/bin/env python3
"""pcblib -- component placement for KiCad PCBs, driven from Python.

Run the generator with KiCad's own Python, which is where ``pcbnew`` lives::

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" place_components.py
    /usr/lib/kicad/bin/python3 place_components.py                 # Linux
    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 place_components.py

The board is described in **board-relative millimetres**: the origin is the
top-left corner of the outline and Y points down, the same convention as the
schematic side. ``origin`` puts that corner somewhere sensible on the page.

Typical use::

    from pcblib import Board

    b = Board(__file__, "myboard", size=(62, 44))
    b.outline(radius=1.0)
    b.place_all(PLACEMENT)                      # {ref: (x, y, rot)}
    b.keepout("ANTENNA", [(30, 0), (62, 0), (62, 7), (30, 7)])
    b.hole(3, 3, 3.2)
    b.link_schematic()                          # symbol UUIDs + pad nets
    b.tidy_references()
    b.check()                                   # geometry oracle, before saving
    b.save()
    b.drc()                                     # KiCad's oracle, categorised
    b.report_nets(["SW", "SDA", "SCL"])
    b.render(outdir)

Everything except ``link_schematic``, ``drc`` and ``render`` works offline;
those three shell out to ``kicad-cli``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import pcbnew


# --------------------------------------------------------------------------
# units and small helpers
# --------------------------------------------------------------------------

def mm(v):
    """Millimetres -> internal units."""
    return pcbnew.FromMM(float(v))


def tomm(v):
    """Internal units -> millimetres."""
    return pcbnew.ToMM(v)


def _area_mm2(poly):
    """SHAPE_POLY_SET area in mm^2 (Area() is in internal units squared)."""
    return tomm(tomm(poly.Area()))


def _poly(points):
    sp = pcbnew.SHAPE_POLY_SET()
    sp.NewOutline()
    for x, y in points:
        sp.Append(int(x), int(y))
    return sp


def _bbox_mm(item, grow=0.0):
    bb = item.GetBoundingBox()
    g = mm(grow)
    return (tomm(bb.GetX() - g), tomm(bb.GetY() - g),
            tomm(bb.GetRight() + g), tomm(bb.GetBottom() + g))


def _hit(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _box_gap(a, b):
    """Shortest distance between two axis-aligned boxes; 0 if they touch."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _is_own_field(fp, item):
    """True for a footprint's own reference or value text.

    ``FOOTPRINT.GraphicalItems()`` hands back the reference and value as
    PCB_TEXT, usually holding the literal ``${REFERENCE}``. Leave them in and
    a reference collides with itself, so the placer shoves it somewhere worse.
    """
    get = getattr(item, "GetText", None)
    if get is None:
        return False
    return get() in ("${REFERENCE}", "${VALUE}",
                     fp.GetReference(), fp.GetValue())


def _silk_shapes(fp, layers):
    """A footprint's real silkscreen artwork, without its own field text."""
    return [it for it in fp.GraphicalItems()
            if it.GetLayer() in layers and not _is_own_field(fp, it)]


def _part_extent(fp):
    """A footprint's own extent in mm, from its pads, silkscreen and courtyard.

    Deliberately blind to the reference text. That text is the thing being
    placed, so letting it into the extent would make both the placement
    order and the candidate slots depend on where the previous run left it.
    """
    xs, ys = [], []
    for p in fp.Pads():
        bx = _bbox_mm(p)
        xs += [bx[0], bx[2]]
        ys += [bx[1], bx[3]]
    for it in _silk_shapes(fp, (pcbnew.F_SilkS, pcbnew.F_CrtYd,
                                pcbnew.B_SilkS, pcbnew.B_CrtYd)):
        try:
            bx = _bbox_mm(it)
            xs += [bx[0], bx[2]]
            ys += [bx[1], bx[3]]
        except Exception:
            pass
    if not xs:
        bx = _bbox_mm(fp)
        xs, ys = [bx[0], bx[2]], [bx[1], bx[3]]
    return min(xs), min(ys), max(xs), max(ys)


def _natural_key(ref):
    """R2 before R10, and the same answer on every run."""
    return tuple((1, int(s)) if s.isdigit() else (0, s)
                 for s in re.split(r"(\d+)", ref) if s != "")


def say(*args):
    """print() that flushes.

    pcbnew can segfault while the interpreter shuts down, after the script
    itself has finished. Buffered output dies with it, so every report this
    module prints would vanish exactly when you need to read it.
    """
    print(*args, flush=True)


#: schematic fields that must not be copied onto a footprint
_SKIP_FIELDS = ("Footprint", "Reference", "ki_fp_filters")

#: value prefix marking a mounting hole this module owns
_HOLE_VALUE = "MountingHole_"
_FID_VALUE = "Fiducial_"


def _find_cli():
    env = os.environ.get("KICAD_CLI")
    if env and os.path.exists(env):
        return env
    for c in (shutil.which("kicad-cli"),
              r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
              r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
              "/usr/bin/kicad-cli",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if c and os.path.exists(c):
            return c
    raise RuntimeError("kicad-cli not found; set the KICAD_CLI environment variable")


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------


# ===========================================================================
# Fab stackups and netclasses
# ===========================================================================
# A board without a stackup has no impedance: KiCad cannot compute one, the
# fab has nothing to build to, and "0.2 mm looked about right" is what ends up
# on a 90-ohm pair. These are JLCPCB's own published constructions -- material
# names, thicknesses and Dk exactly as they specify them -- so the geometry you
# calculate is the geometry they build.
#
# Add a stackup by copying the numbers from the fab, never by estimating them.
JLC_STACKUPS = {
    # JLCPCB's default 4-layer 1.6 mm: outer 1 oz, inner 0.5 oz.
    "JLC04161H-7628": {
        "thickness": 1.6,
        "finish": "ENIG",
        "layers": [
            ("F.Cu", "copper", 0.035, None, None),
            ("dielectric 1", "prepreg", 0.2104, "Nan Ya Plastics NP-155F 7628", 4.4),
            ("In1.Cu", "copper", 0.0152, None, None),
            ("dielectric 2", "core", 1.065, "Nan Ya Plastics NP-155F Core", 4.43),
            ("In2.Cu", "copper", 0.0152, None, None),
            ("dielectric 3", "prepreg", 0.2104, "Nan Ya Plastics NP-155F 7628", 4.4),
            ("B.Cu", "copper", 0.035, None, None),
        ],
    },
    # Same 4-layer board with the thin 3313 prepreg: narrower impedance
    # geometry, useful when a 0.25 mm pair will not fit.
    "JLC04161H-3313": {
        "thickness": 1.6,
        "finish": "ENIG",
        "layers": [
            ("F.Cu", "copper", 0.035, None, None),
            ("dielectric 1", "prepreg", 0.0994, "Nan Ya Plastics NP-155F 3313", 4.1),
            ("In1.Cu", "copper", 0.0152, None, None),
            ("dielectric 2", "core", 1.2650, "Nan Ya Plastics NP-155F Core", 4.6),
            ("In2.Cu", "copper", 0.0152, None, None),
            ("dielectric 3", "prepreg", 0.0994, "Nan Ya Plastics NP-155F 3313", 4.1),
            ("B.Cu", "copper", 0.035, None, None),
        ],
    },
}

# JLCPCB's standard 2-layer 1.6 mm, as their impedance calculator reports it
# (Layers 2, 1.6 mm, 1 oz, read 2026-09-20): 0.030 copper / 1.43 core / 0.030.
# Dk 4.5 for 2-layer boards is from their capabilities page. JLCPCB does not
# offer tested impedance on 2 layers -- see references/stackup-impedance.md.
JLC_STACKUPS["JLC0216A"] = {
    "thickness": 1.6,
    "finish": "HAL lead-free",
    "layers": [
        ("F.Cu", "copper", 0.030, None, None),
        ("dielectric 1", "core", 1.43, "FR-4 core", 4.5),
        ("B.Cu", "copper", 0.030, None, None),
    ],
}

MASK = ("JLCPCB Soldermask", 0.01524, 3.8)


def stackup_sexp(preset, finish=None):
    """The (stackup ...) block for a JLC preset, in KiCad 10's own spelling."""
    spec = JLC_STACKUPS[preset]
    out = ["\t\t(stackup"]
    out.append('\t\t\t(layer "F.SilkS"\n\t\t\t\t(type "Top Silk Screen")\n\t\t\t)')
    out.append('\t\t\t(layer "F.Paste"\n\t\t\t\t(type "Top Solder Paste")\n\t\t\t)')
    out.append(f'\t\t\t(layer "F.Mask"\n\t\t\t\t(type "Top Solder Mask")\n'
               f"\t\t\t\t(thickness {MASK[1]})\n"
               f'\t\t\t\t(material "{MASK[0]}")\n'
               f"\t\t\t\t(epsilon_r {MASK[2]})\n\t\t\t\t(loss_tangent 0)\n\t\t\t)")
    for name, typ, th, mat, er in spec["layers"]:
        body = [f'\t\t\t(layer "{name}"', f'\t\t\t\t(type "{typ}")']
        if typ != "copper":
            body.append('\t\t\t\t(color "FR4 natural")')
        body.append(f"\t\t\t\t(thickness {th})")
        if mat:
            body.append(f'\t\t\t\t(material "{mat}")')
        if er:
            body.append(f"\t\t\t\t(epsilon_r {er})")
            body.append("\t\t\t\t(loss_tangent 0.02)")
        body.append("\t\t\t)")
        out.append("\n".join(body))
    out.append(f'\t\t\t(layer "B.Mask"\n\t\t\t\t(type "Bottom Solder Mask")\n'
               f"\t\t\t\t(thickness {MASK[1]})\n"
               f'\t\t\t\t(material "{MASK[0]}")\n'
               f"\t\t\t\t(epsilon_r {MASK[2]})\n\t\t\t\t(loss_tangent 0)\n\t\t\t)")
    out.append('\t\t\t(layer "B.Paste"\n\t\t\t\t(type "Bottom Solder Paste")\n\t\t\t)')
    out.append('\t\t\t(layer "B.SilkS"\n\t\t\t\t(type "Bottom Silk Screen")\n\t\t\t)')
    out.append(f'\t\t\t(copper_finish "{finish or spec["finish"]}")')
    out.append("\t\t\t(dielectric_constraints yes)")
    out.append("\t\t)")
    return "\n".join(out) + "\n"


_DP_COMPLEMENT = {"P": "N", "N": "P", "+": "-", "-": "+"}


def _dp_complement(name):
    """(polarity, complement) the way BOARD::MatchDpSuffix reads a net name:
    backwards, skipping digits and underscores, wanting P/N or +/- next."""
    count = 0
    for i in range(len(name) - 1, -1, -1):
        ch = name[i]
        if ch.isdigit() or ch == "_":
            count += 1
            continue
        if ch in _DP_COMPLEMENT:
            cut = len(name) - count - 1
            return (1 if ch in "P+" else -1), name[:cut] + _DP_COMPLEMENT[ch] + name[cut + 1:]
        break
    return 0, None


_NETCLASS_DEFAULT = {
    "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2, "line_style": 0,
    "microvia_diameter": 0.3, "microvia_drill": 0.1, "name": "Default",
    "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647,
    "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.2,
    "tuning_profile": "", "via_diameter": 0.6, "via_drill": 0.3,
    "wire_width": 6,
}

def create_board(script_or_dir, name, layers=2):
    """Write an empty <name>.kicad_pcb with `layers` copper layers, if there is
    none yet, so a generated project never needs a trip through the GUI:

        create_board(__file__, NAME, layers=2)
        b = Board(__file__, NAME, size=...); b.sync_footprints(); b.save(force=True)

    Returns True when it created the file. Never touches an existing board."""
    here = script_or_dir
    if os.path.isfile(here):
        here = os.path.dirname(os.path.abspath(here))
    path = os.path.join(here, name + ".kicad_pcb")
    if os.path.exists(path):
        return False
    b = pcbnew.NewBoard(path)
    b.SetCopperLayerCount(int(layers))
    pcbnew.SaveBoard(path, b)
    say("created empty %d-layer board %s" % (layers, os.path.basename(path)))
    return True


class Board:
    """A .kicad_pcb being laid out from a placement table."""

    def __init__(self, script_or_dir, name, size=(100.0, 80.0),
                 origin=(100.0, 50.0), cli=None):
        here = script_or_dir
        if os.path.isfile(here):
            here = os.path.dirname(os.path.abspath(here))
        self.dir = here
        self.name = name
        self.pcb = os.path.join(here, name + ".kicad_pcb")
        self.sch = os.path.join(here, name + ".kicad_sch")
        self.W, self.H = float(size[0]), float(size[1])
        self.origin = (float(origin[0]), float(origin[1]))
        self.cli = cli or _find_cli()
        self.manifest = os.path.join(here, ".pcbgen-manifest.json")

        if not os.path.exists(self.pcb):
            raise SystemExit(
                f"{self.pcb} does not exist.\n"
                "Call pcblib.create_board(dir, name, layers=2) and then "
                "Board.sync_footprints(), or create it in KiCad (File > New "
                "Board, Update PCB from Schematic).")
        self.board = pcbnew.LoadBoard(self.pcb)
        self._removed = []                 # see _remove(); do not let these die
        ds = self.board.GetDesignSettings()
        ds.SetAuxOrigin(self.pt(0, 0))     # fab files measure from the corner
        ds.SetGridOrigin(self.pt(0, 0))
        self.fps = {f.GetReference(): f for f in self.board.GetFootprints()}
        # Holes and fiducials this script added on a previous run are ours
        # to manage, not parts from the schematic: keep them out of the
        # placement table's accounting, and drop the ones this run does
        # not re-create.
        self._holes = {r: f for r, f in self.fps.items()
                       if f.GetValue().startswith((_HOLE_VALUE, _FID_VALUE))}
        for r in self._holes:
            del self.fps[r]
        self._stale_holes = dict(self._holes)
        self._hole_seq = 0
        self._fid_seq = 0
        # Named rule areas are ours too: drop the ones this run does not
        # re-create, or a keep-out deleted from the script lives on in the
        # board file and DRC keeps reporting it.
        self._stale_zones = {z.GetZoneName(): z for z in self.board.Zones()
                             if z.GetIsRuleArea() and z.GetZoneName()}
        self.outline_pts = []
        self.keepouts = {}
        self._placed = {}
        self._routed = len(list(self.board.GetTracks())) > 0

    # -- coordinates ------------------------------------------------------

    def pt(self, x, y):
        """Board-relative mm -> page VECTOR2I."""
        return pcbnew.VECTOR2I(mm(self.origin[0] + x), mm(self.origin[1] + y))

    def rel(self, v):
        """Page VECTOR2I -> board-relative mm (x, y)."""
        return (tomm(v.x) - self.origin[0], tomm(v.y) - self.origin[1])

    # -- removal ----------------------------------------------------------

    def _remove(self, item):
        """Take an item off the board without corrupting the heap.

        BOARD.Remove() hands ownership back to Python, so when the proxy is
        collected SWIG deletes an object the board still points at. The heap
        corruption that follows shows up far from the cause: a later
        board.Zones() or GetDesignSettings() returns an untyped SwigPyObject,
        or the interpreter segfaults on exit after everything looked fine.

        Verified on pcbnew 10.0.6: dropping the proxy crashes, disowning it
        and keeping a reference does not.
        """
        self.board.Remove(item)
        try:
            item.thisown = False
        except Exception:
            pass
        self._removed.append(item)

    # -- outline ----------------------------------------------------------

    def outline(self, radius=0.0, width=0.1):
        """Draw the board edge as a rectangle, optionally rounded.

        Replaces whatever is already on Edge.Cuts, so re-running is safe.
        """
        self.outline_poly([(0, 0), (self.W, 0), (self.W, self.H), (0, self.H)],
                          radius=radius, width=width)

    def outline_poly(self, points, radius=0.0, width=0.1):
        """Draw an arbitrary closed polygon on Edge.Cuts.

        ``radius`` rounds every corner, clamped to half the shorter adjacent
        edge so short edges degrade to square corners instead of crossing.
        """
        for d in list(self.board.GetDrawings()):
            if d.GetLayer() == pcbnew.Edge_Cuts:
                self._remove(d)
        pts = [(float(x), float(y)) for x, y in points]
        self.outline_pts = pts
        n = len(pts)

        def seg(a, b):
            s = pcbnew.PCB_SHAPE(self.board)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetStart(self.pt(*a))
            s.SetEnd(self.pt(*b))
            s.SetLayer(pcbnew.Edge_Cuts)
            s.SetWidth(mm(width))
            self.board.Add(s)

        if radius <= 0:
            for i in range(n):
                seg(pts[i], pts[(i + 1) % n])
        else:
            ends = []
            for i in range(n):
                p, c, q = pts[i - 1], pts[i], pts[(i + 1) % n]
                v1 = (p[0] - c[0], p[1] - c[1])
                v2 = (q[0] - c[0], q[1] - c[1])
                l1 = math.hypot(*v1) or 1.0
                l2 = math.hypot(*v2) or 1.0
                r = min(radius, l1 / 2, l2 / 2)
                a = (c[0] + v1[0] / l1 * r, c[1] + v1[1] / l1 * r)
                bb = (c[0] + v2[0] / l2 * r, c[1] + v2[1] / l2 * r)
                cen = (a[0] + v2[0] / l2 * r, a[1] + v2[1] / l2 * r)
                ends.append((a, bb, cen, r))
            for i in range(n):
                a, bb, cen, r = ends[i]
                if r > 1e-6:
                    arc = pcbnew.PCB_SHAPE(self.board)
                    arc.SetShape(pcbnew.SHAPE_T_ARC)
                    arc.SetCenter(self.pt(*cen))
                    arc.SetStart(self.pt(*a))
                    arc.SetEnd(self.pt(*bb))
                    arc.SetLayer(pcbnew.Edge_Cuts)
                    arc.SetWidth(mm(width))
                    self.board.Add(arc)
                seg(ends[i][1], ends[(i + 1) % n][0])


    # -- placement --------------------------------------------------------

    def place(self, ref, x, y, rot=0, side="top"):
        """Put one footprint at board-relative (x, y), rotated ``rot`` degrees.

        ``side="bottom"`` flips it. A flipped part's rotation reads from the
        bottom, which is one reason to keep everything on top unless the
        board genuinely needs two-sided assembly.
        """
        fp = self.fps.get(ref)
        if fp is None:
            raise SystemExit(f"{ref} is not on the board -- run "
                             "'Update PCB from Schematic' first")
        want_bottom = side == "bottom"
        if fp.IsFlipped() != want_bottom:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetPosition(self.pt(x, y))
        fp.SetOrientationDegrees(rot)
        self._placed[ref] = (x, y, rot, side)
        return fp

    def place_all(self, table):
        """Place every entry of ``{ref: (x, y, rot[, side])}``.

        Refuses to run if the table and the board disagree. That is the
        cheapest way to catch a part added to the schematic and forgotten
        here, which otherwise ends up sitting at the page origin.
        """
        missing = sorted(set(table) - set(self.fps))
        unplaced = sorted(set(self.fps) - set(table))
        if missing or unplaced:
            raise SystemExit(
                "placement table does not match the board:\n"
                f"  in the table but not on the board: {missing}\n"
                f"  on the board but not in the table: {unplaced}")
        for ref, spec in table.items():
            self.place(ref, *spec)

    # -- library sync -----------------------------------------------------

    def _lib_dirs(self):
        """{nickname: directory} from the project and global fp-lib-table."""
        tables = [os.path.join(self.dir, "fp-lib-table")]
        if os.name == "nt":
            base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Preferences/kicad")
        else:
            base = os.path.expanduser("~/.config/kicad")
        if os.path.isdir(base):
            for ver in sorted(os.listdir(base), reverse=True):
                t = os.path.join(base, ver, "fp-lib-table")
                if os.path.exists(t):
                    tables.append(t)

        roots = [r"C:\Program Files\KiCad\10.0\share\kicad",
                 r"C:\Program Files\KiCad\9.0\share\kicad",
                 "/usr/share/kicad",
                 "/Applications/KiCad/KiCad.app/Contents/SharedSupport"]

        def expand(uri):
            uri = uri.replace("${KIPRJMOD}", self.dir)
            for var in re.findall(r"\$\{([A-Z0-9_]+)\}", uri):
                val = os.environ.get(var)
                if not val and var.endswith("_FOOTPRINT_DIR"):
                    for r in roots:
                        if os.path.isdir(os.path.join(r, "footprints")):
                            val = os.path.join(r, "footprints")
                            break
                if val:
                    uri = uri.replace("${%s}" % var, val)
            return os.path.normpath(uri)

        dirs = {}
        for t in tables:
            try:
                text = open(t, encoding="utf-8").read()
            except Exception:
                continue
            for nick, uri in re.findall(
                    r'\(lib\s+\(name\s+"?([^")\s]+)"?\).*?\(uri\s+"?([^")]+)"?\)',
                    text, re.S):
                dirs.setdefault(nick, expand(uri))
        # KiCad's own libraries, by nickname, for anything no table names: a
        # generated project has no reason to list Resistor_SMD to use it
        # (schlib already resolves stock symbol libraries this way)
        for r in roots:
            stock = os.path.join(r, "footprints")
            if os.path.isdir(stock):
                for d in os.listdir(stock):
                    if d.endswith(".pretty"):
                        dirs.setdefault(d[:-7], os.path.join(stock, d))
                break
        return dirs

    def _load_footprint(self, libid, dirs):
        """Load 'nickname:name' from the resolved library table."""
        nick, _, name = libid.partition(":")
        if not name:
            nick, name = "", libid
        cands = [(nick, dirs[nick])] if nick in dirs else []
        if not cands:
            cands = [(n, d) for n, d in dirs.items()
                     if os.path.exists(os.path.join(d, name + ".kicad_mod"))]
        for cand_nick, path in cands:
            try:
                fp = pcbnew.FootprintLoad(path, name)
            except Exception:
                fp = None
            if fp is not None:
                lid = pcbnew.LIB_ID()
                lid.SetLibNickname(pcbnew.UTF8(cand_nick))
                lid.SetLibItemName(pcbnew.UTF8(name))
                fp.SetFPID(lid)
                return fp
        return None

    def sync_footprints(self, prune=False):
        """Bring the board's footprint list into line with the schematic.

        This is the import half of KiCad's *Update PCB from Schematic*, so a
        part added to the schematic reaches the board without a round trip
        through the GUI. New footprints land at the origin with no nets;
        place_all() and link_schematic() sort that out.

        A part whose symbol now names a DIFFERENT footprint is swapped for the
        new one in place. Without this a footprint changed in the schematic
        never reaches the board -- refresh_footprints() reloads each part by
        the ID the board already holds -- and the board quietly keeps the old
        one (this is how a board kept 1.5 mm test pads its schematic had
        dropped for 1.0 mm).

        Parts on the board with no symbol are reported and left alone unless
        `prune` is set -- deleting is the caller's decision. Pass prune=True
        when the schematic is the source of truth.
        """
        parts = self.schematic_parts()
        dirs = self._lib_dirs()
        added, failed, swapped = [], [], []
        for ref in sorted(set(parts) & set(self.fps)):
            want = parts[ref]["footprint"]
            old = self.fps[ref]
            fpid = old.GetFPID()
            have = "%s:%s" % (fpid.GetLibNickname(), fpid.GetLibItemName())
            if not want or want == have:
                continue
            new = self._load_footprint(want, dirs)
            if new is None:
                failed.append("%s (%s not found in any library)" % (ref, want))
                continue
            new.SetReference(ref)
            new.SetValue(parts[ref]["value"])
            new.SetPosition(old.GetPosition())
            new.SetOrientation(old.GetOrientation())
            new.SetPath(old.GetPath())
            if old.IsFlipped() != new.IsFlipped():
                new.Flip(new.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            self._remove(old)
            self.board.Add(new)
            self.fps[ref] = new
            swapped.append("%s %s -> %s" % (ref, have, want))
        for ref in sorted(set(parts) - set(self.fps) - set(self._holes)):
            libid = parts[ref]["footprint"]
            if not libid:
                failed.append("%s (no footprint assigned in the schematic)" % ref)
                continue
            fp = self._load_footprint(libid, dirs)
            if fp is None:
                failed.append("%s (%s not found in any library)" % (ref, libid))
                continue
            fp.SetReference(ref)
            fp.SetValue(parts[ref]["value"])
            fp.SetPosition(self.pt(0, 0))
            self.board.Add(fp)
            self.fps[ref] = fp
            added.append("%s %s" % (ref, libid))
        orphans = sorted(set(self.fps) - set(parts))
        if prune and orphans:
            for ref in orphans:
                self._remove(self.fps.pop(ref))
            say("removed %d footprint(s) the schematic no longer has: %s"
                % (len(orphans), ", ".join(orphans)))
            orphans = []
        if swapped:
            say("swapped %d footprint(s) to match the schematic: %s"
                % (len(swapped), "; ".join(swapped)))
        if added:
            say("added %d footprint(s) from the schematic: %s"
                % (len(added), ", ".join(added)))
        if failed:
            say("  could not add: " + "; ".join(failed))
        if orphans:
            say("  on the board but not in the schematic (left alone): "
                + ", ".join(orphans))
        return added

    def refresh_footprints(self, refs=None):
        """Reload footprints from their libraries, keeping where they are.

        This is KiCad's *Update Footprints from Library* from a script. A
        board stores its own copy of every footprint, so fixing a .kicad_mod
        changes nothing until the board is told to re-read it.

        Position, rotation, side, reference, value and the schematic link are
        carried over, so it is safe to call before or after place_all().
        """
        dirs = self._lib_dirs()
        done, skipped = [], []
        for ref, fp in sorted(self.fps.items()):
            if refs and ref not in refs:
                continue
            fpid = fp.GetFPID()
            nick = str(fpid.GetLibNickname())
            name = str(fpid.GetLibItemName())
            cands = [(nick, dirs[nick])] if nick in dirs else []
            if not cands:
                # A board this method has already touched may have lost the
                # nickname, so fall back to whichever library holds the name.
                cands = [(n, d) for n, d in dirs.items()
                         if os.path.exists(os.path.join(d, name + ".kicad_mod"))]
            new, from_nick = None, nick
            for cand_nick, path in cands:
                try:
                    new = pcbnew.FootprintLoad(path, name)
                except Exception:
                    new = None
                if new is not None:
                    from_nick = cand_nick
                    break
            if new is None:
                skipped.append("%s (%s:%s)" % (ref, nick, name))
                continue
            before = len(list(fp.Pads()))
            # FootprintLoad() takes a directory, so the footprint comes back
            # with no library nickname. Put it back, or the board forgets
            # where each part came from and KiCad's own library tools stop
            # working on it.
            lid = pcbnew.LIB_ID()
            # these setters want pcbnew.UTF8, not a Python str
            lid.SetLibNickname(pcbnew.UTF8(from_nick))
            lid.SetLibItemName(pcbnew.UTF8(name))
            new.SetFPID(lid)
            new.SetReference(ref)
            new.SetValue(fp.GetValue())
            new.SetPosition(fp.GetPosition())
            new.SetOrientation(fp.GetOrientation())
            new.SetPath(fp.GetPath())
            if fp.IsFlipped() != new.IsFlipped():
                new.Flip(new.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            self._remove(fp)
            self.board.Add(new)
            self.fps[ref] = new
            after = len(list(new.Pads()))
            done.append("%s%s" % (ref, "" if before == after
                                  else " (%d->%d pads)" % (before, after)))
        say("refreshed %d footprints from their libraries" % len(done))
        if skipped:
            say("  library not found, left as-is: " + ", ".join(skipped))
        return done

    # -- rule areas and holes --------------------------------------------

    def keepout(self, name, points, tracks=True, vias=True, pads=True,
                fill=True, footprints=False, layers=None):
        """A rule area. The defaults forbid copper but allow footprints.

        Allowing footprints matters for a radio module whose antenna
        legitimately overhangs its own keep-out: forbid them and DRC shouts
        about the part you placed on purpose.
        """
        for z in list(self.board.Zones()):
            if z.GetIsRuleArea() and z.GetZoneName() == name:
                self._remove(z)
        self._stale_zones.pop(name, None)
        z = pcbnew.ZONE(self.board)
        z.SetIsRuleArea(True)
        z.SetZoneName(name)
        z.SetDoNotAllowTracks(tracks)
        z.SetDoNotAllowVias(vias)
        z.SetDoNotAllowPads(pads)
        z.SetDoNotAllowFootprints(footprints)
        for setter in ("SetDoNotAllowZoneFills", "SetDoNotAllowCopperPour"):
            if hasattr(z, setter):
                getattr(z, setter)(fill)
        ls = pcbnew.LSET()
        for layer in layers or (pcbnew.F_Cu, pcbnew.B_Cu):
            ls.AddLayer(layer)
        z.SetLayerSet(ls)
        z.Outline().NewOutline()
        for x, y in points:
            p = self.pt(x, y)
            z.Outline().Append(p.x, p.y)
        self.board.Add(z)
        self.keepouts[name] = [(float(x), float(y)) for x, y in points]
        return z

    def hole(self, x, y, drill, ref=None, annulus=0.0):
        """A mounting hole, as a one-pad footprint.

        ``annulus=0`` gives a non-plated hole; a positive value gives a
        plated one with that much copper ring. KiCad has no standalone hole
        object, so a footprint is the only way to add one from a script.
        """
        if ref is None:
            # numbered in call order, so re-running reuses the same refs
            self._hole_seq += 1
            ref = "H%d" % self._hole_seq
        if ref in self._holes:
            self._remove(self._holes.pop(ref))
            self._stale_holes.pop(ref, None)
        fp = pcbnew.FOOTPRINT(self.board)
        fp.SetReference(ref)
        fp.SetValue(_HOLE_VALUE + "%gmm" % drill)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        # no schematic symbol exists for a hole: mark it board-only so DRC's
        # schematic-parity check doesn't report it as an extra footprint
        for setter in ("SetBoardOnly", "SetExcludedFromBOM", "SetExcludedFromPosFiles"):
            if hasattr(fp, setter):
                getattr(fp, setter)(True)
        pad = pcbnew.PAD(fp)
        pad.SetNumber("" if annulus <= 0 else "1")
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        od = drill + 2 * annulus
        pad.SetSize(pcbnew.VECTOR2I(mm(od), mm(od)))
        pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_CIRCLE)
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(drill), mm(drill)))
        if annulus <= 0:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
            pad.SetLayerSet(pad.UnplatedHoleMask())
        else:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetLayerSet(pad.PTHMask())
        fp.Add(pad)
        fp.SetPosition(self.pt(x, y))
        self.board.Add(fp)
        self._holes[ref] = fp
        self._stale_holes.pop(ref, None)
        return fp

    def fiducial(self, x, y, ref=None, libid="Fiducial:Fiducial_1mm_Mask2mm"):
        """An optical alignment target for the assembler's pick-and-place.

        Loaded from KiCad's stock Fiducial library, then marked board-only:
        a fiducial is board geometry, not a part, so it has no schematic
        symbol and must stay out of the BOM and the placement file. Managed
        exactly like hole() -- re-running reuses the ref, and one dropped
        from the script is removed from the board.

        Three of them in an L, as far apart as the layout allows, is what
        assemblers ask for; two is the minimum.
        """
        if ref is None:
            self._fid_seq += 1
            ref = "FID%d" % self._fid_seq
        if ref in self._holes:
            self._remove(self._holes.pop(ref))
            self._stale_holes.pop(ref, None)
        fp = self._load_footprint(libid, self._lib_dirs())
        if fp is None:
            raise RuntimeError(
                "fiducial footprint %r not found -- add its library to "
                "fp-lib-table" % libid)
        fp.SetReference(ref)
        name = libid.rpartition(":")[2]
        fp.SetValue(name if name.startswith(_FID_VALUE) else _FID_VALUE + name)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        for setter in ("SetBoardOnly", "SetExcludedFromBOM", "SetExcludedFromPosFiles"):
            if hasattr(fp, setter):
                getattr(fp, setter)(True)
        fp.SetPosition(self.pt(x, y))
        self.board.Add(fp)
        self._holes[ref] = fp
        self._stale_holes.pop(ref, None)
        return fp

    # -- schematic linkage ------------------------------------------------

    def _netlist_root(self):
        """Parse KiCad's own netlist export once and keep it."""
        if getattr(self, "_nl_root", None) is None:
            fd, path = tempfile.mkstemp(suffix=".xml")
            os.close(fd)
            try:
                subprocess.run([self.cli, "sch", "export", "netlist",
                                "--format", "kicadxml", "-o", path, self.sch],
                               capture_output=True, check=True)
                self._nl_root = ET.parse(path).getroot()
            finally:
                os.remove(path)
        return self._nl_root

    def schematic_parts(self):
        """{ref: {footprint, value, uuid}} for every symbol in the schematic."""
        out = {}
        for c in self._netlist_root().iter("comp"):
            fp = c.find("footprint")
            val = c.find("value")
            out[c.get("ref")] = {
                "footprint": fp.text if fp is not None else "",
                "value": val.text if val is not None else "",
                "uuid": c.find("tstamps").text,
            }
        return out

    @staticmethod
    def _field(fp, name):
        """A footprint field by name, whatever the KiCad API offers."""
        if hasattr(fp, "GetFieldByName"):
            f = fp.GetFieldByName(name)
            if f is not None:
                return f
        for f in fp.GetFields():
            if f.GetName() == name:
                return f
        return None

    def schematic_fields(self):
        """{ref: {"value": str, "fields": {name: text}}} from the netlist export."""
        out = {}
        for c in self._netlist_root().iter("comp"):
            f = {}
            ds = c.find("datasheet")
            if ds is not None and (ds.text or "").strip():
                f["Datasheet"] = ds.text.strip()
            for fl in c.iter("field"):
                if fl.get("name") and (fl.text or "").strip():
                    f[fl.get("name")] = fl.text.strip()
            val = c.find("value")
            out[c.get("ref")] = {"value": val.text if val is not None else None, "fields": f}
        return out

    def netlist(self):
        """KiCad's own netlist: ({ref: symbol_uuid}, {net: [(ref, pin)]})."""
        root = self._netlist_root()
        uuids = {c.get("ref"): c.find("tstamps").text for c in root.iter("comp")}
        nets = {self._escape_net(n.get("name")): [(x.get("ref"), x.get("pin"))
                                                   for x in n.findall("node")]
                for n in root.iter("net")}
        return uuids, nets

    def _escape_net(self, name):
        """Net names as the board stores them.

        The netlist export writes a sheet titled "Power / Reset" into net
        names as "/Power / Reset/NET", but the board (and DRC's schematic
        parity check) spells that "/Power {slash} Reset/NET". Left raw, every
        net on such a sheet reads as a net conflict."""
        if "/" not in name:
            return name
        if name.startswith("unconnected-("):
            # KiCad names an unused pin's net after the pin, and a pin called
            # "PCM_CLK/I2S_SCK" is stored as "PCM_CLK{slash}I2S_SCK"
            return name.replace("/", "{slash}")
        if getattr(self, "_slash_titles", None) is None:
            titles = set()
            for f in os.listdir(self.dir):
                if f.endswith(".kicad_sch"):
                    text = open(os.path.join(self.dir, f), encoding="utf-8").read()
                    titles.update(t for t in re.findall(r'\(property "Sheetname" "([^"]*)"', text) if "/" in t)
            self._slash_titles = sorted(titles, key=len, reverse=True)
        for t in self._slash_titles:
            name = name.replace("/" + t + "/", "/" + t.replace("/", "{slash}") + "/")
        return name

    def link_schematic(self):
        """Bind footprints to their symbols and assign pad nets.

        Do this and the ratsnest is right the moment the board opens, and
        'Update PCB from Schematic' finds nothing to change. Skip it and
        KiCad treats the footprints as strangers on the next sync.
        """
        uuids, nets = self.netlist()
        sheet = os.path.basename(self.sch)
        fields = self.schematic_fields()
        for ref, fp in self.fps.items():
            if ref not in uuids:
                continue                       # mounting holes and the like
            fp.SetPath(pcbnew.KIID_PATH("/" + uuids[ref]))
            if hasattr(fp, "SetSheetfile"):
                fp.SetSheetfile(sheet)
                fp.SetSheetname("/")
            # Value and the symbol's fields belong on the footprint too, or
            # DRC's schematic-parity check reports every part as a mismatch
            # (and the fab BOM/position files lose the sourcing data).
            f = fields.get(ref, {})
            if f.get("value") is not None and fp.GetValue() != f["value"]:
                fp.SetValue(f["value"])
            for name, text in f.get("fields", {}).items():
                # "Footprint" is implicit on a footprint: KiCad drops a
                # property of that name on its next save.
                if name in _SKIP_FIELDS:
                    continue
                cur = self._field(fp, name)
                if cur is None:
                    fp.SetField(name, text)
                    cur = self._field(fp, name)
                elif cur.GetText() != text:
                    cur.SetText(text)
                if cur is not None:
                    cur.SetVisible(False)          # sourcing data, not silkscreen
                    if cur.GetTextThickness() <= 0:
                        cur.SetTextThickness(mm(0.15))
        assigned = 0
        for name, nodes in nets.items():
            net = self.board.FindNet(name)
            if net is None:
                net = pcbnew.NETINFO_ITEM(self.board, name)
                self.board.Add(net)
            for ref, pin in nodes:
                fp = self.fps.get(ref)
                if fp is None:
                    continue
                for pad in fp.Pads():
                    if pad.GetNumber() == pin:
                        pad.SetNet(net)
                        assigned += 1
        say("linked %d symbols, %d pads on %d nets"
              % (len(uuids), assigned, len(nets)))
        return nets

    # -- silkscreen -------------------------------------------------------

    # -- reference designators: what tidy wrote, and what the user changed ---

    def _ref_ledger_path(self):
        return os.path.join(self.dir, ".pcbgen-refs.json")

    def _ref_state(self, fp):
        """A designator's state in its footprint's own frame, so that moving or
        rotating the part (the text travels with it) is not mistaken for an edit."""
        t = fp.Reference()
        p, q = t.GetPosition(), fp.GetPosition()
        bx, by = tomm(p.x - q.x), tomm(p.y - q.y)
        a = math.radians(fp.GetOrientationDegrees())
        dx = bx * math.cos(a) - by * math.sin(a)
        dy = bx * math.sin(a) + by * math.cos(a)
        return {"dx": round(dx, 3), "dy": round(dy, 3),
                "angle": round((t.GetTextAngleDegrees() - fp.GetOrientationDegrees()) % 360, 1),
                "visible": bool(t.IsVisible()), "flipped": bool(fp.IsFlipped())}

    @staticmethod
    def _ref_same(a, b, tol=0.01):
        return (abs(a["dx"] - b["dx"]) <= tol and abs(a["dy"] - b["dy"]) <= tol
                and abs(((a["angle"] - b["angle"]) + 180) % 360 - 180) <= 0.5
                and a["visible"] == b["visible"] and a["flipped"] == b["flipped"])

    def _ref_library_default(self, fp, cache={}):
        """Where the library puts this footprint's reference. A designator sitting
        exactly there was reset by KiCad (Update PCB / Update Footprints), not
        placed by a person."""
        libid = fp.GetFPIDAsString()
        if libid not in cache:
            cache[libid] = None
            try:
                lib = self._load_footprint(libid, self._lib_dirs())
                if lib is not None:
                    p = lib.Reference().GetPosition()
                    cache[libid] = (round(tomm(p.x), 3), round(tomm(p.y), 3))
            except Exception:
                pass
        return cache[libid]

    def _ref_ledger(self):
        try:
            return json.load(open(self._ref_ledger_path(), encoding="utf-8"))
        except Exception:
            return {}

    def adopt_moved_references(self, size=0.8, thickness=0.15, hide=()):
        """Start the ledger on a board that was tidied before the ledger existed.

        tidy_references() is deterministic, so whatever differs from a dry run of
        it is something a person changed. Those designators are pinned; the rest
        are recorded as tidy's own. Nothing on the board is changed."""
        now = {r: self._ref_state(fp) for r, fp in self.fps.items()}
        keep = {r: (fp.Reference().GetPosition(), fp.Reference().GetTextAngleDegrees(),
                    fp.Reference().IsVisible(), fp.Reference().GetTextSize(),
                    fp.Reference().GetTextThickness()) for r, fp in self.fps.items()}
        self.tidy_references(size, thickness, hide, _dry=True)
        dry = {r: self._ref_state(fp) for r, fp in self.fps.items()}
        for r, fp in self.fps.items():                     # put everything back
            t = fp.Reference()
            pos, ang, vis, sz, th = keep[r]
            t.SetPosition(pos); t.SetTextAngleDegrees(ang); t.SetVisible(vis)
            t.SetTextSize(sz); t.SetTextThickness(th)
        ledger, pinned = {}, []
        for r in self.fps:
            moved = r not in hide and not self._ref_same(now[r], dry[r])
            ledger[r] = dict(now[r], pinned=moved)
            if moved:
                pinned.append(r)
        with open(self._ref_ledger_path(), "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=1, sort_keys=True)
        say("reference ledger started: %d designator(s) pinned as moved by hand: %s"
            % (len(pinned), ", ".join(sorted(pinned, key=_natural_key)) or "none"))
        return pinned

    def tidy_references(self, size=0.8, thickness=0.15, hide=(), retidy=(), _dry=False):
        """Move every reference designator somewhere it is actually readable.

        Designators a person has moved are left alone. tidy records what it
        wrote for each one in ``.pcbgen-refs.json`` (in the footprint's own
        frame); next time, a designator whose state differs from that record
        was edited in KiCad, so it is pinned: kept as it is, avoided by the
        others, and kept on every later run. Two things are not edits: a
        designator back at its library position (KiCad resets them on Update
        PCB / Update Footprints), and anything in ``hide`` (the config wins).
        ``retidy=("R1", ...)`` or ``retidy="all"`` unpins.

        Tries above, below, left, right and then the diagonals, and takes the
        first spot whose text box clears pads, silkscreen, the board edge and
        the references already placed. Biggest parts go first, because they
        have the fewest good spots.

        If nothing beside the part is clear it widens the search: the same
        eight spots with the text turned 90 degrees (a column of parts against
        a board edge has room for upright text and none for level text), then
        both again one, two and three steps further out. Other parts' bodies
        (courtyards) count as obstacles: pads and silk alone leave the middle
        of a SOT-223 looking like free board, and a designator printed there
        is covered the moment the part is fitted.

        ``thickness`` defaults to 0.15 mm, the usual fab minimum for
        silkscreen line width. KiCad's own default is thinner, and DRC then
        complains about every part on the board.
        """
        everything = list(self.fps.values()) + list(self._holes.values())
        pads = [_bbox_mm(p, 0.1) for fp in everything for p in fp.Pads()]
        silk = []
        for fp in everything:
            for it in _silk_shapes(fp, (pcbnew.F_SilkS, pcbnew.B_SilkS)):
                try:
                    silk.append(_bbox_mm(it, 0.1))
                except Exception:
                    pass
        # Other parts' bodies are obstacles too. Pads and silk alone leave the
        # middle of a SOT-223 or an inductor looking like free board, and a
        # designator printed there is covered the moment the part is fitted.
        bodies = []
        for fp in self.fps.values():
            try:
                cy = fp.GetCourtyard(fp.GetLayer() == pcbnew.F_Cu and pcbnew.F_CrtYd
                                     or pcbnew.B_CrtYd).BBox()
                if cy.GetWidth() > 0:
                    x0, y0 = tomm(cy.GetX()), tomm(cy.GetY())
                    bodies.append((fp.GetReference(),
                                   (x0, y0, x0 + tomm(cy.GetWidth()), y0 + tomm(cy.GetHeight()))))
            except Exception:
                pass
        ox, oy = self.origin
        edge = (ox + 0.4, oy + 0.4, ox + self.W - 0.4, oy + self.H - 0.4)
        placed, moved, crowded = [], 0, []

        def _order_key(fp):
            # Biggest parts first: they have the fewest good spots. The
            # reference breaks ties, and it has to, because every 0402 on
            # the board has exactly the same extent. KiCad writes the
            # footprints out in a different order on every save, so a tie
            # left to dict order is decided by the last save -- that is
            # what made this pass path-dependent, with designators drifting
            # between otherwise identical runs.
            x0, y0, x1, y1 = _part_extent(fp)
            return (-round((x1 - x0) * (y1 - y0), 6),
                    _natural_key(fp.GetReference()))

        ledger = {} if _dry else self._ref_ledger()
        kept = []
        for fp in self.fps.values():
            ref = fp.GetReference()
            old = ledger.get(ref)
            if _dry or ref in hide or old is None or retidy == "all" or ref in retidy:
                continue
            now = self._ref_state(fp)
            lib = self._ref_library_default(fp)
            reset = lib is not None and abs(now["dx"] - lib[0]) <= 0.01 and abs(now["dy"] - lib[1]) <= 0.01
            if old.get("pinned") or (not self._ref_same(now, old) and not reset):
                kept.append(ref)
                if now["visible"]:
                    placed.append(_bbox_mm(fp.Reference(), 0.05))   # the others keep clear of it
        order = sorted(self.fps.values(), key=_order_key)
        for fp in order:
            t = fp.Reference()
            if fp.GetReference() in kept:
                continue
            if fp.GetReference() in hide:
                t.SetVisible(False)
                continue
            t.SetVisible(True)
            t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
            t.SetTextThickness(mm(thickness))
            t.SetTextAngleDegrees(0)
            t.SetKeepUpright(True)
            t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
            t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)

            # the part's extent from its pads and outline -- never from its
            # own text, which is the thing being moved
            left, top, right, bot = _part_extent(fp)
            cx, cy = (left + right) / 2, (top + bot) / 2
            was = t.GetPosition()
            was_angle = t.GetTextAngleDegrees()
            best = None
            # nearest first, level text before upright: the first clear spot
            # wins, so this order is the preference order
            for extra in (0.0, 0.7, 1.4, 2.1):
                for angle in (0, 90):
                    # the spots need the text's size, which is only known
                    # once the string is set at its final size and angle
                    t.SetTextAngleDegrees(angle)
                    t.SetPosition(pcbnew.VECTOR2I(mm(cx), mm(cy)))
                    box = t.GetBoundingBox()
                    hw, hh = tomm(box.GetWidth()) / 2, tomm(box.GetHeight()) / 2
                    gx, gy = 0.25 + hw + extra, 0.25 + hh + extra
                    cands = [(cx, top - gy), (cx, bot + gy),
                             (left - gx, cy), (right + gx, cy),
                             (left, top - gy), (right, top - gy),
                             (left, bot + gy), (right, bot + gy)]
                    for x, y in cands:
                        t.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
                        tb = _bbox_mm(t, 0.05)
                        score = (sum(_hit(tb, o) for o in pads)
                                 + sum(_hit(tb, o) for o in silk)
                                 + sum(_hit(tb, o) for o in placed)
                                 + sum(_hit(tb, o) for r, o in bodies
                                       if r != fp.GetReference()))
                        outside = not (tb[0] >= edge[0] and tb[1] >= edge[1]
                                       and tb[2] <= edge[2] and tb[3] <= edge[3])
                        score += 5 * outside
                        if best is None or score < best[0]:
                            best = (score, x, y, angle)
                        if score == 0:
                            break
                    if best[0] == 0:
                        break
                if best[0] == 0:
                    break
            t.SetTextAngleDegrees(best[3])
            t.SetPosition(pcbnew.VECTOR2I(mm(best[1]), mm(best[2])))
            if t.GetPosition() != was or t.GetTextAngleDegrees() != was_angle:
                moved += 1
            if best[0] > 0:
                crowded.append(fp.GetReference())
            placed.append(_bbox_mm(t, 0.05))
        if not _dry:
            out = {}
            for ref, fp in self.fps.items():
                out[ref] = dict(self._ref_state(fp), pinned=ref in kept)
            with open(self._ref_ledger_path(), "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1, sort_keys=True)
            say("tidied %d reference designators (%d moved)" % (len(placed) - len(kept), moved))
            if kept:
                say("  kept as you left them in KiCad (%d): %s -- retidy=(...) to release"
                    % (len(kept), ", ".join(sorted(kept, key=_natural_key))))
        if crowded and not _dry:
            say("  tight, no fully clear spot: " + ", ".join(sorted(crowded)))
            say("  these are bounding-box estimates -- DRC decides. If it "
                  "reports silk_over_copper, open a gap or hide=(...) them.")
        return crowded

    # -- checks -----------------------------------------------------------


    def check_via_in_pad(self, quiet=False):
        """Vias whose centre lies inside an SMD pad. DRC says nothing when the
        net is the same, which is exactly how a scripted stitching via ends up
        in a connector's hold-down tab: solder wicks down it in reflow and the
        mechanical joint is starved. Returns [(ref, pad, net, x, y)]."""
        hits = []
        pads = [(fp.GetReference(), p) for fp in self.board.GetFootprints() for p in fp.Pads()
                if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
        for t in self.board.GetTracks():
            if t.GetClass() != "PCB_VIA":
                continue
            pos = t.GetPosition()
            for ref, p in pads:
                if p.HitTest(pos):
                    x, y = self.rel(pos)
                    hits.append((ref, p.GetNumber(), t.GetNetname(), round(x, 2), round(y, 2)))
        if not quiet:
            if hits:
                say("via in SMD pad (%d) -- move it off the pad or accept it knowingly:" % len(hits))
                for ref, num, net, x, y in hits:
                    say("   %s pad %s [%s] at (%.2f, %.2f)" % (ref, num, net, x, y))
            else:
                say("via in pad: none")
        return hits

    # ---- fab setup ---------------------------------------------------------
    def set_stackup(self, preset, inner=("power", "power"), finish=None, quiet=False):
        """Write the physical stackup into the saved board.

        `inner` types the internal copper layers in the layer table: "power"
        for a solid plane, "signal" for a routed one. Two solid GND planes on
        a 4-layer board is what gives every outer-layer trace a reference
        0.2 mm away and makes a layer change survivable -- with a signal layer
        in the middle there is no reference to return on.

        Call after save(); KiCad preserves the block on later saves."""
        text = open(self.pcb, encoding="utf-8").read()
        block = stackup_sexp(preset, finish)
        if "(stackup" in text:
            start = text.index("\t\t(stackup")
            depth, i = 0, start
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            text = text[:start] + block.rstrip("\n") + text[i + 1:]
        else:
            anchor = "\t(setup\n"
            text = text.replace(anchor, anchor + block, 1)
        # type the inner layers so KiCad and the fab agree on what they are
        for n, kind in zip(("In1.Cu", "In2.Cu"), inner):
            text = re.sub(r'\((\d+) "%s" \w+\)' % n, r'(\1 "%s" %s)' % (n, kind), text)
        with open(self.pcb, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if not quiet:
            spec = JLC_STACKUPS[preset]
            top = next(l for l in spec["layers"] if l[1] in ("prepreg", "core"))
            say(f"stackup: {preset}, {spec['thickness']} mm, inner layers "
                f"{inner[0]}/{inner[1]}, {top[2]} mm {top[3].split()[-1]} "
                f"(Dk {top[4]}) under the outer copper")
        return True

    def set_netclasses(self, classes, patterns, rules=None, quiet=False):
        """Write netclasses, their net patterns and the board rules.

        A netclass with no pattern controls nothing. Patterns have to cover
        *every* segment of a bus -- both sides of a series resistor, both
        sides of Ethernet magnetics -- or half the pair silently keeps the
        default width, which is the most common way a "controlled impedance"
        board turns out not to be one.

        `diff_pair_width` and `diff_pair_gap` apply only to nets KiCad has
        recognised as a pair, from their names alone -- they are gated on
        A.inDiffPair() in drc_engine.cpp. Set them and run check_diff_pairs()
        in the same breath; a class whose nets are not paired quietly routes
        at `track_width` instead."""
        path = os.path.join(self.dir, f"{self.name}.kicad_pro")
        pro = json.load(open(path, encoding="utf-8"))
        ns = pro.setdefault("net_settings", {})
        existing = {c["name"]: c for c in ns.get("classes", [])}
        default = existing.get("Default") or _NETCLASS_DEFAULT.copy()
        out = [default]
        for name, spec in classes.items():
            c = dict(default)
            c.update({"name": name, "priority": len(out)})
            c.update(spec)
            out.append(c)
        ns["classes"] = out
        ns["netclass_patterns"] = [{"netclass": n, "pattern": p} for n, p in patterns]
        if rules:
            ds = pro.setdefault("board", {}).setdefault("design_settings", {})
            ds.setdefault("rules", {}).update(rules)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(pro, f, indent=2)
            f.write("\n")
        if not quiet:
            say(f"netclasses: {len(classes)} class(es), {len(patterns)} pattern(s)")
            for name, spec in classes.items():
                w = spec.get("track_width")
                dw = spec.get("diff_pair_width")
                g = spec.get("diff_pair_gap")
                say(f"   {name:16s} track {w} mm" +
                    (f", pair {dw}/{g} mm" if dw else "") +
                    f", via {spec.get('via_diameter')}/{spec.get('via_drill')} mm")
        return True

    def check_netclasses(self, quiet=False):
        """Every net in a controlled class, and every net in none of them.

        Answers the question the fab cannot: did the patterns actually catch
        the nets they were written for?"""
        path = os.path.join(self.dir, f"{self.name}.kicad_pro")
        pro = json.load(open(path, encoding="utf-8"))
        pats = pro.get("net_settings", {}).get("netclass_patterns", [])
        nets = sorted({self.board.GetNetInfo().GetNetItem(i).GetNetname()
                       for i in range(self.board.GetNetInfo().GetNetCount())}
                      - {""})
        hit = {}
        for p in pats:
            rx = re.compile("^" + re.escape(p["pattern"])
                            .replace(r"\*", ".*").replace(r"\?", ".") + "$")
            for n in nets:
                if rx.match(n):
                    hit.setdefault(n, []).append(p["netclass"])
        if not quiet:
            say(f"netclass coverage: {len(hit)} of {len(nets)} nets matched")
            for n, cs in sorted(hit.items()):
                if len(cs) > 1:
                    say(f"   {n} matches {len(cs)} classes: {cs}")
        return hit



    def check_diff_pairs(self, classes=None, quiet=False):
        """Ask pcbnew which nets it considers differential pairs.

        KiCad stores no pair list. It infers a pair from the two *names*, in
        BOARD::MatchDpSuffix: read the name backwards, skip any run of digits
        and underscores, and the next character must be P/N or +/-, upper
        case. The complement is that character swapped, and the pair exists
        only if a net with exactly that name exists. So:

            USB_P / USB_N      pair      USB_DP / USB_DM     NOT a pair
            TD1_P / TD1_N      pair      TD1P   / TD1M       NOT a pair
            USB+  / USB-       pair      USB_p  / USB_n      NOT a pair

        An unrecognised pair is not an error anywhere. You simply lose the
        differential-pair router, the length tuner, the skew and coupling
        tests, and the netclass's `diff_pair_width` / `diff_pair_gap` -- that
        constraint is gated on A.inDiffPair() in drc_engine.cpp, so on an
        unrecognised pair it never fires and both legs route at the class's
        ordinary track width, at whatever impedance that gives.

        This is the oracle: it uses pcbnew's own DpCoupledNet() rather than a
        copy of the rule. `classes` defaults to every netclass that sets
        diff_pair_width -- the ones where being unrecognised costs money.
        Fix any it reports in the *schematic*, then re-sync: renaming on the
        board alone is undone by the next netlist import.

        Returns (paired, unpaired) lists of net names."""
        path = os.path.join(self.dir, f"{self.name}.kicad_pro")
        pro = json.load(open(path, encoding="utf-8"))
        ns = pro.get("net_settings", {})
        if classes is None:
            # a class that only inherits Default's diff_pair_width was never
            # meant to carry pairs; single-ended power nets would otherwise
            # all read as "unpaired"
            base = next((c.get("diff_pair_width") for c in ns.get("classes", [])
                         if c["name"] == "Default"), None)
            classes = {c["name"] for c in ns.get("classes", [])
                       if c.get("diff_pair_width") not in (None, base)}
        classes = set(classes)
        info = self.board.GetNetInfo()
        nets = sorted({info.GetNetItem(i).GetNetname()
                       for i in range(info.GetNetCount())} - {""})
        want = []
        for p in ns.get("netclass_patterns", []):
            if p["netclass"] in classes:
                want.append(re.compile("^" + re.escape(p["pattern"])
                                       .replace(r"\*", ".*").replace(r"\?", ".") + "$"))
        if not want:
            if not quiet:
                say("diff pairs: no netclass sets diff_pair_width, nothing to check")
            return [], []
        paired, unpaired = [], []
        for n in nets:
            if not any(rx.match(n) for rx in want):
                continue
            ni = self.board.FindNet(n)
            other = self.board.DpCoupledNet(ni) if ni else None
            if other:
                paired.append((n, other.GetNetname()))
            else:
                unpaired.append(n)
        if not quiet:
            say(f"diff pairs: {len(paired)} net(s) coupled, {len(unpaired)} unpaired "
                f"in {sorted(classes)}")
            for n in unpaired:
                pol, comp = _dp_complement(n)
                why = (f"KiCad looks for {comp!r}, which no net is called"
                       if pol else "the name does not end in P/N or +/- (upper case, "
                                   "trailing digits and underscores ignored)")
                say(f"   {n}: {why}")
            if unpaired:
                say("   these route at the class's ordinary track width, not "
                    "diff_pair_width -- rename them in the schematic and re-sync")
        return paired, unpaired

    def check_return_vias(self, max_distance=1.0, classes=("USB_90", "ETH_100", "RF_50"),
                          quiet=False):
        """Every layer change on a controlled-impedance net needs a ground via
        beside it.

        A signal that changes layer has to bring its return current with it.
        Between two plane layers the return crosses through the nearest
        connection between those planes -- a stitching via. If the closest one
        is millimetres away, the return loops that far out and back, and the
        loop is the antenna: it radiates, it couples into whatever it encloses,
        and the pair's impedance is wrong for the length of the detour.

        The rule of thumb is one ground via within about 1 mm of each signal
        via, ideally one per pair. This reports the distance to the nearest
        ground via for every signal via on the named netclasses, worst first.
        Run it after routing; before routing it has nothing to look at."""
        pro_path = os.path.join(self.dir, f"{self.name}.kicad_pro")
        want = set()
        if os.path.exists(pro_path):
            pro = json.load(open(pro_path, encoding="utf-8"))
            pats = pro.get("net_settings", {}).get("netclass_patterns", [])
            for p in pats:
                if p["netclass"] in classes:
                    rx = re.compile("^" + re.escape(p["pattern"])
                                    .replace(r"\*", ".*").replace(r"\?", ".") + "$")
                    want.add(rx)
        gnd_names = {"GND", "/GND", "AGND", "DGND"}
        sig_vias, gnd_vias = [], []
        for item in self.board.Tracks():
            if item.Type() != pcbnew.PCB_VIA_T:
                continue
            net = item.GetNetname()
            pos = (item.GetPosition().x / 1e6, item.GetPosition().y / 1e6)
            if net in gnd_names:
                gnd_vias.append(pos)
            elif any(rx.match(net) for rx in want):
                sig_vias.append((net, pos))
        if not sig_vias:
            if not quiet:
                say("return vias: no controlled-impedance vias yet "
                    "(nothing routed, or no netclass patterns)")
            return []
        out = []
        for net, (x, y) in sig_vias:
            if gnd_vias:
                d = min(math.hypot(x - gx, y - gy) for gx, gy in gnd_vias)
            else:
                d = float("inf")
            out.append((d, net, (round(x, 2), round(y, 2))))
        out.sort(reverse=True)
        bad = [o for o in out if o[0] > max_distance]
        if not quiet:
            say(f"return vias: {len(sig_vias)} signal via(s) on controlled nets, "
                f"{len(gnd_vias)} ground via(s); {len(bad)} farther than "
                f"{max_distance:.1f} mm from a ground via")
            for d, net, pos in bad[:12]:
                say(f"   {d:5.2f} mm  {net} at {pos}")
        return bad

    def check(self, min_gap=0.5):
        """Geometry checks, run before saving. Raises on errors.

        Catches what is cheap to find here and expensive to find in KiCad:
        parts never placed, courtyards overlapping, pads from different parts
        crowding each other, parts hanging off the board, copper inside a
        keep-out.

        ``min_gap`` is the comfortable clearance between the pads of two
        different footprints, in mm. It is deliberately larger than any
        electrical rule: DRC passes at 0.2 mm, but two parts that close leave
        no room to route between them and no margin for assembly.
        """
        errors, warnings = [], []

        for ref in sorted(self.fps):
            if ref not in self._placed:
                errors.append(ref + " was never placed")

        # courtyard overlaps, per side
        cys = {}
        for ref, fp in self.fps.items():
            for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                cy = fp.GetCourtyard(layer)
                if cy.OutlineCount():
                    cys.setdefault(layer, {})[ref] = cy
        for layer, d in cys.items():
            refs = sorted(d)
            for i, a in enumerate(refs):
                for b in refs[i + 1:]:
                    sp = pcbnew.SHAPE_POLY_SET(d[a])
                    sp.BooleanIntersection(d[b])
                    ov = _area_mm2(sp)
                    if ov > 1e-4:
                        errors.append("courtyards of %s and %s overlap by "
                                      "%.3f mm2" % (a, b, ov))
        no_cy = sorted(r for r in self.fps
                       if r not in cys.get(pcbnew.F_CrtYd, {})
                       and r not in cys.get(pcbnew.B_CrtYd, {}))
        if no_cy:
            warnings.append("no courtyard, so overlap is unchecked: "
                            + ", ".join(no_cy))

        # hanging off the board
        if self.outline_pts:
            board_poly = _poly([(self.pt(x, y).x, self.pt(x, y).y)
                                for x, y in self.outline_pts])
            for ref, fp in sorted(self.fps.items()):
                cy = fp.GetCourtyard(pcbnew.F_CrtYd)
                if not cy.OutlineCount():
                    cy = fp.GetCourtyard(pcbnew.B_CrtYd)
                if not cy.OutlineCount():
                    continue
                sp = pcbnew.SHAPE_POLY_SET(cy)
                sp.BooleanSubtract(board_poly)
                out = _area_mm2(sp)
                if out > 1e-3:
                    warnings.append(
                        "%s extends %.2f mm2 past the board edge (right for "
                        "an edge connector or an antenna, wrong for anything "
                        "else)" % (ref, out))

        # copper inside a keep-out
        for name, pts in self.keepouts.items():
            ko = _poly([(self.pt(x, y).x, self.pt(x, y).y) for x, y in pts])
            for ref, fp in sorted(self.fps.items()):
                for pad in fp.Pads():
                    if not pad.IsOnCopperLayer():
                        continue
                    layer = (pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu)
                             else pcbnew.B_Cu)
                    sp = pcbnew.SHAPE_POLY_SET(ko)
                    sp.BooleanIntersection(pad.GetEffectivePolygon(layer))
                    if _area_mm2(sp) > 1e-4:
                        errors.append("pad %s of %s is inside keep-out %s"
                                      % (pad.GetNumber(), ref, name))
                        break

        # pads crowding pads, and courtyards that do not enclose their own
        # pads. Courtyard-only checking is not enough: plenty of library
        # footprints draw a courtyard round the body and leave the pads
        # outside it, so two parts can read as clear and still be 0.3 mm
        # apart. This measures the pads themselves.
        pad_boxes, loose = {}, []
        for ref, fp in self.fps.items():
            boxes = [_bbox_mm(p) for p in fp.Pads() if p.IsOnCopperLayer()]
            if boxes:
                pad_boxes[ref] = boxes
            cy = fp.GetCourtyard(pcbnew.F_CrtYd)
            if cy.OutlineCount() and boxes:
                bb = cy.BBox()
                cl, ct = tomm(bb.GetX()), tomm(bb.GetY())
                cr, cb = tomm(bb.GetRight()), tomm(bb.GetBottom())
                out = max(max(cl - b[0], b[2] - cr, ct - b[1], b[3] - cb)
                          for b in boxes)
                if out > 0.05:
                    loose.append((ref, out))

        if loose:
            worst = max(loose, key=lambda t: t[1])
            warnings.append(
                "%d footprint(s) have pads outside their own courtyard, worst "
                "%s at %.2f mm, so courtyard clearance understates how close "
                "they sit (library issue): %s"
                % (len(loose), worst[0], worst[1],
                   ", ".join(r for r, _ in sorted(loose))))

        refs = sorted(pad_boxes)
        for i, a_ref in enumerate(refs):
            for b_ref in refs[i + 1:]:
                gap = min(_box_gap(pa, pb)
                          for pa in pad_boxes[a_ref] for pb in pad_boxes[b_ref])
                if gap < min_gap:
                    warnings.append(
                        "%s and %s: only %.2f mm between their pads "
                        "(want %.2f)" % (a_ref, b_ref, gap, min_gap))

        if self._routed:
            warnings.append("this board already has tracks -- moving "
                            "footprints leaves them dangling; route once "
                            "placement has settled")

        for w in warnings:
            say("  warning:", w)
        if errors:
            for e in errors:
                say("  ERROR:", e)
            raise SystemExit("check failed: %d error(s)" % len(errors))
        say("check ok (%d warning(s))" % len(warnings))
        return warnings

    # -- saving -----------------------------------------------------------

    def _hash(self):
        with open(self.pcb, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def save(self, force=False):
        """Write the board, refusing to clobber hand edits.

        The hash of the last generated file lives in .pcbgen-manifest.json.
        If the board on disk differs, someone edited it in KiCad -- this
        backs it up to .pcbgen-backup/ and stops, because re-running resets
        every position and would throw that work away. Port the edits into
        the placement table, then pass ``force=True``.
        """
        for ref, fp in list(self._stale_holes.items()):
            self._remove(fp)
            self._holes.pop(ref, None)
            say("removed %s -- no hole()/fiducial() call re-created it" % ref)
        self._stale_holes.clear()
        for name, z in list(self._stale_zones.items()):
            self._remove(z)
            say("removed rule area %r -- no keepout() call re-created it" % name)
        self._stale_zones.clear()
        prev = {}
        if os.path.exists(self.manifest):
            try:
                with open(self.manifest) as f:
                    prev = json.load(f)
            except Exception:
                prev = {}
        cur = self._hash()
        if prev.get("sha256") and prev["sha256"] != cur and not force:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bdir = os.path.join(self.dir, ".pcbgen-backup", stamp)
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(self.pcb, os.path.join(bdir, os.path.basename(self.pcb)))
            raise SystemExit(
                "%s has been edited in KiCad since this script last wrote "
                "it.\nA copy is in %s\nRe-running resets every footprint "
                "position. Port the changes into the placement table, then "
                "call save(force=True)."
                % (os.path.basename(self.pcb), bdir))
        # Fields created from Python have no stroke width; KiCad 10 fills in
        # 0.15 mm on its next save, which shows up as a change to a file
        # nobody edited. Write it explicitly so the board is already native.
        for fp in self.board.GetFootprints():
            for fld in fp.GetFields():
                if fld.GetTextThickness() <= 0:
                    fld.SetTextThickness(mm(0.15))
        self.board.Save(self.pcb)
        with open(self.manifest, "w") as f:
            json.dump({"sha256": self._hash(),
                       "written": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1)
        say("saved %s: %d footprints on %g x %g mm"
              % (os.path.basename(self.pcb), len(self._placed), self.W, self.H))
        self.check_native()

    def check_native(self):
        """Prove the saved board is what KiCad itself would write.

        Lets `kicad-cli pcb upgrade` rewrite a temp copy and compares it line
        by line with the file on disk (which is never touched). Returns the
        number of differing lines; 0 means KiCad opens and saves the board
        without changing it."""
        tmp = tempfile.mkdtemp(prefix="pcblib-native-")
        try:
            dst = os.path.join(tmp, os.path.basename(self.pcb))
            shutil.copy2(self.pcb, dst)
            subprocess.run([self.cli, "pcb", "upgrade", "--force", dst], capture_output=True)
            a = open(self.pcb, encoding="utf-8").read().splitlines()
            b = open(dst, encoding="utf-8").read().splitlines()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        import difflib
        changed = [ln for ln in difflib.unified_diff(a, b, lineterm="", n=0)
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        if changed:
            kinds = {}
            for ln in changed:
                k = re.sub(r"-?\d+(\.\d+)?", "N", re.sub(r'"[^"]*"', '"…"', ln.strip()))
                kinds[k] = kinds.get(k, 0) + 1
            say("  native format: %d line(s) differ from KiCad's own save, e.g. %s"
                % (len(changed), ", ".join("%dx %s" % (n, k) for k, n in
                                           sorted(kinds.items(), key=lambda t: -t[1])[:3])))
        else:
            say("  native format: board matches KiCad's own save")
        return len(changed)

    # -- KiCad's own oracle ----------------------------------------------

    def drc(self, show=6):
        """Run DRC and split the result into what you caused and what you
        inherited.

        A violation whose items all live inside one footprint, touching
        nothing at board level, comes from that footprint's library
        definition: unnumbered thermal vias, undersized drills, silkscreen
        over its own pads. Moving the part cannot fix those, and mixing them
        in with real placement errors is how a board ships with fifty
        'errors' nobody read.
        """
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            # --schematic-parity is not the default, and without it the
            # "schematic_parity" list below is always empty -- a check that
            # cannot fail.
            subprocess.run([self.cli, "pcb", "drc", "--format", "json",
                            "--schematic-parity", "-o", path, self.pcb],
                           capture_output=True, check=False)
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
        finally:
            os.remove(path)

        def refs_of(v):
            refs, board_level = set(), False
            for i in v.get("items", []):
                d = i.get("description", "")
                if " of " in d:
                    refs.add(d.split(" of ", 1)[1].split(" ")[0])
                else:
                    board_level = True
            return refs, board_level

        mine, lib = [], []
        for v in rep.get("violations", []):
            refs, board_level = refs_of(v)
            (lib if len(refs) == 1 and not board_level else mine).append(v)
        unconnected = rep.get("unconnected_items", [])
        parity = rep.get("schematic_parity", [])

        def summarise(title, vs):
            say("  %s: %d" % (title, len(vs)))
            by = {}
            for v in vs:
                by.setdefault((v["severity"], v["type"]), []).append(v)
            for key, group in sorted(by.items()):
                say("    %-8s %-22s x%d" % (key[0], key[1], len(group)))
                for v in group[:show]:
                    where = ", ".join(sorted(refs_of(v)[0])) or "board"
                    say("        %s: %s" % (where, v["description"][:92]))
                if len(group) > show:
                    say("        ... %d more" % (len(group) - show))

        say("DRC:")
        summarise("placement and board level -- yours to fix", mine)
        summarise("inside one footprint -- library, not placement", lib)
        say("  unconnected items: %d%s"
              % (len(unconnected),
                 "  (expected until the board is routed)" if unconnected else ""))
        if parity:
            say("  schematic parity: %d -- sync_footprints() and "
                  "link_schematic() should have made this zero" % len(parity))
            for v in parity[:show]:
                say("        %s" % v.get("description", "")[:110])
        else:
            say("  schematic parity: clean")
        return {"placement": mine, "footprint": lib,
                "unconnected": unconnected, "parity": parity}

    # -- decoupling -------------------------------------------------------

    _GND = re.compile(r"^/?(GND\w*|\w*GND|VSS\w*|0V)$", re.I)

    def _pads_mm(self, fp):
        """[(pad, (x, y) board-relative mm, net name)] for a footprint."""
        return [(p, self.rel(p.GetPosition()), p.GetNetname()) for p in fp.Pads()]

    def _body_box(self, fp):
        """Board-relative box of a footprint's courtyard, or of its pads."""
        layer = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
        try:
            cy = fp.GetCourtyard(layer)
            if cy.OutlineCount():
                bb = cy.BBox()
                a = self.rel(pcbnew.VECTOR2I(bb.GetX(), bb.GetY()))
                b = self.rel(pcbnew.VECTOR2I(bb.GetRight(), bb.GetBottom()))
                return (a[0], a[1], b[0], b[1])
        except Exception:
            pass
        xs, ys = [], []
        for p, (x, y), _n in self._pads_mm(fp):
            sx, sy = tomm(p.GetSize().x) / 2, tomm(p.GetSize().y) / 2
            xs += [x - sx, x + sx]
            ys += [y - sy, y + sy]
        return (min(xs), min(ys), max(xs), max(ys))

    def decoupling_row(self, ic, caps=None, clearance=0.35, gap=0.5, quiet=False):
        """Put each decoupling capacitor beside the IC pin it serves.

        For hand-written placement tables (autoplace.py's recipes already do
        this). Run it after place_all(): the IC must already be where it
        belongs, and the capacitors' table entries are overridden.

        Capacitors on one rail are dealt out across that rail's pins, smallest
        first, so every pin gets a small one before any pin gets a second;
        with several on one pin the smallest value sits nearest. Each goes just
        outside the IC's courtyard on the edge that pin is on, turned so its
        rail pad faces the pin and its ground pad faces away, on the IC's
        side of the board. Capacitors along one edge are then spread apart in
        pin order so they never overlap. `caps=None` takes every two-pad C*
        part whose nets are one IC rail and ground.

        Returns {cap: (x, y, rot)}; check() and DRC still have the last word."""
        fp = self.fps[ic]
        side = "bottom" if fp.IsFlipped() else "top"
        pads = self._pads_mm(fp)
        ic_nets = {n for _p, _xy, n in pads if n and not self._GND.match(n)}
        cand = []
        for ref, cfp in self.fps.items():
            if caps is not None and ref not in caps:
                continue
            if caps is None and not re.match(r"^C\d", ref):
                continue
            nets = [n for _p, _xy, n in self._pads_mm(cfp)]
            if len(nets) != 2:
                continue
            rail = [n for n in nets if n in ic_nets]
            if len(rail) != 1 or not any(self._GND.match(n or "") for n in nets):
                if caps is not None:
                    raise SystemExit(f"decoupling_row: {ref} is not across an {ic} rail and ground")
                continue
            cand.append((ref, rail[0]))
        if not cand:
            say(f"decoupling_row: no capacitors found for {ic}")
            return {}
        box = self._body_box(fp)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

        def value(ref):
            m = re.match(r"^(\d+(?:\.\d+)?)\s*([pnuµm]?)", self.fps[ref].GetValue())
            if not m:
                return 1.0
            return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
                                        "m": 1e-3, "": 1.0}[m.group(2)]

        # deal each rail's caps out over its pins, smallest first
        by_pin = {}
        rails = {}
        for ref, rail in cand:
            rails.setdefault(rail, []).append(ref)
        for rail, refs in rails.items():
            pins = sorted([(p, xy) for p, xy, n in pads if n == rail],
                          key=lambda t: (t[1][1], t[1][0]))
            for i, ref in enumerate(sorted(refs, key=value)):
                p, xy = pins[i % len(pins)]
                by_pin.setdefault((p.GetNumber(), xy), []).append(ref)

        def pin_edge(pad, px, py):
            """The edge a pin leaves by: along a pad's long axis when it has
            one (gull-wing, QFN), else the nearest edge of the body."""
            bb = pad.GetBoundingBox()
            w, h = tomm(bb.GetWidth()), tomm(bb.GetHeight())
            if max(w, h) > 1.2 * min(w, h):
                if w > h:
                    return "left" if px < cx else "right"
                return "top" if py < cy else "bottom"
            d = {"left": px - box[0], "right": box[2] - px, "top": py - box[1],
                 "bottom": box[3] - py}
            return min(d, key=d.get)
        pad_of = {p.GetNumber(): p for p, _xy, _n in pads}

        # footprint geometry of each cap at rotation r, relative to its centre
        def pad_offsets(ref, rot):
            cfp = self.fps[ref]
            cfp.SetOrientationDegrees(rot)
            o = cfp.GetPosition()
            return {p.GetNetname(): (tomm(p.GetPosition().x - o.x), tomm(p.GetPosition().y - o.y))
                    for p in cfp.Pads()}

        edges = {"left": [], "right": [], "top": [], "bottom": []}
        for (num, (px, py)), refs in by_pin.items():
            edge = pin_edge(pad_of[num], px, py)
            for k, ref in enumerate(sorted(refs, key=value)):
                edges[edge].append((ref, (px, py), k, num))
        out = {}
        for edge, items in edges.items():
            if not items:
                continue
            n = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[edge]
            placed = []
            for ref, (px, py), k, num in items:
                rail = dict(cand)[ref]
                # the rotation that points the rail pad back at the IC
                best = None
                for rot in (0, 90, 180, 270):
                    offs = pad_offsets(ref, rot)
                    r = offs[rail]
                    score = r[0] * n[0] + r[1] * n[1]         # most negative wins
                    if best is None or score < best[0]:
                        best = (score, rot, offs)
                _s, rot, offs = best
                half = max(abs(v[0] * n[0] + v[1] * n[1]) for v in offs.values())
                pad_half = max(tomm(p.GetSize().x) for p in self.fps[ref].Pads()) / 2
                reach = half + pad_half + clearance + k * (2 * (half + pad_half) + gap)
                if edge in ("left", "right"):
                    x = (box[0] if edge == "left" else box[2]) + n[0] * reach
                    placed.append([ref, x, py, rot, py])
                else:
                    y = (box[1] if edge == "top" else box[3]) + n[1] * reach
                    placed.append([ref, px, y, rot, px])
            # spread along the edge, keeping pin order, so bodies never overlap
            along = 2 if edge in ("left", "right") else 1     # index of the along-edge coordinate
            lanes = {}
            for p in placed:
                lanes.setdefault(round(p[1] if along == 2 else p[2], 3), []).append(p)
            for lane in lanes.values():
                lane.sort(key=lambda p: p[4])
                width = []
                for p in lane:
                    cfp = self.fps[p[0]]
                    cfp.SetOrientationDegrees(p[3])
                    bb = cfp.GetBoundingBox(False)
                    width.append(tomm(bb.GetHeight() if along == 2 else bb.GetWidth()))
                for i in range(1, len(lane)):
                    need = (width[i - 1] + width[i]) / 2 + gap
                    if lane[i][along] - lane[i - 1][along] < need:
                        lane[i][along] = lane[i - 1][along] + need
                # re-centre the lane on the pins it serves
                shift = (sum(p[4] for p in lane) - sum(p[along] for p in lane)) / len(lane)
                for p in lane:
                    p[along] += shift
            for ref, x, y, rot, _pin in placed:
                self.place(ref, round(x, 3), round(y, 3), rot, side)
                out[ref] = (round(x, 3), round(y, 3), rot)
                if not quiet:
                    say(f"  {ref:5s} -> {ic} {edge:6s} ({x:7.2f}, {y:7.2f}) rot {rot}")
        return out

    # -- escape routing ----------------------------------------------------

    def fanout(self, ref, via=None, drill=None, width=None, rings=1, clearance=None,
               quiet=False):
        """Dogbone fan-out for a BGA: a short track from each inner ball to a
        via in the gap between it and its outward diagonal neighbours.

        The array is split into quadrants about its centre and every ball
        escapes diagonally away from it, so neighbouring balls never want the
        same via site. The outer `rings` rings are left alone -- they escape
        on the top layer. Balls with no net are skipped. Refuses when the via
        cannot fit between four balls with `clearance` to each (0.5 mm pitch
        and below needs via-in-pad, which is a fab option, not a fan-out).

        Re-running replaces this footprint's earlier fan-out: tracks and vias
        lying wholly inside its courtyard are removed first.

        via/drill/width/clearance default to the board's own minimums (Board
        Setup > Constraints), floored at 0.45/0.2/0.1/0.1 mm; a value below
        the board's minimum is refused rather than left for DRC to find."""
        ds = self.board.GetDesignSettings()
        mins = {"via": tomm(ds.m_ViasMinSize), "drill": tomm(ds.m_MinThroughDrill),
                "width": tomm(ds.m_TrackMinWidth), "clearance": tomm(ds.m_MinClearance)}
        via = via if via is not None else max(0.45, mins["via"])
        drill = drill if drill is not None else max(0.2, mins["drill"])
        clearance = clearance if clearance is not None else max(0.1, mins["clearance"])
        for k, v in (("via", via), ("drill", drill), ("clearance", clearance)):
            if v < mins[k] - 1e-6:
                raise SystemExit(f"fanout: {k} {v} mm is below this board's minimum "
                                 f"{mins[k]:g} mm (Board Setup > Constraints)")
        fp = self.fps[ref]
        pads = [(p, xy, n) for p, xy, n in self._pads_mm(fp)]
        xs = sorted({round(xy[0], 3) for _p, xy, _n in pads})
        ys = sorted({round(xy[1], 3) for _p, xy, _n in pads})

        def pitch_of(vals):
            d = [b - a for a, b in zip(vals, vals[1:]) if b - a > 1e-3]
            if not d:
                return None
            p = min(d)
            if any(abs(v / p - round(v / p)) > 0.05 for v in d):
                return None
            return p
        px_, py_ = pitch_of(xs), pitch_of(ys)
        if not px_ or not py_ or abs(px_ - py_) > 0.05 * px_:
            raise SystemExit(f"fanout: {ref}'s pads are not a regular square grid "
                             f"(pitch x {px_}, y {py_}) -- is it a BGA?")
        pitch = px_
        ball = max(tomm(max(p.GetSize().x, p.GetSize().y)) for p, _xy, _n in pads)
        room = pitch / math.sqrt(2) - ball / 2 - via / 2
        if room < clearance - 1e-6:
            raise SystemExit(
                f"fanout: a {via} mm via between {ball} mm balls at {pitch} mm pitch "
                f"leaves {room:.3f} mm, under the {clearance} mm clearance. Use a "
                f"smaller via (max {2 * (pitch / math.sqrt(2) - ball / 2 - clearance):.2f} mm) "
                f"or via-in-pad.")
        if width is None:
            width = max(0.1 if pitch <= 0.65 else 0.15, mins["width"])
        if width < mins["width"] - 1e-6:
            raise SystemExit(f"fanout: track {width} mm is below this board's minimum "
                             f"{mins['width']:g} mm")
        cx, cy = (xs[0] + xs[-1]) / 2, (ys[0] + ys[-1]) / 2
        nx, ny = round((xs[-1] - xs[0]) / pitch) + 1, round((ys[-1] - ys[0]) / pitch) + 1
        layer = pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu

        # clear this footprint's previous fan-out
        box = self._body_box(fp)

        def inside(v):
            x, y = self.rel(v)
            return box[0] - 1e-6 <= x <= box[2] + 1e-6 and box[1] - 1e-6 <= y <= box[3] + 1e-6
        old = [t for t in self.board.GetTracks()
               if inside(t.GetStart()) and inside(t.GetEnd())]
        for t in old:
            self._remove(t)

        made = 0
        for p, (x, y), net in pads:
            if not net or net.startswith("unconnected-"):
                continue
            i, j = round((x - xs[0]) / pitch), round((y - ys[0]) / pitch)
            if min(i, j, nx - 1 - i, ny - 1 - j) < rings:
                continue
            sx = 1 if x >= cx else -1
            sy = 1 if y >= cy else -1
            vx, vy = x + sx * pitch / 2, y + sy * pitch / 2
            ni = p.GetNet()
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(self.pt(x, y))
            t.SetEnd(self.pt(vx, vy))
            t.SetWidth(mm(width))
            t.SetLayer(layer)
            t.SetNet(ni)
            self.board.Add(t)
            v = pcbnew.PCB_VIA(self.board)
            v.SetPosition(self.pt(vx, vy))
            v.SetWidth(mm(via))
            v.SetDrill(mm(drill))
            v.SetNet(ni)
            self.board.Add(v)
            made += 1
        self._routed = True
        if not quiet:
            say(f"fanout {ref}: {nx}x{ny} grid at {pitch:g} mm, {made} ball(s) given a "
                f"{via}/{drill} mm via on a {width} mm track; outer {rings} ring(s) left "
                f"for top-layer escape; {len(old)} old track(s)/via(s) replaced")
        return made

    # -- placement quality ------------------------------------------------

    def report_nets(self, names=None, top=12):
        """How far apart a net's pads are -- the number that says whether a
        placement is actually tight.

        With no argument it ranks every net by span, so the worst offenders
        surface on their own. Pass the nets you care about (a switching node,
        a differential pair, sense lines) to watch just those.
        """
        rows = []
        pads = list(self.board.GetPads())
        by_net = {}
        for p in pads:
            n = p.GetNetname()
            if not n:
                continue
            fp = p.GetParentFootprint()
            by_net.setdefault(n, []).append(
                (tomm(p.GetPosition().x), tomm(p.GetPosition().y),
                 fp.GetReference() if fp else "?", p.GetNumber()))
        for name, pts in by_net.items():
            if names and name not in names:
                continue
            if len(pts) < 2:
                continue
            span, pair = 0.0, None
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                    if d > span:
                        span, pair = d, (pts[i], pts[j])
            rows.append((span, name, len(pts), pair))
        rows.sort(reverse=True)
        say("net spans (longest pad-to-pad distance):")
        for span, name, n, pair in (rows if names else rows[:top]):
            a, b = pair
            say("  %7.2f mm  %-22s %2d pads   %s.%s -> %s.%s"
                  % (span, name, n, a[2], a[3], b[2], b[3]))
        if names:
            for n in names:
                if not any(r[1] == n for r in rows):
                    say("       --     %-22s not found, or a single pad" % n)
        return rows

    # -- pictures ---------------------------------------------------------

    def render(self, outdir, views=("top", "angled")):
        """3D renders plus a flat SVG, so you can look at the board.

        DRC says nothing about whether a layout reads well. Look at it.
        """
        os.makedirs(outdir, exist_ok=True)
        made = []
        for side in views:
            out = os.path.join(outdir, "pcb_%s.png" % side)
            cmd = [self.cli, "pcb", "render", "-o", out, "--quality", "high",
                   "--width", "1600", "--height", "1200",
                   "--side", "bottom" if side == "bottom" else "top"]
            if side == "angled":
                cmd += ["--rotate", "-30,0,25", "--perspective"]
            else:
                # kicad-cli frames the flat views on the board OUTLINE, so
                # anything overhanging an edge -- a module antenna, a
                # connector shell -- is cropped and the part looks cut off.
                cmd += ["--zoom", "0.8"]
            cmd.append(self.pcb)                # positional, and it must be last
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                made.append(out)
            else:
                say("  render failed:", (r.stderr or r.stdout).strip()[:200])
        svg = os.path.join(outdir, "pcb.svg")
        r = subprocess.run([self.cli, "pcb", "export", "svg", "-o", svg,
                            "--page-size-mode", "2", "--exclude-drawing-sheet",
                            "--layers", "F.Cu,F.Silkscreen,Edge.Cuts",
                            self.pcb], capture_output=True, text=True)
        if r.returncode == 0:
            made.append(svg)
        else:
            say("  svg export failed:", (r.stderr or r.stdout).strip()[:200])
        for m in made:
            say("  wrote", m)
        return made
