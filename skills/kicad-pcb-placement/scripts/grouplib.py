#!/usr/bin/env python3
"""Functional groups: arrange each circuit on its own, off the board, and bind
it into a KiCad group so it moves onto the board as one piece.

The idea is the opposite of a floor plan. A floor plan decides where each block
goes; this decides only what each block LOOKS like -- the regulator with its
input cap on the VIN pin, the ESD array in line between connector and IC -- and
leaves where it goes to whoever drags it. Every group is laid out in its own
frame, seen from the top, with its anchor at (0, 0):

    g = Group(model, "buck_5v", "5 V buck (U5V1)", side="bottom", source="TPS565201 fig. 8-2")
    g.put("U5V1", 0, 0, 270)                       # explicit pose
    g.snap("CU5VIN", "1", ("U5V1", "3"))           # nearest free spot, pad 1 to pin 3
    g.pin("R1", 90, "2", g.at("U1", "8"), dx=1.2)  # pad 2 lands 1.2 mm right of U1.8

snap() is the workhorse: it searches for the pose that brings one pad closest to
a target pad without overlapping anything already in the group, so the ORDER of
the calls is the placement priority, exactly as a datasheet states it ("input
cap first, then bootstrap, then ...").

A group destined for the bottom is still designed from the top; commit() mirrors
it, which is what KiCad does when a group is flipped. (x, y, rot) on top becomes
(-x, y, 180 - rot) on the bottom: derived from autoplace.xform, which selftest()
proves against the real pads.
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcbnew                        # noqa: E402
import autoplace as ap               # noqa: E402
from pcblib import say               # noqa: E402

GAP = 0.0                            # courtyards may touch, never overlap (they already hold 0.25 mm round the pads)


def top_frame(x, y, rot, bottom):
    """A board pose as it would read if its side were viewed as the top."""
    return (-x, y, (180 - rot) % 360) if bottom else (x, y, rot % 360)


class Group:
    def __init__(self, model, name, title, side="top", source="", note=""):
        self.m, self.name, self.title, self.side = model, name, title, side
        self.source, self.note = source, note
        self.pose = {}               # ref -> (x, y, rot) in the group's top-view frame
        self.why = {}                # ref -> one line: what it is doing there
        self.links = []              # (ref, pad, ref2, pad2, limit_mm or None) measured in report()
        self.back = set()            # refs on the OTHER side of the board from the rest of the group
        self.gaps = {}               # frozenset({a, b}) -> courtyard gap allowed for that pair

    # -- geometry ---------------------------------------------------------------
    def at(self, ref, num):
        x, y, rot = self.pose[ref]
        return ap.pad_xy(self.m.parts[ref], str(num), x, y, rot, ref in self.back)

    def rect(self, ref, pose=None):
        x, y, rot = pose or self.pose[ref]
        return ap.xrect(self.m.parts[ref].rect, x, y, rot, ref in self.back)

    def bbox(self):
        return ap.union([self.rect(r) for r in self.pose])

    def free(self, ref, x, y, rot, gap=GAP, ignore=()):
        r = ap.xrect(self.m.parts[ref].rect, x, y, rot, ref in self.back)
        for o in self.pose:
            if o == ref or o in ignore or (o in self.back) != (ref in self.back):
                continue
            if ap.overlap(r, self.rect(o), gap - 1e-6):
                return False
        return True

    def tight(self, a, b, gap):
        """Allow one pair closer than GAP (e.g. two caps a datasheet draws touching)."""
        self.gaps[frozenset((a, b))] = gap
        return self

    # -- placing ----------------------------------------------------------------
    def put(self, ref, x, y, rot=0, why="", back=False):
        """back=True: on the other side of the board, seen THROUGH the board from
        the group's own side (so x, y still read in the same frame)."""
        if ref not in self.m.parts:
            raise SystemExit("%s: %s is not on the board" % (self.name, ref))
        if back:
            self.back.add(ref)
        self.pose[ref] = (round(x, 3), round(y, 3), rot % 360)
        if why:
            self.why[ref] = why
        return self

    def pin(self, ref, rot, num, xy, dx=0.0, dy=0.0, why="", back=False):
        """Place ref at rot so that its pad num lands on xy + (dx, dy)."""
        px, py = ap.pad_xy(self.m.parts[ref], str(num), 0, 0, rot, back)
        return self.put(ref, xy[0] + dx - px, xy[1] + dy - py, rot, why, back)

    def take(self, table, refs, anchor):
        """Adopt poses already worked out in board coordinates (placement_config.FIXED)."""
        ax, ay = table[anchor][0], table[anchor][1]
        for r in refs:
            v = table[r]
            bottom = (v[3] if len(v) > 3 else "top") == "bottom"
            x, y, rot = top_frame(v[0] - ax, v[1] - ay, v[2], bottom)
            self.put(r, x, y, rot)
        return self

    def snap(self, ref, num, to, rots=(0, 90, 180, 270), also=(), bias=None, window=9.0,
             limit=None, gap=GAP, why="", ignore=(), back=False):
        """Nearest free pose that brings pad num to the target.

        to      (ref, pad) already in the group, or an (x, y) point
        also    [(my_pad, (ref, pad) or (x, y), weight)] secondary pulls, e.g. the ground pad
        bias    (bx, by): prefer that direction from the target (a unit vector, y down)
        limit   recorded for report(): the datasheet or recipe maximum, mm
        """
        part = self.m.parts[ref]
        if back:
            self.back.add(ref)
        tgt = self.at(*to) if isinstance(to[0], str) else to
        pulls = [(str(num), tgt, 1.0)]
        for mp, t, w in also:
            pulls.append((str(mp), self.at(*t) if isinstance(t[0], str) else t, w))

        def cost(x, y, rot):
            c = 0.0
            for mp, (tx, ty), w in pulls:
                px, py = ap.pad_xy(part, mp, x, y, rot, back)
                c += w * math.hypot(px - tx, py - ty)
            if bias:
                c += 0.6 * max(0.0, -((x - tgt[0]) * bias[0] + (y - tgt[1]) * bias[1]))
            return c + 0.01 * (rot % 180 != 0)

        best = None
        for step, span in ((0.25, window), (0.05, 0.3), (0.01, 0.06)):
            if best is None and step != 0.25:
                break
            cx, cy = tgt if step == 0.25 else (best[1], best[2])
            n = int(round(span / step))
            for rot in (rots if step == 0.25 else (best[3],)):
                for i in range(-n, n + 1):
                    for j in range(-n, n + 1):
                        x, y = cx + i * step, cy + j * step
                        c = cost(x, y, rot)
                        if best is not None and c >= best[0] - 1e-9:
                            continue
                        if self.free(ref, x, y, rot, gap, ignore):
                            best = (c, x, y, rot)
        if best is None:
            raise SystemExit("%s: no room for %s within %.1f mm of its target" % (self.name, ref, window))
        self.put(ref, best[1], best[2], best[3], why)
        if isinstance(to[0], str):
            self.links.append((ref, str(num), to[0], str(to[1]), limit))
        return self

    def link(self, ref, num, ref2, num2, limit=None):
        """Record a distance to measure in report() for a part placed with put()/pin()."""
        self.links.append((ref, str(num), ref2, str(num2), limit))
        return self

    def settle(self, clear=0.02, rounds=40):
        """Push apart any two courtyards that overlap, moving the part placed LATER.

        A layout written as numbers goes stale when a footprint's courtyard changes; this
        keeps the arrangement and only opens the gap, along the axis that needs the smaller
        move. Parts placed earlier (the IC, then whatever the datasheet puts first) stay put.
        Returns [(ref, dx, dy)] for what it moved."""
        order = list(self.pose)
        start = dict(self.pose)
        for _ in range(rounds):
            moved = False
            for i, a in enumerate(order):
                for b_ in order[i + 1:]:
                    if (a in self.back) != (b_ in self.back):
                        continue
                    gap = self.gaps.get(frozenset((a, b_)), GAP)
                    ra, rb = self.rect(a), self.rect(b_)
                    if not ap.overlap(ra, rb, gap - 1e-6):
                        continue
                    px = min(ra[2] - rb[0], rb[2] - ra[0]) + gap
                    py = min(ra[3] - rb[1], rb[3] - ra[1]) + gap
                    x, y, rot = self.pose[b_]
                    if px <= py:
                        x += (px + clear) * (1 if (rb[0] + rb[2]) >= (ra[0] + ra[2]) else -1)
                    else:
                        y += (py + clear) * (1 if (rb[1] + rb[3]) >= (ra[1] + ra[3]) else -1)
                    self.pose[b_] = (round(x, 3), round(y, 3), rot)
                    moved = True
            if not moved:
                break
        return [(r, round(self.pose[r][0] - start[r][0], 3), round(self.pose[r][1] - start[r][1], 3))
                for r in order if self.pose[r] != start[r]]

    # -- checking ---------------------------------------------------------------
    def problems(self):
        out = []
        refs = sorted(self.pose)
        for i, a in enumerate(refs):
            for b_ in refs[i + 1:]:
                gap = self.gaps.get(frozenset((a, b_)), GAP)
                if (a in self.back) == (b_ in self.back) and ap.overlap(self.rect(a), self.rect(b_), gap - 1e-6):
                    out.append("%s and %s closer than %.2f mm" % (a, b_, gap))
        for ref, num, ref2, num2, limit in self.links:
            if limit is None:
                continue
            d = self.dist(ref, num, ref2, num2)
            if d > limit + 1e-6:
                out.append("%s.%s to %s.%s is %.2f mm, limit %.1f" % (ref, num, ref2, num2, d, limit))
        return out

    def dist(self, ref, num, ref2, num2):
        (ax, ay), (bx, by) = self.at(ref, num), self.at(ref2, num2)
        return math.hypot(ax - bx, ay - by)

    def report(self):
        x0, y0, x1, y1 = self.bbox()
        say("%-14s %-44s %2d parts  %5.1f x %4.1f mm  %s" % (self.name, self.title[:44], len(self.pose),
                                                           x1 - x0, y1 - y0, self.side))
        for ref, num, ref2, num2, limit in self.links:
            d = self.dist(ref, num, ref2, num2)
            say("    %-7s pad %-4s to %-5s pad %-4s %5.2f mm%s" % (
                ref, num, ref2, num2, d, "" if limit is None else
                ("   (limit %.1f)%s" % (limit, "" if d <= limit + 1e-6 else "   <-- OVER"))))
        for p in self.problems():
            say("    PROBLEM: " + p)

    # -- drawing ----------------------------------------------------------------
    def draw(self, path, scale=40):
        from PIL import Image, ImageDraw, ImageFont
        x0, y0, x1, y1 = self.bbox()
        pad = 1.5
        W, H = int((x1 - x0 + 2 * pad) * scale), int((y1 - y0 + 2 * pad) * scale)
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype("arial.ttf", max(10, int(scale * 0.42)))
            small = ImageFont.truetype("arial.ttf", max(8, int(scale * 0.27)))
        except Exception:
            font = small = ImageFont.load_default()

        def P(x, y):
            return ((x - x0 + pad) * scale, (y - y0 + pad) * scale)

        nets = sorted({n for r in self.pose for n in self.m.parts[r].nets.values() if n})
        pal = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1",
               "#ff9da7", "#9c755f", "#8cd17d", "#499894", "#d37295", "#86bcb6", "#f1ce63"]
        colour, k = {}, 0
        for n in nets:
            if n in self.m.ground:
                colour[n] = "#9a9a9a"
            elif n.startswith("unconnected"):
                colour[n] = "#e6e6e6"
            else:
                colour[n] = pal[k % len(pal)]
                k += 1
        for ref, (x, y, rot) in self.pose.items():
            part = self.m.parts[ref]
            r = self.rect(ref)
            isback = ref in self.back
            d.rectangle([P(r[0], r[1]), P(r[2], r[3])], outline="#cc0000" if isback else "#555555", width=1)
            for pd in part.padlist:
                pr = ap.xrect((pd.x0, pd.y0, pd.x1, pd.y1), x, y, rot, isback)
                net = part.nets.get(pd.num, "")
                d.rectangle([P(pr[0], pr[1]), P(pr[2], pr[3])], fill=colour.get(net, "#dddddd"), outline="#333333")
                if len(part.pads) > 2 and pd.num and min(pr[2] - pr[0], pr[3] - pr[1]) * scale > 9:
                    d.text(P((pr[0] + pr[2]) / 2, (pr[1] + pr[3]) / 2), pd.num, fill="black", font=small, anchor="mm")
            d.text(P((r[0] + r[2]) / 2, (r[1] + r[3]) / 2), ref, fill="#000080", font=font, anchor="mm")
        ly = 4
        for n in nets:
            if n.startswith("unconnected"):
                continue
            d.rectangle([4, ly, 14, ly + 10], fill=colour[n], outline="black")
            d.text((18, ly - 1), n.split("/")[-1], fill="black", font=small)
            ly += 13
        im.save(path)
        return path


# ===========================================================================
# writing the board
# ===========================================================================
def label(g):
    """The short title drawn over a group on the board."""
    short = g.title.split(" (")[0].split(":")[0].split(" -- ")[0]
    return "%s [%s]" % (short, g.side)


def pack(groups, x0, y0, width, gap=6.0):
    """Shelf-pack group bounding boxes into a band.

    Returns ({name: (x, y) of the bbox top-left}, y of the band's lower edge)."""
    out, x, y, row_h = {}, x0, y0, 0.0
    for g in groups:
        bx0, by0, bx1, by1 = g.bbox()
        w, h = bx1 - bx0, by1 - by0 + 3.0          # 3 mm for the title above
        w = max(w, 0.85 * len(label(g)))           # and room for it: 1 mm text is about 0.85 mm a character
        if x > x0 and x + w > x0 + width:
            x, y, row_h = x0, y + row_h + gap, 0.0
        out[g.name] = (x, y + 3.0)
        x += w + gap
        row_h = max(row_h, h)
    return out, y + row_h


def commit(board, groups, origins, layer=None):
    """Move every group to its gallery slot and bind it into a KiCad group.

    board: pcblib.Board. origins: {group name: (x, y)}, board-relative top-left of the bbox.
    """
    b = board.board
    layer = pcbnew.Cmts_User if layer is None else layer
    for grp in list(b.Groups()):
        if grp.GetName().startswith("grp:"):
            for item in list(grp.GetItems()):
                if item.GetClass() == "PCB_TEXT":
                    board._remove(item)
            board._remove(grp)
    for g in groups:
        bx0, by0, bx1, by1 = g.bbox()
        ox, oy = origins[g.name]
        bottom = g.side == "bottom"
        kg = pcbnew.PCB_GROUP(b)
        kg.SetName("grp:%s" % g.name)
        b.Add(kg)
        other = "top" if bottom else "bottom"
        for ref, (x, y, rot) in g.pose.items():
            if bottom:                              # mirror about the bbox, as Flip would
                X, Y, R = ox + (bx1 - x), oy + (y - by0), (180 - rot) % 360
            else:
                X, Y, R = ox + (x - bx0), oy + (y - by0), rot
            fp = board.place(ref, round(X, 3), round(Y, 3), R, other if ref in g.back else g.side)
            fp.SetLocked(False)
            kg.AddItem(fp)
        t = pcbnew.PCB_TEXT(b)
        t.SetText(label(g))
        t.SetLayer(layer)
        t.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        t.SetTextThickness(pcbnew.FromMM(0.15))
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
        t.SetPosition(board.pt(ox, oy - 1.6))
        b.Add(t)
        kg.AddItem(t)
    return len(groups)
