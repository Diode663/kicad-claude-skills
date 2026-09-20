#!/usr/bin/env python3
"""Netlist-driven PCB placement: recipes -> blocks -> floor plan -> board.

    python autoplace.py PROJECT_DIR --selftest       # prove the geometry model on this board
    python autoplace.py PROJECT_DIR --audit          # score the board's current placement
    python autoplace.py PROJECT_DIR --floorplan      # propose; writes floorplan.svg/.json, board untouched
    python autoplace.py PROJECT_DIR --place          # place, link, check, save, DRC, render

Run with KiCad's Python. PROJECT_DIR must hold placement_config.py (see
references/autoplace.md for every key). Locked footprints (KiCad's lock flag)
and parts in FIXED never move; everything else is re-placed on every run.

How it works
  1. Model: every footprint's pads and courtyard in its own unrotated frame,
     read from the board itself; --selftest proves the rotate/flip maths
     against the real pad positions before anything is trusted.
  2. Blocks: each IC/connector is matched to a recipe (scripts/recipes.py) by
     pin names. Roles claim support parts through the netlist -- "the cap
     between VIN and GND" -- in priority order across the whole board.
     Leftover passives join the block they share the most local net with.
  3. Block solve: inside each block the anchor sits at the origin and members
     are placed one by one, closest-first in priority order, at the position
     that minimises pad-to-pin distance without overlapping.
  4. Board solve: fixed parts and keep-outs go in first. Blocks are then
     placed as rigid units, most-connected first, at the position/rotation/
     side with the lowest weighted wirelength to what is already placed,
     then improved by re-placing each block against all the others.
  5. Audit: every role is measured on the result against its recipe limit.
"""
import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcbnew  # noqa: E402
from pcblib import Board, say  # noqa: E402
import recipes as R  # noqa: E402

PASSIVE = {"C", "R", "L", "D", "FB", "Y"}


# ===========================================================================
# geometry
# ===========================================================================
class Pad:
    __slots__ = ("num", "cx", "cy", "x0", "y0", "x1", "y1", "th")

    def __init__(self, num, cx, cy, x0, y0, x1, y1, th):
        self.num, self.cx, self.cy = num, cx, cy
        self.x0, self.y0, self.x1, self.y1, self.th = x0, y0, x1, y1, th


# Conventions, confirmed by selftest() against real pad positions:
# a bottom-side footprint is mirrored in X about its origin, then rotated by
# its orientation; KiCad's positive rotation turns +X toward -Y (Y is down).
BOTTOM_MIRROR = "y"      # proven on pcbnew 10.0.6 with FLIP_DIRECTION_LEFT_RIGHT
ROT_SIGN = 1


def xform(lx, ly, x, y, rot, bottom):
    if bottom:
        if BOTTOM_MIRROR == "x":
            lx = -lx
        else:
            ly = -ly
    a = math.radians(rot) * ROT_SIGN
    c, s = math.cos(a), math.sin(a)
    return x + lx * c + ly * s, y - lx * s + ly * c


def xrect(r, x, y, rot, bottom):
    pts = [xform(px, py, x, y, rot, bottom) for px, py in
           ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3]))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def overlap(a, b, gap=0.0):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def union(rs):
    return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))


def parse_cap(v):
    m = re.match(r"\s*([\d.]+)\s*([pnuµm]?)F?", v or "", re.I)
    if not m:
        return None
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}[m.group(2).lower()]
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return None


def kind_of(ref):
    m = re.match(r"[A-Za-z]+", ref)
    p = m.group(0).upper() if m else "?"
    for k in ("SW", "FB", "TP"):
        if p.startswith(k):
            return k
    return p[0]


# ===========================================================================
# model
# ===========================================================================
# Seated height in mm, by package, as a *maximum* over the common parts in
# that package -- a zone under a card is only safe if every part that may end
# up there fits, not the typical one. Where a footprint or its 3D model names
# its own height (easyeda2kicad writes "...-H0.6-..."), that number wins.
PACKAGE_HEIGHT = {
    "0201": 0.35, "0402": 0.55, "0603": 0.95, "0805": 1.25, "1206": 1.60,
    "1210": 2.00, "1812": 2.00, "2010": 2.00, "2512": 2.00,
    "SOD-523": 0.60, "SOD-323": 1.10, "SOD-123": 1.35, "SOT-23": 1.45,
    "SOT-25": 1.45, "SOT-26": 1.45, "SOT-323": 1.10, "SOT-353": 1.10,
    "SOT-363": 1.10, "USON": 0.60, "DFN": 0.80, "QFN": 1.00, "SOIC": 1.75,
    "SSOP": 2.00, "TSSOP": 1.20, "VSON": 0.80,
}
# easyeda2kicad spells a known height into the name as "-H0.6-".
# The number has to be a whole token: without the lookahead this
# matched the "H5007" in XFMR-SMD_H5007NLT -- a Pulse part number --
# and called the transformer 5007 mm tall. And a height outside a
# plausible range is a part number too, so treat it as unknown
# rather than as a measurement.
_H_IN_NAME = re.compile(r"[-_]H(\d{1,2}(?:\.\d+)?)(?=[-_]|$)")
_H_MAX = 25.0      # mm; nothing on a board this size is taller


def part_height(p):
    """Seated height in mm, or None when nothing says.

    None is not zero: a part whose height is unknown is refused by a
    height-limited zone rather than assumed to fit."""
    for name in (getattr(p, "model", "") or "", getattr(p, "footprint", "") or ""):
        m = _H_IN_NAME.search(name)
        if m and 0.0 < float(m.group(1)) <= _H_MAX:
            return float(m.group(1))
    fp = (getattr(p, "footprint", "") or "").upper()
    for key, h in sorted(PACKAGE_HEIGHT.items(), key=lambda kv: -len(kv[0])):
        if key in fp:
            return h
    return None


class Part:
    def __init__(self, ref):
        self.ref, self.kind = ref, kind_of(ref)
        self.value, self.pads, self.pin_names, self.nets = "", {}, {}, {}
        self.rect = (0, 0, 0, 0)
        self.x = self.y = self.rot = 0.0
        self.bottom = False
        self.locked = False
        self.cap = None
        self.has_courtyard = False
        self.footprint = ""
        self.model = ""

    @property
    def two_pin(self):
        return len({n for n in self.nets.values() if n}) <= 3 and self.kind in PASSIVE

    def netset(self):
        return {n for n in self.nets.values() if n}

    def pads_on(self, net):
        return [p for num, p in self.pads.items() if self.nets.get(num) == net]


class Model:
    def __init__(self, board):
        self.b = board
        root = board._netlist_root()
        libparts = {(lp.get("lib"), lp.get("part")): lp for lp in root.iter("libpart")}
        comps = {c.get("ref"): c for c in root.iter("comp")}
        self.nets = {}
        for n in root.iter("net"):
            self.nets[n.get("name")] = [(x.get("ref"), x.get("pin")) for x in n.findall("node")]
        self.parts = {}
        for ref, fp in board.fps.items():
            if ref not in comps:
                continue
            p = Part(ref)
            c = comps[ref]
            p.value = c.findtext("value") or ""
            p.footprint = c.findtext("footprint") or ""
            try:
                models = [m.GetFileName() for m in fp.Models()]
                p.model = models[0] if models else ""
            except Exception:
                p.model = ""
            sp = c.find("sheetpath")
            p.sheet = sp.get("names") if sp is not None else "/"
            ls = c.find("libsource")
            lp = libparts.get((ls.get("lib"), ls.get("part"))) if ls is not None else None
            if lp is not None:
                p.pin_names = {pin.get("num"): pin.get("name") or "" for pin in lp.iter("pin")}
            self._read_geometry(fp, p)
            p.cap = parse_cap(p.value) if p.kind == "C" else None
            self.parts[ref] = p
        for name, nodes in self.nets.items():
            for ref, pin in nodes:
                if ref in self.parts:
                    self.parts[ref].nets[pin] = name
        self.ground = {n for n, nodes in self.nets.items() if self._is_ground(n, nodes)}

    def _is_ground(self, name, nodes):
        if R.GROUND_RE.search(name):
            return True
        names = [self.parts[r].pin_names.get(p, "") for r, p in nodes if r in self.parts]
        return bool(names) and all(re.match(r"^(A|D|P)?GND|^VSS", n or "", re.I) for n in names)

    def _read_geometry(self, fp, p):
        mm = pcbnew.ToMM
        p.locked = fp.IsLocked()
        pos = fp.GetPosition()
        p.bottom = fp.IsFlipped()
        p.rot = fp.GetOrientationDegrees() % 360
        p.x, p.y = self.b.rel(pos)
        # normalise to the unrotated, unflipped frame, read, then restore
        if p.bottom:
            fp.Flip(pos, pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(0)
        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
        rects = []
        p.padlist = []
        for pad in fp.Pads():
            bb = pad.GetBoundingBox()
            c = pad.GetPosition()
            th = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
            pd = Pad(pad.GetNumber(), mm(c.x), mm(c.y), mm(bb.GetX()), mm(bb.GetY()),
                     mm(bb.GetRight()), mm(bb.GetBottom()), th)
            p.padlist.append(pd)
            if pad.GetNumber() and pad.GetNumber() not in p.pads:
                p.pads[pad.GetNumber()] = pd
            elif not pad.GetNumber():
                p.pads["_np%d" % len(p.pads)] = pd
            rects.append((pd.x0, pd.y0, pd.x1, pd.y1))
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        if cy.OutlineCount():
            bb = cy.BBox()
            rects.append((mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetRight()), mm(bb.GetBottom())))
            p.has_courtyard = True
        p.rect = union(rects) if rects else (-0.5, -0.5, 0.5, 0.5)
        if p.bottom:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetPosition(pos)
        fp.SetOrientationDegrees(p.rot)

    def pin_nets(self, part, pattern):
        """nets of the anchor pins whose names match a role pin regex"""
        rx = re.compile(pattern, re.I)
        out = {}
        for num, name in part.pin_names.items():
            if rx.search(name or "") and part.nets.get(num):
                out.setdefault(part.nets[num], []).append(num)
        # symbols whose pins are just numbers (connectors): match on the number
        if not out:
            for num, net in part.nets.items():
                if net and rx.search(num):
                    out.setdefault(net, []).append(num)
        return out


def selftest(board, model):
    """Prove xform() reproduces KiCad's own pad positions for every footprint."""
    global BOTTOM_MIRROR, ROT_SIGN
    actual = {}
    for ref, fp in board.fps.items():
        actual[ref] = [board.rel(pd.GetPosition()) for pd in fp.Pads()]
    results, per_part = {}, {}
    # "y" first: with no bottom-side parts both mirrors score 0, and the tie
    # must fall to the convention already proven on a double-sided board
    for mirror in ("y", "x"):
        for sign in (1, -1):
            BOTTOM_MIRROR, ROT_SIGN = mirror, sign
            worst, parts_err = 0.0, {}
            for ref, p in model.parts.items():
                act = actual.get(ref, [])
                for i, pad in enumerate(p.padlist):
                    if i >= len(act):
                        break
                    ex, ey = xform(pad.cx, pad.cy, p.x, p.y, p.rot, p.bottom)
                    e = math.hypot(ex - act[i][0], ey - act[i][1])
                    parts_err[ref] = max(parts_err.get(ref, 0.0), e)
                    worst = max(worst, e)
            results[(mirror, sign)] = worst
            per_part[(mirror, sign)] = parts_err
    best = min(results, key=results.get)
    BOTTOM_MIRROR, ROT_SIGN = best
    if results[best] > 0.01:
        errs = sorted(per_part[best].items(), key=lambda t: -t[1])[:8]
        for ref, e in errs:
            p = model.parts[ref]
            say("   %-6s err %.3f mm  rot %.1f %s" % (ref, e, p.rot, "bottom" if p.bottom else "top"))
        for k, v in results.items():
            say("   convention %s: worst %.3f" % (k, v))
    n_bot = sum(1 for p in model.parts.values() if p.bottom)
    say("selftest: bottom mirror=%s rotation sign=%+d, worst pad error %.4f mm over %d parts (%d on bottom)"
        % (best[0], best[1], results[best], len(model.parts), n_bot))
    if results[best] > 0.01:
        raise SystemExit("selftest FAILED: geometry model does not match KiCad (%.3f mm)" % results[best])
    if n_bot == 0:
        say("  note: no bottom-side parts on this board, so the flip convention is unproven")
    return results[best]


# ===========================================================================
# blocks
# ===========================================================================
class Member:
    def __init__(self, part, role, recipe, targets):
        self.part, self.role, self.recipe, self.targets = part, role, recipe, targets
        self.lx = self.ly = self.lrot = 0.0


class Block:
    def __init__(self, anchor, recipe):
        self.anchor, self.recipe = anchor, recipe
        self.members = []          # Member, anchor excluded
        self.name = anchor.ref
        self.rects = []            # (ref, rect in block frame)
        self.tags = set(recipe.tags) if recipe else set()

    def refs(self):
        return [self.anchor.ref] + [m.part.ref for m in self.members]


def _ref_hint(anchor_ref, part_ref):
    a = re.sub(r"^[A-Z]+", "", anchor_ref.upper())
    body = re.sub(r"^[A-Z]", "", part_ref.upper())
    full = anchor_ref.upper()
    best = 0
    for s in (full, a):
        for i in range(len(s)):
            for j in range(i + 2, len(s) + 1):
                if s[i:j] in body and (j - i) > best:
                    best = j - i
    return best


def build_blocks(model, cfg):
    parts = model.parts
    recipes = list(getattr(cfg, "EXTRA_RECIPES", [])) + R.RECIPES
    anchors = sorted((p for p in parts.values() if p.kind not in PASSIVE or p.kind == "Y" and False),
                     key=lambda p: (-len(p.pads), p.ref))
    anchors = [p for p in anchors if p.kind in {"U", "Q", "J", "T", "SW", "Y"} or p.kind not in PASSIVE]
    recipe_of = {}
    for p in anchors:
        for rc in recipes:
            if rc.matches(p):
                recipe_of[p.ref] = rc
                break
    for p in anchors:
        if p.ref not in recipe_of:
            for rc in R.FALLBACKS:
                if rc.matches(p):
                    recipe_of[p.ref] = rc
                    break

    owner = {}                          # part ref -> (anchor ref, role name)
    assigned = {a.ref: {} for a in anchors}   # anchor -> role -> [refs]
    claimed_anchor = set()

    def role_targets(anchor, role, part):
        """(net, [(target kind, ref, pad nums)], weight) for this candidate, or None"""
        a_nets = _side_nets(anchor, role.a, assigned[anchor.ref])
        if a_nets is None:
            return None
        pn = part.netset()
        hit_a = [n for n in a_nets if n in pn and n not in model.ground]
        if role.a == "GND":
            hit_a = [n for n in pn if n in model.ground]
        if not hit_a:
            return None
        rest = pn - set(hit_a)
        if role.b == "*":
            hit_b = []
        elif role.b == "GND":
            hit_b = [n for n in rest if n in model.ground]
            if not hit_b:
                return None
        else:
            b_nets = _side_nets(anchor, role.b, assigned[anchor.ref])
            if b_nets is None:
                return None
            hit_b = [n for n in rest if n in b_nets]
            if not hit_b:
                return None
        return hit_a, hit_b

    def _side_nets(anchor, spec, roles_done):
        if spec in ("*", "GND"):
            return set()
        if spec.startswith("role:"):
            rname = spec[5:]
            refs = roles_done.get(rname)
            if not refs:
                return None
            nets = set()
            for r in refs:
                nets |= parts[r].netset()
            # the other side of that role's part: every net it has except the anchor's own
            return {n for n in nets if n not in anchor.netset() and n not in model.ground}
        return set(model.pin_nets(anchor, spec))

    def eligible(part, role, anchor):
        if part.ref == anchor.ref or part.ref in owner or part.ref in claimed_anchor:
            return False
        if part.locked and False:
            return False
        if role.kind == "*":
            if part.kind not in PASSIVE:
                return False
        elif part.kind != role.kind:
            return False
        if part.kind in PASSIVE and len(part.pads) > 8:
            return False
        if part.kind not in PASSIVE and role.kind != part.kind:
            return False
        return True

    # nets each schematic sheet's anchors have pins on: a rail capacitor drawn
    # on a sheet belongs to an IC on that sheet if one uses the rail
    sheet_nets = {}
    for a in anchors:
        sheet_nets.setdefault(getattr(a, "sheet", "/"), set()).update(a.netset())

    def run_pass(recipe_filter, min_hint=0):
        prios = sorted({r.prio for a in anchors if a.ref in recipe_of for r in recipe_of[a.ref].roles})
        for prio in prios:
            cands = []
            for a in anchors:
                if a.ref in claimed_anchor:
                    continue
                rc = recipe_of.get(a.ref)
                if rc is None or not recipe_filter(rc):
                    continue
                for role in rc.roles:
                    if role.prio != prio:
                        continue
                    for part in parts.values():
                        if not eligible(part, role, a):
                            continue
                        hit = role_targets(a, role, part)
                        if not hit:
                            continue
                        hit_a, hit_b = hit
                        if rc in R.FALLBACKS:
                            # generic roles only take parts on local nets, or
                            # decoupling on a net this IC actually has a pin on
                            big = [n for n in hit_a if len(model.nets[n]) > 6]
                            if role.name == "local_passive" and big:
                                continue
                        hint = _ref_hint(a.ref, part.ref)
                        if hint < min_hint:
                            continue
                        big = [n for n in hit_a + hit_b if n not in model.ground and len(model.nets[n]) > 6]
                        psheet, asheet = getattr(part, "sheet", "/"), getattr(a, "sheet", "/")
                        if big and psheet != asheet and hint < 2 and \
                                any(n in sheet_nets.get(psheet, ()) for n in big):
                            continue
                        cap = part.cap or 0
                        pref = (-cap if role.prefer == "large" else cap) if role.prefer else 0
                        local = min(len(model.nets[n]) for n in hit_a)
                        cands.append(((-hint, pref, local, part.ref), a, role, part, hit_a, hit_b))
            cands.sort(key=lambda c: c[0])
            for _k, a, role, part, hit_a, hit_b in cands:
                if part.ref in owner or a.ref in claimed_anchor:
                    continue
                taken = assigned[a.ref].setdefault(role.name, [])
                limit = role.count
                if limit == 0 and role.b == "GND":
                    # "all" decoupling means one cap per anchor pin on that net
                    limit = sum(len(model.pin_nets(a, role.a).get(n, [])) for n in hit_a) or 1
                    same = [r for r in taken if set(hit_a) & parts[r].netset()]
                    if len(same) >= limit:
                        continue
                elif limit and len(taken) >= limit:
                    continue
                owner[part.ref] = (a.ref, role.name, hit_a, hit_b)
                taken.append(part.ref)
                if part.kind not in PASSIVE:
                    claimed_anchor.add(part.ref)

    def rc_is_generic(rc, role):
        return rc in R.FALLBACKS and role.name.startswith("c_decouple")

    run_pass(lambda rc: rc not in R.FALLBACKS, min_hint=2)   # designator affinity first (CU33O1 -> U331)
    run_pass(lambda rc: rc not in R.FALLBACKS)
    run_pass(lambda rc: rc in R.FALLBACKS)

    # leftovers: passives nobody claimed join the block they share the most
    # local net with
    leftovers = [p for p in parts.values() if p.kind in PASSIVE and p.ref not in owner]
    anchor_refs = [a.ref for a in anchors if a.ref not in claimed_anchor]
    for p in sorted(leftovers, key=lambda q: q.ref):
        best = None
        for net in p.netset():
            if net in model.ground:
                continue
            size = len(model.nets[net])
            for ref, _pin in model.nets[net]:
                home = ref if ref in anchor_refs else (owner[ref][0] if ref in owner else None)
                if home is None or home == p.ref:
                    continue
                pin_name = parts[ref].pin_names.get(_pin, "") if ref == home else ""
                source = 0 if re.search(r"^(SYS|OUT|VOUT|VBUS)$", pin_name or "", re.I) else 1
                other_sheet = getattr(parts[home], "sheet", "/") != getattr(p, "sheet", "/")
                key = (other_sheet, size, source, home)
                if best is None or key < best[0]:
                    best = (key, home, net)
        if best:
            owner[p.ref] = (best[1], "attached", [best[2]], [])
            assigned[best[1]].setdefault("attached", []).append(p.ref)

    blocks = {}
    for a in anchors:
        if a.ref in claimed_anchor:
            continue
        blocks[a.ref] = Block(a, recipe_of.get(a.ref))
    for ref, (home, role_name, hit_a, hit_b) in owner.items():
        if home not in blocks:
            continue
        blk = blocks[home]
        rc = blk.recipe
        role = next((r for r in rc.roles if r.name == role_name), None) if rc else None
        if role is None:
            role = R.Role(role_name, "*", "*", max_mm=6.0, prio=9)
        blk.members.append(Member(parts[ref], role, rc, (hit_a, hit_b)))
    # orphan passives with no block at all become their own block
    for p in parts.values():
        if p.ref not in owner and p.ref not in blocks and p.ref not in claimed_anchor:
            blocks[p.ref] = Block(p, None)
    for blk in blocks.values():
        blk.members.sort(key=lambda m: (m.role.prio, m.part.ref))
    return blocks, owner


# ===========================================================================
# placement search
# ===========================================================================
class _Probe:
    """A stand-in part for area accounting: allowed only where anything is."""
    ref, kind, rect = "_probe", "?", (0, 0, 10, 10)


_PROBE = _Probe()


class Occupancy:
    """Rectangles per side, bucketed on a coarse grid for fast queries."""
    CELL = 4.0

    def __init__(self):
        self.cells = {False: {}, True: {}}

    def _keys(self, r):
        c = self.CELL
        for i in range(int(math.floor(r[0] / c)), int(math.floor(r[2] / c)) + 1):
            for j in range(int(math.floor(r[1] / c)), int(math.floor(r[3] / c)) + 1):
                yield (i, j)

    def add(self, bottom, rect, tag):
        for k in self._keys(rect):
            self.cells[bottom].setdefault(k, []).append((rect, tag))

    def remove_tag(self, tag):
        for side in self.cells.values():
            for k, lst in side.items():
                side[k] = [e for e in lst if e[1] != tag]

    def hits(self, bottom, rect, gap):
        seen = set()
        for k in self._keys(grow(rect, gap)):
            for r, tag in self.cells[bottom].get(k, ()):
                if id(r) in seen:
                    continue
                seen.add(id(r))
                if overlap(r, rect, gap):
                    return tag
        return None


def part_shapes(part, x, y, rot, bottom):
    """[(side, rect)] occupied by a part: body on its side, through-hole pads on both."""
    body = xrect(part.rect, x, y, rot, bottom)
    out = [(bottom, body)]
    for pad in part.pads.values():
        if pad.th:
            out.append((not bottom, xrect((pad.x0, pad.y0, pad.x1, pad.y1), x, y, rot, bottom)))
    return out


def pad_xy(part, num, x, y, rot, bottom):
    pd = part.pads[num]
    return xform(pd.cx, pd.cy, x, y, rot, bottom)


class Solver:
    def __init__(self, model, blocks, cfg):
        self.m, self.blocks, self.cfg = model, blocks, cfg
        self.W, self.H = cfg.BOARD_SIZE
        self.part_gap = getattr(cfg, "PART_GAP", 0.25)
        self.block_gap = getattr(cfg, "BLOCK_GAP", 0.6)
        self.edge = getattr(cfg, "EDGE_MARGIN", 0.5)
        self.pos = {}                  # ref -> (x, y, rot, bottom)
        self.occ = Occupancy()
        self.keepouts = []             # (bottom, rect, allow(part) -> bool, name)
        self.net_pts = {}              # net -> [x0, y0, x1, y1] of placed pads
        self.weights = {}
        self.log = []
        self._net_weights()

    # -- nets -------------------------------------------------------------
    def _net_weights(self):
        extra = [(re.compile(k, re.I), w) for k, w in getattr(self.cfg, "NET_WEIGHTS", {}).items()]
        for name, nodes in self.m.nets.items():
            w = 1.0
            short = name.split("/")[-1]
            pins = [self.m.parts[r].pin_names.get(p, "") for r, p in nodes if r in self.m.parts]
            if name in self.m.ground:
                w = 0.0
            elif any(re.match(r"^SW\d?$", pn or "", re.I) for pn in pins):
                w = 4.0
            elif re.search(r"(_P|_N|_DP|_DM|DP\d?|DM\d?|D\+|D-|TD\d?[PM]|TRD\d_[PN]|_RX|_TX)$", short, re.I):
                w = 3.0
            elif re.match(r"^\+|VBUS|VSYS|PMID|VBAT|BAT_|3V3|5V|1V8|VCC|VDD", short, re.I) or len(nodes) > 10:
                w = 0.3
            for rx, ew in extra:
                if rx.search(name):
                    w = ew
            self.weights[name] = w

    def _add_net_pts(self, ref):
        p = self.m.parts[ref]
        x, y, rot, bottom = self.pos[ref]
        for num, net in p.nets.items():
            if not net or not self.weights.get(net) or num not in p.pads:
                continue
            px, py = pad_xy(p, num, x, y, rot, bottom)
            bb = self.net_pts.get(net)
            self.net_pts[net] = [px, py, px, py] if bb is None else [min(bb[0], px), min(bb[1], py), max(bb[2], px), max(bb[3], py)]

    def _rebuild_net_pts(self):
        self.net_pts = {}
        for ref in self.pos:
            self._add_net_pts(ref)

    # -- occupancy --------------------------------------------------------
    def commit(self, ref, x, y, rot, bottom, tag, gap=None):
        p = self.m.parts[ref]
        self.pos[ref] = (x, y, rot, bottom)
        g = (self.block_gap if gap is None else gap) / 2.0
        for side, r in part_shapes(p, x, y, rot, bottom):
            self.occ.add(side, grow(r, g), tag)
        self._add_net_pts(ref)

    def feasible(self, part, x, y, rot, bottom, gap, fixed_ok=False):
        for side, r in part_shapes(part, x, y, rot, bottom):
            if not fixed_ok and side == bottom and (r[0] < self.edge or r[1] < self.edge or
                                                    r[2] > self.W - self.edge or r[3] > self.H - self.edge):
                return False
            if self.occ.hits(side, grow(r, gap / 2.0), 0.0):
                return False
            if side == bottom:
                for kb, kr, allow, _n in self.keepouts:
                    if kb == side and overlap(kr, r) and not allow(part):
                        return False
        return True

    # -- fixed things ------------------------------------------------------
    def setup(self):
        cfg = self.cfg
        hk = getattr(cfg, "HOLE_KEEPOUT", 6.0) / 2.0
        for hx, hy, _d in getattr(cfg, "HOLES", []):
            for side in (False, True):
                self.occ.add(side, (hx - hk, hy - hk, hx + hk, hy + hk), "hole")
        fixed = dict(getattr(cfg, "FIXED", {}))
        for ref, p in self.m.parts.items():
            if p.locked and ref not in fixed:
                fixed[ref] = (p.x, p.y, p.rot, "bottom" if p.bottom else "top")
                self.log.append("locked in KiCad, kept: %s" % ref)
        self.fixed = {}
        for ref, spec in fixed.items():
            if ref not in self.m.parts:
                raise SystemExit("FIXED names %s, which is not on the board" % ref)
            x, y, rot = spec[0], spec[1], spec[2]
            bottom = (spec[3] if len(spec) > 3 else "top") == "bottom"
            self.fixed[ref] = (x, y, rot, bottom)
            self.commit(ref, x, y, rot, bottom, "fixed:" + ref, gap=self.part_gap)
        for k in getattr(cfg, "KEEPOUTS", []):
            allow = k.get("allow", "none")
            if allow == "none":
                fn = (lambda p: False)
            elif allow == "low_passives":
                fn = (lambda p: p.kind in {"C", "R", "FB"} and (p.rect[2] - p.rect[0]) < 4.0 and (p.rect[3] - p.rect[1]) < 4.0)
            elif allow == "by_height":
                # the zone states a clearance; a part is allowed only if its
                # known height fits under it
                limit = float(k["max_height"])
                fn = (lambda p, lim=limit: (part_height(p) or 99.0) <= lim)
            elif callable(allow):
                fn = allow
            else:
                refs = set(allow)
                fn = (lambda p, refs=refs: p.ref in refs)
            self.keepouts.append((k["side"] == "bottom", tuple(k["rect"]), fn, k["name"]))
            if k.get("max_height"):
                allowed = [p.ref for p in self.m.parts.values()
                           if (part_height(p) or 99.0) <= float(k["max_height"])]
                say("  %s: %.2f mm clearance admits %d part(s)"
                    % (k["name"], float(k["max_height"]), len(allowed)))
        self._module_keepouts()

    def _module_keepouts(self):
        spec = R.CM4_MODULE
        conns = [p for p in self.m.parts.values()
                 if len(p.pads) >= spec["connector_pins"] and re.search(spec["connector_value"], p.value + p.ref, re.I)
                 and p.ref in self.fixed]
        if len(conns) != 2:
            return
        a, b = conns
        if self.fixed[a.ref][3] != self.fixed[b.ref][3]:
            return
        bottom = self.fixed[a.ref][3]
        centres, pin1s = [], []
        for c in conns:
            x, y, rot, bt = self.fixed[c.ref]
            centres.append((x, y))
            if "1" in c.pads:
                pin1s.append(pad_xy(c, "1", x, y, rot, bt))
        mx, my = (centres[0][0] + centres[1][0]) / 2, (centres[0][1] + centres[1][1]) / 2
        dx, dy = centres[1][0] - centres[0][0], centres[1][1] - centres[0][1]
        horiz_rows = abs(dy) > abs(dx)          # connectors stacked in Y -> rows run along X
        shift = spec["centre_shift_along"]
        if pin1s:
            if horiz_rows:
                sgn = 1 if sum(p[0] for p in pin1s) / len(pin1s) > mx else -1
                mx += sgn * shift
                rect = (mx - spec["along"] / 2, my - spec["across"] / 2, mx + spec["along"] / 2, my + spec["across"] / 2)
            else:
                sgn = 1 if sum(p[1] for p in pin1s) / len(pin1s) > my else -1
                my += sgn * shift
                rect = (mx - spec["across"] / 2, my - spec["along"] / 2, mx + spec["across"] / 2, my + spec["along"] / 2)
            refs = {a.ref, b.ref}
            self.keepouts.append((bottom, rect, lambda p, refs=refs: p.ref in refs, spec["name"] + " module"))
            self.log.append("%s outline %.1f,%.1f-%.1f,%.1f on %s: no footprints under the module"
                            % (spec["name"], rect[0], rect[1], rect[2], rect[3], "bottom" if bottom else "top"))

    def capacity_report(self, free_blocks):
        """Free board area per side against what the unplaced parts need."""
        step = 0.5
        free = {False: 0.0, True: 0.0}
        for bottom in (False, True):
            y = self.edge
            while y < self.H - self.edge:
                x = self.edge
                while x < self.W - self.edge:
                    cell = (x, y, x + step, y + step)
                    if not self.occ.hits(bottom, cell, 0.0) and not any(
                            kb == bottom and overlap(kr, cell) and not fn(_PROBE) for kb, kr, fn, _n in self.keepouts):
                        free[bottom] += step * step
                    x += step
                y += step
        need = 0.0
        for blk in free_blocks:
            for ref in blk.refs():
                r = self.m.parts[ref].rect
                need += (r[2] - r[0] + self.part_gap) * (r[3] - r[1] + self.part_gap)
        msg = ("capacity: free %.0f mm2 top + %.0f mm2 bottom = %.0f mm2; unfixed parts need %.0f mm2 "
               "of courtyard (%.0f%% of free, before routing space)"
               % (free[False], free[True], free[False] + free[True], need, 100.0 * need / max(1.0, free[False] + free[True])))
        self.log.append(msg)
        self.capacity = dict(free_top=free[False], free_bottom=free[True], need=need)

    # -- sides ------------------------------------------------------------
    def sides_for(self, blk):
        rules = getattr(self.cfg, "SIDE_RULES", {})
        for ref in blk.refs():
            if ref in rules:
                return [s == "bottom" for s in rules[ref]]
        if blk.anchor.ref in self.fixed:
            return [self.fixed[blk.anchor.ref][3]]
        return [s == "bottom" for s in getattr(self.cfg, "DEFAULT_SIDES", ["top"])]

    # -- one part near its targets ------------------------------------------
    def _member_cost(self, member, x, y, rot, bottom, frame_pos):
        """distance of this member's pads to its targets, given placed positions in frame_pos"""
        part = member.part
        hit_a, hit_b = member.targets
        cost = 0.0
        for nets, w in ((hit_a, 1.0), (hit_b, 0.35)):
            for net in nets:
                mine = [n for n, nn in part.nets.items() if nn == net and n in part.pads]
                if not mine:
                    continue
                best = None
                for ref, (tx, ty, trot, tb) in frame_pos.items():
                    tp = self.m.parts[ref]
                    for num, nn in tp.nets.items():
                        if nn != net or num not in tp.pads:
                            continue
                        qx, qy = pad_xy(tp, num, tx, ty, trot, tb)
                        for mnum in mine:
                            px, py = pad_xy(part, mnum, x, y, rot, bottom)
                            d = math.hypot(px - qx, py - qy)
                            if best is None or d < best:
                                best = d
                if best is not None:
                    w_eff = w * (0.5 if net in self.m.ground else 1.0)
                    cost += w_eff * best
        return cost

    def _target_centre(self, member, frame_pos):
        pts = []
        for net in member.targets[0]:
            for ref, (tx, ty, trot, tb) in frame_pos.items():
                tp = self.m.parts[ref]
                for num, nn in tp.nets.items():
                    if nn == net and num in tp.pads:
                        pts.append(pad_xy(tp, num, tx, ty, trot, tb))
        if not pts:
            a = next(iter(frame_pos.values()))
            return a[0], a[1]
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    def place_member(self, member, frame_pos, occ_check, window, anchor_xy, sides, compact=0.08):
        part = member.part
        best = None
        ax, ay = anchor_xy
        rots = (0, 90, 180, 270)
        for step, span in ((0.5, window), (0.1, 0.6)):
            if best is None and step == 0.1:
                break
            cx, cy = (ax, ay) if step == 0.5 else (best[1], best[2])
            n = int(span / step)
            rot_list = rots if step == 0.5 else (best[3],)
            for bottom in sides:
                for rot in rot_list:
                    for i in range(-n, n + 1):
                        for j in range(-n, n + 1):
                            x, y = cx + i * step, cy + j * step
                            if not occ_check(part, x, y, rot, bottom):
                                continue
                            c = self._member_cost(member, x, y, rot, bottom, frame_pos)
                            # pull the block together, and keep passives axis-aligned
                            c += compact * math.hypot(x - ax, y - ay)
                            c += 0.02 * (rot % 180 != 0)
                            if best is None or c < best[0] - 1e-9:
                                best = (c, x, y, rot, bottom)
        return best

    # -- block solve in its own frame --------------------------------------------
    def solve_block_local(self, blk):
        occ = Occupancy()
        a = blk.anchor
        frame = {a.ref: (0.0, 0.0, 0.0, False)}
        for side, r in part_shapes(a, 0, 0, 0, False):
            occ.add(side, grow(r, self.part_gap / 2), a.ref)
        blk.rects = [(a.ref, xrect(a.rect, 0, 0, 0, False))]

        def ok(part, x, y, rot, bottom):
            for side, r in part_shapes(part, x, y, rot, bottom):
                if occ.hits(side, grow(r, self.part_gap / 2), 0.0):
                    return False
            return True

        for mem in blk.members:
            # search around the pads this member serves (e.g. the inductor's
            # output pad for an output cap), not around the anchor's centre
            tx, ty = self._target_centre(mem, frame)
            window = mem.role.max_mm + max(mem.part.rect[2] - mem.part.rect[0], mem.part.rect[3] - mem.part.rect[1]) + 3.0
            best = None
            for grow_w in (1.0, 2.0, 3.5):
                best = self.place_member(mem, frame, ok, window * grow_w, (tx, ty), (False,))
                if best:
                    break
            if best is None:
                raise SystemExit("block %s: no room for %s" % (blk.name, mem.part.ref))
            _c, x, y, rot, bottom = best
            mem.lx, mem.ly, mem.lrot = x, y, rot
            frame[mem.part.ref] = (x, y, rot, False)
            for side, r in part_shapes(mem.part, x, y, rot, False):
                occ.add(side, grow(r, self.part_gap / 2), mem.part.ref)
            blk.rects.append((mem.part.ref, xrect(mem.part.rect, x, y, rot, False)))
        blk.bbox = union([r for _ref, r in blk.rects])

    # -- block on the board ------------------------------------------------------
    def block_pose_parts(self, blk, X, Y, R_, bottom):
        """board poses of every block part for a block pose"""
        out = {blk.anchor.ref: (X, Y, R_ % 360, bottom)}
        for m in blk.members:
            px, py = xform(m.lx, m.ly, X, Y, R_, bottom)
            rot = (R_ - m.lrot) % 360 if bottom else (R_ + m.lrot) % 360
            out[m.part.ref] = (px, py, rot, bottom)
        return out

    def block_cost(self, blk, poses):
        cost = 0.0
        pts = {}
        for ref, (x, y, rot, bottom) in poses.items():
            p = self.m.parts[ref]
            for num, net in p.nets.items():
                w = self.weights.get(net, 0)
                if not w or num not in p.pads:
                    continue
                px, py = pad_xy(p, num, x, y, rot, bottom)
                bb = pts.get(net)
                pts[net] = [px, py, px, py] if bb is None else [min(bb[0], px), min(bb[1], py), max(bb[2], px), max(bb[3], py)]
        for net, bb in pts.items():
            placed = self.net_pts.get(net)
            if placed is None:
                continue
            u = [min(bb[0], placed[0]), min(bb[1], placed[1]), max(bb[2], placed[2]), max(bb[3], placed[3])]
            cost += self.weights[net] * ((u[2] - u[0] + u[3] - u[1]) - (placed[2] - placed[0] + placed[3] - placed[1]))
        # radios keep away from switchers
        if "avoid_switching" in blk.tags or "switching" in blk.tags:
            other = "switching" if "avoid_switching" in blk.tags else "avoid_switching"
            ax, ay = poses[blk.anchor.ref][0], poses[blk.anchor.ref][1]
            for ob in self.blocks.values():
                if other in ob.tags and ob.anchor.ref in self.pos:
                    ox, oy = self.pos[ob.anchor.ref][0], self.pos[ob.anchor.ref][1]
                    d = math.hypot(ax - ox, ay - oy)
                    if d < 20.0:
                        cost += (20.0 - d) * 3.0
        side_cost = getattr(self.cfg, "SIDE_COST", {})
        if side_cost:
            bt = poses[blk.anchor.ref][3]
            cost += side_cost.get("bottom" if bt else "top", 0.0) * (1 + len(blk.members)) ** 0.5
        region = getattr(self.cfg, "REGIONS", {}).get(blk.name)
        if region:
            ax, ay = poses[blk.anchor.ref][0], poses[blk.anchor.ref][1]
            dx = max(region[0] - ax, 0, ax - region[2])
            dy = max(region[1] - ay, 0, ay - region[3])
            cost += 25.0 * (dx + dy)
        return cost

    def _envelope(self, blk, poses):
        bottom = poses[blk.anchor.ref][3]
        body = union([xrect(self.m.parts[r].rect, x, y, rot, bt) for r, (x, y, rot, bt) in poses.items()])
        th = [s for r, (x, y, rot, bt) in poses.items()
              for s in part_shapes(self.m.parts[r], x, y, rot, bt)[1:]]
        return bottom, body, th

    def _use_envelope(self, blk):
        return getattr(blk, "envelope", getattr(self.cfg, "BLOCK_ENVELOPE", True))

    def block_feasible(self, blk, poses):
        if not self._use_envelope(blk):
            for ref, (x, y, rot, bottom) in poses.items():
                if not self.feasible(self.m.parts[ref], x, y, rot, bottom, self.block_gap):
                    return False
            return True
        # the block reserves its whole bounding box, so blocks never interleave
        bottom, body, th = self._envelope(blk, poses)
        if body[0] < self.edge or body[1] < self.edge or body[2] > self.W - self.edge or body[3] > self.H - self.edge:
            return False
        if self.occ.hits(bottom, grow(body, self.block_gap / 2.0), 0.0):
            return False
        for side, r in th:
            if self.occ.hits(side, grow(r, self.part_gap / 2.0), 0.0):
                return False
        for ref, (x, y, rot, bt) in poses.items():
            part = self.m.parts[ref]
            r = xrect(part.rect, x, y, rot, bt)
            for kb, kr, allow, _n in self.keepouts:
                if kb == bt and overlap(kr, r) and not allow(part):
                    return False
        return True

    def place_block(self, blk):
        sides = self.sides_for(blk)
        cands = []
        step = 1.0
        for bottom in sides:
            for R_ in (0, 90, 180, 270):
                ex = union([xrect(r, 0, 0, R_, bottom) for _ref, r in blk.rects])
                xs = [self.edge - ex[0] + i * step for i in range(int((self.W - 2 * self.edge - (ex[2] - ex[0])) / step) + 1)]
                ys = [self.edge - ex[1] + j * step for j in range(int((self.H - 2 * self.edge - (ex[3] - ex[1])) / step) + 1)]
                for X in xs:
                    for Y in ys:
                        poses = self.block_pose_parts(blk, X, Y, R_, bottom)
                        c = self.block_cost(blk, poses) + 0.001 * (abs(X - self.W / 2) + abs(Y - self.H / 2))
                        cands.append((c, X, Y, R_, bottom))
        cands.sort(key=lambda t: t[0])
        for c, X, Y, R_, bottom in cands:
            poses = self.block_pose_parts(blk, X, Y, R_, bottom)
            if self.block_feasible(blk, poses):
                best = (c, X, Y, R_, bottom)
                # refine on a finer grid around the winner
                for dx in (-0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75):
                    for dy in (-0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75):
                        pz = self.block_pose_parts(blk, X + dx, Y + dy, R_, bottom)
                        cz = self.block_cost(blk, pz)
                        if cz < best[0] - 1e-9 and self.block_feasible(blk, pz):
                            best = (cz, X + dx, Y + dy, R_, bottom)
                return best
        return None

    def commit_block(self, blk, pose):
        _c, X, Y, R_, bottom = pose
        poses = self.block_pose_parts(blk, X, Y, R_, bottom)
        tag = "block:" + blk.name
        if not self._use_envelope(blk):
            for ref, (x, y, rot, bt) in poses.items():
                self.commit(ref, x, y, rot, bt, tag)
        else:
            bt, body, th = self._envelope(blk, poses)
            self.occ.add(bt, grow(body, self.block_gap / 2.0), tag)
            for side, r in th:
                self.occ.add(side, grow(r, self.part_gap / 2.0), tag)
            for ref, pz in poses.items():
                self.pos[ref] = pz
                self._add_net_pts(ref)
        blk.pose = pose

    def uncommit_block(self, blk):
        self.occ.remove_tag("block:" + blk.name)
        for ref in blk.refs():
            self.pos.pop(ref, None)
        self._rebuild_net_pts()

    # -- blocks whose anchor is fixed: members solved in board frame ----------
    def solve_fixed_block(self, blk):
        ax, ay, arot, abot = self.pos[blk.anchor.ref]
        for mem in blk.members:
            if mem.part.ref in self.fixed:
                continue
            # support passives stay on their anchor's side; a part behind
            # its IC is not "close", whatever the XY distance says
            rules = getattr(self.cfg, "SIDE_RULES", {})
            sides = [s == "bottom" for s in rules[mem.part.ref]] if mem.part.ref in rules else [abot]
            frame = {r: v for r, v in self.pos.items()}

            def ok(part, x, y, rot, bottom):
                return self.feasible(part, x, y, rot, bottom, self.part_gap)
            # start at the anchor pin the member serves
            hit_a = mem.targets[0]
            tx, ty = ax, ay
            for num, net in blk.anchor.nets.items():
                if net in hit_a and num in blk.anchor.pads:
                    tx, ty = pad_xy(blk.anchor, num, ax, ay, arot, abot)
                    break
            best = None
            for w in (mem.role.max_mm + 4.0, 12.0, 25.0):
                best = self.place_member(mem, frame, ok, w, (tx, ty), sides, compact=0.02)
                if best:
                    break
            if best is None:
                self.log.append("no room near fixed %s for %s -- placed with the free parts" % (blk.anchor.ref, mem.part.ref))
                continue
            _c, x, y, rot, bottom = best
            self.commit(mem.part.ref, x, y, rot, bottom, "fixedblock:" + blk.name, gap=self.part_gap)

    # -- the whole board -------------------------------------------------------
    def run(self, improve_passes=2):
        t0 = time.time()
        for blk in self.blocks.values():
            blk.__dict__.pop("envelope", None)
            blk.__dict__.pop("split", None)
            blk.__dict__.pop("pose", None)
        self.setup()
        free, fixed_blocks = [], []
        for blk in self.blocks.values():
            if blk.anchor.ref in self.fixed:
                fixed_blocks.append(blk)
            else:
                self.solve_block_local(blk)
                free.append(blk)
        for blk in fixed_blocks:
            self.solve_fixed_block(blk)
        # free parts of fixed blocks that found no room become singleton blocks
        for blk in fixed_blocks:
            for mem in blk.members:
                if mem.part.ref not in self.pos:
                    nb = Block(mem.part, None)
                    self.solve_block_local(nb)
                    free.append(nb)
                    self.blocks[nb.name] = nb

        self.capacity_report(free)
        placed, self.unplaced = [], []
        remaining = list(free)
        while remaining:
            def conn(b):
                s = 0.0
                for ref in b.refs():
                    for net in self.m.parts[ref].netset():
                        if net in self.net_pts:
                            s += self.weights.get(net, 0)
                area = (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1])
                return s + 0.02 * area
            remaining.sort(key=conn, reverse=True)
            blk = remaining.pop(0)
            pose = self.place_block(blk)
            if pose is None and self._use_envelope(blk):
                # still rigid, but let its parts interleave with neighbours'
                blk.envelope = False
                pose = self.place_block(blk)
                if pose is not None:
                    self.log.append("block %s fitted only without its envelope (parts interleave with neighbours)" % blk.name)
                else:
                    blk.envelope = True
            if pose is not None:
                self.commit_block(blk, pose)
                placed.append(blk)
                continue
            # no rigid fit: place the anchor alone, then its parts one by one
            # around it in board space, where they can use fragmented gaps
            self.log.append("block %s (%d parts, %.1f x %.1f mm) had no rigid fit -- split"
                            % (blk.name, len(blk.refs()), blk.bbox[2] - blk.bbox[0], blk.bbox[3] - blk.bbox[1]))
            members, blk.members = blk.members, []
            self.solve_block_local(blk)
            pose = self.place_block(blk)
            blk.members = members
            if pose is None:
                self.unplaced.extend(blk.refs())
                self.log.append("NO ROOM for %s -- left unplaced" % ", ".join(blk.refs()))
                continue
            _c, X, Y, R_, bottom = pose
            self.commit(blk.anchor.ref, X, Y, R_ % 360, bottom, "split:" + blk.name)
            self.solve_fixed_block(blk)
            for m in blk.members:
                if m.part.ref not in self.pos:
                    self.unplaced.append(m.part.ref)
            blk.split = True
        for _ in range(improve_passes):
            moved = 0
            for blk in placed:
                old = blk.pose
                self.uncommit_block(blk)
                new = self.place_block(blk)
                if new is None:
                    new = old
                old_cost = self.block_cost(blk, self.block_pose_parts(blk, *old[1:]))
                if new[0] < old_cost - 0.5:
                    moved += 1
                    self.commit_block(blk, new)
                else:
                    self.commit_block(blk, (old_cost,) + tuple(old[1:]))
            self.log.append("improvement pass: %d block(s) moved" % moved)
            if not moved:
                break
        self.log.append("solved %d parts in %d blocks in %.1f s" % (len(self.pos), len(self.blocks), time.time() - t0))
        return self.pos


# ===========================================================================
# audit
# ===========================================================================
def audit(model, blocks, pos):
    """Measure every recipe role on a placement. pos: ref -> (x, y, rot, bottom)."""
    rows = []
    for blk in blocks.values():
        a = blk.anchor
        if a.ref not in pos:
            continue
        ax, ay, arot, abot = pos[a.ref]
        for m in blk.members:
            if m.part.ref not in pos or m.role.name == "attached":
                continue
            x, y, rot, bottom = pos[m.part.ref]
            hit_a = m.targets[0]
            best = None
            ref_parts = [a] + [mm_.part for mm_ in blk.members if mm_.role.prio < m.role.prio]
            for net in hit_a:
                mine = [pad_xy(m.part, n, x, y, rot, bottom) for n, nn in m.part.nets.items() if nn == net and n in m.part.pads]
                for tp in ref_parts:
                    if tp.ref not in pos:
                        continue
                    tx, ty, tr, tb = pos[tp.ref]
                    for n, nn in tp.nets.items():
                        if nn != net or n not in tp.pads:
                            continue
                        qx, qy = pad_xy(tp, n, tx, ty, tr, tb)
                        for px, py in mine:
                            d = math.hypot(px - qx, py - qy)
                            if best is None or d < best:
                                best = d
            if best is None:
                continue
            side_ok = bottom == abot or m.part.kind not in PASSIVE
            rows.append(dict(block=blk.name, recipe=blk.recipe.name if blk.recipe else "-", role=m.role.name,
                             ref=m.part.ref, mm=round(best, 2), max=m.role.max_mm,
                             ok=best <= m.role.max_mm + 1e-6 and side_ok, same_side=side_ok))
    return rows


def print_audit(rows, title):
    n_ok = sum(1 for r in rows if r["ok"])
    say("%s: %d/%d recipe roles within limit" % (title, n_ok, len(rows)))
    bad = sorted((r for r in rows if not r["ok"]), key=lambda r: r["mm"] - r["max"], reverse=True)
    for r in bad[:25]:
        say("   FAIL %-6s %-14s %-7s %6.2f mm (limit %.1f)%s"
            % (r["block"], r["role"], r["ref"], r["mm"], r["max"], "" if r["same_side"] else "  other side"))
    if len(bad) > 25:
        say("   ... %d more" % (len(bad) - 25))
    return n_ok, len(rows)


# ===========================================================================
# floor plan drawing
# ===========================================================================
PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1",
           "#ff9da7", "#9c755f", "#8cd17d", "#499894", "#d37295", "#86bcb6", "#f1ce63"]


def floorplan_svg(solver, path, title):
    W, H = solver.W, solver.H
    s = 9.0
    pad = 30
    panel_w = W * s
    # parts that hang off the outline (antennas, edge connectors) need headroom
    rects = [xrect(solver.m.parts[r].rect, *v) for r, v in solver.pos.items()]
    over_top = max(0.0, -min((r[1] for r in rects), default=0.0)) * s
    over_bot = max(0.0, max((r[3] for r in rects), default=H) - H) * s
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="Helvetica,Arial,sans-serif">'
           % (panel_w * 2 + pad * 3, H * s + pad * 2 + 70 + over_top + over_bot),
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           '<text x="%d" y="22" font-size="16" font-weight="bold">%s</text>' % (pad, title)]
    colours = {}
    for i, name in enumerate(sorted(solver.blocks)):
        colours[name] = PALETTE[i % len(PALETTE)]
    owner = {}
    for name, blk in solver.blocks.items():
        for ref in blk.refs():
            owner[ref] = name
    for k, bottom in enumerate((False, True)):
        ox, oy = pad + k * (panel_w + pad), pad + 20 + over_top
        out.append('<text x="%d" y="%d" font-size="13" font-weight="bold">%s</text>'
                   % (ox, oy - 6, "TOP side" if not bottom else "BOTTOM side (seen from the top, not mirrored)"))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" fill="#f4f1e8" stroke="#333" stroke-width="1.5"/>'
                   % (ox, oy, W * s, H * s, getattr(solver.cfg, "CORNER_RADIUS", 0) * s))
        for kb, kr, _fn, name in solver.keepouts:
            if kb != bottom:
                continue
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#e15759" fill-opacity="0.10" '
                       'stroke="#e15759" stroke-dasharray="5,3"/>' % (ox + kr[0] * s, oy + kr[1] * s, (kr[2] - kr[0]) * s, (kr[3] - kr[1]) * s))
            out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#b03030">%s</text>' % (ox + kr[0] * s + 3, oy + kr[3] * s - 4, name))
        for hx, hy, d in getattr(solver.cfg, "HOLES", []):
            out.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#fff" stroke="#333"/>' % (ox + hx * s, oy + hy * s, d / 2 * s))
        boxes = {}
        for ref, (x, y, rot, bt) in solver.pos.items():
            p = solver.m.parts[ref]
            r = xrect(p.rect, x, y, rot, bt)
            if bt == bottom:
                blk = owner.get(ref, ref)
                fixed = ref in solver.fixed
                col = "#7f7f7f" if fixed else colours.get(blk, "#999")
                out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="%.2f" stroke="%s" stroke-width="0.6"/>'
                           % (ox + r[0] * s, oy + r[1] * s, (r[2] - r[0]) * s, (r[3] - r[1]) * s, col, 0.55 if fixed else 0.35, col))
                if not fixed:
                    boxes.setdefault(blk, []).append(r)
                else:
                    out.append('<text x="%.1f" y="%.1f" font-size="9" fill="#222">%s</text>' % (ox + r[0] * s + 2, oy + r[1] * s + 10, ref))
        for blk, rs in boxes.items():
            u = union(rs)
            b = solver.blocks[blk]
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" stroke="%s" stroke-width="2"/>'
                       % (ox + u[0] * s, oy + u[1] * s, (u[2] - u[0]) * s, (u[3] - u[1]) * s, colours.get(blk, "#999")))
            label = blk + ((" " + b.recipe.name.split(":")[0].split("(")[0].strip()) if b.recipe else "")
            out.append('<text x="%.1f" y="%.1f" font-size="10" font-weight="bold" fill="#111">%s</text>'
                       % (ox + u[0] * s + 2, oy + u[1] * s - 2, label[:34]))
    out.append('<text x="%d" y="%.1f" font-size="11" fill="#444">Grey = fixed or locked. Coloured outline = one functional block '
               '(anchor + recipe). Red dashed = keep-out.</text>' % (pad, H * s + pad + 50 + over_top + over_bot))
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


# ===========================================================================
# entry point
# ===========================================================================
def load_config(project, path=None):
    path = path or os.path.join(project, "placement_config.py")
    if not os.path.exists(path):
        raise SystemExit("%s not found -- see references/autoplace.md" % path)
    spec = importlib.util.spec_from_file_location("placement_config", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def describe_blocks(blocks):
    for name in sorted(blocks, key=lambda n: (-len(blocks[n].members), n)):
        b = blocks[name]
        if not b.members and not b.recipe:
            continue
        say("  %-6s %-48s %s" % (name, (b.recipe.name if b.recipe else "(no recipe)")[:48],
                                  ", ".join("%s:%s" % (m.part.ref, m.role.name) for m in b.members)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--audit", action="store_true", help="score the board's current placement")
    ap.add_argument("--floorplan", action="store_true", help="solve and draw; do not touch the board")
    ap.add_argument("--place", action="store_true", help="solve, write the board, check, DRC, render")
    ap.add_argument("--force", action="store_true", help="overwrite a board edited since the last run")
    ap.add_argument("--config", help="config file (default PROJECT/placement_config.py)")
    ap.add_argument("--out", help="where floorplan.svg/.json go (default PROJECT)")
    args = ap.parse_args()
    out_dir = args.out or args.project

    cfg = load_config(args.project, args.config)
    b = Board(args.project, cfg.NAME, size=cfg.BOARD_SIZE, origin=getattr(cfg, "ORIGIN", (100.0, 50.0)))
    model = Model(b)
    selftest(b, model)
    if args.selftest and not (args.audit or args.floorplan or args.place):
        return
    blocks, _owner = build_blocks(model, cfg)
    say("%d blocks from %d parts:" % (len(blocks), len(model.parts)))
    describe_blocks(blocks)

    if args.audit:
        current = {r: (p.x, p.y, p.rot, p.bottom) for r, p in model.parts.items()}
        print_audit(audit(model, blocks, current), "current board")

    if args.floorplan or args.place:
        solver = Solver(model, blocks, cfg)
        pos = solver.run()
        for line in solver.log:
            say("  " + line)
        rows = audit(model, blocks, pos)
        env_default = getattr(cfg, "BLOCK_ENVELOPE", True)
        if env_default and any(getattr(bk, "split", False) for bk in blocks.values()):
            # blocks were split for lack of contiguous room: try the denser
            # interleaving mode and keep whichever meets more recipe limits
            say("  some blocks were split -- trying again with BLOCK_ENVELOPE = False")
            cfg.BLOCK_ENVELOPE = False
            alt = Solver(model, blocks, cfg)
            alt_pos = alt.run()
            alt_rows = audit(model, blocks, alt_pos)
            a_ok, b_ok = sum(r["ok"] for r in rows), sum(r["ok"] for r in alt_rows)
            say("  envelope mode %d/%d, interleaved mode %d/%d" % (a_ok, len(rows), b_ok, len(alt_rows)))
            if b_ok > a_ok and not alt.unplaced:
                solver, pos, rows = alt, alt_pos, alt_rows
                for line in alt.log:
                    say("  " + line)
                say("  using the interleaved result")
            else:
                cfg.BLOCK_ENVELOPE = True
                # re-run so block state matches the kept result
                solver = Solver(model, blocks, cfg)
                pos = solver.run()
                rows = audit(model, blocks, pos)
                say("  keeping the envelope result")
        print_audit(rows, "proposed placement")
        if solver.unplaced:
            say("  UNPLACED: " + ", ".join(sorted(solver.unplaced)))
        svg = floorplan_svg(solver, os.path.join(out_dir, "floorplan.svg"), "%s -- proposed floor plan" % cfg.NAME)
        with open(os.path.join(out_dir, "floorplan.json"), "w") as f:
            json.dump(dict(positions={r: [round(v[0], 3), round(v[1], 3), v[2], "bottom" if v[3] else "top"]
                                      for r, v in sorted(pos.items())},
                           blocks={n: bk.refs() for n, bk in blocks.items()},
                           audit=rows), f, indent=1)
        say("wrote %s and floorplan.json" % svg)

        if args.place:
            b.outline(radius=getattr(cfg, "CORNER_RADIUS", 0.0))
            for hx, hy, d in getattr(cfg, "HOLES", []):
                b.hole(hx, hy, d)
            for ref, (x, y, rot, bottom) in pos.items():
                b.place(ref, round(x, 3), round(y, 3), round(rot, 3) % 360, "bottom" if bottom else "top")
            b.link_schematic()
            b.tidy_references(size=getattr(cfg, "REF_SIZE", 0.8),
                              thickness=getattr(cfg, "REF_THICKNESS", 0.15),
                              hide=getattr(cfg, "REF_HIDE", ()),
                              retidy=getattr(cfg, "REF_RETIDY", ()))
            try:
                b.check()
            except SystemExit as e:
                say("  check() reported errors (saving anyway so they can be inspected): %s" % e)
            b.save(force=args.force)
            b.drc()
            crit = [n for n, w in solver.weights.items() if w >= 3.0]
            b.report_nets(crit[:40])
            b.render(args.project)


if __name__ == "__main__":
    main()
