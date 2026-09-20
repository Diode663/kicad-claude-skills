#!/usr/bin/env python3
"""schlib -- build professional-looking KiCad schematics from Python.

You describe placements, wires, power ports, labels and notes in sheet
coordinates (mm, Y down); schlib turns that into valid KiCad 9/10 .kicad_sch
files, including hierarchical sheets, and gives you the checks needed to trust
hand-authored geometry:

    lint_spec()          design rules on PARTS/NETS before drawing (bad pin
                         numbers, missing decoupling, pull-ups, open pins)
    Sheet.check()        off-grid points, dangling wire ends, wires through
                         bodies, pins landing mid-wire, bus entries that miss
                         their bus, crossing count
    outline_faults()     strings and part bodies touching a block outline (run by
                         check() on the model and by check_text() on KiCad's render)
    Design.verify()      KiCad's own netlist vs. your intended NETS spec
    Design.erc()         ERC summary by category
    Design.check_text()  KiCad's own render: text collisions, crowding, outline faults
    Design.render()      SVG + PDF of every page, for looking at the result

Minimal use:

    from schlib import Design
    d = Design(project_dir, "myboard", title="My Board", rev="A")
    s = d.sheet("01-power.kicad_sch", "Power", paper="A5")
    s.place("R1", "Device:R", "10k", 50.8, 50.8)
    s.wire(s.pin("R1", 1), (50.8, 43.18)); s.power("+3V3", 50.8, 43.18)
    s.wire(s.pin("R1", 2), (50.8, 58.42)); s.gnd(50.8, 58.42)
    root = d.root_sheet(title="My Board")
    d.link(s, x=25.4, y=50.8, w=50.8, h=25.4)
    d.write()
    d.verify(NETS, NO_CONNECT)

Geometry conventions (all verified against KiCad, not assumed)
--------------------------------------------------------------
* Library pin `(at x y a)` is the pin's electrical tip; its line runs from
  there toward the body. Local frame is Y-up; the sheet is Y-down.
* Placement angle R is counter-clockwise on screen. Local +Y ends up:
  rot 0 -> up, rot 90 -> left, rot 180 -> down, rot 270 -> right.
* Mirroring: see _transform() -- semantics confirmed by probe_transforms.py.
"""
from __future__ import annotations

import contextlib
import datetime
import difflib
import glob
import hashlib
import html
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict

# KiCad 10.0's native schematic format. KiCad 10 opens these files without an
# "older version" prompt and saves them back unchanged; KiCad 9 cannot open
# them. Design.check_native() proves every written sheet matches what KiCad 10
# itself would save, so drift after a KiCad update is caught, not shipped.
SCH_VERSION = "20260306"
GENERATOR_VERSION = "10.0"
# Symbol libraries older than this are converted in memory when loaded (via
# kicad-cli sym upgrade), because schematics embed library symbols verbatim.
LIB_NATIVE_VERSION = 20251024

# The order KiCad 10 saves top-level schematic items in; within a type, items
# are sorted by UUID.
_ITEM_ORDER = ["rectangle", "text", "junction", "no_connect", "bus_entry", "wire",
               "bus", "label", "global_label", "hierarchical_label", "symbol", "sheet"]
# wires and buses are one item type in KiCad (SCH_LINE), so they share a rank
# and interleave by UUID
_ITEM_RANK = {k: i for i, k in enumerate(_ITEM_ORDER)}
_ITEM_RANK["bus"] = _ITEM_RANK["wire"]
BUS_ENTRY = 2.54          # a bus entry's run along and across the bus, mm

GRID = 1.27               # connection grid, mm (50 mil)
MARGIN = 12.7             # keep-out from the frame, mm
TITLE_BAND = 40.0         # bottom band reserved for the title block, mm

PAPER_SIZES = {
    "A5": (210.0, 148.0), "A4": (297.0, 210.0), "A3": (420.0, 297.0),
    "A2": (594.0, 420.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
}
AUTO_PAPER_ORDER = ["A5", "A4", "A3", "A2"]
GROW_PAPER_ORDER = ["A5", "A4", "A3", "A2", "A1"]

# Functional-block drafting, from the conventions professional schematics
# follow: one function per outlined block, blocks read left-to-right and
# top-to-bottom, and enough white space between them that no two blocks ever
# touch. These are the defaults; every block can override its own padding.
BLOCK_PAD = 5.08          # clear space between a block's contents and its outline
BLOCK_GUTTER = 12.7       # clear space between neighbouring blocks
BLOCK_TITLE_H = 5.08      # band above a block reserved for its title
OUTLINE_CLEAR = 0.8       # nothing drawn may come closer than this to a block outline
BLOCK_ASPECT = 1.6        # preferred width:height when wrapping child blocks

# Clearance policy. These are what "generous" means, in millimetres, and they
# are deliberately larger than the minimum that merely avoids a collision: a
# sheet that only just clears reads as crowded even when nothing overlaps.
# Two strings crowd in two different ways, and they do not want the same
# number. Side by side, one string ends where the next begins and they read as
# one word: that needs a real gap. Stacked, they are two rows of a column on
# the 2.54 mm pin grid, which leaves 1.0 mm between 1.27 mm text -- normal,
# tidy, and what every professional sheet does; demanding more there would
# force a stagger that reads worse than the thing it fixes.
CLEAR_ROW = 1.27          # horizontal gap between strings that share a row
CLEAR_STACK = 0.85        # vertical gap between strings that share a column
CLEAR_TEXT = CLEAR_ROW    # (for callers that just want "the text gap")
CLEAR_PART = 2.54         # between two part bodies
CLEAR_GROUP = 7.62        # between two wire-connected groups inside a block


def clearance_fault(a, b, row=CLEAR_ROW, stack=CLEAR_STACK):
    """(gap, how) if boxes a and b are too close, else None.

    how is "overlap", "row" (they run together sideways) or "stack" (too close
    one above the other). Boxes that miss each other on both axes are diagonal
    neighbours and never a fault."""
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    if ox > 0.01 and oy > 0.01:
        return (min(ox, oy), "overlap")
    if ox > 0.01:                      # same column
        return (oy, "stack") if -oy < stack else None
    if oy > 0.01:                      # same row
        return (ox, "row") if -ox < row else None
    return None


# ===========================================================================
# Text metrics -- measured from KiCad, never estimated
# ===========================================================================
# Every spacing decision in this file depends on knowing how wide a string is
# when KiCad draws it. A single "0.85 x size per character" factor is wrong by
# up to 40% per glyph ('I' is 0.36, 'm' is 1.00), which is exactly the error
# that puts a label on top of its neighbour. These advances come from
# scripts/calibrate_text.py, which renders a run of every character and reads
# the textLength KiCad itself writes into the SVG. Re-run it after a KiCad
# major upgrade.
STROKE_ADVANCE = {
    ' ': 0.5714, '!': 0.3572, '"': 0.5714, '#': 0.7500, '$': 0.7143,
    '%': 0.8571, '&': 0.9286, "'": 0.3572, '(': 0.5000, ')': 0.5000,
    '*': 0.5714, '+': 0.9286, ',': 0.3572, '-': 0.9286, '.': 0.3572,
    '/': 0.7857, '0': 0.7143, '1': 0.7143, '2': 0.7143, '3': 0.7143,
    '4': 0.7143, '5': 0.7143, '6': 0.7143, '7': 0.7143, '8': 0.7143,
    '9': 0.7143, ':': 0.3572, ';': 0.3572, '<': 0.9286, '=': 0.9286,
    '>': 0.9286, '?': 0.6429, '@': 0.9643, 'A': 0.6429, 'B': 0.7500,
    'C': 0.7500, 'D': 0.7500, 'E': 0.6786, 'F': 0.6429, 'G': 0.7500,
    'H': 0.7857, 'I': 0.3572, 'J': 0.5714, 'K': 0.7500, 'L': 0.6072,
    'M': 0.8571, 'N': 0.7857, 'O': 0.7857, 'P': 0.7500, 'Q': 0.7857,
    'R': 0.7500, 'S': 0.7143, 'T': 0.5714, 'U': 0.7857, 'V': 0.6429,
    'W': 0.8571, 'X': 0.7143, 'Y': 0.6429, 'Z': 0.7143, '[': 0.5000,
    ']': 0.5000, '^': 0.4286, '_': 0.5714, '`': 0.2857, 'a': 0.6786,
    'b': 0.6786, 'c': 0.6429, 'd': 0.6786, 'e': 0.6429, 'f': 0.4286,
    'g': 0.6786, 'h': 0.6786, 'i': 0.3572, 'j': 0.3572, 'k': 0.6072,
    'l': 0.3928, 'm': 1.0000, 'n': 0.6786, 'o': 0.6786, 'p': 0.6786,
    'q': 0.6786, 'r': 0.4643, 's': 0.6072, 't': 0.4286, 'u': 0.6786,
    'v': 0.5714, 'w': 0.7857, 'x': 0.6072, 'y': 0.5714, 'z': 0.6072,
    '{': 0.5000, '|': 0.7143, '}': 0.5000, '~': 0.5357
}
STROKE_DEFAULT = 0.75     # unmeasured glyph: the width of a digit
STROKE_PEN = 0.12         # fixed per-string offset, x size (the pen width)
STROKE_BOLD = 0.015       # bold adds this much per glyph, x size

# Vertical ink allowance for one line, x size. KiCad's cap height is one size
# unit; this leaves room for the descenders and the pen, and is deliberately
# generous because it is used to reserve space, not to draw.
TEXT_HEIGHT = 1.4
TEXT_LINE = 1.61          # line pitch of a multi-line note, x size
HLABEL_ARROW = 1.15       # a hierarchical label's arrow, x size, at its anchor


# KiCad draws text of height `size` with an SVG font-size of 4/3 x size, and
# the advances above were measured against that font-size. Forgetting this
# factor makes every string 25% narrower than it really is, which is exactly
# wide enough for a checker to believe a sheet is clean while the render shows
# labels running into each other. Cross-checked against KiCad's own textLength
# by scripts/calibrate_text.py.
FONT_SCALE = 4.0 / 3.0


def text_width(s, size=1.27, bold=False):
    """Width in mm of `s` drawn by KiCad at `size`, from measured advances."""
    adv = sum(STROKE_ADVANCE.get(c, STROKE_DEFAULT) for c in str(s))
    if bold:
        adv += STROKE_BOLD * len(str(s))
    return size * FONT_SCALE * (adv + STROKE_PEN)


def text_extent(s, size=1.27, bold=False):
    """(width, height) in mm, multi-line aware."""
    lines = str(s).split("\n")
    w = max(text_width(ln, size, bold) for ln in lines)
    return (w, size * (TEXT_HEIGHT + TEXT_LINE * (len(lines) - 1)))


# ===========================================================================
# Small utilities
# ===========================================================================

def bus_members(name):
    """Member net names of a KiCad bus name, in order.

    'D[0..7]' -> D0..D7, 'A[7..0]' -> A7..A0, '{SDA SCL}' -> SDA, SCL,
    'I2C{SDA SCL}' -> I2C.SDA, I2C.SCL (KiCad's named-group convention).
    A vector inside a group expands in place: '{D[0..1] WR}' -> D0, D1, WR."""
    m = re.fullmatch(r"([^\[{]*)\[(\d+)\.\.(\d+)\]", name)
    if m:
        a, b = int(m.group(2)), int(m.group(3))
        step = 1 if b >= a else -1
        return [f"{m.group(1)}{i}" for i in range(a, b + step, step)]
    m = re.fullmatch(r"([^{]*)\{(.*)\}", name)
    if m:
        prefix = m.group(1) + "." if m.group(1) else ""
        out = []
        for part in m.group(2).split():
            out += [prefix + n for n in (bus_members(part) if "[" in part else [part])]
        return out
    raise ValueError(f"not a bus name: {name!r} (want 'D[0..7]' or '{{SDA SCL}}')")


def _new_bucket():
    return {"sym": [], "seg": [], "lab": [], "nc": [], "txt": [], "bus": [], "ent": []}


_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def sid(*parts):
    """Deterministic UUID from key parts.

    KiCad links PCB footprints to schematic symbols by UUID path. Random UUIDs
    would silently break every board association on each regeneration; stable
    ones also keep the generated files diffable.
    """
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


def fmt(n):
    s = f"{float(n):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def q(s):
    """Escape a string for a KiCad s-expression."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def on_grid(v, g=GRID):
    return abs(v / g - round(v / g)) < 1e-6


def snap(v, g=GRID):
    return round(round(v / g) * g, 4)


def _same(a, b):
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _on_segment(pt, seg):
    """True if pt lies anywhere on an orthogonal segment, ends included."""
    px, py = pt
    x1, y1, x2, y2 = seg
    if abs(y1 - y2) < 1e-6 and abs(py - y1) < 1e-6:
        return min(x1, x2) - 1e-6 <= px <= max(x1, x2) + 1e-6
    if abs(x1 - x2) < 1e-6 and abs(px - x1) < 1e-6:
        return min(y1, y2) - 1e-6 <= py <= max(y1, y2) + 1e-6
    return False


# ===========================================================================
# Locating KiCad
# ===========================================================================

def find_kicad_cli():
    """kicad-cli path: $KICAD_CLI, PATH, then the usual install locations."""
    env = os.environ.get("KICAD_CLI")
    if env and os.path.exists(env):
        return env
    w = shutil.which("kicad-cli")
    if w:
        return w
    cands = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cands += glob.glob(os.path.join(base, "KiCad", "*", "bin", "kicad-cli.exe"))
    elif sys.platform == "darwin":
        cands += ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    else:
        cands += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    cands = [c for c in cands if os.path.exists(c)]

    def ver(p):
        m = re.search(r"KiCad[\\/](\d+(?:\.\d+)?)", p)
        return float(m.group(1)) if m else 0.0
    cands.sort(key=ver, reverse=True)
    if cands:
        return cands[0]
    raise FileNotFoundError("kicad-cli not found; set KICAD_CLI to its path")


def find_symbol_dir(cli=None):
    """KiCad's stock symbol library directory."""
    for v in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"):
        p = os.environ.get(v)
        if p and os.path.isdir(p):
            return p
    try:
        cli = cli or find_kicad_cli()
    except FileNotFoundError:
        cli = None
    cands = []
    if cli:
        root = os.path.dirname(os.path.dirname(os.path.abspath(cli)))
        cands += [os.path.join(root, "share", "kicad", "symbols"),
                  os.path.join(os.path.dirname(os.path.abspath(cli)), "..",
                               "SharedSupport", "symbols")]
    cands += ["/usr/share/kicad/symbols", "/usr/local/share/kicad/symbols",
              "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"]
    for c in cands:
        if os.path.isdir(c):
            return os.path.normpath(c)
    raise FileNotFoundError("KiCad symbol directory not found; set KICAD10_SYMBOL_DIR")


def _global_sym_lib_tables():
    if sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    else:
        base = os.path.expanduser("~/.config/kicad")
    found = glob.glob(os.path.join(base, "*", "sym-lib-table"))

    def ver(p):
        try:
            return float(os.path.basename(os.path.dirname(p)))
        except ValueError:
            return 0.0
    return sorted(found, key=ver, reverse=True)[:1]


# ===========================================================================
# S-expression handling
# ===========================================================================

_TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse_sexp(text):
    """Tiny s-expression parser -> nested lists of strings."""
    stack = [[]]
    pos, n = 0, len(text)
    while pos < n:
        m = _TOK.match(text, pos)
        if not m:
            if not text[pos:].strip():
                break
            raise ValueError(f"s-expression parse error near {text[pos:pos + 40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            top = stack.pop()
            stack[-1].append(top)
        elif m.group(3) is not None:
            stack[-1].append(re.sub(r"\\(.)", r"\1", m.group(3)))
        else:
            stack[-1].append(m.group(4))
    return stack[0]


def _balanced_end(text, open_idx):
    """Index one past the ')' closing the '(' at open_idx (string-aware)."""
    depth, in_str, esc = 0, False, False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced parentheses")


def _child_spans(block):
    """(start, end) spans of the direct children of a '(...)' block."""
    spans = []
    i = 1
    while i < len(block) - 1:
        ch = block[i]
        if ch == "(":
            end = _balanced_end(block, i)
            spans.append((i, end))
            i = end
            continue
        if ch == '"':
            j = i + 1
            while j < len(block) and not (block[j] == '"' and block[j - 1] != "\\"):
                j += 1
            i = j + 1
            continue
        i += 1
    return spans


def _child_key(raw):
    """('property', 'Value') style key for a child block's raw text."""
    t = parse_sexp(raw)[0]
    return (t[0], t[1] if len(t) > 1 and isinstance(t[1], str) else None)


# ===========================================================================
# Symbols and libraries
# ===========================================================================

class Symbol:
    """A library symbol: its raw text (flattened, renamed to lib_id), pins and
    body geometry. Pins are grouped by unit; unit 0 means 'common to all'."""

    HIDE_NAMES = re.compile(r"\(pin_names[^)]*\(hide yes\)|\(pin_names[^)]*hide\b")
    HIDE_NUMS = re.compile(r"\(pin_numbers[^)]*\(hide yes\)|\(pin_numbers[^)]*hide\b")

    def __init__(self, lib_id, raw):
        self.lib_id = lib_id
        self.raw = raw
        head = raw[:400]
        self.hide_names = bool(self.HIDE_NAMES.search(head))
        self.hide_numbers = bool(self.HIDE_NUMS.search(head))
        tree = parse_sexp(raw)[0]
        self.is_power = any(isinstance(c, list) and c and c[0] == "power" for c in tree)
        self._pins = []
        self._graphics = defaultdict(list)
        for sub in tree:
            if not (isinstance(sub, list) and sub and sub[0] == "symbol"):
                continue
            m = re.search(r"_(\d+)_(\d+)$", sub[1])
            unit = int(m.group(1)) if m else 0
            style = int(m.group(2)) if m else 1
            if style > 1:            # De Morgan alternate body style -- skip
                continue
            for el in sub[2:]:
                if not (isinstance(el, list) and el):
                    continue
                if el[0] == "pin":
                    self._pins.append(self._pin(el, unit))
                elif el[0] in ("rectangle", "polyline", "circle", "arc", "bezier"):
                    self._graphics[unit].append(el)
        units = sorted({p["unit"] for p in self._pins if p["unit"] > 0})
        self.units = units or [1]

    @staticmethod
    def _pin(el, unit):
        d = {"etype": el[1], "shape": el[2], "unit": unit, "name": "", "num": ""}
        for c in el[3:]:
            if not isinstance(c, list) or not c:
                continue
            if c[0] == "at":
                d["x"], d["y"] = float(c[1]), float(c[2])
                d["angle"] = int(float(c[3])) if len(c) > 3 else 0
            elif c[0] == "length":
                d["length"] = float(c[1])
            elif c[0] == "name":
                d["name"] = c[1]
            elif c[0] == "number":
                d["num"] = c[1]
        return d

    def pins(self, unit=1):
        seen, out = set(), []
        for p in self._pins:
            if p["unit"] in (0, unit) and p["num"] not in seen:
                seen.add(p["num"])
                out.append(p)
        return out

    def find_pin(self, key, unit=1):
        key = str(key)
        pins = self.pins(unit)
        for p in pins:
            if p["num"] == key:
                return p
        named = [p for p in pins if p["name"] == key]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise KeyError(f"{self.lib_id}: pin name {key!r} is ambiguous "
                           f"(numbers {[p['num'] for p in named]}); use a number")
        raise KeyError(f"{self.lib_id} unit {unit} has no pin {key!r}; pins are "
                       + ", ".join(f"{p['num']}={p['name']}" for p in pins))

    def field_pos(self, name):
        """(x, y) of a library field, in the symbol's own frame, or None."""
        m = re.search(r'\(property "%s" "[^"]*"\s*\(at ([-\d.]+) ([-\d.]+)' % name,
                      self.raw)
        return (float(m.group(1)), float(m.group(2))) if m else None

    def bbox(self, unit=1):
        xs, ys = [], []

        def pt(node):
            xs.append(float(node[1]))
            ys.append(float(node[2]))
        for el in self._graphics[0] + self._graphics.get(unit, []):
            for c in el[1:]:
                if not isinstance(c, list) or not c:
                    continue
                if c[0] in ("start", "end", "mid", "center"):
                    pt(c)
                elif c[0] == "pts":
                    for xy in c[1:]:
                        if isinstance(xy, list) and xy and xy[0] == "xy":
                            pt(xy)
            if el[0] == "circle":
                cx = cy = r = None
                for c in el[1:]:
                    if isinstance(c, list) and c and c[0] == "center":
                        cx, cy = float(c[1]), float(c[2])
                    if isinstance(c, list) and c and c[0] == "radius":
                        r = float(c[1])
                if r is not None:
                    xs += [cx - r, cx + r]
                    ys += [cy - r, cy + r]
        if not xs:
            return (-1.27, -1.27, 1.27, 1.27)
        return (min(xs), min(ys), max(xs), max(ys))


class _LibraryPlaceholder:
    pass


class Library:
    """Resolves lib_ids like 'Device:R' or 'myproj:ESP32-S3' to Symbols.

    Library nicknames come from the global sym-lib-table, then the project's
    sym-lib-table (which wins), then <stock dir>/<nick>.kicad_sym as a fallback.
    """

    def __init__(self, project_dir=None, stock_dir=None, cli=None):
        self.stock_dir = stock_dir or find_symbol_dir(cli)
        self.project_dir = project_dir
        self.paths = {}
        for t in _global_sym_lib_tables():
            self._load_table(t, None)
        if project_dir:
            self._load_table(os.path.join(project_dir, "sym-lib-table"), project_dir)
        self._text = {}
        self._cache = {}

    def add(self, nick, path):
        self.paths[nick] = path

    def _resolve_uri(self, uri, project_dir):
        def sub(m):
            var = m.group(1)
            if var == "KIPRJMOD":
                return project_dir or ""
            if re.fullmatch(r"KICAD\d*_SYMBOL_DIR", var):
                return self.stock_dir
            return os.environ.get(var, m.group(0))
        path = re.sub(r"\$\{([^}]+)\}", sub, uri)
        return None if "${" in path else os.path.normpath(path)

    def _load_table(self, path, project_dir):
        if not os.path.exists(path):
            return
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r'\(lib\s*\(name\s*"?([^")]+)"?\)\s*\(type\s*"?([^")]+)"?\)'
                             r'\s*\(uri\s*"?([^")]+)"?\)', text):
            nick, typ, uri = m.groups()
            if typ.strip() != "KiCad":
                continue
            p = self._resolve_uri(uri.strip(), project_dir)
            if p:
                self.paths[nick.strip()] = p

    def path_for(self, nick):
        p = self.paths.get(nick)
        if p and os.path.exists(p):
            return p
        fallback = os.path.join(self.stock_dir, f"{nick}.kicad_sym")
        if os.path.exists(fallback):
            return fallback
        raise KeyError(f"symbol library {nick!r} not found (not in any sym-lib-table "
                       f"and no {fallback})")

    def _file(self, nick):
        if nick not in self._text:
            path = self.path_for(nick)
            text = open(path, encoding="utf-8").read()
            m = re.search(r"\(version\s+(\d+)\)", text[:400])
            if m and int(m.group(1)) < LIB_NATIVE_VERSION:
                text = self._upgraded(path, text, int(m.group(1)))
            self._text[nick] = text
        return self._text[nick]

    def _upgraded(self, path, text, version):
        """An old-format library, converted to KiCad 10 syntax in memory.

        Schematics embed library symbols as-is, so an old library would put
        old syntax into every sheet. The file on disk is left alone; run
        `kicad-cli sym upgrade` on it once to stop this conversion happening."""
        cli = getattr(self, "_cli", None) or find_kicad_cli()
        tmp = tempfile.mkdtemp(prefix="schlib-lib-")
        try:
            dst = os.path.join(tmp, os.path.basename(path))
            subprocess.run([cli, "sym", "upgrade", "--force", "--output", dst, path],
                           capture_output=True, text=True)
            if not os.path.exists(dst):
                raise RuntimeError(f"kicad-cli could not upgrade {path}")
            print(f"  note: {os.path.basename(path)} is library format {version}; converted in "
                  f"memory. Upgrade it once with: kicad-cli sym upgrade \"{path}\"")
            return open(dst, encoding="utf-8").read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def names(self, nick):
        return [n for n in re.findall(r'\(symbol "([^"]+)"', self._file(nick))
                if not re.search(r"_\d+_\d+$", n)]

    def _raw(self, nick, name):
        text = self._file(nick)
        m = re.search(r'\(symbol "' + re.escape(name) + r'"[\s)]', text)
        if not m:
            close = difflib.get_close_matches(name, self.names(nick), n=5)
            raise KeyError(f"{nick}:{name} not found. Close matches: {close}")
        return text[m.start():_balanced_end(text, m.start())]

    def _flatten(self, nick, name, depth=0):
        """Resolve `(extends "Parent")`: schematics embed only flat symbols."""
        if depth > 8:
            raise ValueError(f"extends chain too deep at {nick}:{name}")
        raw = self._raw(nick, name)
        em = re.search(r'\(extends "([^"]+)"\)', raw)
        if not em:
            return raw
        parent = em.group(1)
        out = self._flatten(nick, parent, depth + 1)
        out = out.replace(f'(symbol "{parent}"', f'(symbol "{name}"', 1)
        out = out.replace(f'(symbol "{parent}_', f'(symbol "{name}_')
        for cs, ce in _child_spans(raw):
            child = raw[cs:ce]
            key = _child_key(child)
            if key[0] != "property":
                continue
            replaced = False
            for ps, pe in _child_spans(out):
                if _child_key(out[ps:pe]) == key:
                    out = out[:ps] + child + out[pe:]
                    replaced = True
                    break
            if not replaced:
                # A property the parent lacks goes after the parent's last
                # property, not straight after the header line: KiCad writes
                # pin_names / exclude_from_sim / in_bom ahead of every
                # property, and putting one first fails the native-format
                # check (seen on Transistor_FET:AO3401A, which adds fields
                # its TP0610T parent does not have).
                props = [(ps, pe) for ps, pe in _child_spans(out)
                         if _child_key(out[ps:pe])[0] == "property"]
                if props:
                    at = props[-1][1]
                    out = out[:at] + "\n\t\t" + child + out[at:]
                else:
                    head_end = out.index("\n") if "\n" in out else len(out) - 1
                    out = out[:head_end] + "\n" + child + out[head_end:]
        return out

    def get(self, lib_id):
        if lib_id in self._cache:
            return self._cache[lib_id]
        if ":" not in lib_id:
            raise ValueError(f"lib_id must be 'Library:Symbol', got {lib_id!r}")
        nick, name = lib_id.split(":", 1)
        raw = self._flatten(nick, name)
        raw = re.sub(r'^\(symbol "[^"]+"', f'(symbol "{q(lib_id)}"', raw, count=1)
        sym = Symbol(lib_id, raw)
        self._cache[lib_id] = sym
        return sym


# ===========================================================================
# Placement transforms
# ===========================================================================

def _rot(px, py, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (px * c - py * s, px * s + py * c)


def _rotate_point(px, py, cx, cy, deg):
    """Rotate (px, py) by `deg` about (cx, cy) -- SVG's rotate(deg cx cy)."""
    dx, dy = _rot(px - cx, py - cy, deg)
    return (cx + dx, cy + dy)


def _transform(px, py, rot, mirror):
    """Local (Y-up) offset -> sheet-space (Y-down) offset.

    Verified by probe_transforms.py against KiCad's exported netlist for every
    rotation x mirror combination: KiCad rotates first, then mirrors in sheet
    space. `mirror="x"` flips top/bottom (about the X axis); `mirror="y"`
    flips left/right (about the Y axis).
    """
    rx, ry = _rot(px, py, rot)
    sx, sy = rx, -ry
    if mirror == "x":
        sy = -sy
    elif mirror == "y":
        sx = -sx
    return sx, sy


def pin_position(sym, pin, x, y, rot=0, mirror=None):
    sx, sy = _transform(pin["x"], pin["y"], rot, mirror)
    return (round(x + sx, 4), round(y + sy, 4))


def pin_outward(pin, rot=0, mirror=None):
    """Unit vector (sheet space) pointing away from the body at a pin tip."""
    a = math.radians(pin["angle"])
    ox, oy = _transform(-math.cos(a), -math.sin(a), rot, mirror)
    return (round(ox), round(oy))


# ===========================================================================
# Sheet
# ===========================================================================

class Sheet:
    """One .kicad_sch file's worth of drawing."""

    def __init__(self, lib, filename, title, paper="A4", comment=""):
        self.lib = lib
        self.filename = filename
        self.title = title
        self.paper = paper
        self.comments = [comment] if isinstance(comment, str) else list(comment)
        self.uuid = sid("sheet", filename)
        self.instance_uuid = None     # set when linked from the root
        self.symbols = []
        self.segments = []
        self.labels = []              # (kind, name, x, y, angle, justify, shape)
        self.no_connects = []
        self.texts = []               # (content, x, y, size, justify, bold)
        self.rects = []               # (x1, y1, x2, y2)
        self.buses = []               # (x1, y1, x2, y2) bus segments
        self.bus_entries = []         # (x, y, sx, sy): wire end (x, y) -> bus (x+sx, y+sy)
        self.child_sheets = []        # root only
        self.blocks = []              # top-level functional blocks, in order
        self._block_stack = []
        self._block_art = None        # where finish_blocks() started appending
        self._fields = None           # cached reference/value spots
        self._pin_pts = None          # cached pin positions
        self._relieved = False
        self._paras = None            # cached note paragraphs
        self._items = None            # cached item_boxes()
        self._grid = None             # its spatial index
        self._grid_for = None

    # ---- placement ---------------------------------------------------------
    def place(self, ref, lib_id, value, x, y, rot=0, mirror=None, unit=1,
              props=None, dnp=False, in_bom=True, on_board=True):
        """Place a part. `props` become hidden fields (Footprint, MPN, LCSC...)."""
        if any(s["ref"] == ref and s["unit"] == unit for s in self.symbols):
            raise ValueError(f"{ref} unit {unit} placed twice on {self.filename}")
        sym = self.lib.get(lib_id)
        if unit not in sym.units:
            raise ValueError(f"{lib_id} has units {sym.units}, not {unit}")
        self.symbols.append(dict(ref=ref, lib_id=lib_id, value=value, x=x, y=y,
                                 rot=rot, mirror=mirror, unit=unit,
                                 props=dict(props or {}), power=False, dnp=dnp,
                                 in_bom=in_bom, on_board=on_board))
        return ref

    def power(self, kind, x, y, rot=0):
        """Power port (GND, +3V3, +5V, VBUS, PWR_FLAG ...). Its pin is at (x, y);
        at rot=0, GND hangs below that point and supply arrows point up from
        it -- rotate explicitly if you need another orientation. stub(power=)
        picks this automatically to match the stub's own direction."""
        lib_id = kind if ":" in kind else f"power:{kind}"
        self.lib.get(lib_id)
        value = lib_id.split(":", 1)[1]
        # PWR_FLAG is an ERC marker, not a net: its name says nothing about the
        # circuit and, printed next to the rail's own port, it reads as though
        # the net were called PWR_FLAG. The symbol stays (ERC needs it); only
        # its text is hidden.
        hide = value.upper() == "PWR_FLAG"
        self.symbols.append(dict(ref=None, lib_id=lib_id, value=value, x=x, y=y,
                                 rot=rot, mirror=None, unit=1, props={}, power=True,
                                 dnp=False, in_bom=True, on_board=True,
                                 hide_value=hide))
        self._fields = None
        self._items = None
        return (x, y)

    def gnd(self, x, y):
        return self.power("GND", x, y)

    def _sym(self, ref, unit=None):
        hits = [s for s in self.symbols if s["ref"] == ref]
        if unit is not None:
            hits = [s for s in hits if s["unit"] == unit]
        if not hits:
            raise KeyError(f"no component {ref} on {self.filename}")
        return hits

    def pin(self, ref, key, unit=None):
        """Sheet coordinates of a pin, by number or (unique) name."""
        err = None
        for s in self._sym(ref, unit):
            sym = self.lib.get(s["lib_id"])
            try:
                p = sym.find_pin(key, s["unit"])
            except KeyError as e:
                err = e
                continue
            return pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])
        raise err

    def pin_dir(self, ref, key, unit=None):
        for s in self._sym(ref, unit):
            sym = self.lib.get(s["lib_id"])
            try:
                return pin_outward(sym.find_pin(key, s["unit"]), s["rot"], s["mirror"])
            except KeyError:
                continue
        raise KeyError(f"{ref} has no pin {key!r}")

    def pins_of(self, ref):
        """[(num, name, (x, y))] for every pin of every placed unit of ref."""
        out = []
        for s in self._sym(ref):
            sym = self.lib.get(s["lib_id"])
            for p in sym.pins(s["unit"]):
                out.append((p["num"], p["name"],
                            pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])))
        return out

    # ---- wiring --------------------------------------------------------------
    def wire(self, *points):
        """Orthogonal polyline through (x, y) points. Returns the last point.

        Careful fanning multiple signals out from one box to another (e.g. on
        a root/hierarchy sheet) with a shared "over then down" pattern that
        routes them all through a common trunk x (or y): each signal's corner
        point then lands on the *interior* of every other signal's trunk
        segment at that coordinate, and KiCad's connectivity rule treats an
        endpoint landing on another wire's interior as a junction -- silently
        merging every fanned-out net into one. A direct multi-segment wire
        between two pins is only safe when nothing else shares its trunk
        coordinate; for real fan-out, give each pin its own matching label
        (same net name, placed right at the pin) instead of routing wires
        that converge."""
        pts = [(round(p[0], 4), round(p[1], 4)) for p in points]
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
                raise ValueError(f"{self.filename}: diagonal wire {a} -> {b}; "
                                 f"add a corner point or use route()")
            if not _same(a, b):
                seg = (a[0], a[1], b[0], b[1])
                # An identical segment drawn twice is invisible on the page but
                # gets the same deterministic UUID, which KiCad renumbers on its
                # next save -- so the file is no longer byte-identical to what
                # the generator wrote. Drop the duplicate instead.
                if seg not in self.segments and seg[2:] + seg[:2] not in self.segments:
                    self.segments.append(seg)
        return pts[-1]

    def route(self, a, b, first="h"):
        """L-shaped route from a to b: horizontal first ('h') or vertical first."""
        corner = (b[0], a[1]) if first == "h" else (a[0], b[1])
        return self.wire(a, corner, b)

    def stub(self, ref, key, length=2.54, label=None, hlabel=None, power=None,
             nc=False, shape="bidirectional", unit=None):
        """Short wire straight out of a pin, optionally terminated by a net
        label, hierarchical label or power port. Returns the stub's end.

        A power port's rotation is chosen so its own pin faces back along the
        stub, whatever direction that is -- otherwise every power flag would
        render in its library-default vertical orientation (GND hanging
        below, +XXX arrow pointing up) even on a horizontal stub, colliding
        with whatever sits one or two rows above or below it.

        Watch for two components placed symmetrically so their facing pins'
        stubs reach the same point (e.g. a feedback divider's two resistors,
        each stubbed halfway toward the other) -- two independent `label=`
        calls then land their labels on the exact same coordinate and render
        as one garbled overlapping mess (harmless electrically, broken
        visually). Wire the two raw pins directly instead (`s.pin()`, not the
        stub ends) and add a single label branching off that shared wire."""
        x, y = self.pin(ref, key, unit)
        if nc:
            self.no_connects.append((x, y))
            return (x, y)
        dx, dy = self.pin_dir(ref, key, unit)
        end = (round(x + dx * length, 4), round(y + dy * length, 4))
        self.wire((x, y), end)
        angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
        if label:
            self.label(label, *end, angle=angle)
        if hlabel:
            self.hlabel(hlabel, *end, angle=angle, shape=shape)
        if power:
            self.power(power, *end, rot=self._power_rot(power, (-dx, -dy)))
        return end

    def _power_rot(self, kind, want_outward):
        """Rotation (0/90/180/270) that makes `kind`'s pin face `want_outward`,
        whatever its native pin angle is -- GND and +XXX-style flags don't
        share one (see power())."""
        lib_id = kind if ":" in kind else f"power:{kind}"
        pin = self.lib.get(lib_id).find_pin("1", 1)
        for rot in (0, 90, 180, 270):
            if pin_outward(pin, rot) == want_outward:
                return rot
        raise ValueError(f"{lib_id}: no rotation makes its pin face {want_outward}")

    def label(self, name, x, y, angle=0, justify=None):
        """Local net label. angle 0: text runs right (wire arrives from the
        left); 180: text runs left; 90/270 for vertical wires."""
        justify = justify or ("right" if angle in (180, 270) else "left")
        self.labels.append(("label", name, x, y, angle, justify, None))
        return (x, y)

    def hlabel(self, name, x, y, angle=0, shape="bidirectional", justify=None):
        """Hierarchical label -- pairs with a same-named pin on the root's sheet
        symbol. shape: input, output, bidirectional, tri_state, passive."""
        justify = justify or ("right" if angle in (180, 270) else "left")
        self.labels.append(("hierarchical_label", name, x, y, angle, justify, shape))
        return (x, y)

    def nc(self, ref, *keys, unit=None):
        for k in keys:
            self.no_connects.append(self.pin(ref, k, unit))

    # ---- buses ---------------------------------------------------------------
    # A bus is drawing, not connectivity: the member nets are joined by their
    # labels, exactly as they would be without it. What the bus buys is a
    # readable page -- eight parallel data lines drawn as one thick trunk with
    # a name, instead of eight wires or eight loose labels. So the rules are
    # KiCad's: every wire that meets a bus does so through a 45-degree entry,
    # carries a member label, and the bus itself carries the bus name.
    def bus(self, *points):
        """Orthogonal bus polyline through (x, y) points. Returns the last point."""
        pts = [(round(p[0], 4), round(p[1], 4)) for p in points]
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
                raise ValueError(f"{self.filename}: diagonal bus {a} -> {b}")
            if not _same(a, b):
                seg = (a[0], a[1], b[0], b[1])
                if seg not in self.buses and seg[2:] + seg[:2] not in self.buses:
                    self.buses.append(seg)
        self._pin_pts = None
        return pts[-1]

    def bus_entry(self, x, y, sx, sy):
        """45-degree wire-to-bus entry from the wire end (x, y) to the bus point
        (x + sx, y + sy); sx and sy are +-BUS_ENTRY. Returns the bus point."""
        if abs(abs(sx) - BUS_ENTRY) > 1e-6 or abs(abs(sy) - BUS_ENTRY) > 1e-6:
            raise ValueError(f"bus entry size must be +-{BUS_ENTRY}, got ({sx}, {sy})")
        self.bus_entries.append((round(x, 4), round(y, 4), sx, sy))
        return (round(x + sx, 4), round(y + sy, 4))

    def bus_label(self, name, x, y, angle=0):
        """Name a bus -- 'D[0..7]', '{SDA SCL}' or 'I2C{SDA SCL}' -- at a point
        on it. The members' own wires carry the member names (bus_members())."""
        return self.label(name, x, y, angle)

    def pins_to_bus(self, ref, keys, bus_name, members=None, unit=None,
                    toward=None, reach=None, tail=None, label_bus=True):
        """Fan a row of pins on one flank of a part onto a bus.

        Each pin gets a straight stub carrying its member label, then a 45-degree
        entry onto a bus running parallel to the flank. `members` names the
        pins' nets in order (default: bus_members(bus_name)). `toward` is the
        direction the bus leaves in, along the flank: 'up'/'down' for a left or
        right flank, 'left'/'right' for a top or bottom one (default: down /
        right). The bus runs `tail` beyond the last entry -- long enough for its
        own name by default -- and the returned point is that end, so the run
        can be continued with bus() to the next part's pins_to_bus().

        Returns (bus_end, bus_start): both ends of the trunk drawn here."""
        keys = list(keys)
        members = list(members) if members is not None else bus_members(bus_name)
        if len(members) != len(keys):
            raise ValueError(f"{ref}: {len(keys)} pins but {len(members)} members "
                             f"for bus {bus_name!r}")
        pins = [self.pin(ref, k, unit) for k in keys]
        dirs = {self.pin_dir(ref, k, unit) for k in keys}
        if len(dirs) != 1:
            raise ValueError(f"{ref}: pins {keys} are not all on one flank")
        dx, dy = dirs.pop()
        horiz = dy == 0                      # stubs run horizontally
        along = 1 if horiz else 0            # the flank's own axis index
        default = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}
        tv = default[toward or ("down" if horiz else "right")]
        if (tv[1] == 0) == horiz:
            raise ValueError(f"{ref}: toward={toward!r} does not run along this flank")
        t = tv[along]
        if reach is None:
            reach = max(5.08, math.ceil((max(text_width(m) for m in members) + 2.54)
                                        / 2.54) * 2.54)
        # every stub ends on one line parallel to the flank, however ragged the
        # pin tips are (a stagger of 1.27 on some symbols)
        tips = [p[0] * dx if horiz else p[1] * dy for p in pins]
        edge = max(tips) + reach                        # in the outward direction
        label_angle = {(1, 0): 180, (-1, 0): 0, (0, -1): 270, (0, 1): 90}[(dx, dy)]
        order = sorted(range(len(pins)), key=lambda i: pins[i][along] * t)
        ends = []
        for i in order:
            px, py = pins[i]
            e = (round(edge * dx, 4), py) if horiz else (px, round(edge * dy, 4))
            self.wire((px, py), e)
            self.label(members[i], *e, angle=label_angle)
            sx = dx * BUS_ENTRY if horiz else t * BUS_ENTRY
            sy = t * BUS_ENTRY if horiz else dy * BUS_ENTRY
            ends.append(self.bus_entry(e[0], e[1], sx, sy))
        first, last = ends[0], ends[-1]
        if tail is None:
            tail = (math.ceil((text_width(bus_name) + 3.81) / 2.54) * 2.54
                    if label_bus else 2.54)
        end = (last[0], round(last[1] + t * tail, 4)) if horiz else \
              (round(last[0] + t * tail, 4), last[1])
        self.bus(first, end)
        if label_bus:
            # on the tail, clear of the entries, reading along the bus
            lp = (last[0], round(last[1] + t * 2.54, 4)) if horiz else \
                 (round(last[0] + t * 2.54, 4), last[1])
            if horiz:
                ang = 270 if t > 0 else 90
            else:
                ang = 0 if t > 0 else 180
            self.bus_label(bus_name, *lp, angle=ang)
        return end, first

    def _bus_points(self):
        """Every point where a wire meets bus graphics: entry wire ends and
        entry bus ends. A wire end sitting on one of these is connected, so it
        is never free to slide."""
        pts = set()
        for x, y, sx, sy in self.bus_entries:
            pts.add((round(x, 3), round(y, 3)))
            pts.add((round(x + sx, 3), round(y + sy, 3)))
        return pts

    def note(self, content, x, y, size=1.27, justify="left", bold=False):
        self.texts.append((content, x, y, size, justify, bold))

    def box(self, x1, y1, x2, y2, title=None):
        """Dashed functional-block outline, optionally titled above its corner."""
        self.rects.append((x1, y1, x2, y2))
        if title:
            self.note(title, x1, y1 - 2.54, 1.6, bold=True)

    # ---- functional blocks -------------------------------------------------
    # A block is one function: a regulator and its passives, a connector and
    # its protection, an IC and its entourage. Professional sheets draw each
    # as an outlined group, lay the groups out left-to-right and top-to-bottom
    # in signal order, and leave real white space between them. Declaring the
    # block instead of drawing its rectangle means the outline is derived from
    # the contents -- it can never be too tight - and arrange() can move the
    # whole group as one piece.
    _LISTS = ("symbols", "segments", "labels", "no_connects", "texts",
              "buses", "bus_entries")

    def _marks(self):
        return tuple(len(getattr(self, n)) for n in self._LISTS)

    def block_start(self, title=None, pad=BLOCK_PAD, width=None):
        """Open a functional block; everything drawn until block_end() is in it.

        Blocks nest: a container block (a whole charger, say) holds the IC core
        and each support group as children, and arrange() flows the children
        inside the parent before placing the parent on the page.

        pad=None makes the block a layout group only -- its children are
        flowed together but no outline is drawn around them. width sets the
        wrap width used for the children (0 stacks them one per row);
        the default derives a readable aspect from their total area."""
        b = {"title": title, "pad": pad, "width": width, "start": self._marks(),
             "end": None, "children": [],
             "parent": self._block_stack[-1] if self._block_stack else None}
        (b["parent"]["children"] if b["parent"] else self.blocks).append(b)
        self._block_stack.append(b)
        return b

    def block_end(self):
        if not self._block_stack:
            raise ValueError(f"{self.filename}: block_end() with no block open")
        b = self._block_stack.pop()
        b["end"] = self._marks()
        return b

    @contextlib.contextmanager
    def block(self, title=None, pad=BLOCK_PAD, width=None):
        """with s.block("USB-C INPUT"): ... -- same as block_start/block_end."""
        b = self.block_start(title, pad, width)
        try:
            yield b
        finally:
            self.block_end()

    def all_blocks(self, blocks=None):
        """Every block, parents before children, in declaration order."""
        out = []
        for b in (self.blocks if blocks is None else blocks):
            out.append(b)
            out += self.all_blocks(b["children"])
        return out

    def _slices(self, b):
        if b["end"] is None:
            raise ValueError(f"{self.filename}: block {b['title']!r} was never closed")
        return {n: (b["start"][i], b["end"][i]) for i, n in enumerate(self._LISTS)}

    def _full_slices(self):
        return {n: (0, len(getattr(self, n))) for n in self._LISTS}

    def _bbox(self, sl):
        """Extent of the items in `sl`, text and field allowances included."""
        xs, ys = [], []

        def add(x, y):
            xs.append(x)
            ys.append(y)
        a, z = sl["symbols"]
        spots = self.layout_fields() if z > a else {}
        for s in self.symbols[a:z]:
            l, t, r, b = self.body_bbox(s)
            add(l, t)
            add(r, b)
            # where the reference, value or port name will REALLY be drawn, at
            # its measured width. The allowances below are a floor, not the
            # answer: a "10uF/50V" beside a capacitor on the block's flank ran
            # 3 mm past them and lay across the outline.
            for text, spot in zip((s["ref"], str(s["value"])), spots.get(id(s), ())):
                fx, fy, fj, _fa, hidden = spot
                if hidden or not text or (s["power"] and text == s["ref"]):
                    continue
                bx = self._text_box(text, fx, fy, 1.27, fj, 0)
                add(bx[0], bx[1])
                add(bx[2], bx[3])
            if not s["power"]:
                # reference/value text sits just outside the body
                add(l, t - 4.0)
                add(r + 12.0 if (b - t) >= (r - l) else r, b + 4.0)
            sym = self.lib.get(s["lib_id"])
            for p in sym.pins(s["unit"]):
                add(*pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"]))
        a, z = sl["segments"]
        for x1, y1, x2, y2 in self.segments[a:z]:
            add(x1, y1)
            add(x2, y2)
        a, z = sl["labels"]
        for _k, name, x, y, angle, _j, _sh in self.labels[a:z]:
            # measured, not guessed: the outline's clearance is only as good as
            # the extents it is derived from (+1.5 for a label's own offset)
            w = text_width(name, 1.27) + 1.5
            add(x, y)
            if angle == 180:
                add(x - w, y)
            elif angle == 0:
                add(x + w, y)
            elif angle == 90:
                add(x, y - w)
            else:
                add(x, y + w)
        a, z = sl["texts"]
        for content, x, y, size, _j, _b in self.texts[a:z]:
            tw, th = text_extent(content, size, bool(_b))
            add(x, y - size)
            add(x + tw, y - size + th)
        a, z = sl["no_connects"]
        for x, y in self.no_connects[a:z]:
            add(x, y)
        a, z = sl["buses"]
        for x1, y1, x2, y2 in self.buses[a:z]:
            add(x1, y1)
            add(x2, y2)
        a, z = sl["bus_entries"]
        for x, y, sx, sy in self.bus_entries[a:z]:
            add(x, y)
            add(x + sx, y + sy)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def block_rect(self, b):
        """The outline drawn for a block: its contents plus its padding."""
        bb = self._bbox(self._slices(b))
        for c in b["children"]:
            ce = self.block_extent(c)
            if ce is None:
                continue
            bb = ce if bb is None else (min(bb[0], ce[0]), min(bb[1], ce[1]),
                                        max(bb[2], ce[2]), max(bb[3], ce[3]))
        if bb is None:
            return None
        p = b["pad"] or 0.0
        return (bb[0] - p, bb[1] - p, bb[2] + p, bb[3] + p)

    def block_extent(self, b):
        """block_rect plus the band its title needs above it."""
        r = self.block_rect(b)
        if r is None:
            return None
        if not b["title"]:
            return r
        # the title is drawn from the top-left corner and is often wider than a
        # narrow block: reserve its width, or it runs into the next block along
        right = max(r[2], r[0] + text_width(b["title"], 1.6, True))
        return (r[0], r[1] - BLOCK_TITLE_H, right, r[3])

    def _block_holding(self, list_name, idx):
        """Innermost block whose range covers item `idx` of `list_name`."""
        best = None
        for b in self.all_blocks():
            if b["end"] is None:
                continue
            a, z = self._slices(b)[list_name]
            if a <= idx < z and (best is None or (z - a) < best[1]):
                best = (b, z - a)
        return best[0] if best else None

    def _insert_item(self, list_name, item, block):
        """Add an item so that it lands *inside* `block`.

        A block remembers the range of item indices it owns, so anything
        appended after it closed -- a wire the relief pass added, say -- would
        sit outside every block and be left behind the moment the blocks are
        arranged. Inserting at the block's end and shifting the marks keeps the
        bookkeeping honest."""
        lst = getattr(self, list_name)
        if block is None:
            lst.append(item)
            return len(lst) - 1
        i = self._LISTS.index(list_name)
        pos = self._slices(block)[list_name][1]
        lst.insert(pos, item)
        for b in self.all_blocks():
            for key in ("start", "end"):
                marks = b[key]
                if marks is None:
                    continue
                # everything at or after the insertion point moves up one --
                # except the target block's own start, which must stay put so
                # the new item lands inside it and not in the block next door
                if b is block and key == "start":
                    continue
                if marks[i] >= pos:
                    b[key] = marks[:i] + (marks[i] + 1,) + marks[i + 1:]
        return pos

    def translate_block(self, b, dx, dy):
        """Move one block's contents; connectivity inside it is unaffected."""
        if not dx and not dy:
            return
        self._fields = None
        self._items = None
        self._pin_pts = None
        self._paras = None
        sl = self._slices(b)

        def mv(x, y):
            return round(x + dx, 4), round(y + dy, 4)
        a, z = sl["symbols"]
        for s in self.symbols[a:z]:
            s["x"], s["y"] = mv(s["x"], s["y"])
        a, z = sl["segments"]
        self.segments[a:z] = [(*mv(x1, y1), *mv(x2, y2))
                              for x1, y1, x2, y2 in self.segments[a:z]]
        a, z = sl["labels"]
        self.labels[a:z] = [(k, n, *mv(x, y), ang, j, sh)
                            for k, n, x, y, ang, j, sh in self.labels[a:z]]
        a, z = sl["no_connects"]
        self.no_connects[a:z] = [mv(x, y) for x, y in self.no_connects[a:z]]
        a, z = sl["texts"]
        self.texts[a:z] = [(c, *mv(x, y), size, j, bold)
                           for c, x, y, size, j, bold in self.texts[a:z]]
        a, z = sl["buses"]
        self.buses[a:z] = [(*mv(x1, y1), *mv(x2, y2)) for x1, y1, x2, y2 in self.buses[a:z]]
        a, z = sl["bus_entries"]
        self.bus_entries[a:z] = [(*mv(x, y), sx, sy) for x, y, sx, sy in self.bus_entries[a:z]]

    def _target_width(self, blocks, gutter, maxw):
        """Wrap width giving a group a readable aspect: never wider than the
        page, never narrower than its widest member."""
        ext = [e for e in (self.block_extent(b) for b in blocks) if e]
        if not ext:
            return maxw
        widest = max(e[2] - e[0] for e in ext)
        area = sum((e[2] - e[0] + gutter) * (e[3] - e[1] + gutter) for e in ext)
        return max(widest, min(maxw, math.sqrt(area * BLOCK_ASPECT)))

    def _flow(self, blocks, width, gutter):
        """Pack blocks into rows: left to right, wrapping top to bottom."""
        for b in blocks:
            if b["children"]:
                w = b["width"]
                self._flow(b["children"],
                           self._target_width(b["children"], gutter, width) if w is None else w,
                           gutter)
        ext = [e for e in (self.block_extent(b) for b in blocks) if e]
        if not ext:
            return
        x0 = min(e[0] for e in ext)
        y0 = min(e[1] for e in ext)
        cx, cy, row_h = x0, y0, 0.0
        for b in blocks:
            e = self.block_extent(b)
            if e is None:
                continue
            w, h = e[2] - e[0], e[3] - e[1]
            if cx > x0 + 1e-9 and (cx - x0) + w > width:
                cx, cy, row_h = x0, cy + row_h + gutter, 0.0
            self.translate_block(b, snap(cx - e[0]), snap(cy - e[1]))
            # measure where it actually landed rather than trusting the
            # pre-move extent: snapping to the grid, and anything that shifts
            # an item's ownership, can move the edge by a fraction of a
            # millimetre -- which is exactly enough for two outlines to touch
            after = self.block_extent(b) or e
            cx = after[2] + gutter
            row_h = max(row_h, after[3] - cy)

    def drawing_bbox(self):
        """content_bbox widened to the block outlines and titles."""
        bb = self.content_bbox()
        for b in self.blocks:
            e = self.block_extent(b)
            if e:
                bb = (min(bb[0], e[0]), min(bb[1], e[1]),
                      max(bb[2], e[2]), max(bb[3], e[3]))
        return bb

    def arrange(self, gutter=BLOCK_GUTTER, width=None, grow=True, relieve=True):
        """Lay the blocks out in reading order and size the paper to fit.

        Blocks are placed in the order they were declared -- so declare them in
        signal order, input first -- flowing left to right and wrapping to a new
        row when the width runs out. `grow` steps the paper up (A4 -> A3 -> A2
        -> A1) until the drawing fits."""
        if self._block_stack:
            raise ValueError(f"{self.filename}: {len(self._block_stack)} block(s) never closed")
        if relieve and not self._relieved:
            # space the text out first: relief grows a group, and a block's
            # outline is derived from what is inside it
            self.relieve()
        if not self.blocks:
            return
        if self.paper == "auto" and width is None and grow:
            # Flow at each page width in turn and keep the first page the result
            # fits. Flowing once at the widest page put every block in one row,
            # and the page then picked for that row was a size too big: four
            # blocks went to A2 at 99% x 40% when A3 holds them at 76% x 94%.
            for p in GROW_PAPER_ORDER:
                uw, uh = self.usable_area(p)
                self._flow(self.blocks, uw, gutter)
                x0, y0, x1, y1 = self.drawing_bbox()
                if (x1 - x0) <= uw and (y1 - y0) <= uh:
                    break
            self.paper = p
            return
        paper = self.paper if self.paper in PAPER_SIZES else GROW_PAPER_ORDER[-1]
        self._flow(self.blocks, width or self.usable_area(paper)[0], gutter)
        while grow and width is None and self.paper in GROW_PAPER_ORDER:
            uw, uh = self.usable_area(self.paper)
            x0, y0, x1, y1 = self.drawing_bbox()
            if (x1 - x0) <= uw and (y1 - y0) <= uh:
                break
            i = GROW_PAPER_ORDER.index(self.paper)
            if i + 1 >= len(GROW_PAPER_ORDER):
                break
            self.paper = GROW_PAPER_ORDER[i + 1]
            self._flow(self.blocks, self.usable_area(self.paper)[0], gutter)

    def finish_blocks(self):
        """Draw every block's outline and title. Idempotent."""
        if self._block_stack:
            raise ValueError(f"{self.filename}: {len(self._block_stack)} block(s) never closed")
        if self._block_art is not None:
            nt, nr = self._block_art
            del self.texts[nt:]
            del self.rects[nr:]
        self._block_art = (len(self.texts), len(self.rects))
        for b in self.all_blocks():
            r = self.block_rect(b)
            if r is None:
                continue
            r = tuple(round(v, 4) for v in r)
            if b["pad"] is not None:      # pad=None: a layout group, not a drawn block
                self.rects.append(r)
            if b["title"]:
                self.texts.append((b["title"], r[0], round(r[1] - 2.03, 4), 1.6, "left", True))

    def check_blocks(self):
        """Blocks overlapping a block they are not nested inside."""
        blocks = [b for b in self.all_blocks() if self.block_rect(b)]
        rects = {id(b): self.block_rect(b) for b in blocks}
        names = {id(b): (b["title"] or "untitled") for b in blocks}

        def ancestors(b):
            out, p = set(), b["parent"]
            while p is not None:
                out.add(id(p))
                p = p["parent"]
            return out
        out = []
        for i, a in enumerate(blocks):
            for b in blocks[i + 1:]:
                if id(a) in ancestors(b) or id(b) in ancestors(a):
                    continue
                ra, rb = rects[id(a)], rects[id(b)]
                ox = min(ra[2], rb[2]) - max(ra[0], rb[0])
                oy = min(ra[3], rb[3]) - max(ra[1], rb[1])
                if ox > 0.01 and oy > 0.01:
                    out.append(f"block {names[id(a)]!r} overlaps block {names[id(b)]!r} "
                               f"by {ox:.1f} x {oy:.1f} mm")
        return out

    def check_spacing(self, min_gap=1.27):
        """Parts and notes drawn on top of, or too close to, each other.

        check() catches wires through bodies; this catches the other half: two
        symbols whose bodies collide, a note written over a part, or two notes
        written over each other. None of them is visible to ERC and all three
        read as a mistake on the page."""
        items = []
        for s in self.symbols:
            if s["power"]:
                continue
            items.append((s["ref"] or s["value"], self.body_bbox(s)))
        for content, x, y, size, j, _b in self.texts:
            lines = content.split(chr(10))
            w = max(len(ln) for ln in lines) * size * 0.85
            h = size * 1.6 * (len(lines) - 1) + size * 1.2
            x0 = x - w if j == "right" else x
            items.append((f"note {lines[0][:28]!r}",
                          (x0, y - size * 0.8, x0 + w, y - size * 0.8 + h)))
        out = []
        for i, (na, a) in enumerate(items):
            for nb, b in items[i + 1:]:
                ox = min(a[2], b[2]) - max(a[0], b[0])
                oy = min(a[3], b[3]) - max(a[1], b[1])
                if ox <= -min_gap or oy <= -min_gap:
                    continue
                if ox > 0.01 and oy > 0.01:
                    out.append(f"{na} overlaps {nb} by {ox:.1f} x {oy:.1f} mm")
                else:
                    out.append(f"{na} is {max(-ox, -oy):.1f} mm from {nb} "
                               f"(want {min_gap:.1f} mm)")
        return out


    # ---- what is drawn where ----------------------------------------------
    def layout_fields(self, force=False):
        """Choose every reference/value spot, once, and remember it.

        The emitter used to do this inline, which meant nothing else could
        know where the text would land -- so the spacing checks and the relief
        pass were blind to the two strings attached to every part. Computing
        it here lets the writer, the checks and the relief pass all read the
        same answer."""
        if self._fields is not None and not force:
            return self._fields
        taken = _field_obstacles(self)
        out = {}
        for s in self.symbols:
            if s["power"]:
                # Take the offset from the library and turn it with the symbol,
                # which is what KiCad does. A fixed "4 mm below" put the name of
                # a port on an upward pin straight back on top of the pin it
                # was labelling.
                sym = self.lib.get(s["lib_id"])
                off = sym.field_pos("Value") or (0.0, -3.81)
                dx, dy = _transform(off[0], off[1], s["rot"], s["mirror"])
                out[id(s)] = ((s["x"], s["y"] - 2.54, None, 0, True),
                              (round(s["x"] + dx, 4), round(s["y"] + dy, 4),
                               None, 0, s.get("hide_value", False)))
                continue
            sym = self.lib.get(s["lib_id"])
            body = self.body_bbox(s)
            fa = 0 if s["rot"] == 180 else (-s["rot"]) % 360
            pins = sym.pins(s["unit"])
            vertical_pair = False
            if len(pins) == 2:
                (ax, ay), (bx, by) = (pin_position(sym, pp, s["x"], s["y"], s["rot"], s["mirror"])
                                      for pp in pins)
                vertical_pair = abs(ax - bx) < abs(ay - by)
            (rx, ry, rj), (vx, vy, vj) = _choose_field_spots(
                s, body, vertical_pair, taken, s["ref"], str(s["value"]))
            out[id(s)] = ((rx, ry, rj, fa, False), (vx, vy, vj, fa, False))
        self._fields = out
        return out

    def _text_box(self, text, x, y, size, justify, angle=0, bold=False):
        """Box a string will occupy, from measured glyph advances."""
        w, h = text_extent(text, size, bold)
        if angle in (90, 270):
            w, h = h, w
            x0 = x - h / 2 if angle == 90 else x - h / 2
            return (x - w / 2 + (0 if justify is None else 0), y - h / 2,
                    x + w / 2, y + h / 2)
        if justify == "left":
            x0 = x
        elif justify == "right":
            x0 = x - w
        else:
            x0 = x - w / 2
        return (x0, y - h / 2, x0 + w, y + h / 2)

    def note_groups(self):
        """Consecutive note() calls that form one paragraph.

        Commentary is written a line at a time, so the sheet holds five notes
        where the reader sees one block of text. Treating them separately is
        how a layout pass ends up spacing a paragraph's own lines 7 mm apart,
        and how the spacing check reports a paragraph as crowding itself."""
        if self._paras is not None:
            return self._paras
        order = sorted(range(len(self.texts)), key=lambda i: (round(self.texts[i][1], 2),
                                                             self.texts[i][2]))
        out, cur = [], []
        for i in order:
            _c, x, y, size, j, bold = self.texts[i]
            if cur:
                _pc, px, py, psize, pj, pbold = self.texts[cur[-1]]
                same = (abs(px - x) < 0.01 and abs(psize - size) < 0.01
                        and pj == j and pbold == bold
                        and 0 < y - py <= size * 2.9)
                if not same:
                    out.append(cur)
                    cur = []
            cur.append(i)
        if cur:
            out.append(cur)
        self._paras = out
        return out

    def pin_text_boxes(self, s):
        """Where a symbol's own pin names and numbers are drawn.

        Not decoration: a label on a 2.54 mm stub starts exactly where the pin
        number is drawn, which is the single most common way a generated sheet
        ends up looking cramped. The layout cannot avoid what it cannot see."""
        sym = self.lib.get(s["lib_id"])
        if s["power"]:
            return []
        out = []
        for p in sym.pins(s["unit"]):
            tip = pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])
            ox, oy = pin_outward(p, s["rot"], s["mirror"])
            ln = p.get("length", 2.54)
            root = (tip[0] - ox * ln, tip[1] - oy * ln)      # where it meets the body
            h = 1.27 * TEXT_HEIGHT
            if not sym.hide_numbers and p["num"]:
                w = text_width(p["num"], 1.27)
                mx, my = (tip[0] + root[0]) / 2, (tip[1] + root[1]) / 2
                if ox:
                    out.append((mx - w / 2, my - h, mx + w / 2, my))
                else:
                    out.append((mx - h, my - w / 2, mx, my + w / 2))
            name = p["name"]
            if not sym.hide_names and name and name not in ("~", "NC"):
                w = text_width(name, 1.27) + 0.5
                x0 = min(root[0], root[0] - ox * w)
                x1 = max(root[0], root[0] - ox * w)
                y0 = min(root[1], root[1] - oy * w)
                y1 = max(root[1], root[1] - oy * w)
                if ox:
                    out.append((x0, root[1] - h / 2, x1, root[1] + h / 2))
                else:
                    out.append((root[0] - h / 2, y0, root[0] + h / 2, y1))
        return out

    def item_boxes(self, fields=True):
        if fields and self._items is not None:
            return self._items
        out = self._item_boxes(fields)
        if fields:
            self._items = out
        return out

    def _item_boxes(self, fields=True):
        """Everything drawn on this sheet, as boxes with an owner and a kind.

        kinds: body, field, label, note, nc. Wires are not included -- they are
        checked separately, because text is allowed to sit beside a wire but
        never on another string."""
        out = []
        fs = self.layout_fields() if fields else {}
        for n, s in enumerate(self.symbols):
            name = s["ref"] or str(s["value"])
            # the owner is per symbol, not per name: two GND ports crowding
            # each other is a real fault, and sharing an owner would hide it
            owner = f"{name}#{n}"
            out.append({"owner": owner, "kind": "body", "box": self.body_bbox(s),
                        "text": name, "sym": s})
            for box in self.pin_text_boxes(s):
                out.append({"owner": owner, "kind": "pin", "text": "pin", "sym": s,
                            "box": box})
            if not fields:
                continue
            spots = fs.get(id(s))
            if not spots:
                continue
            for text, (fx, fy, fj, fa, hidden) in zip((s["ref"], str(s["value"])), spots):
                if hidden or (s["power"] and text == s["ref"]):
                    continue
                if not text:
                    continue
                out.append({"owner": owner, "kind": "field", "text": text, "sym": s,
                            "box": self._text_box(text, fx, fy, 1.27, fj, 0),
                            "anchor": (fx, fy), "port": s["power"]})
        for kind, name, x, y, angle, _j, shape in self.labels:
            w, h = text_extent(name, 1.27)
            if shape:
                w += HLABEL_ARROW * 1.27
            if angle == 0:
                box = (x, y - h / 2, x + w, y + h / 2)
            elif angle == 180:
                box = (x - w, y - h / 2, x, y + h / 2)
            elif angle == 90:
                box = (x - h / 2, y - w, x + h / 2, y)
            else:
                box = (x - h / 2, y, x + h / 2, y + w)
            out.append({"owner": f"label {name}", "kind": "label", "text": name,
                        "box": box, "anchor": (x, y), "angle": angle})
        for gi, para in enumerate(self.note_groups()):
            box = None
            for i in para:
                content, x, y, size, j, bold = self.texts[i]
                w, h = text_extent(content, size, bold)
                x0 = x - w if j == "right" else (x - w / 2 if j == "center" else x)
                b = (x0, y - size * 0.8, x0 + w, y - size * 0.8 + h)
                box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                             max(box[2], b[2]), max(box[3], b[3]))
            first = str(self.texts[para[0]][0]).split(chr(10))[0]
            lines = [ln.strip() for i in para
                     for ln in str(self.texts[i][0]).split(chr(10))]
            out.append({"owner": f"note#{gi}", "kind": "note", "text": first,
                        "lines": lines,
                        "box": box, "anchor": (self.texts[para[0]][1],
                                               self.texts[para[0]][2]),
                        "para": para})
        pin_of = {}
        for n, s in enumerate(self.symbols):
            for (px, py) in self._own_pins(s):
                pin_of[(round(px, 3), round(py, 3))] = f"{s['ref'] or s['value']}#{n}"
        for x, y in self.no_connects:
            # the X is drawn on a pin, so it belongs to that part: counting it
            # as a separate item makes every unused pin look like a collision
            out.append({"owner": pin_of.get((round(x, 3), round(y, 3)), "nc"),
                        "kind": "nc", "text": "x",
                        "box": (x - 0.9, y - 0.9, x + 0.9, y + 0.9)})
        return out

    def check_clearance(self, row=CLEAR_ROW, stack=CLEAR_STACK, part_gap=CLEAR_PART):
        """Model-side spacing check: anything drawn on, or too close to,
        anything else. Fast enough to run inside a layout loop; KiCad's own
        render is the authority (Design.check_text) and must agree."""
        items = self.item_boxes()
        out = []
        for a, b in itertools.combinations(items, 2):
            if a["owner"] == b["owner"]:
                continue
            both = a["kind"] == "body" and b["kind"] == "body"
            f = clearance_fault(a["box"], b["box"],
                                part_gap if both else row,
                                part_gap if both else stack)
            if f is None:
                continue
            gap, how = f
            if how == "overlap":
                out.append((gap, f"{a['kind']} {a['owner']} overlaps "
                                 f"{b['kind']} {b['owner']} by {gap:.2f} mm"))
            else:
                out.append((gap, f"{a['kind']} {a['owner']} is {-gap:.2f} mm from "
                                 f"{b['kind']} {b['owner']} ({how})"))
        out.sort(key=lambda t: -t[0])
        return [m for _g, m in out]




    # ---- drawing conventions ----------------------------------------------
    def part_pitch(self, refs, extra=0.0):
        """Centre spacing that lets a row of two-pin parts show their text.

        A vertical two-pin part gets its reference and value stacked to its
        right, so the column spacing has to hold the body, that text and a
        readable gap -- not a round number someone liked the look of."""
        widest = 0.0
        for ref in refs:
            s = self._sym(ref)[0]
            l, _t, r, _b = self.body_bbox(s)
            text = max(text_width(str(s["ref"] or ""), 1.27),
                       text_width(str(s["value"]), 1.27))
            widest = max(widest, (r - l) / 2 + 1.78 + text)
        return snap(widest + CLEAR_ROW + extra + 1.27)

    def rail_bank(self, refs, x, y, rail, place=None, gnd="GND", pitch=None,
                  rail_kind=None, gnd_kind="power", note=None, flag=False):
        """A decoupling or bulk bank, drawn the way schematics draw one.

        The parts stand side by side between two bars: the rail along the top,
        ground along the bottom, each with a single symbol on its own short
        drop. That is the convention because it says the thing the reader
        needs -- these parts all sit across the same rail -- in one line
        instead of repeating a rail label and a ground symbol beside every
        capacitor, which is what makes a bank of caps read as a crowd.

        `place(ref, x, y)` places one part (the caller's own wrapper, so the
        project keeps its properties); parts already on the sheet are moved
        instead. `rail`/`gnd` are power-port names by default -- pass
        rail_kind="label" for a local net, or gnd=None for no ground bar.
        Returns (x0, y0, x1, y1)."""
        refs = list(refs)
        if not refs:
            return None
        x, y = snap(x), snap(y)
        for i, ref in enumerate(refs):
            if place is not None:
                place(ref, snap(x + i * (pitch or 12.7)), y)
        if pitch is None:
            pitch = self.part_pitch(refs)
        for i, ref in enumerate(refs):
            s = self._sym(ref)[0]
            dx = snap(x + i * pitch) - s["x"]
            dy = y - s["y"]
            if dx or dy:
                s["x"] = round(s["x"] + dx, 4)
                s["y"] = round(s["y"] + dy, 4)
        self._fields = None
        self._items = None
        self._pin_pts = None
        tops, bots = [], []
        for ref in refs:
            pins = self.pins_of(ref)
            if len(pins) != 2:
                raise ValueError(f"rail_bank: {ref} has {len(pins)} pins, not 2")
            (n1, _nm1, p1), (n2, _nm2, p2) = sorted(pins, key=lambda pp: pp[2][1])
            tops.append((ref, n1, p1))
            bots.append((ref, n2, p2))
        top_y = snap(min(p[1] for _r, _n, p in tops) - 3.81)
        bot_y = snap(max(p[1] for _r, _n, p in bots) + 3.81)
        for _ref, _n, (px, py) in tops:
            self.wire((px, py), (px, top_y))
        for _ref, _n, (px, py) in bots:
            self.wire((px, py), (px, bot_y))
        x0 = min(p[0] for _r, _n, p in tops)
        x1 = max(p[0] for _r, _n, p in tops)
        if x1 > x0:
            self.wire((x0, top_y), (x1, top_y))
            self.wire((x0, bot_y), (x1, bot_y))
        # one symbol per bar, on its own drop, at the left end where the rail
        # arrives -- not one per capacitor
        rail_y = snap(top_y - 2.54)
        self.wire((x0, top_y), (x0, rail_y))
        if (rail_kind or ("label" if ":" not in str(rail) and not str(rail).startswith("+")
                          and str(rail).upper() not in ("GND", "VCC", "VDD")
                          else "power")) == "label":
            self.label(rail, x0, rail_y, angle=90)
        else:
            self.power(rail, x0, rail_y)
        if flag:
            # the rail's ERC marker goes on the far end of the same bar, on its
            # own drop, so it never shares a point with the rail symbol
            if x1 > x0:
                self.wire((x1, top_y), (x1, rail_y))
                self.power("PWR_FLAG", x1, rail_y)
            else:               # a single part: take the flag out sideways
                self.wire((x0, rail_y), (snap(x0 + 7.62), rail_y))
                self.power("PWR_FLAG", snap(x0 + 7.62), rail_y)
        if gnd:
            gnd_y = snap(bot_y + 2.54)
            self.wire((x0, bot_y), (x0, gnd_y))
            if gnd_kind == "label":
                self.label(gnd, x0, gnd_y, angle=270)
            else:
                self.power(gnd, x0, gnd_y)
        if note:
            self.note(note, x0 - 1.27, snap(bot_y + 10.16), 1.0)
        return (x0, rail_y, x1, bot_y)

    # ---- groups: the parts a wire actually holds together ------------------
    def groups(self):
        """Split the sheet into pieces that can be moved independently.

        Two parts belong to the same piece only if a wire runs between them.
        Anything joined by a label or a power port is *not* connected on the
        page -- moving it changes nothing electrically -- which is what makes
        it safe to push pieces apart until they stop crowding each other.
        Notes are pieces of their own; a note is free to move.

        Returns [{items, indices}] where indices say which symbols, segments,
        labels, no-connects and texts belong to the piece."""
        pt = lambda x, y: (round(x, 3), round(y, 3))          # noqa: E731
        parent = {}

        def find(a):
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for x1, y1, x2, y2 in self.segments:
            union(pt(x1, y1), pt(x2, y2))
        # a wire ending in the middle of another wire joins it too
        for i, seg in enumerate(self.segments):
            for j, other in enumerate(self.segments):
                if i == j:
                    continue
                for p in ((other[0], other[1]), (other[2], other[3])):
                    if _on_segment(p, seg):
                        union(pt(*p), pt(seg[0], seg[1]))
        # a bus holds everything on it together, like a wire: its segments
        # join where they touch, and each entry joins its wire end to the bus
        for x1, y1, x2, y2 in self.buses:
            union(pt(x1, y1), pt(x2, y2))
        for i, seg in enumerate(self.buses):
            for j, other in enumerate(self.buses):
                if i != j:
                    for p in ((other[0], other[1]), (other[2], other[3])):
                        if _on_segment(p, seg):
                            union(pt(*p), pt(seg[0], seg[1]))
        for x, y, sx, sy in self.bus_entries:
            union(pt(x, y), pt(x + sx, y + sy))
            for seg in self.buses:
                if _on_segment((x + sx, y + sy), seg):
                    union(pt(x + sx, y + sy), pt(seg[0], seg[1]))
        sym_key = {}
        for n, s in enumerate(self.symbols):
            pins = self._own_pins(s)
            key = ("sym", n)
            parent.setdefault(key, key)
            for (px, py) in pins:
                union(key, pt(px, py))
                # a pin touching the middle of a wire is connected to it --
                # KiCad says so -- and a group that does not know that gets
                # moved away from a net it is part of
                for seg in self.segments:
                    if _on_segment((px, py), seg):
                        union(key, pt(seg[0], seg[1]))
            sym_key[n] = key
        for gi, _para in enumerate(self.note_groups()):
            parent.setdefault(("note", gi), ("note", gi))

        buckets = {}
        for n in range(len(self.symbols)):
            buckets.setdefault(find(sym_key[n]), _new_bucket())["sym"].append(n)
        for n, seg in enumerate(self.segments):
            r = find(pt(seg[0], seg[1]))
            buckets.setdefault(r, _new_bucket())["seg"].append(n)
        for n, (_k, _name, x, y, *_r) in enumerate(self.labels):
            key = pt(x, y)
            if key not in parent:
                # a label dropped on the middle of a wire names that net and
                # has to travel with it; treated as its own piece it gets left
                # behind and the net quietly splits in two
                for seg in self.segments + self.buses:
                    if _on_segment((x, y), seg):
                        key = pt(seg[0], seg[1])
                        break
            r = find(key)
            buckets.setdefault(r, _new_bucket())["lab"].append(n)
        for n, (x, y) in enumerate(self.no_connects):
            r = find(pt(x, y))
            buckets.setdefault(r, _new_bucket())["nc"].append(n)
        for n, seg in enumerate(self.buses):
            buckets.setdefault(find(pt(seg[0], seg[1])), _new_bucket())["bus"].append(n)
        for n, (x, y, _sx, _sy) in enumerate(self.bus_entries):
            buckets.setdefault(find(pt(x, y)), _new_bucket())["ent"].append(n)
        for gi, para in enumerate(self.note_groups()):
            b = _new_bucket()
            b["txt"] = list(para)
            buckets[("note", gi)] = b
        return list(buckets.values())

    def group_box(self, g, fields=None):
        """Extent of one group, text included."""
        xs, ys = [], []

        def add(b):
            xs.append(b[0])
            ys.append(b[1])
            xs.append(b[2])
            ys.append(b[3])
        fields = self.layout_fields() if fields is None else fields
        for n in g["sym"]:
            s = self.symbols[n]
            add(self.body_bbox(s))
            spots = fields.get(id(s))
            if not spots:
                continue
            for text, (fx, fy, fj, _fa, hidden) in zip((s["ref"], str(s["value"])), spots):
                if hidden or not text or (s["power"] and text == s["ref"]):
                    continue
                add(self._text_box(text, fx, fy, 1.27, fj))
        for n in g["seg"]:
            x1, y1, x2, y2 = self.segments[n]
            add((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        for n in g["lab"]:
            _k, name, x, y, angle, _j, shape = self.labels[n]
            w, h = text_extent(name, 1.27)
            if shape:
                w += HLABEL_ARROW * 1.27
            add({0: (x, y - h / 2, x + w, y + h / 2),
                 180: (x - w, y - h / 2, x, y + h / 2),
                 90: (x - h / 2, y - w, x + h / 2, y),
                 270: (x - h / 2, y, x + h / 2, y + w)}[angle])
        for n in g["nc"]:
            x, y = self.no_connects[n]
            add((x - 0.9, y - 0.9, x + 0.9, y + 0.9))
        for n in g.get("bus", ()):
            x1, y1, x2, y2 = self.buses[n]
            add((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        for n in g.get("ent", ()):
            x, y, sx, sy = self.bus_entries[n]
            add((min(x, x + sx), min(y, y + sy), max(x, x + sx), max(y, y + sy)))
        for n in g["txt"]:
            content, x, y, size, j, bold = self.texts[n]
            w, h = text_extent(content, size, bold)
            x0 = x - w if j == "right" else (x - w / 2 if j == "center" else x)
            add((x0, y - size * 0.8, x0 + w, y - size * 0.8 + h))
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def _group_contacts(self, g):
        """The points at which a group could touch something: its wire ends,
        its pins and its label anchors."""
        pts = set()
        for n in g["seg"]:
            x1, y1, x2, y2 = self.segments[n]
            pts.add((round(x1, 3), round(y1, 3)))
            pts.add((round(x2, 3), round(y2, 3)))
        for n in g["sym"]:
            for (px, py) in self._own_pins(self.symbols[n]):
                pts.add((round(px, 3), round(py, 3)))
        for n in g["lab"]:
            _k, _nm, x, y, *_r = self.labels[n]
            pts.add((round(x, 3), round(y, 3)))
        for n in g.get("bus", ()):
            x1, y1, x2, y2 = self.buses[n]
            pts.add((round(x1, 3), round(y1, 3)))
            pts.add((round(x2, 3), round(y2, 3)))
        for n in g.get("ent", ()):
            x, y, _sx, _sy = self.bus_entries[n]
            pts.add((round(x, 3), round(y, 3)))
        return pts

    def _move_is_safe(self, g, dx, dy):
        """True if moving this group cannot change what is connected.

        Groups are independent -- nothing but labels joins them -- but a group
        that lands with a wire end on another group's wire *is* connected to
        it, and KiCad would merge the two nets without a word. Every move is
        checked against everyone else's geometry before it is kept."""
        mine_seg = set(g["seg"])
        mine_bus = set(g.get("bus", ()))
        pts = {(round(x + dx, 3), round(y + dy, 3)) for (x, y) in self._group_contacts(g)}
        others = [seg for i, seg in enumerate(self.segments) if i not in mine_seg]
        others += [seg for i, seg in enumerate(self.buses) if i not in mine_bus]
        mine_ent = set(g.get("ent", ()))
        for i, (x, y, sx, sy) in enumerate(self.bus_entries):
            if i not in mine_ent and ((round(x, 3), round(y, 3)) in pts or
                                      (round(x + sx, 3), round(y + sy, 3)) in pts):
                return False
        for p in pts:
            for seg in others:
                if _on_segment(p, seg):
                    return False
        mine_pts = pts
        for i, (x1, y1, x2, y2) in enumerate(self.segments):
            if i not in mine_seg:
                continue
            a = (round(x1 + dx, 3), round(y1 + dy, 3))
            b = (round(x2 + dx, 3), round(y2 + dy, 3))
            moved = (a[0], a[1], b[0], b[1])
            for j, other in enumerate(self.segments):
                if j in mine_seg:
                    continue
                for pt in ((other[0], other[1]), (other[2], other[3])):
                    if _on_segment(pt, moved):
                        return False
        for n, s in enumerate(self.symbols):
            if n in g["sym"]:
                continue
            for (px, py) in self._own_pins(s):
                if (round(px, 3), round(py, 3)) in mine_pts:
                    return False
        return True

    def move_group(self, g, dx, dy):
        if not dx and not dy:
            return
        self._fields = None
        self._items = None
        self._pin_pts = None
        self._paras = None
        for n in g["sym"]:
            s = self.symbols[n]
            s["x"] = round(s["x"] + dx, 4)
            s["y"] = round(s["y"] + dy, 4)
        for n in g["seg"]:
            x1, y1, x2, y2 = self.segments[n]
            self.segments[n] = (round(x1 + dx, 4), round(y1 + dy, 4),
                                round(x2 + dx, 4), round(y2 + dy, 4))
        for n in g["lab"]:
            k, name, x, y, a, j, sh = self.labels[n]
            self.labels[n] = (k, name, round(x + dx, 4), round(y + dy, 4), a, j, sh)
        for n in g["nc"]:
            x, y = self.no_connects[n]
            self.no_connects[n] = (round(x + dx, 4), round(y + dy, 4))
        for n in g["txt"]:
            c, x, y, size, j, bold = self.texts[n]
            self.texts[n] = (c, round(x + dx, 4), round(y + dy, 4), size, j, bold)
        for n in g.get("bus", ()):
            x1, y1, x2, y2 = self.buses[n]
            self.buses[n] = (round(x1 + dx, 4), round(y1 + dy, 4),
                             round(x2 + dx, 4), round(y2 + dy, 4))
        for n in g.get("ent", ()):
            x, y, sx, sy = self.bus_entries[n]
            self.bus_entries[n] = (round(x + dx, 4), round(y + dy, 4), sx, sy)

    def _group_block(self, g):
        """The block a group belongs to (the innermost one holding its first
        symbol or note), so spreading cannot push a part out of its block."""
        for b in reversed(self.all_blocks()):
            sl = self._slices(b)
            for n in g["sym"]:
                if sl["symbols"][0] <= n < sl["symbols"][1]:
                    return b
            for n in g["txt"]:
                if sl["texts"][0] <= n < sl["texts"][1]:
                    return b
        return None

    def spread(self, gap=CLEAR_GROUP, rounds=120, quiet=True):
        """Push wire-connected groups apart until nothing crowds.

        Relative positions are kept -- a group only ever moves away from what
        it is too close to -- so the sheet still reads the way it was drawn,
        just with room around each piece. Groups joined only by labels are
        independent, so this changes no connection at all; `Design.verify()`
        proves it after every run.

        Groups inside different blocks are spread against each other too, and
        the blocks are re-flowed afterwards, so a block simply grows."""
        moved_total = 0
        for _ in range(rounds):
            groups = self.groups()
            fields = self.layout_fields()
            boxes = [self.group_box(g, fields) for g in groups]
            owner = [self._group_block(g) for g in groups]
            # A wire that ends on a child sheet's pin is nailed there: the pin
            # belongs to the sheet box, not to the group, so moving the group
            # would leave the wire dangling. Freeze those groups in place and
            # let everything else move around them.
            anchors = {(round(x, 3), round(y, 3)) for x, y in self._sheet_pin_points()}
            frozen = []
            for g in groups:
                pts = set()
                for n in g["seg"]:
                    x1, y1, x2, y2 = self.segments[n]
                    pts.add((round(x1, 3), round(y1, 3)))
                    pts.add((round(x2, 3), round(y2, 3)))
                for n in g["lab"]:
                    _k, _nm, x, y, *_r = self.labels[n]
                    pts.add((round(x, 3), round(y, 3)))
                frozen.append(bool(pts & anchors))
            push = [[0.0, 0.0] for _ in groups]
            worst = 0.0
            for i, j in itertools.combinations(range(len(groups)), 2):
                a, b = boxes[i], boxes[j]
                if a is None or b is None:
                    continue
                if owner[i] is not None and owner[j] is not None and owner[i] is not owner[j]:
                    # different blocks: the block flow handles their spacing
                    continue
                ox = min(a[2], b[2]) - max(a[0], b[0]) + gap
                oy = min(a[3], b[3]) - max(a[1], b[1]) + gap
                if ox <= 0 or oy <= 0:
                    continue
                worst = max(worst, min(ox, oy))
                ax = 0 if ox <= oy else 1
                d = (ox if ax == 0 else oy)
                if frozen[i] and frozen[j]:
                    continue
                # Push the *later* one along the axis, never both: pushing two
                # neighbours apart symmetrically cancels out in the middle of a
                # row (each group gets an equal shove from either side) and the
                # row never actually expands. Moving only the one further along
                # makes a row or a column grow outward, monotonically.
                lo_i = (a[0] + a[2]) < (b[0] + b[2]) if ax == 0 else (a[1] + a[3]) < (b[1] + b[3])
                later = j if lo_i else i
                earlier = i if lo_i else j
                if frozen[later]:
                    push[earlier][ax] = min(push[earlier][ax], -d)
                else:
                    push[later][ax] = max(push[later][ax], d)
            if worst < 0.01:
                break
            stepped = 0
            for g, (dx, dy) in zip(groups, push):
                dx, dy = snap(dx), snap(dy)
                if (dx or dy) and self._move_is_safe(g, dx, dy):
                    self.move_group(g, dx, dy)
                    moved_total += 1
                    stepped += 1
            if not stepped:                 # nothing left that a grid step fixes
                break
        if not quiet:
            print(f"  spread: {moved_total} group move(s)")
        return moved_total

    # ---- relief: push crowded text apart, change no connection -------------
    CELL = 16.0                  # spatial bucket, mm

    @staticmethod
    def _bucket(items):
        """Grid index of item boxes, so a clearance pass only compares things
        that could possibly be close. All-pairs on a 900-item sheet is 400k
        comparisons per round, and the relief loop runs it hundreds of times."""
        grid = {}
        for n, it in enumerate(items):
            b = it["box"]
            for cx in range(int(b[0] // Sheet.CELL), int(b[2] // Sheet.CELL) + 1):
                for cy in range(int(b[1] // Sheet.CELL), int(b[3] // Sheet.CELL) + 1):
                    grid.setdefault((cx, cy), []).append(n)
        return grid

    @staticmethod
    def _near(grid, box, pad=2.0):
        out = set()
        for cx in range(int((box[0] - pad) // Sheet.CELL), int((box[2] + pad) // Sheet.CELL) + 1):
            for cy in range(int((box[1] - pad) // Sheet.CELL), int((box[3] + pad) // Sheet.CELL) + 1):
                out.update(grid.get((cx, cy), ()))
        return out

    def _crowded(self, min_gap=CLEAR_ROW, part_gap=CLEAR_PART, items=None):
        """[(a, b, gap)] for every pair of drawn items that is too close."""
        items = self.item_boxes() if items is None else items
        grid = self._bucket(items)
        out, seen = [], set()
        for i, a in enumerate(items):
            for j in self._near(grid, a["box"]):
                if j <= i or (i, j) in seen:
                    continue
                seen.add((i, j))
                b = items[j]
                if a["owner"] == b["owner"]:
                    continue
                both = a["kind"] == "body" and b["kind"] == "body"
                f = clearance_fault(a["box"], b["box"],
                                    part_gap if both else min_gap,
                                    part_gap if both else CLEAR_STACK)
                if f:
                    out.append((a, b, f[0]))
        return out

    def _pin_points(self, ignore_sym=None):
        """Every placed pin, optionally excluding one symbol's own.

        A power port *is* a symbol with a pin, so its anchor is always a pin
        point -- without the exclusion, _free_end() answers "not free" for
        every port and relief can never slide one along its own stub."""
        if ignore_sym is not None:
            skip = {(round(x, 3), round(y, 3)) for (x, y) in self._own_pins(ignore_sym)}
            return {p for p in self._pin_points() if p not in skip}
        if self._pin_pts is None:
            self._pin_pts = {(round(x, 3), round(y, 3))
                             for _s, _p, (x, y) in self._placed_pins()}
        return self._pin_pts

    def _free_end(self, pt, ignore_sym=None):
        """Index of the one wire ending at `pt`, if the point is otherwise
        free -- no pin, no second wire, nothing to break by moving it."""
        pt = (round(pt[0], 3), round(pt[1], 3))
        if pt in self._pin_points(ignore_sym) or pt in self._bus_points():
            return None
        hits = []
        for i, (x1, y1, x2, y2) in enumerate(self.segments):
            a = (round(x1, 3), round(y1, 3))
            b = (round(x2, 3), round(y2, 3))
            if pt == a:
                hits.append((i, 0))
            elif pt == b:
                hits.append((i, 1))
            elif _on_segment(pt, (x1, y1, x2, y2)):
                return None          # the point sits on another wire's middle
        return hits[0] if len(hits) == 1 else None

    def _safe_wire(self, seg, ignore, moving=None):
        """True if a wire may run here without changing what is connected.

        `moving` is the end being relocated. Only that end has to land on
        empty space: the other end is a corner that is already part of this
        net -- the stub it continues, the pin it leaves -- and treating that
        as a fault refuses every relief move there is. Everything else is
        still forbidden: a pin or another wire's end on this wire's interior,
        a collinear overlap, a run across a part's body. Each of those would
        merge two nets, and the netlist check would catch it afterwards; this
        stops it happening at all."""
        x1, y1, x2, y2 = seg
        ends = {(round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))}
        free = None if moving is None else (round(moving[0], 3), round(moving[1], 3))
        fixed = (ends - {free}) if free is not None else set()
        ignore = ignore if isinstance(ignore, (set, frozenset)) else (
            set() if ignore is None else {ignore})
        for (px, py) in self._pin_points():
            if (px, py) in fixed:
                continue
            if (px, py) in ends and free is None:
                continue
            if _on_segment((px, py), seg):
                return False
        for i, other in enumerate(self.segments):
            if i in ignore:
                continue
            ox1, oy1, ox2, oy2 = other
            horiz_o = abs(oy1 - oy2) < 1e-6
            horiz_s = abs(y1 - y2) < 1e-6
            if horiz_o == horiz_s:
                # parallel: only an overlap matters (it would merge the nets)
                if horiz_s and abs(y1 - oy1) < 1e-6:
                    if min(x1, x2) < max(ox1, ox2) - 1e-6 and max(x1, x2) > min(ox1, ox2) + 1e-6:
                        return False
                if not horiz_s and abs(x1 - ox1) < 1e-6:
                    if min(y1, y2) < max(oy1, oy2) - 1e-6 and max(y1, y2) > min(oy1, oy2) + 1e-6:
                        return False
                continue
            # A wire ending on this one's *interior* is a new junction and
            # would merge two nets. A wire ending at one of its corners is
            # just a corner -- which is what every stub-and-drop is, so
            # treating that as a fault refused every relief move going.
            for pt in ((ox1, oy1), (ox2, oy2)):
                if (round(pt[0], 3), round(pt[1], 3)) in fixed:
                    continue          # a corner of this wire's own net
                if _on_segment(pt, seg):
                    return False
            for pt in ((x1, y1), (x2, y2)):
                if (round(pt[0], 3), round(pt[1], 3)) in fixed:
                    continue
                if _on_segment(pt, other):
                    return False
        # a wire may cross a bus, but never end on one, run along one, or pass
        # through an entry's end -- the first is an ERC error, the last a new
        # connection
        for bseg in self.buses:
            bx1, by1, bx2, by2 = bseg
            for pt in ((x1, y1), (x2, y2)):
                if (round(pt[0], 3), round(pt[1], 3)) not in fixed and _on_segment(pt, bseg):
                    return False
            if abs(y1 - y2) < 1e-6 and abs(by1 - by2) < 1e-6 and abs(y1 - by1) < 1e-6 and \
               min(x1, x2) < max(bx1, bx2) and max(x1, x2) > min(bx1, bx2):
                return False
            if abs(x1 - x2) < 1e-6 and abs(bx1 - bx2) < 1e-6 and abs(x1 - bx1) < 1e-6 and \
               min(y1, y2) < max(by1, by2) and max(y1, y2) > min(by1, by2):
                return False
        for bp in self._bus_points():
            if bp not in fixed and _on_segment(bp, seg):
                return False
        for s in self.symbols:
            if s["power"]:
                continue
            l, t, r, b = self.body_bbox(s)
            own = any(_same((px, py), (x1, y1)) or _same((px, py), (x2, y2))
                      for (px, py) in self._own_pins(s))
            if own:
                continue
            if min(x1, x2) < r - 0.2 and max(x1, x2) > l + 0.2 and \
               min(y1, y2) < b - 0.2 and max(y1, y2) > t + 0.2:
                return False
        return True

    def _seg_clear_of_text(self, seg, boxes):
        """True if a wire can run here without crossing any string."""
        x1, y1, x2, y2 = seg
        lo = (min(x1, x2) - 0.3, min(y1, y2) - 0.3,
              max(x1, x2) + 0.3, max(y1, y2) + 0.3)
        return not any(_boxes_hit(lo, b) for b in boxes)

    def _text_clear(self, box, own_seg=None, own_sym=None):
        """True if a string may be drawn here: no wire through it, no part
        body under it. Relief uses this as a hard constraint -- a move that
        parks a label on a wire trades one fault for a worse one."""
        for i, (x1, y1, x2, y2) in enumerate(self.segments):
            if i == own_seg:
                continue
            if abs(y1 - y2) < 1e-6:
                if box[1] < y1 < box[3] and min(x1, x2) < box[2] and max(x1, x2) > box[0]:
                    return False
            elif abs(x1 - x2) < 1e-6:
                if box[0] < x1 < box[2] and min(y1, y2) < box[3] and max(y1, y2) > box[1]:
                    return False
        for s in self.symbols:
            if s is own_sym:
                continue
            if _boxes_hit(box, self.body_bbox(s)):
                return False
        return True

    def _own_pins(self, s):
        sym = self.lib.get(s["lib_id"])
        return [pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])
                for p in sym.pins(s["unit"])]

    def _remove_item(self, list_name, idx):
        """Delete an item and keep every block's range honest."""
        i = self._LISTS.index(list_name)
        del getattr(self, list_name)[idx]
        for b in self.all_blocks():
            for key in ("start", "end"):
                marks = b[key]
                if marks is None:
                    continue
                if marks[i] > idx:
                    b[key] = marks[:i] + (marks[i] - 1,) + marks[i + 1:]

    def bundle_ports(self, min_count=2, quiet=True, limit=200):
        """Bundle one group at a time until there is nothing left to bundle.

        Applying a bundle renumbers symbols and segments, so the indices the
        scan collected for every *other* group on the same part go stale --
        deleting by a stale index removes the wrong port and leaves its stub
        hanging. One bundle per scan keeps the bookkeeping trivially right."""
        made = 0
        while made < limit and self._bundle_one(min_count):
            made += 1
        if not quiet and made:
            print(f"  bundled {made} port group(s)")
        return made

    def _bundle_one(self, min_count=2):
        """Join a flank's repeated power pins onto one bar with one symbol.

        A 100-pin connector with twenty grounds gets twenty GND flags, each
        with its name drawn on a 2.54 mm pitch: the names collide with the pin
        numbers and with each other, and the reader learns nothing from the
        nineteenth flag. Every schematic drawn by hand bundles them instead --
        one bar down the flank, one symbol at its end -- and that is what this
        does. It is also strictly fewer items on the page.

        Only pins that already end in a plain one-segment stub to a port are
        touched, the bar is checked against everything before it is drawn, and
        nothing is moved, so the netlist cannot change."""
        for sym_i, s in enumerate(list(self.symbols)):
            if s["power"]:
                continue
            groups = {}
            for (px, py) in self._own_pins(s):
                hit = None
                for seg_i, (x1, y1, x2, y2) in enumerate(self.segments):
                    if _same((x1, y1), (px, py)):
                        hit = (seg_i, (x2, y2))
                    elif _same((x2, y2), (px, py)):
                        hit = (seg_i, (x1, y1))
                    if hit:
                        break
                if hit is None:
                    continue
                seg_i, end = hit
                if self._free_end(end) is not None:
                    continue                      # no port there: a label end
                port_i = None
                for j, o in enumerate(self.symbols):
                    if o["power"] and _same((o["x"], o["y"]), end):
                        port_i = j
                        break
                if port_i is None:
                    continue
                dx = 0 if abs(end[0] - px) < 1e-6 else (1 if end[0] > px else -1)
                dy = 0 if abs(end[1] - py) < 1e-6 else (1 if end[1] > py else -1)
                if dx and dy:
                    continue
                key = (self.symbols[port_i]["value"], dx, dy)
                groups.setdefault(key, []).append((px, py, seg_i, port_i, end))
            for (value, dx, dy), members in groups.items():
                if len(members) < min_count:
                    continue
                if not dx:                        # vertical stubs: not handled
                    continue
                reach = max(abs(m[4][0] - m[0]) for m in members)
                ys = sorted(m[1] for m in members)
                down = str(value).upper() in ("GND", "GNDA", "GNDD", "AGND",
                                              "DGND", "VSS")
                end_y = round((max(ys) + 2.54) if down else (min(ys) - 2.54), 4)
                # the bar runs down the whole flank, so it has to clear that
                # flank's label lane -- step it outward until it does
                mine = {id(m) for m in members}
                boxes = [it["box"] for it in self.item_boxes()
                         if it["kind"] in ("label", "field", "note")
                         and not (it.get("sym") is not None and it["sym"]["power"]
                                  and any(_same((it["sym"]["x"], it["sym"]["y"]), m[4])
                                          for m in members))]
                new = None
                for step in range(1, 14):
                    bar_x = round(members[0][0] + dx * (reach + 2.54 * step), 4)
                    cand = [(m[0], m[1], bar_x, m[1]) for m in members]
                    cand.append((bar_x, min(ys), bar_x, max(ys)))
                    cand.append((bar_x, max(ys) if down else min(ys), bar_x, end_y))
                    fresh = [w for w in cand
                             if not any(_same(w[:2], (m[0], m[1])) for m in members)]
                    if not all(self._safe_wire(w, ignore=None) for w in fresh):
                        continue
                    # the lengthened stubs were never checked: _safe_wire
                    # cannot, because each overlaps its own old stub and
                    # passes over the port that is about to be removed. Check
                    # them here, against everything else's pins and ends.
                    if not all(self._stub_path_clear(w, members)
                               for w in cand if w not in fresh):
                        continue
                    if not all(self._seg_clear_of_text(w, boxes) for w in cand):
                        continue
                    lib = self._port_lib(value)
                    off = self.lib.get(lib).field_pos("Value") or (0.0, -3.81)
                    tw, th = text_extent(value, 1.27)
                    tx, ty = bar_x + off[0], end_y - off[1]
                    label_box = (tx - tw / 2, ty - th / 2, tx + tw / 2, ty + th / 2)
                    if not self._seg_clear_of_text((label_box[0], label_box[1],
                                                    label_box[2], label_box[3]), boxes):
                        continue
                    new = cand
                    break
                if new is None:
                    continue
                block = self._block_holding("segments", members[0][2])
                for m, w in zip(members, new):    # stub -> bar
                    self.segments[m[2]] = w
                for w in new[len(members):]:
                    self._insert_item("segments", w, block)
                for port_i in sorted((m[3] for m in members), reverse=True):
                    self._remove_item("symbols", port_i)
                self.power(value if ":" in value else self._port_lib(value),
                           bar_x, end_y, rot=0 if down else 0)
                # the port was appended at the end: move it into the block
                sym = self.symbols.pop()
                self._insert_item("symbols", sym, block)
                self._fields = None
                self._items = None
                self._pin_pts = None
                return True
        return False

    def _stub_path_clear(self, w, members):
        """True if a stub lengthened by bundle_ports crosses nothing but its
        own old stub and the ports being bundled."""
        r3 = lambda p: (round(p[0], 3), round(p[1], 3))       # noqa: E731
        skip = {r3(m[4]) for m in members} | {r3((m[0], m[1])) for m in members}
        mine = {m[2] for m in members}
        for p in self._pin_points() | self._bus_points():
            if p not in skip and _on_segment(p, w):
                return False
        for i, o in enumerate(self.segments):
            if i in mine:
                continue
            if any(_on_segment(q, w) for q in ((o[0], o[1]), (o[2], o[3]))):
                return False
            if any(_on_segment(q, o) for q in (w[:2], w[2:])):
                return False
        return True

    def _port_lib(self, value):
        """The lib_id a power port with this value came from."""
        for s in self.symbols:
            if s["power"] and s["value"] == value:
                return s["lib_id"]
        return value

    def upright_power_ports(self, reach=5):
        """Stand every rotated power port back up: rails above the wire,
        grounds below, so the name reads horizontally.

        A port at the end of a horizontal stub has to be rotated to face the
        wire, which turns its name through 90 degrees. A name is longer than
        the 2.54 mm pin pitch of a dense IC flank, so a column of rotated
        names overlaps into an unreadable smear.

        Standing it up is only half the job. Upright, the name is roughly 4 mm
        tall, so on a 2.54 mm pitch it reaches into the row above or below --
        which is why this also chooses how far along the stub the port sits.
        Trying successive positions gives neighbouring ports different columns
        (the stagger a draughtsman would use) rather than one column of text
        fighting over the same space."""
        n = 0
        for s in list(self.symbols):
            if not s["power"] or s["rot"] not in (90, 270):
                continue
            x, y = s["x"], s["y"]
            found = self._free_end((x, y), ignore_sym=s)
            dx = dy = 0.0
            seg_i = None
            if found is not None:
                seg_i, which = found
                seg = self.segments[seg_i]
                other = (seg[2], seg[3]) if which == 0 else (seg[0], seg[1])
                dx = 0.0 if abs(other[0] - x) < 1e-6 else (1.0 if x > other[0] else -1.0)
                dy = 0.0 if abs(other[1] - y) < 1e-6 else (1.0 if y > other[1] else -1.0)
                base = other
            down = str(s["value"]).upper() in ("GND", "GNDA", "GNDD", "AGND", "DGND", "VSS")
            saved = (self.segments[seg_i] if seg_i is not None else None, x, y, s["rot"])
            best = None
            for k in range(reach):
                px = round(x + dx * 2.54 * k, 4)
                py = round(y + dy * 2.54 * k, 4)
                for drop in ((2.54, -2.54) if down else (-2.54, 2.54)):
                    stub = (base[0], base[1], px, py) if seg_i is not None else None
                    leg = (px, py, px, round(py + drop, 4))
                    if stub is not None and _same((stub[0], stub[1]), (stub[2], stub[3])):
                        continue
                    if stub is not None and not self._safe_wire(stub, ignore=seg_i,
                                                                moving=(px, py)):
                        continue
                    if not self._safe_wire(leg, ignore=seg_i,
                                           moving=(px, round(py + drop, 4))):
                        continue
                    if seg_i is not None:
                        self.segments[seg_i] = stub
                    self.segments.append(leg)
                    s["x"], s["y"], s["rot"] = px, round(py + drop, 4), 0
                    self._fields = None
                    self._items = None
                    self._pin_pts = None
                    probe = next((it for it in self.item_boxes()
                                  if it["kind"] == "field" and it.get("sym") is s), None)
                    score = 1e9
                    if probe and self._text_clear(probe["box"], own_seg=seg_i, own_sym=s):
                        score = self._score(probe, CLEAR_ROW)
                    if best is None or score < best[0]:
                        best = (score, self.segments[seg_i] if seg_i is not None else None,
                                leg, px, round(py + drop, 4))
                    self.segments.pop()
                    if seg_i is not None:
                        self.segments[seg_i] = saved[0]
                    s["x"], s["y"], s["rot"] = saved[1], saved[2], saved[3]
                    self._fields = None
                    self._items = None
                    self._pin_pts = None
                    if score <= 0.01:
                        break
                if best and best[0] <= 0.01:
                    break
            if best is None:
                continue
            _score, stub, leg, px, py = best
            if stub is not None:
                self.segments[seg_i] = stub
            owner = (self._block_holding("segments", seg_i) if seg_i is not None
                     else self._block_holding("symbols", self.symbols.index(s)))
            self._insert_item("segments", leg, owner)
            s["x"], s["y"], s["rot"] = px, py, 0
            self._fields = None
            self._items = None
            self._pin_pts = None
            n += 1
        return n

    def _move_label(self, idx, newpt):
        """Slide a label and the free wire end it sits on. Returns True if the
        move is geometrically safe."""
        kind, name, x, y, angle, just, shape = self.labels[idx]
        found = self._free_end((x, y))
        if found is None:
            return False
        seg_i, which = found
        seg = list(self.segments[seg_i])
        other = (seg[2], seg[3]) if which == 0 else (seg[0], seg[1])
        if abs(other[0] - newpt[0]) > 1e-6 and abs(other[1] - newpt[1]) > 1e-6:
            return False             # would make the wire diagonal
        cand = (other[0], other[1], newpt[0], newpt[1])
        if abs(cand[0] - cand[2]) < 1e-6 and abs(cand[1] - cand[3]) < 1e-6:
            return False
        if not self._safe_wire(cand, ignore=seg_i, moving=newpt):
            return False
        self.segments[seg_i] = (round(other[0], 4), round(other[1], 4),
                                round(newpt[0], 4), round(newpt[1], 4))
        self.labels[idx] = (kind, name, round(newpt[0], 4), round(newpt[1], 4),
                            angle, just, shape)
        self._fields = None
        self._items = None
        return True

    def _label_index(self, item):
        for i, (_k, name, x, y, *_r) in enumerate(self.labels):
            if name == item["text"] and _same((x, y), item["anchor"]):
                return i
        return None

    def _note_index(self, item):
        para = item.get("para")
        return para[0] if para else None

    def relieve(self, rounds=8, min_gap=CLEAR_TEXT, quiet=True):
        """Space crowded text out without changing one connection.

        Three moves, in order of how little they disturb the drawing:
        stand rotated power ports up, slide a label further along its own
        stub (which is also what staggers a dense flank), and shift a note.
        Every move is checked against the wires, pins and bodies first, so a
        label can never be slid onto a neighbouring net. What it cannot fix
        it reports -- those are the ones that need the parts themselves
        moved."""
        self._relieved = True
        report = {"bundled": self.bundle_ports(quiet=quiet)}
        report["ports"] = self.upright_power_ports()
        report["labels"] = 0
        report["notes"] = 0
        report["spread"] = self.spread(quiet=quiet)
        for _round in range(rounds):
            self._fields = None
            self._items = None
            bad = self._crowded(min_gap)
            if not bad:
                break
            busy = {}
            for a, b, gap in bad:
                for it in (a, b):
                    movable = (it["kind"] in ("label", "note")
                               or (it["kind"] == "field" and it.get("port")))
                    if movable:
                        busy[id(it)] = (it, max(busy.get(id(it), (None, -99))[1], gap))
            progress = 0
            for it, _g in sorted(busy.values(), key=lambda t: -t[1]):
                if it["kind"] == "label":
                    progress += self._try_label(it, min_gap)
                elif it["kind"] == "note":
                    progress += self._try_note(it, min_gap)
                else:
                    moved = self._try_port(it, min_gap)
                    if not moved:
                        moved = self._slide_elbow(it, min_gap)
                    progress += moved
            report["labels"] += progress
            if not progress:
                break
        self._fields = None
        self._items = None
        report["nudged"] = self._nudge_crowded(min_gap)
        left = self._crowded(min_gap)
        if not quiet:
            print(f"  relief: {report['ports']} port(s) stood up, "
                  f"{report['labels']} item(s) moved, {len(left)} pair(s) left")
        report["left"] = left
        return report

    def _try_label(self, item, min_gap):
        idx = self._label_index(item)
        if idx is None:
            return 0
        x, y = item["anchor"]
        found = self._free_end((x, y))
        if found is None:
            return 0
        seg = self.segments[found[0]]
        other = (seg[2], seg[3]) if found[1] == 0 else (seg[0], seg[1])
        # slide along the wire, away from where it comes from -- which is the
        # only direction that keeps the wire straight and the label on its end
        dx = 0.0 if abs(other[0] - x) < 1e-6 else (1.27 if x > other[0] else -1.27)
        dy = 0.0 if abs(other[1] - y) < 1e-6 else (1.27 if y > other[1] else -1.27)
        step = (dx, dy)
        if not dx and not dy:
            angle = item["angle"]
            step = {0: (1.27, 0), 180: (-1.27, 0), 90: (0, -1.27), 270: (0, 1.27)}[angle]
        for k in range(1, 9):
            newpt = (round(x + step[0] * k, 4), round(y + step[1] * k, 4))
            before = self._score(item, min_gap)
            old = self.labels[idx]
            old_segs = list(self.segments)
            if not self._move_label(idx, newpt):
                continue
            self._fields = None
            self._items = None
            probe = self._find_item("label", item["text"], newpt)
            seg_i = self._free_end(newpt)
            ok = probe is not None and self._text_clear(
                probe["box"], own_seg=seg_i[0] if seg_i else None)
            after = self._score(probe, min_gap) if ok else 1e9
            if after < before - 0.01:
                return 1
            self.labels[idx] = old
            self.segments[:] = old_segs
            self._fields = None
            self._items = None
        return 0

    def _paragraph(self, idx):
        """Indices of the notes written as one paragraph with `idx`.

        A block of commentary is usually several note() calls one line apart.
        Moving one of them lands it on its own next line, so they move as a
        unit or not at all."""
        content, x, y, size, j, _b = self.texts[idx]
        step = size * TEXT_LINE + 0.6
        group = [idx]
        for i, (c2, x2, y2, s2, j2, _b2) in enumerate(self.texts):
            if i == idx or abs(x2 - x) > 0.01 or abs(s2 - size) > 0.01 or j2 != j:
                continue
            # walk outward from idx: only lines that chain without a gap
            lo = min(y, min(self.texts[g][2] for g in group))
            hi = max(y, max(self.texts[g][2] for g in group))
            if lo - step - 0.01 <= y2 <= hi + step + 0.01:
                group.append(i)
        return sorted(set(group))

    def _try_port(self, item, min_gap):
        """Slide a power port further out along its own stub.

        A port is nailed to a point on a wire, not to the part, so pushing it
        out along that wire is free -- and it is usually all that is needed to
        get its name out of the pin numbers of whatever is next to it."""
        s = item["sym"]
        pt = (s["x"], s["y"])
        found = self._free_end(pt, ignore_sym=s)
        if found is None:
            return 0
        seg_i, which = found
        seg = self.segments[seg_i]
        other = (seg[2], seg[3]) if which == 0 else (seg[0], seg[1])
        dx = 0 if abs(other[0] - pt[0]) < 1e-6 else (1 if pt[0] > other[0] else -1)
        dy = 0 if abs(other[1] - pt[1]) < 1e-6 else (1 if pt[1] > other[1] else -1)
        if dx == 0 and dy == 0:
            return 0
        before = self._score(item, min_gap)
        saved_seg, saved = self.segments[seg_i], (s["x"], s["y"])
        for k in range(1, 7):
            nx, ny = round(pt[0] + dx * 1.27 * k, 4), round(pt[1] + dy * 1.27 * k, 4)
            cand = (other[0], other[1], nx, ny)
            if not self._safe_wire(cand, ignore=seg_i, moving=(nx, ny)):
                continue
            self.segments[seg_i] = cand
            s["x"], s["y"] = nx, ny
            self._fields = None
            self._items = None
            self._pin_pts = None
            probe = None
            for it in self.item_boxes():
                if it["kind"] == "field" and it.get("sym") is s:
                    probe = it
                    break
            if probe and self._text_clear(probe["box"], own_seg=seg_i, own_sym=s) and \
                    self._score(probe, min_gap) < before - 0.01:
                return 1
            self.segments[seg_i], (s["x"], s["y"]) = saved_seg, saved
            self._fields = None
            self._items = None
            self._pin_pts = None
        return 0

    def _slide_elbow(self, item, min_gap):
        """Move a port that hangs off an elbow further along the stub.

        Standing a port up leaves it at the end of a short drop whose base sits
        on the original stub. Sliding the port along that drop only moves it
        up or down; what a crowded port usually needs is to move *out*, which
        means moving the elbow -- the two wires and the symbol together."""
        s = item["sym"]
        leg = self._free_end((s["x"], s["y"]), ignore_sym=s)
        if leg is None:
            return 0
        leg_i, which = leg
        seg = self.segments[leg_i]
        base = (seg[2], seg[3]) if which == 0 else (seg[0], seg[1])
        stub_i = None
        for i, (x1, y1, x2, y2) in enumerate(self.segments):
            if i == leg_i:
                continue
            if _same((x1, y1), base) or _same((x2, y2), base):
                if stub_i is not None:
                    return 0                     # a junction, not an elbow
                stub_i = i
        if stub_i is None:
            return 0
        sx1, sy1, sx2, sy2 = self.segments[stub_i]
        far = (sx1, sy1) if _same((sx2, sy2), base) else (sx2, sy2)
        dx = 0.0 if abs(far[0] - base[0]) < 1e-6 else (1.27 if base[0] > far[0] else -1.27)
        dy = 0.0 if abs(far[1] - base[1]) < 1e-6 else (1.27 if base[1] > far[1] else -1.27)
        if (dx and dy) or (not dx and not dy):
            return 0
        if (round(base[0], 3), round(base[1], 3)) in self._pin_points(ignore_sym=s):
            return 0
        before = self._score(item, min_gap)
        saved = (self.segments[leg_i], self.segments[stub_i], s["x"], s["y"])
        for k in range(1, 7):
            nb = (round(base[0] + dx * k, 4), round(base[1] + dy * k, 4))
            np_ = (round(s["x"] + dx * k, 4), round(s["y"] + dy * k, 4))
            new_stub = (far[0], far[1], nb[0], nb[1])
            new_leg = (nb[0], nb[1], np_[0], np_[1])
            both = {stub_i, leg_i}
            if not self._safe_wire(new_stub, ignore=both, moving=nb) or \
               not self._safe_wire(new_leg, ignore=both, moving=np_):
                continue
            self.segments[stub_i] = new_stub
            self.segments[leg_i] = new_leg
            s["x"], s["y"] = np_
            self._fields = None
            self._items = None
            self._pin_pts = None
            probe = next((it for it in self.item_boxes()
                          if it["kind"] == "field" and it.get("sym") is s), None)
            if probe and self._text_clear(probe["box"], own_seg=leg_i, own_sym=s) and \
                    self._score(probe, min_gap) < before - 0.01:
                return 1
            self.segments[leg_i], self.segments[stub_i] = saved[0], saved[1]
            s["x"], s["y"] = saved[2], saved[3]
            self._fields = None
            self._items = None
            self._pin_pts = None
        return 0

    def _group_of_item(self, item, groups, index):
        """Which movable group an item belongs to, or None."""
        sym = item.get("sym")
        if sym is not None:
            return index["sym"].get(id(sym))
        if item.get("para"):
            return index["txt"].get(item["para"][0])
        if item["kind"] == "label":
            fe = self._free_end(item["anchor"])
            if fe is not None:
                return index["seg"].get(fe[0])
        return None

    def _nudge_crowded(self, min_gap=CLEAR_ROW, rounds=6, step=1.27):
        """Move a whole group one step when its text still crowds another's.

        Sliding a label or a port along its own wire fixes most crowding, but
        not a label whose neighbour simply sits too close. Groups are joined
        to each other only by labels, so moving one is free -- and one grid
        step is usually all it takes."""
        moved = 0
        for _ in range(rounds):
            groups = self.groups()
            index = {"sym": {}, "txt": {}, "seg": {}}
            for k, g in enumerate(groups):
                for n in g["sym"]:
                    index["sym"][id(self.symbols[n])] = k
                for n in g["txt"]:
                    index["txt"][n] = k
                for n in g["seg"]:
                    index["seg"][n] = k
            anchors = {(round(x, 3), round(y, 3)) for x, y in self._sheet_pin_points()}
            frozen = set()
            for k, g in enumerate(groups):
                for n in g["seg"]:
                    x1, y1, x2, y2 = self.segments[n]
                    if (round(x1, 3), round(y1, 3)) in anchors or \
                       (round(x2, 3), round(y2, 3)) in anchors:
                        frozen.add(k)
            bad = self._crowded(min_gap)
            if not bad:
                break
            did = 0
            for a, b, _gap in bad:
                ga = self._group_of_item(a, groups, index)
                gb = self._group_of_item(b, groups, index)
                if ga is None or gb is None or ga == gb:
                    continue
                pick, other = (gb, a) if gb not in frozen else (ga, b)
                if pick in frozen:
                    continue
                box_a, box_b = a["box"], b["box"]
                ox = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
                oy = min(box_a[3], box_b[3]) - max(box_a[1], box_b[1])
                if ox <= oy:
                    sign = 1 if (box_b[0] + box_b[2]) >= (box_a[0] + box_a[2]) else -1
                    delta = (step * sign * (1 if pick == gb else -1), 0)
                else:
                    sign = 1 if (box_b[1] + box_b[3]) >= (box_a[1] + box_a[3]) else -1
                    delta = (0, step * sign * (1 if pick == gb else -1))
                if not self._move_is_safe(groups[pick], *delta):
                    continue
                before = len(self._crowded(min_gap))
                self.move_group(groups[pick], *delta)
                if len(self._crowded(min_gap)) < before:
                    did += 1
                    moved += 1
                    break                      # geometry changed; re-index
                self.move_group(groups[pick], -delta[0], -delta[1])
            if not did:
                break
        return moved

    def _try_note(self, item, min_gap):
        idx = self._note_index(item)
        if idx is None:
            return 0
        group = item.get("para") or [idx]
        saved = [self.texts[i] for i in group]
        before = self._score(item, min_gap)
        offsets = [(0, d) for d in (2.54, -2.54, 5.08, -5.08, 7.62, -7.62,
                                    10.16, -10.16, 12.7, -12.7, 17.78, -17.78)]
        offsets += [(d, 0) for d in (5.08, -5.08, 10.16, -10.16, 15.24, -15.24)]
        for dx, dy in offsets:
            for i, (c, x, y, size, j, bold) in zip(group, saved):
                self.texts[i] = (c, round(x + dx, 4), round(y + dy, 4), size, j, bold)
            self._fields = None
            self._items = None
            items = self.item_boxes()
            probes = [it for it in items if it["kind"] == "note"
                      and any(_same(it.get("anchor", (0, 0)),
                                    (self.texts[i][1], self.texts[i][2]))
                              for i in group)]
            if not probes or not all(self._text_clear(pb["box"]) for pb in probes):
                continue
            after = sum(self._score(pb, min_gap, items) for pb in probes)
            base = sum(self._score(it, min_gap) for it in probes) if False else before
            if after < base - 0.01:
                return 1
        for i, old in zip(group, saved):
            self.texts[i] = old
        self._fields = None
        self._items = None
        return 0

    def _find_item(self, kind, text, anchor):
        for it in self.item_boxes():
            if it["kind"] == kind and it["text"] == text and \
               _same(it.get("anchor", (0, 0)), anchor):
                return it
        return None

    def _score(self, item, min_gap, items=None):
        """How crowded one item is. Severity, not a count: an overlap costs
        far more than a near miss, so a move that trades three near misses for
        one overlap is correctly rejected."""
        if item is None:
            return 1e9
        items = items if items is not None else self.item_boxes()
        if self._items is items:
            if self._grid_for is not items:
                self._grid, self._grid_for = self._bucket(items), items
            near = self._near(self._grid, item["box"])
            candidates = (items[j] for j in near)
        else:
            candidates = items
        total = 0.0
        # text drawn across a wire is as bad as text drawn across text, and
        # relief can only fix what it scores
        anchor = item.get("anchor")
        if anchor is not None:
            for x1, y1, x2, y2 in self.segments:
                if _same((x1, y1), anchor) or _same((x2, y2), anchor):
                    continue
                b = item["box"]
                if abs(y1 - y2) < 1e-6:
                    if b[1] < y1 < b[3] and min(x1, x2) < b[2] and max(x1, x2) > b[0]:
                        total += 6.0
                elif abs(x1 - x2) < 1e-6:
                    if b[0] < x1 < b[2] and min(y1, y2) < b[3] and max(y1, y2) > b[1]:
                        total += 6.0
        for other in candidates:
            if other["owner"] == item["owner"]:
                continue
            f = clearance_fault(item["box"], other["box"], min_gap, CLEAR_STACK)
            if f is None:
                continue
            gap, how = f
            total += (10.0 + gap * 10.0) if how == "overlap" else (1.0 - gap / min_gap)
        return total

    # ---- geometry --------------------------------------------------------
    def _placed_pins(self):
        out = []
        for s in self.symbols:
            sym = self.lib.get(s["lib_id"])
            for p in sym.pins(s["unit"]):
                out.append((s, p, pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])))
        return out

    def body_bbox(self, s):
        sym = self.lib.get(s["lib_id"])
        x0, y0, x1, y1 = sym.bbox(s["unit"])
        pts = [_transform(px, py, s["rot"], s["mirror"]) for px in (x0, x1) for py in (y0, y1)]
        xs = [s["x"] + a for a, _ in pts]
        ys = [s["y"] + b for _, b in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def _sheet_pin_points(self):
        pts = []
        for cs in self.child_sheets:
            for (_n, py, side, _sh) in cs["pins"]:
                px = cs["x"] if side == "left" else cs["x"] + cs["w"]
                pts.append((round(px, 4), round(py, 4)))
        return pts

    def content_bbox(self):
        xs, ys = [], []

        def add(x, y):
            xs.append(x)
            ys.append(y)
        for s in self.symbols:
            l, t, r, b = self.body_bbox(s)
            add(l, t)
            add(r, b)
            if not s["power"]:
                # reference/value text sits just outside the body
                add(l, t - 4.0)
                add(r + 12.0 if (b - t) >= (r - l) else r, b + 4.0)
        for s, p, (x, y) in self._placed_pins():
            add(x, y)
        for x1, y1, x2, y2 in self.segments:
            add(x1, y1)
            add(x2, y2)
        for _k, name, x, y, angle, _j, _sh in self.labels:
            w = len(name) * 1.1 + 3.0
            add(x, y)
            if angle == 180:
                add(x - w, y)
            elif angle == 0:
                add(x + w, y)
            elif angle == 90:
                add(x, y - w)
            else:
                add(x, y + w)
        for content, x, y, size, _j, _b in self.texts:
            longest = max(len(line) for line in content.split("\n"))
            add(x, y - size)
            add(x + longest * size * 0.85, y + size * content.count("\n") * 1.6)
        for x1, y1, x2, y2 in self.rects:
            add(x1, y1)
            add(x2, y2)
        for x, y in self.no_connects:
            add(x, y)
        for x1, y1, x2, y2 in self.buses:
            add(x1, y1)
            add(x2, y2)
        for x, y, sx, sy in self.bus_entries:
            add(x + sx, y + sy)
        for cs in self.child_sheets:
            add(cs["x"], cs["y"] - 3)
            add(cs["x"] + cs["w"], cs["y"] + cs["h"] + 3)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def translate(self, dx, dy):
        """Move everything; connectivity is unaffected."""
        self._fields = None
        self._items = None
        self._pin_pts = None
        self._paras = None
        dx, dy = round(dx, 4), round(dy, 4)

        def mv(x, y):
            return round(x + dx, 4), round(y + dy, 4)
        for s in self.symbols:
            s["x"], s["y"] = mv(s["x"], s["y"])
        self.segments = [(*mv(a, b), *mv(c, d)) for a, b, c, d in self.segments]
        self.labels = [(k, n, *mv(x, y), a, j, sh) for k, n, x, y, a, j, sh in self.labels]
        self.no_connects = [mv(x, y) for x, y in self.no_connects]
        self.texts = [(c, *mv(x, y), s, j, b) for c, x, y, s, j, b in self.texts]
        self.rects = [(*mv(a, b), *mv(c, d)) for a, b, c, d in self.rects]
        self.buses = [(*mv(a, b), *mv(c, d)) for a, b, c, d in self.buses]
        self.bus_entries = [(*mv(x, y), sx, sy) for x, y, sx, sy in self.bus_entries]
        for cs in self.child_sheets:
            cs["x"], cs["y"] = mv(cs["x"], cs["y"])
            cs["pins"] = [(n, round(py + dy, 4), side, sh) for n, py, side, sh in cs["pins"]]

    @staticmethod
    def usable_area(paper):
        pw, ph = PAPER_SIZES[paper]
        return (pw - 2 * MARGIN, ph - MARGIN - TITLE_BAND)

    def suggest_paper(self):
        """Smallest paper in AUTO_PAPER_ORDER that holds the drawing."""
        x0, y0, x1, y1 = self.content_bbox()
        w, h = x1 - x0, y1 - y0
        for p in AUTO_PAPER_ORDER:
            uw, uh = self.usable_area(p)
            if w <= uw and h <= uh:
                return p
        return AUTO_PAPER_ORDER[-1]

    def title_block_overlap(self):
        """True if any drawn point lands in the bottom-right title-block corner.

        The reserved bottom band spans the full width for centring, but the
        title block itself only fills the right-hand ~115 mm, so a tall drawing
        can legitimately dip into the band on the left. Only the corner counts.
        """
        pw, ph = PAPER_SIZES[self.paper]
        tx, ty = pw - 10.0 - 115.0, ph - 10.0 - 38.0
        x0, y0, x1, y1 = self.content_bbox()
        if x1 < tx or y1 < ty:
            return False
        pts = []
        for s in self.symbols:
            l, t, r, b = self.body_bbox(s)
            pts += [(l, t), (r, b), (l, b), (r, t)]
        pts += [(a, b) for a, b, _c, _d in self.segments]
        pts += [(c, d) for _a, _b, c, d in self.segments]
        pts += [p for a, b, c, d in self.buses for p in ((a, b), (c, d))]
        pts += [(x, y) for _k, _n, x, y, *_ in self.labels]
        for content, x, y, size, _j, _b in self.texts:
            pts += [(x, y), (x + max(len(ln) for ln in content.split("\n")) * size * 0.85, y)]
        for a, b, c, d in self.rects:
            pts += [(c, d), (a, d), (c, b)]
        return any(px > tx and py > ty for px, py in pts)

    def utilization(self):
        x0, y0, x1, y1 = self.content_bbox()
        uw, uh = self.usable_area(self.paper)
        return ((x1 - x0) / uw, (y1 - y0) / uh)

    def center_on_page(self):
        """Centre the drawing in the frame, clear of the title block, on-grid."""
        pw, ph = PAPER_SIZES[self.paper]
        left, right = MARGIN, pw - MARGIN
        top, bottom = MARGIN, ph - TITLE_BAND
        x0, y0, x1, y1 = self.content_bbox()
        dx = max((left + right) / 2 - (x0 + x1) / 2, left - x0)
        dy = max((top + bottom) / 2 - (y0 + y1) / 2, top - y0)
        self.translate(snap(dx), snap(dy))

    # ---- checks ------------------------------------------------------------
    def check(self):
        """Return (errors, warnings, info) lists of strings.

        errors   off-grid pins/wire ends/labels (KiCad won't join them),
                 dangling wire ends
        warnings a pin landing mid-wire (KiCad connects it -- usually an
                 accident), a wire running through a part's body
        info     number of wire crossings (aim low; no dot = not connected)
        """
        errors, warnings, info = [], [], []
        for s, p, (x, y) in self._placed_pins():
            if not (on_grid(x) and on_grid(y)):
                errors.append(f"off grid: {s['ref'] or s['value']} pin {p['num']} at ({x}, {y})")
        for x1, y1, x2, y2 in self.segments:
            for x, y in ((x1, y1), (x2, y2)):
                if not (on_grid(x) and on_grid(y)):
                    errors.append(f"off grid: wire end at ({x}, {y})")
        for _k, name, x, y, *_ in self.labels:
            if not (on_grid(x) and on_grid(y)):
                errors.append(f"off grid: label {name} at ({x}, {y})")
        for x1, y1, x2, y2 in self.buses:
            for x, y in ((x1, y1), (x2, y2)):
                if not (on_grid(x) and on_grid(y)):
                    errors.append(f"off grid: bus end at ({x}, {y})")
        for x, y, sx, sy in self.bus_entries:
            if not (on_grid(x) and on_grid(y)):
                errors.append(f"off grid: bus entry at ({x}, {y})")
            if not any(_on_segment((x + sx, y + sy), b) for b in self.buses):
                errors.append(f"bus entry at ({x}, {y}) does not reach a bus "
                              f"(its end ({fmt(x + sx)}, {fmt(y + sy)}) is on no bus segment)")
            if not any(_on_segment((x, y), w) for w in self.segments):
                errors.append(f"bus entry at ({x}, {y}) has no wire on its free end")

        pin_pts = [(round(x, 4), round(y, 4)) for _s, _p, (x, y) in self._placed_pins()]
        anchors = set(pin_pts)
        anchors |= {(round(x, 4), round(y, 4)) for _k, _n, x, y, *_ in self.labels}
        anchors |= set(self._sheet_pin_points())
        anchors |= {(round(x, 4), round(y, 4)) for x, y, _sx, _sy in self.bus_entries}

        def interior(px, py, seg):
            x1, y1, x2, y2 = seg
            if abs(y1 - y2) < 1e-6 and abs(py - y1) < 1e-6:
                return min(x1, x2) + 1e-6 < px < max(x1, x2) - 1e-6
            if abs(x1 - x2) < 1e-6 and abs(px - x1) < 1e-6:
                return min(y1, y2) + 1e-6 < py < max(y1, y2) - 1e-6
            return False

        def touches(px, py, seg):
            return _same((px, py), seg[:2]) or _same((px, py), seg[2:]) or interior(px, py, seg)

        for i, seg in enumerate(self.segments):
            for pt in (seg[:2], seg[2:]):
                if (round(pt[0], 4), round(pt[1], 4)) in anchors:
                    continue
                if any(touches(pt[0], pt[1], o) for j, o in enumerate(self.segments) if j != i):
                    continue
                errors.append(f"dangling wire end at ({pt[0]}, {pt[1]})")
            for b in self.buses:
                for p_ in (seg[:2], seg[2:]):
                    if _on_segment(p_, b):
                        errors.append(f"wire end on a bus at ({p_[0]}, {p_[1]}) -- "
                                      f"join wires to a bus through bus_entry()")

        for s, p, (x, y) in self._placed_pins():
            for seg in self.segments:
                if interior(x, y, seg):
                    warnings.append(f"{s['ref'] or s['value']} pin {p['num']} lands mid-wire "
                                    f"at ({x}, {y}) -- KiCad will connect it")
        # A wire at a pin has to leave along that pin, not arrive across the
        # part. The old rule -- "ignore any wire touching this part's own
        # pins" -- was there for symbols whose artwork overhangs the pin tip,
        # and it also excused a wire that came in from the far side and ran
        # straight through the body. Checking the direction catches that
        # without reintroducing the false positives.
        for s, p, (x, y) in self._placed_pins():
            if s["power"]:
                continue
            sym = self.lib.get(s["lib_id"])
            ox, oy = pin_outward(p, s["rot"], s["mirror"])
            for x1, y1, x2, y2 in self.segments:
                other = None
                if _same((x1, y1), (x, y)):
                    other = (x2, y2)
                elif _same((x2, y2), (x, y)):
                    other = (x1, y1)
                if other is None:
                    continue
                vx, vy = other[0] - x, other[1] - y
                if (vx * ox + vy * oy) < -1e-6:
                    warnings.append(f"wire at {s['ref'] or s['value']} pin {p['num']} leaves "
                                    f"back across the part ({x}, {y}) -> {other}")
        for s in self.symbols:
            if s["power"]:
                continue
            l, t, r, b = self.body_bbox(s)
            l, t, r, b = l + 0.3, t + 0.3, r - 0.3, b - 0.3
            sym = self.lib.get(s["lib_id"])
            own = [pin_position(sym, p, s["x"], s["y"], s["rot"], s["mirror"])
                   for p in sym.pins(s["unit"])]
            for x1, y1, x2, y2 in self.segments:
                # A wire ending on this part's own pin is normal even when the
                # body graphic (e.g. an LED's emission arrows) overhangs the tip.
                if any(_same(o, (x1, y1)) or _same(o, (x2, y2)) for o in own):
                    continue
                if abs(y1 - y2) < 1e-6 and t < y1 < b and min(x1, x2) < r and max(x1, x2) > l:
                    warnings.append(f"wire at y={y1} runs through {s['ref']}'s body")
                elif abs(x1 - x2) < 1e-6 and l < x1 < r and min(y1, y2) < b and max(y1, y2) > t:
                    warnings.append(f"wire at x={x1} runs through {s['ref']}'s body")

        horiz = [s for s in self.segments if abs(s[1] - s[3]) < 1e-6]
        vert = [s for s in self.segments if abs(s[0] - s[2]) < 1e-6 and abs(s[1] - s[3]) > 1e-6]
        crossings = 0
        for h in horiz:
            for v in vert:
                if min(h[0], h[2]) + 1e-6 < v[0] < max(h[0], h[2]) - 1e-6 and \
                   min(v[1], v[3]) + 1e-6 < h[1] < max(v[1], v[3]) - 1e-6:
                    crossings += 1
        info.append(f"{crossings} wire crossing(s)")

        # Drafting faults: blocks that run into each other, parts drawn on top
        # of each other, text crowded against its neighbours. ERC sees none of
        # these, and all of them are what makes a sheet look amateur.
        errors += self.check_blocks()
        # the model's view of it; check_text() repeats this on KiCad's own render
        warnings += outline_faults(self)
        for m in self.check_spacing():
            (warnings if m.startswith("note ") or " note " in m else errors).append(m)
        return (sorted(set(errors)), sorted(set(warnings)), info)

    def _junctions(self):
        pts = [(round(x, 4), round(y, 4)) for _s, _p, (x, y) in self._placed_pins()]
        pin_set = set(pts)
        cands = set(pin_set)
        for x1, y1, x2, y2 in self.segments:
            cands.add((x1, y1))
            cands.add((x2, y2))
        out = []
        for (px, py) in cands:
            n = 0
            for x1, y1, x2, y2 in self.segments:
                if _same((px, py), (x1, y1)) or _same((px, py), (x2, y2)):
                    n += 1
                elif (abs(y1 - y2) < 1e-6 and abs(py - y1) < 1e-6 and
                      min(x1, x2) < px < max(x1, x2)) or \
                     (abs(x1 - x2) < 1e-6 and abs(px - x1) < 1e-6 and
                      min(y1, y2) < py < max(y1, y2)):
                    n += 2
            if (px, py) in pin_set:
                n += 1
            if n >= 3:
                out.append((px, py))
        return sorted(out)


# ===========================================================================
# Emission
# ===========================================================================

def _prop(name, value, x, y, hide, size=1.27, justify=None, bold=False, angle=0):
    j = f"\n\t\t\t\t(justify {justify})" if justify else ""
    b = "\n\t\t\t\t\t(bold yes)" if bold else ""
    h = "\n\t\t\t(hide yes)" if hide else ""
    return (f'\t\t(property "{q(name)}" "{q(value)}"\n'
            f"\t\t\t(at {fmt(x)} {fmt(y)} {angle}){h}\n"
            "\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n"
            f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size {fmt(size)} {fmt(size)}){b}\n\t\t\t\t){j}\n\t\t\t)\n"
            f"\t\t)\n")


def _kicad_order(text):
    """Reorder a sheet's top-level items the way KiCad 10 saves them: header
    and lib_symbols first, then items by type (_ITEM_ORDER) and UUID, then
    sheet_instances and embedded_fonts."""
    spans = _child_spans(text)
    head, items, tail = [], [], []
    for a, b in spans:
        chunk = text[a:b]
        kind = re.match(r"\((\w+)", chunk).group(1)
        if kind in _ITEM_ORDER:
            um = re.search(r'\n\t\t\(uuid "([^"]+)"\)', chunk) or re.search(r'\(uuid "([^"]+)"\)', chunk)
            items.append((_ITEM_RANK[kind], um.group(1) if um else "", chunk))
        elif kind in ("sheet_instances", "embedded_fonts"):
            tail.append(chunk)
        else:
            head.append(chunk)
    items.sort(key=lambda t: (t[0], t[1]))
    body = head + [c for _r, _u, c in items] + tail
    return "(kicad_sch\n" + "".join("\t" + c + "\n" for c in body) + ")\n"


def _tbox(x, y, text, size=1.27, justify=None):
    """Approximate box of one line of field text, vertically centred on y."""
    w = len(str(text)) * size * 0.95 + 0.2
    x0 = x if justify == "left" else (x - w if justify == "right" else x - w / 2)
    return (x0, y - size * 0.6, x0 + w, y + size * 0.6)


def _boxes_hit(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


class Obstacles:
    """Boxes to keep text off, bucketed so a lookup is local.

    The field placer compares each candidate spot against everything already
    on the sheet. On a 900-item sheet that is a million comparisons per pass,
    and the relief loop runs the placer hundreds of times -- so the list is
    indexed by a coarse grid and only the neighbourhood is ever scanned."""

    CELL = 16.0

    def __init__(self, boxes=()):
        self.grid = {}
        for b in boxes:
            self.add(b)

    def add(self, box):
        for cx in range(int(box[0] // self.CELL), int(box[2] // self.CELL) + 1):
            for cy in range(int(box[1] // self.CELL), int(box[3] // self.CELL) + 1):
                self.grid.setdefault((cx, cy), []).append(box)

    def extend(self, boxes):
        for b in boxes:
            self.add(b)

    def near(self, box, pad=2.0):
        out = []
        for cx in range(int((box[0] - pad) // self.CELL), int((box[2] + pad) // self.CELL) + 1):
            for cy in range(int((box[1] - pad) // self.CELL), int((box[3] + pad) // self.CELL) + 1):
                out += self.grid.get((cx, cy), ())
        return out


def _field_obstacles(sheet):
    """Everything reference/value text should stay off: wires, part bodies,
    pin lines, power-port graphics, labels, notes and no-connect flags."""
    obs = []
    for x1, y1, x2, y2 in sheet.segments:
        obs.append((min(x1, x2) - 0.25, min(y1, y2) - 0.25,
                    max(x1, x2) + 0.25, max(y1, y2) + 0.25))
    for s in sheet.symbols:
        sym = sheet.lib.get(s["lib_id"])
        x, y = s["x"], s["y"]
        if s["power"]:
            obs.append((x - 1.6, y, x + 1.6, y + 3.0) if s["value"] == "GND"
                       else (x - 1.6, y - 3.2, x + 1.6, y))
            # the port's own name is text too: leaving it out of the obstacle
            # list is how a part's value ends up printed against "+3V3"
            if not s.get("hide_value"):
                off = sym.field_pos("Value") or (0.0, -3.81)
                fdx, fdy = _transform(off[0], off[1], s["rot"], s["mirror"])
                tw, th = text_extent(s["value"], 1.27)
                obs.append((x + fdx - tw / 2, y + fdy - th / 2,
                            x + fdx + tw / 2, y + fdy + th / 2))
            continue
        obs.append(sheet.body_bbox(s))
        obs.extend(sheet.pin_text_boxes(s))
        for pin in sym.pins(s["unit"]):
            tx, ty = pin_position(sym, pin, x, y, s["rot"], s["mirror"])
            ox, oy = pin_outward(pin, s["rot"], s["mirror"])
            ln = pin.get("length", 2.54)
            bx, by = tx - ox * ln, ty - oy * ln
            obs.append((min(tx, bx) - 0.3, min(ty, by) - 0.3,
                        max(tx, bx) + 0.3, max(ty, by) + 0.3))
    for _k, name, x, y, angle, _j, shape in sheet.labels:
        w = len(name) * 1.27 * 0.95 + (2.5 if shape else 0.5)
        obs.append({0: (x, y - 1.6, x + w, y + 0.3),
                    180: (x - w, y - 1.6, x, y + 0.3),
                    90: (x - 1.6, y - w, x + 0.3, y)}.get(angle, (x - 1.6, y, x + 0.3, y + w)))
    for content, x, y, size, _j, _b in sheet.texts:
        lines = content.split("\n")
        w = max(len(ln) for ln in lines) * size * 0.9
        obs.append((x, y - size * 0.8, x + w, y + size * (1.7 * (len(lines) - 1) + 0.4)))
    for x, y in sheet.no_connects:
        obs.append((x - 0.8, y - 0.8, x + 0.8, y + 0.8))
    for x1, y1, x2, y2 in sheet.buses:
        obs.append((min(x1, x2) - 0.4, min(y1, y2) - 0.4,
                    max(x1, x2) + 0.4, max(y1, y2) + 0.4))
    for x, y, sx, sy in sheet.bus_entries:
        obs.append((min(x, x + sx), min(y, y + sy), max(x, x + sx), max(y, y + sy)))
    return Obstacles(obs)


def _choose_field_spots(s, body, vertical_pair, taken, ref, value):
    """Place reference and value clear of everything in `taken`.

    Tries the conventional spot first -- stacked beside a vertical two-pin
    part, above/below anything else -- then the alternatives, falling back to
    the least crowded. The chosen boxes join `taken` so later parts avoid
    them too."""
    l, t, r, b = body
    cx, cy = (l + r) / 2, (t + b) / 2
    y0 = s["y"] if vertical_pair else cy
    right = [(r + 1.78, y0 - 1.27, "left"), (r + 1.78, y0 + 1.27, "left")]
    left = [(l - 1.78, y0 - 1.27, "right"), (l - 1.78, y0 + 1.27, "right")]
    split = [(cx, t - 2.03, None), (cx, b + 2.03, None)]
    above = [(cx, t - 4.57, None), (cx, t - 2.03, None)]
    below = [(cx, b + 2.03, None), (cx, b + 4.57, None)]
    # corners of the top edge: where a big IC's text goes when a supply pin
    # leaves the middle of that edge and its stub runs through "above"
    # -- text runs outward from the corner, so even a long value on a small
    # body stays clear of a stub leaving the middle of the edge
    above_l = [(l, t - 4.57, "right"), (l, t - 2.03, "right")]
    above_r = [(r, t - 4.57, "left"), (r, t - 2.03, "left")]
    order = [right, left, above, below] if vertical_pair else \
        [split, above, below, right, left, above_l, above_r]
    best = None
    for cand in order:
        boxes = [_tbox(cand[0][0], cand[0][1], ref, justify=cand[0][2]),
                 _tbox(cand[1][0], cand[1][1], value, justify=cand[1][2])]
        # Count how badly each spot crowds, not merely whether it touches:
        # "no overlap" is not the same as "readable", and scoring by hits
        # alone picks a spot 0.2 mm from a label over one 2 mm away.
        score = 0.0
        for bx in boxes:
            for o in taken.near(bx):
                f = clearance_fault(bx, o)
                if f is None:
                    continue
                score += (10.0 + f[0]) if f[1] == "overlap" else (1.0 - f[0] / CLEAR_ROW)
        if best is None or score < best[0]:
            best = (score, cand, boxes)
        if score <= 0.01:
            break
    _score, cand, boxes = best
    taken.extend(boxes)
    return cand[0], cand[1]


def _file_just(j, s):
    """Justification to write so it *displays* as j.

    Two things flip left against right: a left/right mirror, and a 180 degree
    rotation now that its fields are written at angle 0 rather than 180. Both
    together cancel out.
    """
    if j and (s["mirror"] == "y") != (s["rot"] == 180):
        return {"left": "right", "right": "left"}[j]
    return j


def _emit(sheet, project, title_block, root_uuid=None, pwr_counter=None):
    p = []
    lib_ids = sorted({s["lib_id"] for s in sheet.symbols})
    p.append(f'(kicad_sch\n\t(version {SCH_VERSION})\n\t(generator "eeschema")\n'
             f'\t(generator_version "{GENERATOR_VERSION}")\n')
    p.append(f'\t(uuid "{sheet.uuid}")\n\t(paper "{sheet.paper}")\n')
    tb = [f'\t\t(title "{q(sheet.title)}")',
          f'\t\t(date "{q(title_block["date"])}")',
          f'\t\t(rev "{q(title_block["rev"])}")']
    if title_block["company"]:
        tb.append(f'\t\t(company "{q(title_block["company"])}")')
    for i, c in enumerate(sheet.comments[:9], 1):
        if c:
            tb.append(f'\t\t(comment {i} "{q(c)}")')
    p.append("\t(title_block\n" + "\n".join(tb) + "\n\t)\n")

    p.append("\t(lib_symbols\n")
    for lid in lib_ids:
        raw = sheet.lib.get(lid).raw
        p.append("\n".join("\t\t" + ln if ln.strip() else ln for ln in raw.split("\n")) + "\n")
    p.append("\t)\n")

    fn = sheet.filename
    for x1, y1, x2, y2 in sheet.rects:
        p.append("\t(rectangle\n"
                 f"\t\t(start {fmt(x1)} {fmt(y1)})\n\t\t(end {fmt(x2)} {fmt(y2)})\n"
                 "\t\t(stroke\n\t\t\t(width 0.254)\n\t\t\t(type dash)\n"
                 "\t\t\t(color 132 132 132 1)\n\t\t)\n"
                 "\t\t(fill\n\t\t\t(type none)\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, "rect", x1, y1, x2, y2)}")\n\t)\n')
    for x1, y1, x2, y2 in sheet.segments:
        p.append("\t(wire\n"
                 f"\t\t(pts\n\t\t\t(xy {fmt(x1)} {fmt(y1)}) (xy {fmt(x2)} {fmt(y2)})\n\t\t)\n"
                 "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, "wire", x1, y1, x2, y2)}")\n\t)\n')
    for x1, y1, x2, y2 in sheet.buses:
        p.append("\t(bus\n"
                 f"\t\t(pts\n\t\t\t(xy {fmt(x1)} {fmt(y1)}) (xy {fmt(x2)} {fmt(y2)})\n\t\t)\n"
                 "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, "bus", x1, y1, x2, y2)}")\n\t)\n')
    for x, y, sx, sy in sheet.bus_entries:
        p.append("\t(bus_entry\n"
                 f"\t\t(at {fmt(x)} {fmt(y)})\n\t\t(size {fmt(sx)} {fmt(sy)})\n"
                 "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, "busentry", x, y, sx, sy)}")\n\t)\n')
    for (jx, jy) in sheet._junctions():
        p.append("\t(junction\n"
                 f"\t\t(at {fmt(jx)} {fmt(jy)})\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n"
                 f'\t\t(uuid "{sid(fn, "junction", jx, jy)}")\n\t)\n')
    for (nx, ny) in sheet.no_connects:
        p.append(f"\t(no_connect\n\t\t(at {fmt(nx)} {fmt(ny)})\n"
                 f'\t\t(uuid "{sid(fn, "nc", nx, ny)}")\n\t)\n')
    for kind, name, x, y, angle, justify, shape in sheet.labels:
        sh = f"\t\t(shape {shape})\n" if shape else ""
        p.append(f'\t({kind} "{q(name)}"\n{sh}'
                 f"\t\t(at {fmt(x)} {fmt(y)} {angle})\n"
                 "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
                 f"\t\t\t(justify {justify})\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, kind, name, x, y)}")\n\t)\n')
    for content, x, y, size, justify, bold in sheet.texts:
        b = "\n\t\t\t\t(bold yes)" if bold else ""
        p.append(f'\t(text "{q(content)}"\n\t\t(exclude_from_sim no)\n'
                 f"\t\t(at {fmt(x)} {fmt(y)} 0)\n"
                 f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {fmt(size)} {fmt(size)}){b}\n\t\t\t)\n"
                 f"\t\t\t(justify {justify})\n\t\t)\n"
                 f'\t\t(uuid "{sid(fn, "text", content, x, y)}")\n\t)\n')

    for cs in sheet.child_sheets:
        sx, sy, sw, sh_ = cs["x"], cs["y"], cs["w"], cs["h"]
        p.append("\t(sheet\n"
                 f"\t\t(at {fmt(sx)} {fmt(sy)})\n\t\t(size {fmt(sw)} {fmt(sh_)})\n"
                 "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
                 "\t\t(stroke\n\t\t\t(width 0.1524)\n\t\t\t(type solid)\n\t\t)\n"
                 "\t\t(fill\n\t\t\t(color 0 0 0 0)\n\t\t)\n"
                 f'\t\t(uuid "{cs["uuid"]}")\n')
        p.append(_prop("Sheetname", cs["name"], sx, sy - 1.0, False, 1.524, "left bottom", True))
        p.append(_prop("Sheetfile", cs["file"], sx, sy + sh_ + 1.2, False, 1.27, "left top"))
        for (pname, py, side, pshape) in cs["pins"]:
            px = sx if side == "left" else sx + sw
            ang = 180 if side == "left" else 0
            just = "left" if side == "left" else "right"
            p.append(f'\t\t(pin "{q(pname)}" {pshape}\n'
                     f"\t\t\t(at {fmt(px)} {fmt(py)} {ang})\n"
                     f'\t\t\t(uuid "{sid(cs["file"], "sheetpin", pname)}")\n'
                     "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n"
                     f"\t\t\t\t(justify {just})\n\t\t\t)\n\t\t)\n")
        p.append("\t\t(instances\n"
                 f'\t\t\t(project "{q(project)}"\n'
                 f'\t\t\t\t(path "/{sheet.uuid}"\n\t\t\t\t\t(page "{cs["page"]}")\n\t\t\t\t)\n'
                 "\t\t\t)\n\t\t)\n\t)\n")

    spots = sheet.layout_fields()
    used_uuids = set()
    for s in sheet.symbols:
        sym = sheet.lib.get(s["lib_id"])
        x, y, rot = s["x"], s["y"], s["rot"]
        if s["power"]:
            ref = f"#PWR{next(pwr_counter):03d}"
            # rotation is part of the key: two ports stacked on one point at
            # different angles used to share a UUID, and KiCad renumbered one
            uid_ = sid(fn, "pwr", s["lib_id"], x, y, rot)
            while uid_ in used_uuids:
                uid_ = sid(uid_, "dup")
            used_uuids.add(uid_)
        else:
            ref = s["ref"]
            uid_ = sid(fn, "sym", ref, s["unit"])
        mir = f"\t\t(mirror {s['mirror']})\n" if s["mirror"] else ""
        p.append("\t(symbol\n"
                 f'\t\t(lib_id "{q(s["lib_id"])}")\n'
                 f"\t\t(at {fmt(x)} {fmt(y)} {rot})\n{mir}"
                 f'\t\t(unit {s["unit"]})\n\t\t(body_style 1)\n\t\t(exclude_from_sim no)\n'
                 f'\t\t(in_bom {"yes" if s["in_bom"] else "no"})\n'
                 f'\t\t(on_board {"yes" if s["on_board"] else "no"})\n'
                 "\t\t(in_pos_files yes)\n"
                 f'\t\t(dnp {"yes" if s["dnp"] else "no"})\n'
                 f'\t\t(uuid "{uid_}")\n')
        if s["power"]:
            (_rx, _ry, _rj, _ra, _rh), (vx, vy, _vj, _va, hide_v) = spots[id(s)]
            p.append(_prop("Reference", ref, x, y - 2.54, True))
            p.append(_prop("Value", s["value"], vx, vy, hide_v))
        else:
            (rx_, ry_, rj), (vx_, vy_, vj) = ((a, b, c) for a, b, c, _d, _e in spots[id(s)])
            fa = spots[id(s)][0][3]
            p.append(_prop("Reference", ref, rx_, ry_, False, justify=_file_just(rj, s), angle=fa))
            p.append(_prop("Value", s["value"], vx_, vy_, False, justify=_file_just(vj, s), angle=fa))
        # KiCad 10 always stores Footprint, Datasheet and Description, in that
        # order, before any user fields.
        props = dict(s["props"])
        for k in ("Footprint", "Datasheet", "Description"):
            p.append(_prop(k, props.pop(k, ""), x, y, not s["power"]))
        for k, v in props.items():
            p.append(_prop(k, v, x, y, True))
        for pin in sym.pins(s["unit"]):
            p.append(f'\t\t(pin "{q(pin["num"])}"\n'
                     f'\t\t\t(uuid "{sid(fn, "sympin", ref, s["unit"], pin["num"])}")\n\t\t)\n')
        path = f"/{root_uuid}/{sheet.instance_uuid}" if root_uuid else f"/{sheet.uuid}"
        p.append("\t\t(instances\n"
                 f'\t\t\t(project "{q(project)}"\n'
                 f'\t\t\t\t(path "{path}"\n'
                 f'\t\t\t\t\t(reference "{q(ref)}")\n\t\t\t\t\t(unit {s["unit"]})\n\t\t\t\t)\n'
                 "\t\t\t)\n\t\t)\n\t)\n")

    if root_uuid is None:
        p.append('\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n')
    p.append(")\n")          # KiCad 10 omits a sheet-level (embedded_fonts no)
    return _kicad_order("".join(p))


# ===========================================================================
# Design: a set of sheets written together
# ===========================================================================

class GeometryError(RuntimeError):
    pass


class Design:
    """A project's schematic: an optional root sheet plus sub-sheets."""

    MANIFEST = ".schgen-manifest.json"

    def __init__(self, project_dir, name, lib=None, title="", rev="A", date=None,
                 company="", cli=None):
        self.project_dir = os.path.abspath(project_dir)
        self.name = name
        self.cli = cli
        self.lib = lib or Library(self.project_dir, cli=cli)
        self.title_block = {"date": date or datetime.date.today().isoformat(),
                            "rev": rev, "company": company}
        self.title = title or name
        self.children = []
        self.root = None

    # ---- construction ------------------------------------------------------
    def sheet(self, filename, title, paper="A4", comment=""):
        s = Sheet(self.lib, filename, title, paper, comment)
        self.children.append(s)
        return s

    def root_sheet(self, filename=None, title=None, paper="A4", comment=""):
        filename = filename or f"{self.name}.kicad_sch"
        self.root = Sheet(self.lib, filename, title or self.title, paper, comment)
        return self.root

    def link(self, child, x, y, w, h, pins=()):
        """Draw `child` as a sheet box on the root. pins: (name, side, y) or
        (name, side, y, shape); side 'left' or 'right'; y absolute, on-grid."""
        if self.root is None:
            raise RuntimeError("call root_sheet() before link()")
        child.instance_uuid = sid("sheetinstance", child.filename)
        norm = []
        for pin in pins:
            name, side, py = pin[:3]
            shape = pin[3] if len(pin) > 3 else "bidirectional"
            if side not in ("left", "right"):
                raise ValueError("sheet pin side must be 'left' or 'right'")
            norm.append((name, py, side, shape))
        self.root.child_sheets.append(dict(uuid=child.instance_uuid, name=child.title,
                                           file=child.filename, x=x, y=y, w=w, h=h,
                                           page=str(len(self.root.child_sheets) + 2),
                                           pins=norm))

    def all_sheets(self):
        return ([self.root] if self.root else []) + self.children

    # ---- output ------------------------------------------------------------
    def _hash(self, path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def _guard(self, targets, force):
        """Refuse to overwrite schematics edited outside the generator.

        Opening a generated file in KiCad and saving it -- even just moving a
        part -- changes it. Blindly regenerating would silently discard that
        work, so compare against the hashes recorded at the last write.
        """
        man_path = os.path.join(self.project_dir, self.MANIFEST)
        manifest = json.load(open(man_path)) if os.path.exists(man_path) else {}
        changed = []
        for f in targets:
            p = os.path.join(self.project_dir, f)
            if os.path.exists(p) and manifest.get(f) != self._hash(p):
                changed.append(f)
        if changed:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            bdir = os.path.join(self.project_dir, ".schgen-backup", stamp)
            os.makedirs(bdir, exist_ok=True)
            for f in changed:
                shutil.copy2(os.path.join(self.project_dir, f), os.path.join(bdir, f))
            if not force:
                raise RuntimeError(
                    "These schematic files were modified outside the generator (or were "
                    f"never written by it): {changed}. Backed up to {bdir}. Open them in "
                    "KiCad, port any edits worth keeping into the generator script, then "
                    "rerun with write(force=True).")
            print(f"  force: overwriting {changed} (backups in {bdir})")
        return man_path, manifest

    def _patch_project(self):
        """Give every netclass a wire/bus width. A netclass without `wire_width`
        makes KiCad plot every wire and junction invisibly (stroke:none) in SVG
        and PDF while they stay electrically present. Verified by controlled
        test: stroke type is irrelevant, the width is the whole story."""
        pro = os.path.join(self.project_dir, f"{self.name}.kicad_pro")
        if not os.path.exists(pro):
            return
        data = json.load(open(pro, encoding="utf-8"))
        changed = False
        for cls in data.get("net_settings", {}).get("classes", []):
            for key in ("wire_width", "bus_width"):
                if not cls.get(key):
                    cls[key] = 6
                    changed = True
        if changed:
            json.dump(data, open(pro, "w", encoding="utf-8"), indent=2)
            print(f"  patched netclass wire/bus widths in {os.path.basename(pro)}")

    def write(self, center=True, force=False, strict=True, native_check=True,
              relieve=True):
        """Check, lay out and write every sheet. Returns written file paths."""
        sheets = self.all_sheets()
        if not sheets:
            raise RuntimeError("nothing to write")
        if self.children and self.root is None:
            if len(self.children) == 1:
                # a single sheet is its own root
                self.root, self.children = self.children[0], []
                sheets = [self.root]
            else:
                raise RuntimeError("several sheets but no root_sheet(); add one and link() them")
        linked = {cs["file"] for cs in (self.root.child_sheets if self.root else [])}
        for c in self.children:
            if c.filename not in linked:
                raise RuntimeError(f"{c.filename} is never link()ed from the root sheet")

        all_errors = []
        for s in sheets:
            if relieve and not s._relieved:
                s.relieve()
            s.finish_blocks()
            # relief and spreading make a drawing bigger; a sheet with no
            # blocks never went through arrange(), so grow its paper here
            while s.paper in GROW_PAPER_ORDER:
                uw, uh = s.usable_area(s.paper)
                x0, y0, x1, y1 = s.drawing_bbox()
                i = GROW_PAPER_ORDER.index(s.paper)
                if ((x1 - x0) <= uw and (y1 - y0) <= uh) or i + 1 >= len(GROW_PAPER_ORDER):
                    break
                s.paper = GROW_PAPER_ORDER[i + 1]
                print(f"  {s.filename}: grew to {s.paper} to fit the drawing")
            if s.paper == "auto":
                s.paper = s.suggest_paper()
            errors, warnings, info = s.check()
            for e in errors:
                all_errors.append(f"{s.filename}: {e}")
            for w in warnings:
                print(f"  warning {s.filename}: {w}")
            print(f"  {s.filename}: {'; '.join(info)}")
        if all_errors and strict:
            raise GeometryError("geometry errors:\n  " + "\n  ".join(all_errors))

        targets = [s.filename for s in sheets]
        man_path, manifest = self._guard(targets, force)
        self._patch_project()

        pwr = iter(range(1, 100000))
        written = []
        for s in sheets:
            if center:
                s.center_on_page()
            root_uuid = None if s is self.root else self.root.uuid
            text = _emit(s, self.name, self.title_block, root_uuid, pwr)
            path = os.path.join(self.project_dir, s.filename)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            manifest[s.filename] = self._hash(path)
            ux, uy = s.utilization()
            written.append(path)
            print(f"  wrote {s.filename:32s} {s.paper:8s} fill {ux:4.0%} x {uy:4.0%}")
            if s.title_block_overlap():
                print(f"  warning {s.filename}: drawing reaches into the title-block "
                      f"corner -- check the render, or use a larger paper")
        json.dump(manifest, open(man_path, "w"), indent=2)
        if native_check:
            self.check_native()
        return written

    def check_native(self, quiet=False):
        """Prove the written sheets are what KiCad 10 itself would save.

        Copies every sheet to a temp folder, lets `kicad-cli sch upgrade`
        rewrite the copies, and compares structure token by token (whitespace
        ignored). The project files are never touched. Returns
        {filename: first difference} for sheets that differ."""
        cli = _cli(self.cli)
        tmp = tempfile.mkdtemp(prefix="schlib-native-")
        problems = {}
        try:
            # without the project file KiCad blanks the instance project names
            pro = os.path.join(self.project_dir, f"{self.name}.kicad_pro")
            if os.path.exists(pro):
                shutil.copy2(pro, tmp)
            elif not quiet:
                # without it KiCad blanks the instance project name on upgrade,
                # so every root sheet reads as different for that reason alone
                print(f"  note: no {self.name}.kicad_pro -- expect an "
                      f"instances/project difference below")
            for s in self.all_sheets():
                src = os.path.join(self.project_dir, s.filename)
                dst = os.path.join(tmp, s.filename)
                shutil.copy2(src, dst)
                subprocess.run([cli, "sch", "upgrade", "--force", dst], capture_output=True, text=True)
                ours = parse_sexp(open(src, encoding="utf-8").read())
                theirs = parse_sexp(open(dst, encoding="utf-8").read())
                d = _tree_diff(ours, theirs)
                if d:
                    problems[s.filename] = d
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not quiet:
            if problems:
                print(f"  native format: {len(problems)} sheet(s) differ from KiCad {GENERATOR_VERSION}'s own save:")
                for f, d in problems.items():
                    print(f"    {f}: {d}")
            else:
                print(f"  native format: all {len(self.all_sheets())} sheets match KiCad {GENERATOR_VERSION}'s own save")
        return problems

    # ---- verification ----------------------------------------------------
    def root_path(self):
        return os.path.join(self.project_dir, self.root.filename)

    def verify(self, nets, no_connect=(), quiet=False):
        ok, lines = verify_netlist(self.root_path(), nets, no_connect, self.cli)
        if not quiet:
            print("\n".join(lines))
        return ok

    def lint(self, parts, nets, no_connect=(), quiet=False, test_points=False):
        """lint_spec() with this design's libraries -- run before drawing."""
        return lint_spec(parts, nets, no_connect, lib=self.lib, quiet=quiet,
                         test_points=test_points)

    def erc(self, quiet=False):
        res = run_erc(self.root_path(), self.cli)
        if not quiet:
            print(f"  ERC: {res['errors']} error(s), {res['warnings']} warning(s)")
            for (sev, typ), n in sorted(res["by_type"].items()):
                print(f"    {n:4d}  {sev:8s} {typ}")
        return res

    def render(self, outdir):
        return render(self.root_path(), outdir, self.cli)

    def check_text(self, quiet=False, min_gap=CLEAR_ROW, show=8):
        """Render every sheet and check the text as KiCad actually drew it.

        Three things, from one render:
        * collisions -- text drawn sideways, or on top of a part or a wire;
        * outline faults (counted with the collisions) -- a string or a part
          body touching or straddling a block outline;
        * crowding -- any two strings closer than `min_gap`, which is the
          fault that makes a sheet look amateur and that no netlist, ERC run
          or internal estimate can see. Pairs inside one library symbol are
          excluded: that spacing is the symbol's, not the layout's.

        Returns (collisions, crowded)."""
        tmp = tempfile.mkdtemp(prefix="schlib-text-")
        problems, crowded, skipped = [], [], []
        try:
            pages = render(self.root_path(), tmp, self.cli)
            by_title = {_sanitize_sheet_label(s.title): s for s in self.children}
            for path, label in pages:
                sheet = self.root if label == "root" else by_title.get(label)
                if sheet is None:
                    skipped.append(label)
                    continue
                problems += [f"{sheet.filename}: {p}" for p in text_collisions(sheet, path)]
                problems += [f"{sheet.filename}: {p}" for p in outline_faults(sheet, path)]
                crowded += [f"{sheet.filename}: {c}"
                            for c in crowding(sheet, path, min_gap, CLEAR_STACK)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not quiet:
            print(f"  text check: {len(problems)} collision(s), "
                  f"{len(crowded)} pair(s) closer than {min_gap:.2f} mm")
            for p in problems:
                print("    " + p)
            for c in crowded[:show]:
                print("    " + c)
            if len(crowded) > show:
                print(f"    ... and {len(crowded) - show} more")
            if skipped:
                print(f"    (could not match rendered pages {skipped} to sheets)")
        return problems, crowded


# ===========================================================================
# KiCad-backed checks
# ===========================================================================

def _tree_diff(a, b, path="kicad_sch"):
    """First structural difference between two parsed s-expressions, or None."""
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a) or i >= len(b):
                extra = a[i] if i < len(a) else b[i]
                who = "ours" if i < len(a) else "KiCad"
                snippet = extra if isinstance(extra, str) else "(" + " ".join(
                    x if isinstance(x, str) else "(" + str(x[0]) + " ..." for x in extra[:4]) + ")"
                return f"at {path}: only {who} has {snippet}"
            here = a[0] if a and isinstance(a[0], str) else "?"
            d = _tree_diff(a[i], b[i], f"{path}/{here}[{i}]" if i else path)
            if d:
                return d
        return None
    return None if a == b else f"at {path}: ours {a!r}, KiCad {b!r}"


def _cli(cli):
    return cli or find_kicad_cli()


def export_netlist(root_sch, cli=None):
    """{net_name: {(ref, pin)}} from KiCad's own connectivity engine."""
    fd, out = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        r = subprocess.run([_cli(cli), "sch", "export", "netlist", "--format", "kicadxml",
                            "-o", out, root_sch], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.getsize(out):
            raise RuntimeError(f"netlist export failed (KiCad could not load the "
                               f"schematic?)\n{r.stdout}\n{r.stderr}")
        nets = {}
        for net in ET.parse(out).getroot().iter("net"):
            nets[net.get("name")] = {(n.get("ref"), n.get("pin")) for n in net.findall("node")}
        return nets
    finally:
        os.remove(out)


def verify_netlist(root_sch, nets, no_connect=(), cli=None):
    """Diff KiCad's extracted netlist against the intended one.

    Nets are matched by their *set of pins*, not by name -- a different name is
    reported but isn't an error; a different grouping is. This is what catches
    a mistyped coordinate silently shorting two nets or orphaning a pin.
    Returns (ok, report_lines).
    """
    actual = export_netlist(root_sch, cli)
    actual = {n: {p for p in pins if not p[0].startswith("#")} for n, pins in actual.items()}
    actual = {n: pins for n, pins in actual.items() if pins}
    nc_drawn = {p for n, ps in actual.items() if n.startswith("unconnected-") for p in ps}
    actual = {n: ps for n, ps in actual.items() if not n.startswith("unconnected-")}

    expected = {n: {(r, str(p)) for r, p in pins} for n, pins in nets.items()}
    nc_want = {(r, str(p)) for r, p in no_connect}
    short = lambda n: n.rsplit("/", 1)[-1]  # noqa: E731  (strip sheet path)

    lines, errors, renames = [], [], []
    if nc_drawn - nc_want:
        errors.append(f"unconnected but not in NO_CONNECT: {sorted(nc_drawn - nc_want)}")
    if nc_want - nc_drawn:
        errors.append(f"in NO_CONNECT but wired to something: {sorted(nc_want - nc_drawn)}")

    by_pins = {frozenset(v): short(k) for k, v in actual.items()}
    want_by_pins = {frozenset(v): k for k, v in expected.items()}
    for pins, want in want_by_pins.items():
        if pins in by_pins:
            if by_pins[pins] != want:
                renames.append(f"{want} -> drawn as {by_pins[pins]}")
            continue
        touching = sorted({short(n) for n, ps in actual.items() if ps & pins})
        errors.append(f"net {want}: pins {sorted(pins)} land in drawn net(s) {touching or ['<none>']}")
        for n in touching:
            got = set().union(*(ps for k, ps in actual.items() if short(k) == n))
            if got - pins:
                errors.append(f"    {n} also has {sorted(got - pins)}")
            if pins - got:
                errors.append(f"    {n} is missing {sorted(pins - got)}")
    for pins, name in by_pins.items():
        if pins not in want_by_pins and not any(pins & e for e in want_by_pins):
            errors.append(f"unexpected net {name}: {sorted(pins)}")

    lines.append(f"  nets drawn: {len(actual)}   expected: {len(expected)}")
    if renames:
        lines.append("  renamed (same pins, different name -- usually fine; add a label "
                     "to keep your name):")
        lines += [f"    {r}" for r in renames]
    if errors:
        lines.append("  NETLIST MISMATCH:")
        lines += [f"    {e}" for e in errors]
    else:
        lines.append("  netlist matches the specification exactly")
    return (not errors, lines)


# ===========================================================================
# Spec lint: design rules checked on PARTS/NETS before anything is drawn
# ===========================================================================
# verify() proves the drawing matches the spec; nothing proves the spec is a
# sensible circuit. These rules catch the mistakes that survive a perfect
# netlist match -- a pin number that does not exist, an IC power pin with no
# decoupling, an I2C bus with no pull-ups -- at the point where fixing them
# costs one line of the spec and no geometry. They are heuristics, not a
# design review: the kicad review skill does that on the finished sheets.

GROUND_NET = re.compile(r"^(GND\w*|\w*GND|VSS\w*|0V|COM|EARTH|CHASSIS)$", re.I)
RAIL_NET = re.compile(r"^(\+.*|-\d.*|V(CC|DD|BUS|BAT|IN|SYS|MAIN|REF)\w*|\d+V\d*\w*|\d+V)$", re.I)
_POWER_PIN_NAME = re.compile(
    r"^(V(CC|DD|IN|BAT|SYS|DDA|DDIO|DDQ|CCA|CCIO)\w*|AVCC|AVDD|DVCC|DVDD|"
    r"PVCC|PVDD|3V3|5V|VS|V\+|VP|IOVDD|VIO)$", re.I)
_RESET_PIN_NAME = re.compile(r"^(~?\{?)?(N?RST|N?RESET|RST_?N|RESET_?N|MCLR|CHIP_?EN|EN)\}?$", re.I)
_DRIVER_TYPES = {"output", "bidirectional", "tri_state", "power_out", "open_collector",
                 "open_emitter", "passive", "unspecified", "free"}
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "R": 1.0}


def parse_value(text):
    """Numeric value of a part value string: '100n', '4.7uF', '4u7', '10k',
    '2R2', '1M 1%' -> float, or None if it is not a plain value."""
    s = str(text).strip().replace(",", ".")
    m = re.match(r"^(\d+)([pnuµμmkKMR])(\d+)", s)            # 4u7, 2R2, 1k5
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _SI[m.group(2)]
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([pnuµμmkKMR]?)", s)
    if m:
        return float(m.group(1)) * _SI[m.group(2)]
    return None


def _kind(ref, lib_id):
    """'C', 'R', 'L', 'FB', 'TP', 'IC' or 'other', from the reference and library."""
    prefix = re.match(r"[A-Za-z#]+", ref).group(0).upper() if ref else ""
    name = lib_id.split(":", 1)[-1] if lib_id else ""
    if prefix == "C" or re.match(r"^C(_|$)", name) or name.startswith("CP"):
        return "C"
    if prefix in ("R", "RN") or re.match(r"^R(_|$)", name):
        return "R"
    if prefix == "FB" or name.startswith("FerriteBead"):
        return "FB"
    if prefix == "L":
        return "L"
    if prefix == "TP":
        return "TP"
    if prefix in ("U", "IC", "Q", "MOD", "M", "A"):
        return "IC"
    return "other"


# ---------------------------------------------------------------------------
# Differential pairs
# ---------------------------------------------------------------------------
# KiCad does not store which nets form a pair. It *infers* it from the two
# names, every time, in BOARD::MatchDpSuffix (pcbnew/board.cpp): read the name
# backwards, skip any run of digits and underscores, and look at the next
# character. Only "P", "N", "+" and "-" count, and the letters must be upper
# case. The complement is that one character swapped -- so the pair exists
# only if a net with exactly that name exists.
#
# Everything follows from that one rule:
#
#   USB_P   / USB_N    pair        USB_DP   / USB_DM    NOT a pair
#   USBP    / USBN     pair        USB_p    / USB_n     NOT a pair (lower case)
#   USB+    / USB-     pair        CAN_H    / CAN_L     NOT a pair
#   TD1_P   / TD1_N    pair        TD1P     / TD1M      NOT a pair
#   USB_P12 / USB_N12  pair        MX1P_INT / MX1M_INT  NOT a pair
#
# "DP/DM" is the commonest way to lose this, because it is what the USB spec
# calls the two wires and what most datasheets print on the pins. KiCad reads
# the P and then goes looking for "..._DN", which nobody writes.
#
# An unrecognised pair costs you, silently and with no error anywhere: the
# differential-pair router and its length tuner, the skew and coupling DRC
# tests, and -- the expensive one -- the netclass's `diff_pair_width` and
# `diff_pair_gap`, because that constraint is gated on A.inDiffPair() in
# drc_engine.cpp. The pair then routes at the netclass's ordinary track width,
# at whatever impedance that happens to give, and nothing complains.
#
# So name every pair <BASE>_P / <BASE>_N, in the schematic, from the start.
# Renaming afterwards means the schematic, the netlist, the netclass patterns
# and any cable pinout you have already written down.

_DP_POLARITY = {"P": 1, "+": 1, "N": -1, "-": -1}
_DP_COMPLEMENT = {"P": "N", "N": "P", "+": "-", "-": "+"}
# what a human writes for the two halves, including the forms KiCad cannot
# read: M for minus, and the lower-case letters
_DP_INTENT = {"P": 1, "+": 1, "p": 1, "N": -1, "M": -1, "-": -1, "n": -1, "m": -1}


def match_dp_suffix(name):
    """KiCad's own pair rule, as (polarity, complement_name).

    polarity is +1, -1 or 0; complement_name is None when polarity is 0.
    A reimplementation of BOARD::MatchDpSuffix, checked name for name against
    pcbnew's own DpCoupledNet()."""
    count = 0
    for i in range(len(name) - 1, -1, -1):
        ch = name[i]
        if ch.isdigit() or ch == "_":
            count += 1
            continue
        if ch in _DP_POLARITY:
            cut = len(name) - count - 1
            return _DP_POLARITY[ch], name[:cut] + _DP_COMPLEMENT[ch] + name[cut + 1:]
        break
    return 0, None


def _dp_suggestion(prefix, suffix):
    """<BASE>_P / <BASE>_N for a pair whose two names differ at one character,
    with `prefix` before that character and `suffix` after it."""
    base = prefix
    # "..._DP" / "..._DM": the D belongs to the old spelling, not to the name
    if len(base) > 1 and base.endswith("D") and base[-2] == "_":
        base = base[:-1]
    base = (base + suffix).strip("_") or (prefix + suffix)
    return base + "_P", base + "_N"


def diff_pair_faults(names, quiet=True):
    """Net names that will not do what their author meant.

    Returns [(severity, rule, message)], the same shape as lint_spec().

    dp-name     two nets plainly meant to be a pair -- their names differ at
                exactly one character and that character is P/N/M/+/- on both
                sides -- that KiCad will not couple. The message gives the
                <BASE>_P / <BASE>_N names to use instead.
    dp-suffix   a single-ended net that KiCad reads as half of a pair, usually
                an active-low signal called something_N. Harmless while no net
                is called something_P, and a trap the day one is.

    Power rails are skipped: +12V and -12V differ at one character and are not
    a differential pair."""
    names = sorted(set(names))
    out = []

    def rail(n):
        base = n.rsplit("/", 1)[-1]
        return bool(GROUND_NET.match(base) or RAIL_NET.match(base))

    signals = [n for n in names if not rail(n)]

    intended = {}
    for a, b in itertools.combinations(signals, 2):
        if len(a) != len(b):
            continue
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff) != 1:
            continue
        i = diff[0]
        pa, pb = _DP_INTENT.get(a[i]), _DP_INTENT.get(b[i])
        if pa is None or pb is None or pa == pb:
            continue
        pos, neg = (a, b) if pa > 0 else (b, a)
        intended[(pos, neg)] = (a[:i], a[i + 1:])

    for (pos, neg), (prefix, suffix) in sorted(intended.items()):
        if match_dp_suffix(pos)[1] == neg and match_dp_suffix(neg)[1] == pos:
            continue                                        # KiCad agrees
        want_p, want_n = _dp_suggestion(prefix, suffix)
        out.append(("warning", "dp-name",
                    f"{pos} / {neg} read as a differential pair to a human but not to "
                    f"KiCad, which reads the name backwards and wants P/N or +/- last, "
                    f"upper case -- rename to {want_p} / {want_n}"))

    paired = {n for p in intended for n in p}
    stray = []
    for n in signals:
        if n in paired:
            continue
        pol, comp = match_dp_suffix(n)
        if pol and comp not in names:
            stray.append(f"{n} (-> {comp})")
    if stray:
        show = ", ".join(stray[:8]) + (f", ... {len(stray) - 8} more" if len(stray) > 8 else "")
        out.append(("info", "dp-suffix",
                    f"{len(stray)} single-ended net(s) end like half a differential pair; "
                    f"KiCad's pair tools offer them, and they couple silently the day a net "
                    f"with the complement name appears: {show}"))

    if not quiet:
        for sev, rule, msg in out:
            print(f"    {sev:7s} {rule:18s} {msg}")
    return out


def lint_spec(parts, nets, no_connect=(), lib=None, project_dir=None, quiet=False,
              test_points=False):
    """Check a PARTS/NETS/NO_CONNECT spec against design rules. Run it before
    drawing; fix the spec, not the rule.

    parts   {ref: (lib_id, value, ...)} or {ref: {"lib_id": ..., "value": ...}}
    nets    {net: [(ref, pin), ...]} -- pins by number (or unique name)
    lib     a Library (default: Library(project_dir))

    Structural rules (errors): unknown refs; pins the symbol does not have;
    one pin in two nets or in a net and NO_CONNECT; a power-input pin left
    unconnected.
    Design rules (warnings): an IC power pin whose rail has no capacitor to
    ground; a rail feeding ICs with no bulk (>= 10 uF) capacitor; SDA/SCL
    with no pull-up; a reset/enable input with no pull-up and no driver; an
    input nothing drives; a single-pin net; a pin in no net and not marked
    no-connect; a differential pair KiCad will not recognise from its two
    names (diff_pair_faults). test_points=True adds an info line per rail
    without a TP.

    Returns [(severity, rule, message)], severity 'error'|'warning'|'info'."""
    lib = lib or Library(project_dir)
    out = []

    def add(sev, rule, msg):
        out.append((sev, rule, msg))

    def part(ref):
        p = parts[ref]
        if isinstance(p, dict):
            return p["lib_id"], p.get("value", "")
        return p[0], (p[1] if len(p) > 1 else "")

    # ---- resolve every pin -------------------------------------------------
    syms, pins_of = {}, {}
    for ref in parts:
        lib_id, _v = part(ref)
        try:
            sym = lib.get(lib_id)
        except Exception as e:                           # noqa: BLE001
            add("error", "unknown-symbol", f"{ref}: cannot load {lib_id} ({e})")
            continue
        syms[ref] = sym
        pins_of[ref] = {}
        for u in sym.units:
            for p in sym.pins(u):
                pins_of[ref].setdefault(p["num"], p)

    def resolve(ref, key):
        """(num, pin dict) or None."""
        key = str(key)
        if ref not in pins_of:
            return None
        if key in pins_of[ref]:
            return key, pins_of[ref][key]
        named = [(n, p) for n, p in pins_of[ref].items() if p["name"] == key]
        return named[0] if len(named) == 1 else None

    where = {}                     # (ref, num) -> net
    net_pins = {}                  # net -> [(ref, num, pin)]
    for net, members in nets.items():
        net_pins[net] = []
        for ref, key in members:
            if ref not in parts:
                add("error", "unknown-ref", f"net {net}: {ref} is not in PARTS")
                continue
            r = resolve(ref, key)
            if r is None:
                if ref in pins_of:
                    have = ", ".join(n + (f"={p['name']}" if p["name"] not in ("", "~") else "")
                                     for n, p in list(pins_of[ref].items())[:12])
                    add("error", "no-such-pin",
                        f"net {net}: {ref} ({part(ref)[0]}) has no pin {key!r} -- pins: {have}"
                        + (" ..." if len(pins_of[ref]) > 12 else ""))
                continue
            num, pin = r
            if (ref, num) in where and where[(ref, num)] != net:
                add("error", "pin-in-two-nets",
                    f"{ref} pin {num} ({pin['name']}) is in both {where[(ref, num)]} and {net}")
            where[(ref, num)] = net
            net_pins[net].append((ref, num, pin))
    nc = set()
    for ref, key in no_connect:
        r = resolve(ref, key)
        if r is None:
            add("error", "no-such-pin", f"NO_CONNECT: {ref} has no pin {key!r}")
            continue
        nc.add((ref, r[0]))
        if (ref, r[0]) in where:
            add("error", "nc-and-net", f"{ref} pin {r[0]} is no-connect and also in {where[(ref, r[0])]}")

    # ---- unconnected pins --------------------------------------------------
    for ref, pins in pins_of.items():
        if syms[ref].is_power:
            continue
        loose = [(n, p) for n, p in pins.items() if (ref, n) not in where and (ref, n) not in nc]
        for n, p in loose:
            if p["etype"] == "power_in":
                add("error", "power-pin-open", f"{ref} pin {n} ({p['name']}) is a power input "
                    f"and is in no net")
        other = [f"{n}" + (f"={p['name']}" if p["name"] and p["name"] != "~" else "")
                 for n, p in loose if p["etype"] != "power_in" and p["etype"] != "no_connect"]
        if other:
            add("warning", "pin-unassigned", f"{ref}: pin(s) {', '.join(other)} in no net and "
                f"not in NO_CONNECT")

    for net, members in net_pins.items():
        if len(members) == 1:
            ref, num, pin = members[0]
            add("warning", "single-pin-net", f"net {net} reaches only {ref} pin {num} "
                f"({pin['name']}) -- a typo in a net name splits a net this way")

    # ---- design rules --------------------------------------------------------
    kinds = {ref: _kind(ref, part(ref)[0]) for ref in parts}
    grounds = {n for n in net_pins if GROUND_NET.match(n)}

    def two_terminal(ref):
        """(net_a, net_b) of a two-pin part, or None."""
        ns = [where.get((ref, n)) for n in pins_of.get(ref, {})]
        ns = [n for n in ns if n]
        return tuple(ns) if len(ns) == 2 else None

    caps_on, pullup_on = defaultdict(list), defaultdict(list)
    for ref, k in kinds.items():
        tt = two_terminal(ref)
        if not tt:
            continue
        a, b = tt
        if k == "C":
            for x, y in ((a, b), (b, a)):
                if y in grounds:
                    caps_on[x].append(ref)
        elif k == "R":
            for x, y in ((a, b), (b, a)):
                if y not in grounds and (RAIL_NET.match(y) or _is_rail(y, net_pins)):
                    pullup_on[x].append((ref, y))

    def is_power_pin(pin):
        if pin["etype"] == "power_in":
            return True
        return pin["etype"] in ("unspecified", "passive", "bidirectional") and \
            bool(_POWER_PIN_NAME.match(pin["name"] or ""))

    rails_fed = defaultdict(set)
    for net, members in net_pins.items():
        if net in grounds:
            continue
        for ref, num, pin in members:
            if kinds[ref] == "IC" and is_power_pin(pin):
                rails_fed[net].add(ref)
    for net, ics in sorted(rails_fed.items()):
        if not caps_on[net]:
            add("warning", "no-decoupling",
                f"rail {net} feeds {', '.join(sorted(ics))} but has no capacitor to ground")
            continue
        bulk = [c for c in caps_on[net] if (parse_value(part(c)[1]) or 0) >= 9.9e-6]
        if not bulk:
            vals = ", ".join(f"{c}={part(c)[1]}" for c in caps_on[net])
            add("warning", "no-bulk-cap",
                f"rail {net} has only small capacitors ({vals}); add >= 10 uF bulk "
                f"near where it enters or is generated")
        small = [c for c in caps_on[net] if 0 < (parse_value(part(c)[1]) or 0) <= 1.1e-6]
        if len(ics) > len(small):
            add("info", "decoupling-count",
                f"rail {net}: {len(ics)} IC(s) ({', '.join(sorted(ics))}) but "
                f"{len(small)} capacitor(s) <= 1 uF -- one 100 nF per IC power pin is usual")

    for net, members in net_pins.items():
        names = {(pin["name"] or "").upper() for _r, _n, pin in members}
        is_i2c = any(re.search(r"\bSDA|\bSCL|^SDA|^SCL", nm) for nm in names) or \
            re.search(r"SDA|SCL", net, re.I)
        if is_i2c and not pullup_on[net]:
            add("warning", "i2c-no-pullup",
                f"{net} is an I2C line with no pull-up resistor to a rail "
                f"(unless a module on the bus provides one)")
        for ref, num, pin in members:
            if kinds[ref] != "IC" or pin["etype"] not in ("input", "unspecified", "bidirectional"):
                continue
            if not _RESET_PIN_NAME.match(pin["name"] or ""):
                continue
            driven = any(p["etype"] in ("output", "open_collector", "power_out")
                         for r, _n, p in members if r != ref)
            tied = net in grounds or RAIL_NET.match(net) or _is_rail(net, net_pins)
            if not pullup_on[net] and not driven and not tied:
                add("warning", "reset-no-pullup",
                    f"{ref} pin {num} ({pin['name']}) on {net} has no pull-up and nothing "
                    f"drives it -- check the datasheet for an internal pull-up")

    for net, members in net_pins.items():
        types = [p["etype"] for _r, _n, p in members]
        if types and all(t == "input" for t in types):
            add("warning", "undriven-input",
                f"net {net} connects only inputs ({', '.join(f'{r}.{n}' for r, n, _p in members)})")

    if test_points:
        tps = {where.get((r, n)) for r in parts if kinds[r] == "TP" for n in pins_of.get(r, {})}
        for net in sorted(rails_fed):
            if net not in tps:
                add("info", "no-test-point", f"rail {net} has no test point")

    out += diff_pair_faults(nets)

    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda t: (order[t[0]], t[1], t[2]))
    if not quiet:
        n = {s: sum(1 for t in out if t[0] == s) for s in order}
        print(f"  spec lint: {n['error']} error(s), {n['warning']} warning(s), {n['info']} note(s)")
        for sev, rule, msg in out:
            print(f"    {sev:7s} {rule:18s} {msg}")
    return out


def _is_rail(net, net_pins):
    """A net is a rail if a power-output pin or a power symbol drives it."""
    return any(p["etype"] == "power_out" for _r, _n, p in net_pins.get(net, ()))


def run_erc(root_sch, cli=None):
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        subprocess.run([_cli(cli), "sch", "erc", "--format", "json", "-o", out, root_sch],
                       capture_output=True, text=True)
        data = json.load(open(out, encoding="utf-8"))
    finally:
        os.remove(out)
    by_type = defaultdict(int)
    errors = warnings = 0
    for sh in data.get("sheets", []):
        for v in sh.get("violations", []):
            sev = v.get("severity", "?")
            by_type[(sev, v.get("type", "?"))] += 1
            if sev == "error":
                errors += 1
            elif sev == "warning":
                warnings += 1
    return {"errors": errors, "warnings": warnings, "by_type": dict(by_type), "raw": data}


def svg_strings(svg_path):
    """Every string KiCad drew, with its exact box.

    The SVG carries an invisible <text> for each string with x, y, font-size
    and textLength, so these boxes are KiCad's own numbers rather than our
    idea of them -- which is why the crowding check is built on this rather
    than on the model. A rotated string is wrapped in its own
    <g transform="rotate(deg cx cy)">, so the rotation comes from the wrapper;
    <text>'s own attributes never carry it."""
    t = open(svg_path, encoding="utf-8").read()
    pat = re.compile(r'(?:<g transform="rotate\(([-\d.]+)\s+([-\d.]+)[, ]+([-\d.]+)\)">\s*)?'
                     r"<text\b([^>]*)>([^<]*)</text>")
    out, seen = [], set()
    for rdeg, rcx, rcy, attrs, body in pat.findall(t):
        g = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        try:
            x, y = float(g["x"]), float(g["y"])
            w, fs = float(g.get("textLength", 0)), float(g.get("font-size", 1.27))
        except (KeyError, ValueError):
            continue
        txt = html.unescape(body).strip()
        if not txt:
            continue
        anchor = g.get("text-anchor", "start")
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        corners = [(x0, y - fs * 0.8), (x0 + w, y - fs * 0.8),
                   (x0, y + fs * 0.1), (x0 + w, y + fs * 0.1)]
        rot = 0.0
        if rdeg:
            rot = float(rdeg)
            corners = [_rotate_point(px, py, float(rcx), float(rcy), rot)
                       for px, py in corners]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        box = (min(xs), min(ys), max(xs), max(ys))
        key = (txt, round(box[0], 2), round(box[1], 2))
        if key in seen:                     # KiCad repeats a string per layer
            continue
        seen.add(key)
        out.append({"text": txt, "box": box, "rot": rot, "size": fs * 0.75})
    return out


def _attribute(sheet, strings):
    """Tag each drawn string with the item that put it there.

    Matching is by *position*, against the boxes the model says it placed --
    matching by text alone attributes an IC's own pin name to the label of the
    same net a few millimetres away, and then reports the pin name crowding
    its neighbouring pin number as a layout fault. The model's boxes agree
    with KiCad's to about 0.2 mm (scripts/calibrate_text.py), so a 2.5 mm
    radius is generous and unambiguous.

    'ours' -- a label, note, reference, value or power-port name we placed:
    its position is ours to fix. 'pin' -- a pin name or number belonging to a
    library symbol: the symbol's business, not the layout's."""
    model = [it for it in sheet.item_boxes() if it["kind"] not in ("body", "nc")]
    pin_owner = []
    for n, s in enumerate(sheet.symbols):
        tag = f"{s['ref'] or s['value']}#{n}"
        for box in sheet.pin_text_boxes(s):
            pin_owner.append((tag, box))
        pin_owner.append((tag, sheet.body_bbox(s)))

    KIND = {"label": "label", "note": "note", "field": "field"}
    used = set()
    for st in strings:
        cx = (st["box"][0] + st["box"][2]) / 2
        cy = (st["box"][1] + st["box"][3]) / 2
        best = None
        for i, it in enumerate(model):
            if i in used or it["kind"] == "pin":
                continue
            texts = it.get("lines") or [it["text"]]
            if not any(st["text"] == x or str(x).startswith(st["text"]) for x in texts):
                continue
            if len(texts) > 1:
                # a multi-line note is one item drawn as several strings: every
                # line inside its box is its own. Matching by distance to the
                # box centre claimed one line and left the rest as strangers,
                # which then "crowded" each other at KiCad's own line pitch.
                bx = it["box"]
                if bx[0] - 1.0 <= cx <= bx[2] + 1.0 and bx[1] - 2.5 <= cy <= bx[3] + 2.5:
                    best = (-1.0, None, it)
                    break
                continue
            mx = (it["box"][0] + it["box"][2]) / 2
            my = (it["box"][1] + it["box"][3]) / 2
            d = (mx - cx) ** 2 + (my - cy) ** 2
            if d < 6.25 and (best is None or d < best[0]):     # within 2.5 mm
                best = (d, i, it)
        if best:
            if best[1] is not None:
                used.add(best[1])
            it = best[2]
            st["owner"] = it["owner"]
            kind = KIND.get(it["kind"], it["kind"])
            if it["kind"] == "field":
                sym = it.get("sym")
                kind = "port" if (sym and sym["power"]) else (
                    "reference" if it["text"] == (sym or {}).get("ref") else "value")
            st["kind"] = kind
            continue
        st["owner"], st["kind"] = None, "pin"
        for tag, box in pin_owner:
            if box[0] - 0.6 <= cx <= box[2] + 0.6 and box[1] - 0.6 <= cy <= box[3] + 0.6:
                st["owner"] = tag
                break
    return strings


def crowding(sheet, svg_path, row=CLEAR_ROW, stack=CLEAR_STACK):
    """Strings KiCad drew too close to another item's string.

    Side by side they must clear `row`; stacked they must clear `stack`, which
    is smaller because a column of labels on the 2.54 mm pin grid is how a
    schematic is supposed to look. Pairs inside one symbol are skipped -- that
    spacing belongs to the library -- as is a part's own reference against its
    own value."""
    strings = _attribute(sheet, svg_strings(svg_path))
    out = []
    for a, b in itertools.combinations(strings, 2):
        if a["kind"] == "pin" and b["kind"] == "pin":
            continue
        if a["owner"] is not None and a["owner"] == b["owner"]:
            continue
        f = clearance_fault(a["box"], b["box"], row, stack)
        if f is None:
            continue
        gap, how = f
        where = (round(a["box"][0], 2), round(a["box"][1], 2))
        if how == "overlap":
            out.append((gap, f"{a['text']!r} overlaps {b['text']!r} by {gap:.2f} mm at {where}"))
        else:
            want = row if how == "row" else stack
            out.append((gap, f"{a['text']!r} is {-gap:.2f} mm from {b['text']!r} at {where} "
                             f"({how}, want {want:.2f})"))
    out.sort(key=lambda t: -t[0])
    return [m for _g, m in out]


def outline_faults(sheet, svg_path=None, clear=OUTLINE_CLEAR):
    """Strings and part bodies that touch or straddle a block outline.

    An outline is a dashed rectangle; a label lying across it reads as a
    drafting error even when every connection is right, and it is the usual
    result of typing a rectangle by hand instead of deriving it from the
    block's contents. With `svg_path` the strings are KiCad's own (every
    field, label, note, pin and power-port name, at the size KiCad drew it);
    without it, the model's boxes are used, which is what write() can afford.

    A box is in fault with a rectangle when it comes within `clear` of one of
    its four edges: inside-and-clear is fine, outside-and-clear is fine."""
    rects = []
    named = {}
    for b in sheet.all_blocks():
        r = sheet.block_rect(b)
        if r is not None:
            named[tuple(round(v, 2) for v in r)] = b["title"] or "(untitled block)"
    for r in sheet.rects:
        x1, y1, x2, y2 = r[:4]
        key = tuple(round(v, 2) for v in (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        rects.append((key, named.get(key, "box at (%.1f, %.1f)" % key[:2])))
    if not rects:
        return []
    boxes = []
    if svg_path:
        for st in svg_strings(svg_path):
            boxes.append((repr(st["text"][:40]), st["box"]))
    else:
        for it in sheet.item_boxes():
            if it["kind"] in ("field", "label", "note"):
                boxes.append((repr(str(it["text"]).split(chr(10))[0][:40]), it["box"]))
    for sym in sheet.symbols:
        if not sym["power"]:
            boxes.append(("part " + sym["ref"], sheet.body_bbox(sym)))
    out, seen = [], set()
    for what, (l, t, r_, b_) in boxes:
        for (x1, y1, x2, y2), title in rects:
            outside = r_ < x1 - clear or l > x2 + clear or b_ < y1 - clear or t > y2 + clear
            inside = l > x1 + clear and r_ < x2 - clear and t > y1 + clear and b_ < y2 - clear
            if outside or inside:
                continue
            edges = [n for n, hit in (("left", l <= x1 + clear and r_ >= x1 - clear),
                                      ("right", l <= x2 + clear and r_ >= x2 - clear),
                                      ("top", t <= y1 + clear and b_ >= y1 - clear),
                                      ("bottom", t <= y2 + clear and b_ >= y2 - clear)) if hit]
            key = (what, title)
            if key in seen:
                continue
            seen.add(key)
            out.append("%s touches the %s edge of %r at (%.1f, %.1f)"
                       % (what, "/".join(edges) or "outline", title, (l + r_) / 2, (t + b_) / 2))
    return out


def text_collisions(sheet, svg_path):
    """Check reference/value, label and note text as KiCad actually drew it.

    The netlist can't see text, so this reads the exported SVG, which carries an
    invisible <text> element for every string (position, length, rotation),
    and compares each string we placed against part bodies and wires. Returns
    a list of problems: sideways text, and text lying on a body or a wire.

    KiCad wraps a rotated (vertical) label's <text> in its own
    `<g transform="rotate(deg cx cy)">` rather than rotating the <text>
    element itself, so the rotation has to be pulled from that wrapper --
    `<text>`'s own attrs never carry it. Get this wrong (or skip it, as a
    prior version of this function did) and every vertical label's box is
    computed as if it were horizontal: a thin sideways sliver near its
    anchor point instead of the tall sliver it actually occupies running
    along the wire. That both misses real overlaps along the label's true
    (vertical) extent and reports false ones against unrelated geometry that
    merely happens to sit within the wrong sideways sliver.
    """
    t = open(svg_path, encoding="utf-8").read()
    drawn = []
    pat = re.compile(r'(?:<g transform="rotate\(([-\d.]+)\s+([-\d.]+)[, ]+([-\d.]+)\)">\s*)?'
                      r"<text\b([^>]*)>([^<]*)</text>")
    for rdeg, rcx, rcy, attrs, body in pat.findall(t):
        g = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        try:
            x, y = float(g["x"]), float(g["y"])
            w, fs = float(g.get("textLength", 0)), float(g.get("font-size", 1.27))
        except (KeyError, ValueError):
            continue
        anchor = g.get("text-anchor", "start")
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        has_rotate = bool(rdeg)
        if has_rotate:
            # Box in the text's own unrotated frame, then rotate its corners
            # the same way the wrapping <g> rotates the glyphs.
            deg, cx, cy = float(rdeg), float(rcx), float(rcy)
            corners = [(x0, y - fs * 0.8), (x0 + w, y - fs * 0.8),
                       (x0, y + fs * 0.1), (x0 + w, y + fs * 0.1)]
            rc = [_rotate_point(px, py, cx, cy, deg) for px, py in corners]
            xs, ys = [p[0] for p in rc], [p[1] for p in rc]
            box = (min(xs), min(ys), max(xs), max(ys))
        else:
            box = (x0, y - fs * 0.8, x0 + w, y + fs * 0.1)
        # Two different faults, and the SVG wrapper tells them apart:
        # an odd multiple of 90 reads top-to-bottom (sideways), 180 reads
        # upside down. KiCad keeps its own field text upright, so either one
        # means we wrote the wrong field angle.
        deg_ = round(float(rdeg)) % 360 if has_rotate else 0
        orient = "sideways" if deg_ % 180 == 90 else (
            "upside down" if deg_ == 180 else None)
        drawn.append((body.strip(), box, orient, (x, y)))

    # what we placed: fields of real parts, labels, notes (first line)
    wanted = []
    # Labels first, deliberately. Each drawn string is claimed by the nearest
    # unclaimed item with the same text, and a label knows exactly where it
    # was put, while a symbol field is only located by its symbol's origin.
    # A test point valued after the net it probes ("TP1", value "LOOP_V",
    # wired to a LOOP_V label) puts the label nearer the symbol origin than
    # the symbol's own value field, so matching fields first made the value
    # steal the label's text and report it as sitting on a wire.
    for _k, name, x, y, *_ in sheet.labels:
        wanted.append((name, (x, y), "label"))
    for s in sheet.symbols:
        if not s["power"]:
            wanted.append((s["ref"], s, "reference"))
            wanted.append((s["value"], s, "value"))
    for content, x, y, *_ in sheet.texts:
        wanted.append((content.split("\n")[0], (x, y), "note"))

    bodies = [(s["ref"], sheet.body_bbox(s)) for s in sheet.symbols if not s["power"]]

    def hits(a, b, pad=0.15):
        return a[0] < b[2] - pad and a[2] > b[0] + pad and a[1] < b[3] - pad and a[3] > b[1] + pad

    problems = []
    used = set()
    for text, owner, kind in wanted:
        if not text:
            continue
        if isinstance(owner, dict):
            ox, oy = owner["x"], owner["y"]
        else:
            ox, oy = owner
        best, bd = None, 1e9
        for i, (body, box, rot, (tx, ty)) in enumerate(drawn):
            if body == text and i not in used:
                d = (tx - ox) ** 2 + (ty - oy) ** 2
                if d < bd:
                    best, bd = i, d
        if best is None or bd > 30 ** 2:
            continue
        used.add(best)
        _b, box, rot, _xy = drawn[best]
        who = f"{kind} '{text}'" + (f" of {owner['ref']}" if isinstance(owner, dict) else "")
        if rot and kind in ("reference", "value"):
            problems.append(f"{who} is drawn {rot}")
        for ref, bb in bodies:
            if hits(box, bb):
                problems.append(f"{who} overlaps {ref}'s body")
        for x1, y1, x2, y2 in sheet.segments:
            seg = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            seg = (seg[0] - 0.01, seg[1] - 0.01, seg[2] + 0.01, seg[3] + 0.01)
            if kind == "label" and _on_segment(owner, (x1, y1, x2, y2)):
                continue       # a label sits on its own wire by design
            if hits(box, seg, pad=0.0):
                problems.append(f"{who} lies on a wire at ({fmt(x1)}, {fmt(y1)})-({fmt(x2)}, {fmt(y2)})")
                break
        for x1, y1, x2, y2 in sheet.buses:
            if kind == "label" and _on_segment(owner, (x1, y1, x2, y2)):
                continue       # a bus's own name sits on it
            seg = (min(x1, x2) - 0.2, min(y1, y2) - 0.2, max(x1, x2) + 0.2, max(y1, y2) + 0.2)
            if hits(box, seg, pad=0.0):
                problems.append(f"{who} lies on a bus at ({fmt(x1)}, {fmt(y1)})-({fmt(x2)}, {fmt(y2)})")
                break
    return sorted(set(problems))


def _sanitize_sheet_label(title):
    """Match kicad-cli's filename sanitization for a sheet title, so a title
    that isn't filesystem-safe (e.g. "Power / Reset Control", a legitimate
    sheet name) can still be matched back from the exported SVG's filename,
    where kicad-cli has already replaced each such character with "_"."""
    for ch in '\\/:*?"<>|':
        title = title.replace(ch, "_")
    return title


def render(root_sch, outdir, cli=None):
    """Export every page as SVG (short names p1.svg, p2.svg...) plus one PDF,
    and drop a pan/zoom viewer (view.html) next to them."""
    os.makedirs(outdir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, "*.svg")):
        os.remove(f)
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run([_cli(cli), "sch", "export", "svg", "-o", tmp, root_sch],
                       capture_output=True, text=True)
        base = os.path.splitext(os.path.basename(root_sch))[0]
        files = sorted(glob.glob(os.path.join(tmp, "*.svg")), key=os.path.getmtime)
        files.sort(key=lambda f: os.path.basename(f) != f"{base}.svg")   # root first
        pages = []
        for i, f in enumerate(files, 1):
            dst = os.path.join(outdir, f"p{i}.svg")
            shutil.move(f, dst)
            label = os.path.basename(f)[len(base):].lstrip("-")[:-4] or "root"
            pages.append((dst, label))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run([_cli(cli), "sch", "export", "pdf", "-o",
                    os.path.join(outdir, "schematic.pdf"), root_sch],
                   capture_output=True, text=True)
    viewer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "view.html")
    if os.path.exists(viewer):
        shutil.copy(viewer, os.path.join(outdir, "view.html"))
    with open(os.path.join(outdir, "pages.txt"), "w", encoding="utf-8") as f:
        for dst, label in pages:
            f.write(f"{os.path.basename(dst)}\t{label}\n")
    return pages
