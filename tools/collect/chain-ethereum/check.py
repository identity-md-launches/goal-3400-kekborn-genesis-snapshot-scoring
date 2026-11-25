#!/usr/bin/env python3
"""Verify sources/chain-ethereum/*.json offline: sums, order, duplicates, digest, payments.

  python3 tools/collect/chain-ethereum/check.py [--dir sources/chain-ethereum]

No network. Prints one line a check; exits 1 if one failed. Python 3 standard library only.
"""
import argparse, hashlib, json, os, re, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TOKEN = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
PAY_TO = "0x4e0fa57bde726079356537e2f34d671e9f41adbc"
BLOCK, T, ZERO = 26491756, "2026-11-25T11:00:00Z", "0x" + "0" * 40
ADDR, TX, UINT = re.compile(r"0x[0-9a-f]{40}"), re.compile(r"0x[0-9a-f]{64}"), re.compile(r"0|[1-9][0-9]*")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
failed = 0


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, "  (%s)" % detail if detail != "" else ""))


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "sources", "chain-ethereum"))
    d = ap.parse_args().dir
    doc = {}
    for name, cols in (("holders", ["wallet", "balanceRaw", "isContract"]),
                       ("payments", ["txHash", "logIndex", "block", "blockTime", "from", "to", "valueRaw"]),
                       ("scan", ["fromBlock", "toBlock", "logs"])):
        raw = open(os.path.join(d, name + ".json"), "rb").read()
        o = doc[name] = json.loads(raw)
        check(name + ".json: shape", {"source", "fetchedAt", "coverage", "errors", "columns", "rows"} <= set(o)
              and o["columns"] == cols and all(isinstance(r, list) and len(r) == len(cols) for r in o["rows"])
              and bool(TIME.fullmatch(o["fetchedAt"])), "%d rows" % len(o["rows"]))
        check(name + ".json: compact", raw == json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode() + b"\n",
              "sha256 " + hashlib.sha256(raw).hexdigest())
    h, p, s = doc["holders"]["rows"], doc["payments"]["rows"], doc["scan"]

    snap = json.load(open(os.path.join(ROOT, "snapshot.json")))["chains"]["ethereum"]
    check("scan is of the snapshot's chain, token and block", s["chainId"] == snap["chainId"] == 1
          and s["token"] == snap["token"] == TOKEN and s["snapshotBlock"] == snap["block"] == BLOCK
          and s["snapshotBlockHash"] == snap["blockHash"])

    # holders
    ws = [r[0] for r in h]
    check("holders: lowercase addresses, no zero address", all(ADDR.fullmatch(w) for w in ws) and ZERO not in ws)
    check("holders: no duplicates, sorted", ws == sorted(set(ws)), "%d" % len(ws))
    check("holders: balances are raw integer strings above zero",
          all(isinstance(r[1], str) and UINT.fullmatch(r[1]) and r[1] != "0" for r in h))
    check("holders: isContract is true, false or null", all(r[2] in (True, False, None) and not is_int(r[2]) for r in h),
          "true %d false %d null %d" % tuple(sum(r[2] is v for r in h) for v in (True, False, None)))
    check("holders: each unread code is recorded as an error", sum(r[2] is None for r in h) == s["unreadCode"]
          == sum("getCode" in e["read"] for e in s["errors"]))
    check("holders: contract count and delegated accounts agree with scan", sum(r[2] is True for r in h) == s["contractHolders"]
          and {w for w, _ in s["eip7702Delegated"]} <= {r[0] for r in h if r[2] is True}, "%d delegated" % len(s["eip7702Delegated"]))
    total = sum(int(r[1]) for r in h)
    check("sum of balances equals scan sum and supply", str(total) == s["sumRaw"] == s["supplyRaw"], total)
    check("mints less burns equals supply", int(s["mintedRaw"]) - int(s["burnedRaw"]) == int(s["supplyRaw"]))
    check("holder count", len(h) == s["holderCount"] <= s["addressesSeen"])
    check("no negative balance recorded", s["negativeBalances"] == [])
    dg = hashlib.sha256("".join("%s:%s\n" % (r[0], r[1]) for r in h).encode()).hexdigest()
    check("holdersDigest", dg == s["holdersDigest"], dg)

    # scan
    c, at, gap = s["rows"], s["firstBlock"], False
    for lo, hi, n in c:
        gap |= not (is_int(lo) and is_int(hi) and is_int(n)) or lo != at or hi < lo or n < 0
        at = hi + 1
    check("chunks run from the first block to the snapshot block, no gap or overlap",
          not gap and at == BLOCK + 1 and s["range"] == [s["firstBlock"], BLOCK] and len(c) == s["chunkCount"], "%d chunks" % len(c))
    check("chunk log counts add up to totalLogs", sum(r[2] for r in c) == s["totalLogs"], s["totalLogs"])
    bad = [x for x in s["checks"] if x[1] == "failed"]
    check("no check failed when the files were collected", not bad, "%d passed, %d not run%s" % (
        sum(x[1] == "passed" for x in s["checks"]), sum(x[1] == "not run" for x in s["checks"]),
        "; failed: " + "; ".join(x[0] for x in bad) if bad else ""))
    req = [x for x in s["checks"] if x[0] in ("balances sum to totalSupply() at the block", "balances sum to mints less burns",
           "no negative balance", "balanceOf at the block equals ours for 25 holders")]
    check("the required checks were run", len(req) == 3 and all(x[1] == "passed" for x in req))

    # payments
    watched = [w["address"] for w in doc["payments"]["watched"]]
    check("payments: watched addresses are unique and hold the payment address",
          len(set(watched)) == len(watched) and PAY_TO in watched and all(ADDR.fullmatch(w) for w in watched), "%d" % len(watched))
    check("payments: hashes, addresses and amounts well formed", all(TX.fullmatch(r[0]) and is_int(r[1]) and is_int(r[2])
          and ADDR.fullmatch(r[4]) and ADDR.fullmatch(r[5]) and isinstance(r[6], str) and UINT.fullmatch(r[6]) for r in p))
    check("payments: no duplicates, in block and log order", [(r[2], r[1]) for r in p] == sorted({(r[2], r[1]) for r in p})
          and len({(r[0], r[1]) for r in p}) == len(p), "%d" % len(p))
    check("payments: count agrees with scan", len(p) == s["paymentRows"])
    check("payments: each touches a watched address", all(r[4] in watched or r[5] in watched for r in p))
    check("payments: blocks inside the scanned range", all(s["firstBlock"] <= r[2] <= BLOCK for r in p))
    tm = {}
    for r in p:
        tm.setdefault(r[2], set()).add(r[3])
    seq = [next(iter(tm[b])) for b in sorted(tm)]
    known = [x for x in seq if x]
    check("payments: one time a block, rising with the block, before T", all(len(x) == 1 for x in tm.values())
          and all(TIME.fullmatch(x) for x in known) and all(x < y for x, y in zip(known, known[1:])) and all(x < T for x in known),
          "%s to %s, %d null" % (known[0] if known else None, known[-1] if known else None, len(seq) - len(known)))
    per = {}
    for lo, hi, n in c:
        per[lo] = (hi, n)
    los, i, over, cnt = sorted(per), 0, False, {}
    for r in p:
        while i + 1 < len(los) and los[i + 1] <= r[2]:
            i += 1
        cnt[los[i]] = cnt.get(los[i], 0) + 1
    check("payments: no chunk holds more payments than logs", all(n <= per[lo][1] for lo, n in cnt.items()))
    hb = {r[0]: int(r[1]) for r in h}
    for w in doc["payments"]["watched"]:
        a = w["address"]
        inn, out = [r for r in p if r[5] == a], [r for r in p if r[4] == a]
        si, so = sum(int(r[6]) for r in inn), sum(int(r[6]) for r in out)
        check("payments: %s %s in less out equals its balance" % (w["role"], a), si - so == hb.get(a, 0)
              and [w["in"], w["inRaw"], w["out"], w["outRaw"]] == [len(inn), str(si), len(out), str(so)],
              "in %d out %d balance %d" % (len(inn), len(out), hb.get(a, 0)))
    print("FAILED %d" % failed if failed else "all checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
