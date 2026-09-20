#!/usr/bin/env python3
"""Check schlib.match_dp_suffix() against pcbnew's own pair matcher.

KiCad decides whether two nets are a differential pair by looking at their
names, in BOARD::MatchDpSuffix. schlib carries a reimplementation of that
rule so `lint_spec` can catch a bad pair name at spec time, before anything
is drawn. A copy of somebody else's rule is only worth having if it is
provably the same rule, so this asks KiCad.

For each candidate name we build a throwaway BOARD holding that name and the
complement our function predicts, then ask pcbnew's DpCoupledNet() what it
pairs the net with. Disagreement in either direction fails.

Run it with KiCad's own Python, from anywhere:

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" probe_diffpair.py

Last run: 2315 names, 0 mismatches (KiCad 10.0.6).
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcbnew                                   # noqa: E402
from schlib import match_dp_suffix              # noqa: E402

# the forms that actually turn up on a board, plus the edges: one-character
# names, names that are nothing but digits and underscores, a hierarchical
# path, lower case, and the DP/DM and P/M spellings that started all this
CURATED = [
    "USB_P", "USB_N", "USB_p", "USB_n", "USBP", "USBN", "USB+", "USB-",
    "USB_DP", "USB_DM", "USB_D+", "USB_D-", "TD1P", "TD1N", "TD1_P", "TD1_N",
    "TD_P1", "TD_N1", "TD_P_1", "TD_N_1", "LVDS_P0", "LVDS_N0", "LVDS0_P",
    "GPS_RF_IN", "VIN", "CAN_H", "CAN_L", "MAIN", "PLAN", "RESET_N", "CLK_P",
    "USB_P_", "USB_P_1", "USB_P12", "USB_2P", "A_P", "USB_Pa", "P", "N", "_P",
    "+", "-", "_", "1", "12_", "/Sheet Title/USB_P", "x/TD1M", "MX1P_INT",
    "SDA", "3V3", "+3V3", "GND",
]


def main(count=3000, seed=7):
    random.seed(seed)
    alphabet = "PNM+-_pn0123AB"
    names = list(dict.fromkeys(CURATED + [
        "".join(random.choice(alphabet) for _ in range(random.randint(1, 7)))
        for _ in range(count)]))

    bad = 0
    for i in range(0, len(names), 200):          # a fresh board per batch
        chunk = names[i:i + 200]
        board = pcbnew.BOARD()
        keep, want = [], {}
        for n in chunk:
            want[n] = match_dp_suffix(n)[1]
            for m in (n, want[n]):
                if m and board.FindNet(m) is None:
                    item = pcbnew.NETINFO_ITEM(board, m)
                    board.Add(item)
                    keep.append(item)            # see pcbnew-api.md: do not
        board.BuildListOfNets()                  # let the proxies be collected
        for n in chunk:
            net = board.FindNet(n)
            if net is None:
                continue
            other = board.DpCoupledNet(net)
            theirs = other.GetNetname() if other else None
            if theirs != want[n]:
                bad += 1
                if bad <= 20:
                    print(f"  MISMATCH {n!r}: schlib {want[n]!r}, KiCad {theirs!r}",
                          flush=True)
    print(f"{len(names)} names checked, {bad} mismatch(es)", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
