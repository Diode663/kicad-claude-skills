#!/usr/bin/env python3
"""routelib -- scripted routing for a placed KiCad board, with an optional
autorouter for whatever the script leaves.

Run under KiCad's own Python, like pcblib:

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" route_board.py

The pattern it supports (references/example_route_board.py is an older, all-scripted
router written before this library existed; its structure is the same):

    r = Router(__file__, "my-board", size=(40.0, 61.0))
    r.clear()
    a = r.pad("U5", 2, expect=(28.00, 33.15), net="GND")   # proves placement AND netlist
    r.track("GND", [a, (28.0, 35.6)], 0.6)
    r.via("GND", (28.0, 34.5))
    ...
    r.check_moved()                  # stop, listing every pad that is not where expected
    r.zone("GND", F, "GND top")      # BEFORE the autorouter: ground reads as a plane
    r.zone("GND", B, "GND bottom")
    r.normalise()
    if "--autoroute" in sys.argv:
        r.autoroute(scripted_nets=SCRIPTED)
    r.replay()
    r.fill()
    r.save()
    r.drc_report()
    r.report()

Why each piece exists:
  * pad(expect=, net=)  a routing script is coordinates; when a part moves or
    a net is renamed, it must refuse to draw rather than draw through a pad.
  * clear()             idempotence: every run removes and redraws all copper.
    Removed pcbnew proxies are kept alive -- letting one be garbage-collected
    corrupts pcbnew's type table.
  * normalise()         splits tracks at T junctions. KiCad does not need it;
    an autorouter's idea of "connected" does.
  * autoroute()         KiCad -> Specctra DSN -> Freerouting -> SES, with the
    three repairs that hand-off needs (see the method), and the result stored
    as JSON rather than left in the board.
  * replay()            ordinary runs redraw the stored autorouter result, so
    the board is reproducible without Java and without re-rolling anything.
  * report()            what DRC does not say: how many pieces each pour is
    in, where the far layer is cut, via-in-pad, per-net length.

Coordinates are board mm, origin at the board's top-left corner, Y down --
the same frame pcblib uses.
"""
import json
import os
import re
import subprocess
import sys

import pcbnew

F, B = pcbnew.F_Cu, pcbnew.B_Cu
mm = pcbnew.FromMM
tomm = pcbnew.ToMM


def say(*a):
    print(*a)
    sys.stdout.flush()


def find_kicad_cli():
    exe = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    here = os.path.join(os.path.dirname(sys.executable), exe)
    if os.path.exists(here):
        return here
    return exe


def find_freerouting(home=None):
    """(java, jar) from a folder holding freerouting-*.jar and, optionally, a
    portable JRE (a jdk-* folder). Folder: argument, else FREEROUTING_HOME,
    else ~/tools/freerouting. Freerouting 2.4 needs Java 25."""
    home = home or os.environ.get("FREEROUTING_HOME") or os.path.join(os.path.expanduser("~"), "tools", "freerouting")
    if not os.path.isdir(home):
        raise SystemExit("no Freerouting folder at %s (set FREEROUTING_HOME)" % home)
    jars = sorted(f for f in os.listdir(home) if f.lower().startswith("freerouting") and f.endswith(".jar"))
    if not jars:
        raise SystemExit("no freerouting-*.jar in %s" % home)
    exe = "java.exe" if os.name == "nt" else "java"
    java = exe
    for d in sorted(os.listdir(home)):
        cand = os.path.join(home, d, "bin", exe)
        if os.path.exists(cand):
            java = cand
    return java, os.path.join(home, jars[-1])


def _sexpr(text):
    """Minimal S-expression reader: nested lists of strings."""
    tok = re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', text)
    stack, cur = [], []
    for t in tok:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            cur.append(t[1:-1] if t.startswith('"') else t)
    return cur[0]


class Router:
    def __init__(self, script_or_dir, name, size=None, origin=(100.0, 50.0), cli=None):
        d = script_or_dir
        self.dir = os.path.dirname(os.path.abspath(d)) if os.path.isfile(d) else os.path.abspath(d)
        self.name = name
        self.path = os.path.join(self.dir, name + ".kicad_pcb")
        self.ox, self.oy = float(origin[0]), float(origin[1])
        self.size = size
        self.cli = cli or find_kicad_cli()
        self.auto_json = os.path.join(self.dir, "routing", "autoroute.json")
        self.moved = []
        self._removed = []       # pcbnew: a removed item's proxy must never be garbage-collected
        self.board = pcbnew.LoadBoard(self.path)
        self.nets = {n.GetNetname(): n for n in self.board.GetNetInfo().NetsByNetcode().values()}
        self.fps = {f.GetReference(): f for f in self.board.GetFootprints()}
        if size is None:
            bb = self.board.GetBoardEdgesBoundingBox()
            self.size = (round(tomm(bb.GetWidth()), 1), round(tomm(bb.GetHeight()), 1))

    # ------------------------------------------------------------------ drawing
    def pt(self, x, y):
        return pcbnew.VECTOR2I(mm(self.ox + x), mm(self.oy + y))

    def pad(self, ref, num, expect=None, net=None, tol=0.02):
        """Board-mm centre of a pad. `expect` proves the placement is the one
        the script was written for; `net` proves the netlist is. Mismatches
        are collected, not raised: check_moved() lists them all at once."""
        for p in self.fps[ref].Pads():
            if p.GetNumber() == str(num):
                q = p.GetPosition()
                xy = (round(tomm(q.x) - self.ox, 3), round(tomm(q.y) - self.oy, 3))
                if expect and (abs(xy[0] - expect[0]) > tol or abs(xy[1] - expect[1]) > tol):
                    self.moved.append("%s.%s is at %s, the script expects %s" % (ref, num, xy, expect))
                if net and p.GetNetname() != net:
                    self.moved.append("%s.%s is on %s, the script expects %s" % (ref, num, p.GetNetname(), net))
                return xy
        raise KeyError((ref, num))

    def check_moved(self):
        if self.moved:
            raise SystemExit("placement or netlist changed -- these routes have to follow:\n  "
                             + "\n  ".join(self.moved))

    def track(self, net, pts, width, layer=F, board=None):
        bd = board or self.board
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            t = pcbnew.PCB_TRACK(bd)
            t.SetStart(self.pt(*a))
            t.SetEnd(self.pt(*b))
            t.SetWidth(mm(width))
            t.SetLayer(layer)
            t.SetNet(bd.FindNet(net))
            bd.Add(t)

    def via(self, net, xy, dia=0.6, drill=0.3, board=None):
        bd = board or self.board
        v = pcbnew.PCB_VIA(bd)
        v.SetPosition(self.pt(*xy))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetWidth(mm(dia))
        v.SetDrill(mm(drill))
        v.SetLayerPair(F, B)
        v.SetNet(bd.FindNet(net))
        bd.Add(v)
        return xy

    def clear(self):
        """Drop every track, via and copper zone. Zones are collected before
        anything is removed, and every removed proxy is kept alive."""
        zones = [self.board.GetArea(i) for i in range(self.board.GetAreaCount())]
        doomed = list(self.board.GetTracks()) + [z for z in zones if not z.GetIsRuleArea()]
        for item in doomed:
            self.board.Remove(item)
            self._removed.append(item)

    def zone(self, net, layer, name, poly=None, priority=0, clearance=0.2, solid=False,
             min_thickness=0.25, spoke=0.3, gap=0.25):
        """A copper zone; `poly` defaults to the whole board. Not filled here."""
        w, h = self.size
        z = pcbnew.ZONE(self.board)
        z.SetLayer(layer)
        z.SetNet(self.nets[net])
        z.SetZoneName(name)
        o = z.Outline()
        o.NewOutline()
        for x, y in (poly or ((0, 0), (w, 0), (w, h), (0, h))):
            o.Append(mm(self.ox + x), mm(self.oy + y))
        z.SetLocalClearance(mm(clearance))
        z.SetMinThickness(mm(min_thickness))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL if solid else pcbnew.ZONE_CONNECTION_THERMAL)
        z.SetThermalReliefGap(mm(gap))
        z.SetThermalReliefSpokeWidth(mm(spoke))
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        z.SetAssignedPriority(priority)
        self.board.Add(z)
        return z

    def zone_connection(self, ref, num, mode):
        """Per-pad pour connection. `num=None` means every pad of the part;
        a pad number shared by several pads (a module's centre pad and its
        thermal holes) sets them all.
        FULL for pads a power loop closes through; NONE for a Kelvin tap that
        is on GND by name only and must see its sense point and nothing else."""
        for p in self.fps[ref].Pads():
            if num is None or p.GetNumber() == str(num):
                p.SetLocalZoneConnection(mode)

    def fill(self):
        pcbnew.ZONE_FILLER(self.board).Fill([self.board.GetArea(i) for i in range(self.board.GetAreaCount())])

    def save(self):
        pcbnew.SaveBoard(self.path, self.board)

    # --------------------------------------------------------------- bookkeeping
    def normalise(self):
        """Split a track wherever another track or via of its net ends on it
        (a T junction), so every junction is an endpoint. Geometry is
        unchanged; an autorouter's connectivity check then agrees with KiCad's."""
        splits = 0
        for _ in range(4):
            tracks = [t for t in self.board.GetTracks() if t.GetClass() != "PCB_VIA"]
            ends = {}
            for t in self.board.GetTracks():
                if t.GetClass() == "PCB_VIA":
                    ends.setdefault(t.GetNetname(), set()).add((t.GetPosition().x, t.GetPosition().y, None))
                else:
                    for q in (t.GetStart(), t.GetEnd()):
                        ends.setdefault(t.GetNetname(), set()).add((q.x, q.y, t.GetLayer()))
            changed = False
            for t in tracks:
                a, b = t.GetStart(), t.GetEnd()
                L2 = float(b.x - a.x) ** 2 + float(b.y - a.y) ** 2
                if L2 == 0:
                    continue
                for (x, y, layer) in sorted(ends.get(t.GetNetname(), ()), key=lambda e: (e[0], e[1])):
                    if layer is not None and layer != t.GetLayer():
                        continue
                    if (x, y) in ((a.x, a.y), (b.x, b.y)):
                        continue
                    u = ((x - a.x) * (b.x - a.x) + (y - a.y) * (b.y - a.y)) / L2
                    if not 0.0 < u < 1.0:
                        continue
                    px, py = a.x + u * (b.x - a.x), a.y + u * (b.y - a.y)
                    if (px - x) ** 2 + (py - y) ** 2 > 1000.0 ** 2:       # 1 um
                        continue
                    t2 = pcbnew.PCB_TRACK(self.board)
                    t2.SetStart(pcbnew.VECTOR2I(int(x), int(y)))
                    t2.SetEnd(b)
                    t2.SetWidth(t.GetWidth())
                    t2.SetLayer(t.GetLayer())
                    t2.SetNet(t.GetNet())
                    t.SetEnd(pcbnew.VECTOR2I(int(x), int(y)))
                    self.board.Add(t2)
                    splits += 1
                    changed = True
                    break
            if not changed:
                break
        return splits

    def snapshot(self, board=None):
        """Every track and via as plain data."""
        out = []
        for t in (board or self.board).GetTracks():
            n = t.GetNetname()
            if t.GetClass() == "PCB_VIA":
                q = t.GetPosition()
                out.append({"via": True, "net": n, "x": round(tomm(q.x) - self.ox, 4),
                            "y": round(tomm(q.y) - self.oy, 4),
                            "d": round(tomm(t.GetWidth(F)), 3), "drill": round(tomm(t.GetDrill()), 3)})
            else:
                a, b = t.GetStart(), t.GetEnd()
                out.append({"net": n, "layer": "F" if t.GetLayer() == F else "B",
                            "x1": round(tomm(a.x) - self.ox, 4), "y1": round(tomm(a.y) - self.oy, 4),
                            "x2": round(tomm(b.x) - self.ox, 4), "y2": round(tomm(b.y) - self.oy, 4),
                            "w": round(tomm(t.GetWidth()), 3)})
        return out

    def count(self):
        return len(list(self.board.GetTracks()))

    # ---------------------------------------------------------------- autorouter
    def parse_ses(self, path):
        """New (unprotected) wires and vias from a Specctra session file, as
        snapshot()-style records. KiCad's ImportSpecctraSES refuses a session
        that carries (type protect), silently, so this reads it directly."""
        root = _sexpr(open(path, encoding="utf-8").read())
        routes = [x for x in root if isinstance(x, list) and x and x[0] == "routes"][0]
        res = [x for x in routes if isinstance(x, list) and x[0] == "resolution"][0]
        per_mm = {"um": 1000.0, "mm": 1.0, "mil": 39.3701, "inch": 0.0393701}[res[1]] * float(res[2])
        net_out = [x for x in routes if isinstance(x, list) and x[0] == "network_out"][0]
        layers = {self.board.GetLayerName(F): "F", self.board.GetLayerName(B): "B", "top_cu": "F", "bottom_cu": "B",
                  "F.Cu": "F", "B.Cu": "B"}
        out = []
        for net in (x for x in net_out if isinstance(x, list) and x[0] == "net"):
            name = net[1]
            for item in net[2:]:
                if not isinstance(item, list):
                    continue
                if any(isinstance(k, list) and k[:2] == ["type", "protect"] for k in item):
                    continue
                if item[0] == "wire":
                    path_ = [k for k in item if isinstance(k, list) and k[0] == "path"][0]
                    layer = layers.get(path_[1])
                    if layer is None:
                        raise SystemExit("SES names layer %s; routelib handles two-layer boards" % path_[1])
                    w = float(path_[2]) / per_mm
                    xy = [float(v) / per_mm for v in path_[3:]]
                    pts = [(round(xy[i] - self.ox, 4), round(-xy[i + 1] - self.oy, 4)) for i in range(0, len(xy), 2)]
                    for a, b in zip(pts, pts[1:]):
                        if a != b:
                            out.append({"net": name, "layer": layer, "x1": a[0], "y1": a[1],
                                        "x2": b[0], "y2": b[1], "w": round(w, 3)})
                elif item[0] == "via":
                    m = re.search(r"_(\d+):(\d+)_um", item[1])
                    out.append({"via": True, "net": name, "x": round(float(item[2]) / per_mm - self.ox, 4),
                                "y": round(-float(item[3]) / per_mm - self.oy, 4),
                                "d": int(m.group(1)) / 1000.0, "drill": int(m.group(2)) / 1000.0})
        return out

    def autoroute(self, scripted_nets=(), passes=30, freerouting_home=None, fanout=False, timeout=1800, tries=3, best_of=1):
        """Scripted routes -> Specctra DSN -> Freerouting -> SES -> keep what is new.

        Call it with the scripted copper and the (unfilled) ground zones in
        place: ground then exports as a plane and the autorouter leaves it
        alone. `scripted_nets` are nets the script routes completely; whatever
        the autorouter adds to them is a duplicate (it does not count a track
        that ends inside a pad, off its centre, as connected) and is dropped.
        The result goes to routing/autoroute.json, not into the board:
        replay() draws it.

        Repairs made to KiCad's DSN: `(type route)` -> `(type protect)`, or
        the autorouter may rip the scripted tracks up; and netclass names
        come out as "GND,Default", so ",Default" is stripped.

        The autorouter is not deterministic and some runs leave a connection
        open. Its own log is no judge of that (it lists scripted connections
        it cannot see as open), so each run is drawn on a trial board and
        KiCad's DRC counts what is unconnected on the nets that are the
        autorouter's job. It runs again, up to `tries` times, until that is
        zero. `best_of=N` always makes N runs and keeps the best by (open
        connections, far-layer track length, vias): runs differ a good deal
        in how much of the far-layer pour they cut."""
        work = os.path.join(self.dir, "routing", "work")
        os.makedirs(work, exist_ok=True)
        tmp = os.path.join(work, self.name + ".kicad_pcb")
        for ext in (".kicad_pro", ".kicad_dru"):
            src = os.path.join(self.dir, self.name + ext)
            if os.path.exists(src):
                open(os.path.join(work, self.name + ext), "wb").write(open(src, "rb").read())
        pcbnew.SaveBoard(tmp, self.board)
        dsn, ses = os.path.join(work, self.name + ".dsn"), os.path.join(work, self.name + ".ses")
        for f in (dsn, ses):
            if os.path.exists(f):
                os.remove(f)
        wb = pcbnew.LoadBoard(tmp)
        if not pcbnew.ExportSpecctraDSN(wb, dsn):
            raise SystemExit("DSN export failed")
        text = open(dsn, encoding="utf-8").read()
        text = text.replace("(type route)", "(type protect)").replace(",Default ", " ")
        open(dsn, "w", encoding="utf-8").write(text)
        java, jar = find_freerouting(freerouting_home)
        # Headless, telemetry off, settings file kept in the work folder rather
        # than in the user profile. The SMD fan-out stage scatters vias: off.
        cmd = [java, "-jar", jar, "--gui.enabled=false", "--user_data_path=" + os.path.join(work, "freerouting-user"),
               "--usage_and_diagnostic_data.disable_analytics=true", "--profile.allow_telemetry=false",
               "--router.fanout.enabled=%s" % ("true" if fanout else "false"), "--router.max_passes=%d" % passes,
               "-de", dsn, "-do", ses]
        scripted_nets = set(scripted_nets)
        best = None
        for attempt in range(1, max(1, tries, best_of) + 1):
            if os.path.exists(ses):
                os.remove(ses)
            say("running Freerouting (run %d of up to %d):" % (attempt, tries),
                " ".join(os.path.basename(c) if os.sep in c else c for c in cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=work)
            log = r.stdout + "\n" + r.stderr
            open(os.path.join(work, "freerouting.log"), "w", encoding="utf-8").write(log)
            if not os.path.exists(ses):
                raise SystemExit("Freerouting wrote no session file; see routing/work/freerouting.log")
            items = self.parse_ses(ses)
            mine = self._left_open(tmp, items, scripted_nets)
            kept = [t for t in items if t["net"] not in scripted_nets]
            far = sum(((t["x2"] - t["x1"]) ** 2 + (t["y2"] - t["y1"]) ** 2) ** 0.5
                      for t in kept if not t.get("via") and t["layer"] == "B")
            score = (sum(mine.values()), round(far), sum(1 for t in kept if t.get("via")))
            say("   run %d: %d open, %d mm on the far layer, %d vias" % ((attempt,) + score))
            if best is None or score < best[2]:
                best = (mine, items, score)
            if not best[0] and attempt >= best_of:
                break
            if mine:
                say("   left open on its own nets: %s" % ", ".join("%s x%d" % kv for kv in sorted(mine.items())))
        mine, new, _ = best
        if mine:
            say("WARNING: best of %d runs still leaves %s open -- script those connections, or move what blocks them"
                % (tries, ", ".join("%s x%d" % kv for kv in sorted(mine.items()))))
        dropped = [t for t in new if t["net"] in scripted_nets]
        new = [t for t in new if t["net"] not in scripted_nets]
        say("Freerouting added %d items on %d nets (%d more on scripted nets, dropped)"
            % (len(new), len({t["net"] for t in new}), len(dropped)))
        os.makedirs(os.path.dirname(self.auto_json), exist_ok=True)
        json.dump({"tool": os.path.basename(jar), "passes": passes, "items": new},
                  open(self.auto_json, "w", encoding="utf-8"), indent=0)
        return len(new)

    def _left_open(self, board_path, items, scripted_nets):
        """{net: unconnected count} on non-scripted nets if `items` were added
        to the board at `board_path`. Zones are not filled on the trial board,
        so pour-connected nets must be in `scripted_nets` (ground always is)."""
        trial = os.path.join(os.path.dirname(board_path), "trial.kicad_pcb")
        tb = pcbnew.LoadBoard(board_path)
        for t in items:
            if t["net"] in scripted_nets:
                continue
            if t.get("via"):
                self.via(t["net"], (t["x"], t["y"]), t["d"], t["drill"], board=tb)
            else:
                self.track(t["net"], [(t["x1"], t["y1"]), (t["x2"], t["y2"])], t["w"],
                           F if t["layer"] == "F" else B, board=tb)
        pcbnew.SaveBoard(trial, tb)
        out = os.path.join(os.path.dirname(board_path), "trial_drc.json")
        subprocess.run([self.cli, "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
                        "-o", out, trial], capture_output=True, text=True)
        d = json.load(open(out, encoding="utf-8"))
        left = {}
        for u in d.get("unconnected_items", []):
            nets = set(re.findall(r"\[([^\]]+)\]", " ".join(i["description"] for i in u.get("items", []))))
            for n in nets - set(scripted_nets):
                left[n] = left.get(n, 0) + 1
        return left

    def replay(self):
        """Draw the stored autorouter result. Returns the item count."""
        if not os.path.exists(self.auto_json):
            say("no routing/autoroute.json yet -- run with --autoroute")
            return 0
        data = json.load(open(self.auto_json, encoding="utf-8"))
        for t in data["items"]:
            if t["net"] not in self.nets:
                raise SystemExit("autoroute.json names net %s, which no longer exists -- run --autoroute" % t["net"])
            if t.get("via"):
                self.via(t["net"], (t["x"], t["y"]), t["d"], t["drill"])
            else:
                self.track(t["net"], [(t["x1"], t["y1"]), (t["x2"], t["y2"])], t["w"], F if t["layer"] == "F" else B)
        return len(data["items"])

    # ------------------------------------------------------------------- oracles
    def drc_report(self, show=True):
        """Full DRC on the saved board, every item with its board-mm position."""
        out = os.path.join(self.dir, "routing", "drc.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        subprocess.run([self.cli, "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
                        "--schematic-parity", "-o", out, self.path], capture_output=True, text=True)
        d = json.load(open(out, encoding="utf-8"))

        def where(it):
            p = it.get("pos", {})
            return "(%.2f, %.2f)" % (p.get("x", 0) - self.ox, p.get("y", 0) - self.oy)
        v = d.get("violations", [])
        say("DRC: %d violations, %d unconnected, %d parity" % (len(v), len(d.get("unconnected_items", [])),
                                                                 len(d.get("schematic_parity", []))))
        if show:
            for x in v:
                items = x.get("items", [])
                say("  %-8s %-22s %s  %s" % (x["severity"], x["type"], where(items[0]) if items else "",
                                             " | ".join(i["description"][:60] for i in items)))
            for x in d.get("unconnected_items", []):
                items = x.get("items", [])
                say("  unconnected  %s  %s" % (where(items[0]) if items else "",
                                               " | ".join(i["description"][:70] for i in items)))
        return d

    def via_in_pad(self):
        """Same-net vias inside SMD pads: DRC is silent about them."""
        hits = []
        for t in self.board.GetTracks():
            if t.GetClass() != "PCB_VIA":
                continue
            q = t.GetPosition()
            for f in self.board.GetFootprints():
                for p in f.Pads():
                    if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD and p.GetNetname() == t.GetNetname() and p.HitTest(q):
                        hits.append("%s.%s (%s) at (%.2f, %.2f)" % (f.GetReference(), p.GetNumber(), p.GetNetname(),
                                                                     tomm(q.x) - self.ox, tomm(q.y) - self.oy))
        return hits

    def report(self, far_layer=B, pairs=()):
        """What DRC does not say. Pours: how many separate pieces (a far-layer
        ground in more than one piece is a broken plane). Far-layer tracks:
        every one is a cut in that plane. Vias in pads. Pair skew."""
        bd = self.board
        length, vias = {}, {}
        for t in bd.GetTracks():
            n = t.GetNetname()
            if t.GetClass() == "PCB_VIA":
                vias[n] = vias.get(n, 0) + 1
            else:
                length[n] = length.get(n, 0.0) + tomm(t.GetLength())
        say("vias: %d (%s)" % (sum(vias.values()), ", ".join("%s %d" % kv for kv in sorted(vias.items(), key=lambda kv: -kv[1])[:4])))
        for p, n in pairs:
            say("pair %s / %s: %.2f / %.2f mm, skew %.2f mm, vias %d / %d"
                % (p, n, length.get(p, 0), length.get(n, 0), abs(length.get(p, 0) - length.get(n, 0)),
                   vias.get(p, 0), vias.get(n, 0)))
        cuts = {}
        for t in bd.GetTracks():
            if t.GetClass() != "PCB_VIA" and t.GetLayer() == far_layer:
                cuts[t.GetNetname()] = cuts.get(t.GetNetname(), 0.0) + tomm(t.GetLength())
        say("far-layer tracks (each one cuts that pour): " +
            (", ".join("%s %.1f mm" % kv for kv in sorted(cuts.items())) or "none"))
        for i in range(bd.GetAreaCount()):
            z = bd.GetArea(i)
            if z.GetIsRuleArea():
                continue
            ps = z.GetFilledPolysList(z.GetLayer())
            areas = sorted((abs(ps.Outline(k).Area()) / 1e12 for k in range(ps.OutlineCount())), reverse=True)
            say("zone %-20s %-6s %4.0f mm2 in %d piece(s)%s" % (
                z.GetZoneName(), bd.GetLayerName(z.GetLayer()), sum(areas), len(areas),
                "" if len(areas) < 2 else "  [largest %.0f, then %s]" % (areas[0], ", ".join("%.1f" % a for a in areas[1:6]))))
        vip = self.via_in_pad()
        say("via in pad: " + ("none" if not vip else "; ".join(vip)))
        return {"length": length, "vias": vias, "far_cuts": cuts, "via_in_pad": vip}
