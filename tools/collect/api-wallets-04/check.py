#!/usr/bin/env python3
"""Offline check of sources/api-wallets-04/. Exit 0 only if every check passes.

Recomputes shard 4 of 6 from sources/chain-ethereum/holders.json and
sources/chain-robinhood/holders.json and requires exactly one wallets.json row per
shard wallet, plus shape and consistency checks across the output files.
Python 3, standard library only, no network.
"""
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter

OUT = os.path.join("sources", "api-wallets-04")
T = dt.datetime(2026, 11, 25, 11, 0, 0, tzinfo=dt.timezone.utc)
KEYS = ["source", "fetchedAt", "coverage", "errors", "columns", "rows"]
fails = 0


def check(name, ok, detail=""):
    global fails
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (": " + detail) if detail else ""))
    if not ok:
        fails += 1


def ts(s):
    return dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)


def load(name):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        check("%s exists" % name, False)
        return None
    raw = open(p, "rb").read()
    try:
        d = json.loads(raw)
    except ValueError as e:
        check("%s parses" % name, False, str(e))
        return None
    check("%s is {source,fetchedAt,coverage,errors,columns,rows}" % name,
          isinstance(d, dict) and sorted(d) == sorted(KEYS) and isinstance(d["rows"], list)
          and all(isinstance(r, list) and len(r) == len(d["columns"]) for r in d["rows"]),
          "%d rows, sha256 %s" % (len(d.get("rows", [])), hashlib.sha256(raw).hexdigest()))
    return d


def main():
    wallets = set()
    for p in ("sources/chain-ethereum/holders.json", "sources/chain-robinhood/holders.json"):
        d = json.load(open(p))
        iw = d["columns"].index("wallet")
        wallets.update(r[iw].lower() for r in d["rows"])
    mine = sorted(w for w in wallets if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 4)
    mineset = set(mine)
    print("shard 4 wallets from holder lists: %d (union %d)" % (len(mine), len(wallets)))

    W = load("wallets.json")
    O = load("orders.json")
    E = load("earnings.json")
    S = load("seats.json")
    L = load("late-jobs.json")
    if W is None:
        return 1
    c = W["columns"]
    ws = [r[c.index("wallet")] for r in W["rows"]]
    cnt = Counter(ws)
    check("wallets.json has one row per shard wallet", set(ws) == mineset and all(v == 1 for v in cnt.values()),
          "rows %d, shard %d, missing %d, extra %d, duplicates %d" % (
              len(ws), len(mine), len(mineset - set(ws)), len(set(ws) - mineset),
              sum(1 for v in cnt.values() if v > 1)))
    check("addresses lowercase", all(w == w.lower() and w.startswith("0x") and len(w) == 42 for w in ws))
    if O is not None:
        oc = O["columns"]
        iw, ic, ib = oc.index("wallet"), oc.index("createdAt"), oc.index("beforeT")
        per = Counter(r[iw] for r in O["rows"])
        check("orders wallets are shard wallets", set(per) <= mineset)
        bad = [r for r in W["rows"] if r[c.index("orders")] is not None and r[c.index("orders")] != per.get(r[0], 0)]
        check("wallets.orders == orders.json rows per wallet", not bad, "%d mismatches" % len(bad))
        badt = [r for r in W["rows"] if r[c.index("orders")] is not None
                and r[c.index("truncated")] != (r[c.index("orders")] >= 100)]
        check("truncated == (orders >= 100)", not badt)
        badb = [r for r in O["rows"] if r[ic] is not None and r[ib] != (ts(r[ic]) < T)]
        check("beforeT == createdAt < T", not badb, "%d mismatches" % len(badb))
        ids = [r[oc.index("orderId")] for r in O["rows"]]
        check("orderId unique", len(ids) == len(set(ids)))
    if E is not None:
        ec = E["columns"]
        per = Counter(r[ec.index("wallet")] for r in E["rows"])
        bad = [r for r in W["rows"] if r[c.index("earnings")] is not None and r[c.index("earnings")] != per.get(r[0], 0)]
        check("wallets.earnings == earnings.json rows per wallet", not bad, "%d mismatches" % len(bad))
        EL = load("earnings-launches.json")
        if EL is not None:
            lc = EL["columns"]
            nums = [r[lc.index("launchNumber")] for r in EL["rows"]]
            check("earnings-launches launchNumber unique", len(nums) == len(set(nums)))
            used = {r[ec.index("launchNumber")] for r in E["rows"]}
            check("every earnings launchNumber is in earnings-launches.json", used <= set(nums),
                  "%d missing" % len(used - set(nums)))
            iw_, ia = ec.index("rewardWork"), ec.index("amount")
            badr = [r for r in E["rows"] if r[iw_] is not None and not (0 <= int(r[iw_]) <= int(r[ia]))]
            check("rewardWork within amount", not badr, "%d rows" % len(badr))
    if S is not None:
        sc = S["columns"]
        per = Counter(r[sc.index("owner")] for r in S["rows"])
        check("seat owners are shard wallets", set(per) <= mineset)
        bad = [r for r in W["rows"] if r[c.index("seatsOwned")] is not None and r[c.index("seatsOwned")] != per.get(r[0], 0)]
        check("wallets.seatsOwned == seats.json rows per owner", not bad, "%d mismatches" % len(bad))
    if L is not None:
        lc = L["columns"]
        check("late-jobs wallets are shard wallets", {r[lc.index("wallet")] for r in L["rows"]} <= mineset)
    nerr = sum(len(r[c.index("errors")]) for r in W["rows"])
    print("INFO wallets with failed reads: %d (errors %d)" % (sum(1 for r in W["rows"] if r[c.index("errors")]), nerr))
    print("%s: %d failed" % ("OK" if not fails else "FAILED", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
