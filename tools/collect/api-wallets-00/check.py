#!/usr/bin/env python3
"""Offline check of sources/api-wallets-00/. Exit 0 only when every check passes.

- recomputes shard 0 of 6 from the landed holder lists
- wallets.json has exactly one row per shard wallet
- every source file is a compact {source, fetchedAt, coverage, errors, columns, rows}
  object with array rows of the column width
- orders/earnings/seats rows name only shard wallets; addresses lowercase
- beforeT agrees with createdAt < 2026-11-25T11:00:00Z
- truncated agrees with the order count (true at 100)
- per-wallet order and earnings counts agree with orders.json / earnings.json
Python 3 standard library only, no network.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT = os.path.join(ROOT, "sources", "api-wallets-00")
sys.path.insert(0, HERE)
from collect import load_holders, in_shard, parse_time, T  # noqa: E402

fails = []
passes = []


def check(name, ok, detail=""):
    (passes if ok else fails).append(name + (": " + detail if detail else ""))


def load(name):
    p = os.path.join(OUT, name)
    raw = open(p, "rb").read()
    d = json.loads(raw)
    check(name + " is an object with the six keys", isinstance(d, dict) and
          set(d) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"}, str(list(d)) if isinstance(d, dict) else "")
    check(name + " is compact JSON", raw == json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode())
    cols = d.get("columns") or []
    bad = [i for i, r in enumerate(d.get("rows") or []) if not isinstance(r, list) or len(r) != len(cols)]
    check(name + " rows are arrays of the column width", not bad, "bad rows %s" % bad[:5])
    print("%s: %d rows, %d errors, sha256 %s" % (name, len(d.get("rows") or []), len(d.get("errors") or []),
                                                 hashlib.sha256(raw).hexdigest()))
    return d


def main():
    _, allw = load_holders()
    shard = sorted(w for w in allw if in_shard(w))
    sset = set(shard)
    print("shard wallets: %d" % len(shard))
    files = {}
    for n in ("orders.json", "earnings.json", "earnings-launches.json", "wallets.json", "seats.json",
              "late-jobs.json"):
        if not os.path.exists(os.path.join(OUT, n)):
            check(n + " exists", False)
            continue
        files[n] = load(n)

    w = files.get("wallets.json")
    if w:
        c = w["columns"]
        wi = c.index("wallet")
        ws = [r[wi] for r in w["rows"]]
        check("wallets.json one row per shard wallet", len(ws) == len(set(ws)) and set(ws) == sset,
              "rows %d unique %d shard %d missing %d extra %d" % (len(ws), len(set(ws)), len(shard),
                                                                 len(sset - set(ws)), len(set(ws) - sset)))
        check("wallets.json wallets lowercase", all(x == x.lower() for x in ws))
        oc = {r[wi]: r[c.index("orders")] for r in w["rows"]}
        tr = {r[wi]: r[c.index("truncated")] for r in w["rows"]}
        ec = {r[wi]: r[c.index("earnings")] for r in w["rows"]}
        check("truncated is true exactly at 100 orders",
              all((tr[x] is None and oc[x] is None) or tr[x] == (oc[x] >= 100) for x in oc))
        o = files.get("orders.json")
        if o:
            oc2 = {}
            ci = o["columns"].index("createdAt")
            bi = o["columns"].index("beforeT")
            for r in o["rows"]:
                oc2[r[0]] = oc2.get(r[0], 0) + 1
            check("orders.json wallets are in the shard", set(oc2) <= sset)
            check("order counts agree with wallets.json",
                  all(oc.get(x) == n for x, n in oc2.items()) and
                  all(oc[x] in (None, 0) for x in oc if x not in oc2))
            badb = [r[1] for r in o["rows"] if r[bi] != ((parse_time(r[ci]) < T) if parse_time(r[ci]) else None)]
            check("beforeT agrees with createdAt < T", not badb, str(badb[:5]))
            ids = [r[1] for r in o["rows"]]
            check("orderIds unique per wallet", len(set(zip([r[0] for r in o["rows"]], ids))) == len(ids))
        e = files.get("earnings.json")
        if e:
            ec2 = {}
            for r in e["rows"]:
                ec2[r[0]] = ec2.get(r[0], 0) + 1
            check("earnings.json wallets are in the shard", set(ec2) <= sset)
            el = files.get("earnings-launches.json")
            li = e["columns"].index("launchId")
            if el:
                lids = {r[0] for r in el["rows"]}
                check("every earnings launchId is in earnings-launches.json",
                      {r[li] for r in e["rows"]} <= lids and len(lids) == len(el["rows"]))
            ai = e["columns"].index("amount") if "amount" in e["columns"] else None
            if ai is not None:
                check("earnings amounts are raw integer strings",
                      all(r[ai] is None or (isinstance(r[ai], str) and r[ai].isdigit()) for r in e["rows"]))
            check("earnings counts agree with wallets.json",
                  all(ec.get(x) == n for x, n in ec2.items()) and
                  all(ec[x] in (None, 0) for x in ec if x not in ec2))
        s = files.get("seats.json")
        if s:
            oi = s["columns"].index("owner")
            check("seats.json owners lowercase", all(r[oi] is None or r[oi] == r[oi].lower() for r in s["rows"]))
    for p in passes:
        print("PASS " + p)
    for f in fails:
        print("FAIL " + f)
    print("%d passed, %d failed" % (len(passes), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
