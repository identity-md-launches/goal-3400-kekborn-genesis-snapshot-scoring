#!/usr/bin/env python3
"""Offline verification of sources/chain-robinhood/*.json. Exits 1 on any failure."""
import json, os, sys, hashlib, re
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "sources", "chain-robinhood")
fails = []
def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name); cond or fails.append(name)
L = lambda n: json.load(open(os.path.join(D, n)))
h, p, s = L("holders.json"), L("payments.json"), L("scan.json")
A = re.compile(r"^0x[0-9a-f]{40}$")
for n, o in (("holders", h), ("payments", p), ("scan", s)):
    ok(n + " has source/fetchedAt/coverage/errors/columns/rows", all(k in o for k in ("source", "fetchedAt", "coverage", "errors", "columns", "rows")))
rows = h["rows"]
ws = [r[0] for r in rows]
ok("holders: addresses lowercase 0x40", all(A.match(w) for w in ws))
ok("holders: no duplicate wallet", len(set(ws)) == len(ws))
ok("holders: no zero address", "0x" + "0" * 40 not in ws)
ok("holders: balances are positive integer strings", all(isinstance(r[1], str) and r[1].isdigit() and int(r[1]) > 0 for r in rows))
ok("holders: isContract is bool or null", all(r[2] in (True, False, None) for r in rows))
tot = sum(int(r[1]) for r in rows)
ok("sum of balances == scan.sumRaw", str(tot) == s["sumRaw"])
ok("sum of balances == scan.supplyRaw", str(tot) == s["supplyRaw"])
ok("mintedRaw - burnedRaw == sum", int(s["mintedRaw"]) - int(s["burnedRaw"]) == tot)
ok("holderCount matches", s["holderCount"] == len(rows))
dg = hashlib.sha256("".join("%s:%s\n" % (r[0], r[1]) for r in sorted(rows)).encode()).hexdigest()
ok("holdersDigest matches", dg == s["holdersDigest"])
ok("chunk log counts sum to totalLogs", sum(c[2] for c in s["rows"]) == s["totalLogs"] + s["checks"]["duplicateLogsDropped"])
ch = sorted(s["rows"])
ok("chunks contiguous from firstBlock to snapshot block", ch[0][0] == s["firstBlock"] and ch[-1][1] == s["coverage"]["toBlock"] == 122989778 and all(a[1] + 1 == b[0] for a, b in zip(ch, ch[1:])))
ok("no negative balances recorded", s["checks"]["noNegativeBalance"] and not s["checks"]["negativeBalances"])
keys = [(r[0], r[1]) for r in p["rows"]]
ok("payments: no duplicate (txHash, logIndex)", len(set(keys)) == len(keys))
watch = set(p["coverage"]["payTo"] + p["coverage"]["intakes"])
ok("payments: each row touches payTo or an intake", all(r[4] in watch or r[5] in watch for r in p["rows"]))
ok("payments: blocks within range, values integer strings", all(r[2] <= 122989778 and r[6].isdigit() for r in p["rows"]))
print("holders=%d payments=%d logs=%d sum=%s" % (len(rows), len(p["rows"]), s["totalLogs"], tot))
sys.exit(1 if fails else 0)
