#!/usr/bin/env python3
"""Offline check of the independent Ethereum verification (target e04 "ethereum").

  python3 -B tools/e04-chain-ethereum/check.py                 verify; exit 0 only if nothing failed
  python3 -B tools/e04-chain-ethereum/check.py --write-report  recompute and write report.json, then verify
  [--root DIR]  check another copy of the repository (used for tamper tests)

No network, Python 3 standard library only. It re-derives every result from the saved RPC
responses in sources/e04-chain-ethereum/ and the saved source files in sources/chain-ethereum/,
and compares that with tools/e04-chain-ethereum/report.json. A discrepancy, a changed pinned
file or a report that does not match the evidence is a failure. Reads that public endpoints
refused are listed as "unavailable"; they are never counted as passes.
"""
import argparse, hashlib, json, os, re, subprocess, sys

TOKEN = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
PAY_TO = "0x4e0fa57bde726079356537e2f34d671e9f41adbc"
BLOCK, T, T_UNIX = 26491756, "2026-11-25T11:00:00Z", 1795604400
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
TENDERLY = "https://gateway.tenderly.co/public/mainnet"
MEVBLOCKER = "https://rpc.mevblocker.io"
DRPC = "https://eth.drpc.org"
PUBLICNODE = "https://ethereum-rpc.publicnode.com"
EV_FILES = ["replay-tenderly.json", "replay-mevblocker.json", "windows.json", "balances.json", "code.json",
            "state-calls.json", "imd-capabilities.json"]
PINNED = ["snapshot.json", "sources/snapshot-blocks.json", "sources/chain-ethereum/holders.json",
          "sources/chain-ethereum/payments.json", "sources/chain-ethereum/scan.json",
          "tools/collect/chain-ethereum/collect.py", "tools/collect/chain-ethereum/check.py",
          "tools/e04-chain-ethereum/verify_chain.py"]


def sha_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class R:
    def __init__(self):
        self.items = []

    def add(self, name, status, detail):
        assert status in ("pass", "fail", "unavailable")
        self.items.append({"check": name, "status": status, "detail": detail})

    def ok(self, name, cond, detail):
        self.add(name, "pass" if cond else "fail", detail)


def res(c):
    r = c.get("response")
    return r["result"] if isinstance(r, dict) and r.get("result") is not None else None


def why(c):
    if "error" in c:
        return c["error"][:160]
    r = c.get("response")
    return json.dumps(r.get("error") if isinstance(r, dict) else r)[:160]


def canon(l):
    t = l["topics"]
    bad = (l["address"].lower() != TOKEN or len(t) != 3 or t[0].lower() != TRANSFER or l.get("removed")
           or len(l["data"]) != 66 or any(int(x[2:26], 16) for x in t[1:]))
    ts = l.get("blockTimestamp")
    return (int(l["blockNumber"], 16), int(l["logIndex"], 16), l["transactionHash"].lower(), "0x" + t[1][26:].lower(),
            "0x" + t[2][26:].lower(), int(l["data"], 16), int(ts, 16) if ts else None, bool(bad))


def build(root):
    r = R()
    J = lambda *p: json.load(open(os.path.join(root, *p)))
    ev = lambda n: J("sources", "e04-chain-ethereum", n)
    scan, hold, pay = (J("sources", "chain-ethereum", n + ".json") for n in ("scan", "holders", "payments"))
    snap = J("snapshot.json")["chains"]["ethereum"]
    H = {w: int(v) for w, v, _ in hold["rows"]}
    supply = int(scan["supplyRaw"])
    watched = {w["address"] for w in pay["watched"]}

    # ---- the source's own offline check
    p = subprocess.run([sys.executable, "-B", os.path.join(root, "tools", "collect", "chain-ethereum", "check.py"),
                        "--dir", os.path.join(root, "sources", "chain-ethereum")], capture_output=True, text=True, cwd=root)
    r.ok("source invariants: tools/collect/chain-ethereum/check.py", p.returncode == 0 and "all checks passed" in p.stdout,
         "exit %d, %d ok lines" % (p.returncode, p.stdout.count("\nok") + p.stdout.startswith("ok")))
    r.ok("source: scan pinned to snapshot.json block and hash", scan["snapshotBlock"] == snap["block"] == BLOCK
         and scan["snapshotBlockHash"] == snap["blockHash"] and scan["token"] == snap["token"] == TOKEN,
         "%d %s" % (BLOCK, snap["blockHash"]))
    r.ok("source: totals equal the brief's baseline", scan["totalLogs"] == 349832 and len(H) == scan["holderCount"] == 4840
         and scan["supplyRaw"] == "4210819525771000000000000" and sum(H.values()) == supply,
         "349832 logs, 4840 holders, supply 4210819525771000000000000")

    # ---- block, first block, supply on each endpoint
    calls = ev("state-calls.json")["calls"]
    for url in (TENDERLY, MEVBLOCKER, DRPC, PUBLICNODE):
        cs = [c for c in calls if c["url"] == url]
        blk = {int(c["request"]["params"][0], 16): c for c in cs if c["request"]["method"] == "eth_getBlockByNumber"}
        b0, b1 = res(blk[BLOCK]) if BLOCK in blk else None, res(blk[BLOCK + 1]) if BLOCK + 1 in blk else None
        if b0 and b1:
            r.ok("%s: block %d hash/time, next block links and is after T" % (url, BLOCK),
                 b0["hash"] == snap["blockHash"] and int(b0["timestamp"], 16) == snap["blockTimestamp"] <= T_UNIX
                 and b1["parentHash"] == b0["hash"] and int(b1["timestamp"], 16) > T_UNIX,
                 "%s %s, next %s at %s" % (b0["hash"], iso(int(b0["timestamp"], 16)), b1["hash"], iso(int(b1["timestamp"], 16))))
        else:
            r.add("%s: block %d hash/time" % (url, BLOCK), "unavailable", why(blk.get(BLOCK, {"error": "no call"})))
        gc = {int(c["request"]["params"][1], 16): c for c in cs if c["request"]["method"] == "eth_getCode"}
        g0, g1 = gc.get(scan["firstBlock"] - 1), gc.get(scan["firstBlock"])
        if g0 and g1 and res(g0) is not None and res(g1) is not None:
            r.ok("%s: token has no code at block %d and code at %d (first block)" % (url, scan["firstBlock"] - 1, scan["firstBlock"]),
                 res(g0) in ("0x", "0x0") and len(res(g1)) > 2, "code %d bytes" % ((len(res(g1)) - 2) // 2))
        else:
            r.add("%s: first block code" % url, "unavailable", why(g0 or {"error": "no call"}))
        sc = [c for c in cs if c["request"]["method"] == "eth_call" and c["request"]["params"][0]["data"] == "0x18160ddd"]
        if sc and res(sc[0]) is not None:
            r.ok("%s: totalSupply() at %d equals sum of holder balances" % (url, BLOCK), int(res(sc[0]), 16) == supply,
                 str(int(res(sc[0]), 16)))
        else:
            r.add("%s: totalSupply() at %d" % (url, BLOCK), "unavailable", why(sc[0]) if sc else "no call")
        bs = [c for c in cs if c["request"]["method"] == "eth_call" and c["request"]["params"][0]["data"].startswith("0x70a08231")]
        if bs:
            got = [(("0x" + c["request"]["params"][0]["data"][-40:]), res(c)) for c in bs]
            served = [(w, v) for w, v in got if v is not None]
            eq = sum(int(v, 16) == H.get(w, 0) for w, v in served)
            if served:
                r.ok("%s: %d deterministic balanceOf samples (scan.balanceOfSample) equal holders.json" % (url, len(got)),
                     eq == len(served) == len(got) == 25 and [w for w, _ in got] == scan["balanceOfSample"],
                     "%d of %d served, %d equal" % (len(served), len(got), eq))
            else:
                r.add("%s: balanceOf samples" % url, "unavailable", why(bs[0]))
    r.ok("block hash, time and supply agree on at least 3 public endpoints",
         sum(x["status"] == "pass" and "totalSupply" in x["check"] for x in r.items) >= 3, "tenderly, mevblocker, drpc")

    # ---- every balance
    B = ev("balances.json")
    rows = {w: v for w, v, _ in B["rows"]}
    pos = {w for w, v in rows.items() if v is not None and int(v) > 0}
    bad = [w for w, v in rows.items() if v is None or int(v) != H.get(w, 0)]
    r.ok("balanceOf at %d for every address the replay saw (%d, %s)" % (BLOCK, len(rows), B["endpoint"]),
         len(rows) == scan["addressesSeen"] and not B["unread"] and not bad and pos == set(H)
         and sum(int(rows[w]) for w in pos) == supply,
         "%d read, %d unread, %d differ, %d positive = holders.json, sum %d" % (len(rows), len(B["unread"]), len(bad), len(pos),
                                                                             sum(int(rows[w]) for w in pos)))

    # ---- code: EIP-7702 vs contracts
    C = ev("code.json")
    kinds = {w: (k, x) for w, k, n, x in C["rows"]}
    dl = {w: x for w, (k, x) in kinds.items() if k == "eip7702"}
    ct = {w for w, (k, x) in kinds.items() if k == "contract"}
    ic = {w: c for w, _, c in hold["rows"]}
    r.ok("eth_getCode of every holder: EIP-7702 delegated set equals scan.eip7702Delegated",
         set(kinds) == set(ic) and not C["unread"] and dl == {w: d for w, d in scan["eip7702Delegated"]} and len(dl) == 1514,
         "%d delegated (0xef0100 + delegate), %d with contract code, %d without code" % (len(dl), len(ct), len(kinds) - len(dl) - len(ct)))
    r.ok("isContract true is exactly delegated + contract code; balances of all of them retained",
         {w for w, c in ic.items() if c is True} == set(dl) | ct and {w for w, c in ic.items() if c is False} == set(kinds) - set(dl) - ct
         and all(H[w] > 0 for w in set(dl) | ct), "%d ordinary contracts, %d EIP-7702 wallets" % (len(ct), len(dl)))

    # ---- full replay
    for label in ("tenderly", "mevblocker"):
        X = ev("replay-%s.json" % label)
        if "unavailable" in X:
            r.add("full replay on %s" % X["endpoint"], "unavailable", "%s at %s" % (X["unavailable"], X["utc"]))
            continue
        W, at, adj = X["windows"], scan["firstBlock"], True
        starts = {c[0] for c in scan["rows"]}
        for lo, hi, n, *_ in W:
            adj &= lo == at and hi >= lo and lo in starts
            at = hi + 1
        r.ok("%s replay: %d windows adjacent, aligned to scan chunks, %d-%d" % (label, len(W), scan["firstBlock"], BLOCK),
             adj and at == BLOCK + 1, "%s to %s" % (X["startedUtc"], X["finishedUtc"]))
        t = X["totals"]
        r.ok("%s replay: per-chunk log counts equal all %d scan rows" % (label, len(scan["rows"])),
             X["chunkCounts"] == [c[2] for c in scan["rows"]] and sum(w[2] for w in W) == t["logs"] == scan["totalLogs"], t["logs"])
        r.ok("%s replay: no duplicate log, foreign/removed log or log outside range" % label,
             t["duplicateLogKeys"] == 0 and t["logProblems"] == {} and t["outsideRange"] == 0 and t["logsWithoutBlockTimestamp"] == 0, "0")
        r.ok("%s replay: mint/burn accounting equals scan and totalSupply" % label,
             [t["mints"], t["mintedRaw"], t["burns"], t["burnedRaw"]] == [scan["mints"], scan["mintedRaw"], scan["burns"], scan["burnedRaw"]]
             and int(t["mintedRaw"]) - int(t["burnedRaw"]) == supply and t["negative"] == 0,
             "%d mints %s, %d burns %s" % (t["mints"], t["mintedRaw"], t["burns"], t["burnedRaw"]))
        hd = hashlib.sha256("".join("%s:%s\n" % (w, v) for w, v, _ in hold["rows"]).encode()).hexdigest()
        r.ok("%s replay: holders digest, logs digest, addresses seen, every balance" % label,
             t["holdersDigest"] == scan["holdersDigest"] == hd and t["logsDigest"] == scan["logsDigest"]
             and t["addressesSeen"] == scan["addressesSeen"] and t["holders"] == len(H) and t["sumRaw"] == scan["sumRaw"]
             and X["holderMismatchCount"] == 0, "%s / %s" % (t["holdersDigest"], t["logsDigest"]))
        pd = hashlib.sha256("".join("%s:%d:%d:%s:%s:%s:%s\n" % tuple(x) for x in pay["rows"]).encode()).hexdigest()
        r.ok("%s replay: payment rows (incl. blockTime) equal payments.json" % label,
             X["paymentRowsEqualSaved"] is True and t["paymentRows"] == len(pay["rows"]) and t["paymentsDigest"] == pd
             and not X["paymentDiff"]["missingFromSaved"] and not X["paymentDiff"]["extraInSaved"], "%d rows, digest %s" % (t["paymentRows"], pd))

    # ---- deterministic windows on several endpoints, from the saved raw responses
    Wd = ev("windows.json")
    plan = Wd["plan"]
    groups = [("first chunk", plan["firstChunk"]), ("last chunk", plan["lastChunk"])] + [("payment window", w) for w in plan["paymentWindows"]]
    lists = {}
    for c in Wd["calls"]:
        f = c["request"]["params"][0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        g = next((i for i, (_, (a, b)) in enumerate(groups) if [a, b] == [lo, hi]), None)
        if g is None:      # a 100-block drpc piece of the first or last chunk
            g = next((i for i, (k, (a, b)) in enumerate(groups) if k.endswith("chunk") and a <= lo and hi <= b), None)
        lists.setdefault((g, c["url"]), []).append((lo, hi, c))
    prs = sorted((x[2], x[1]) for x in pay["rows"])
    for gi, (kind, (a, b)) in enumerate(groups):
        got, unavail = {}, []
        for url in (TENDERLY, MEVBLOCKER, DRPC):
            parts = sorted(lists.get((gi, url), []), key=lambda x: x[0])
            if not parts or any(res(c) is None for _, _, c in parts):
                unavail.append("%s: %s" % (url, why(next(c for _, _, c in parts if res(c) is None)) if parts else "not asked"))
                continue
            cover, at = True, a
            for lo, hi, _ in parts:
                cover &= lo == at
                at = hi + 1
            rows_ = sorted(canon(l) for _, _, c in parts for l in res(c))
            got[url] = rows_ if cover and at == b + 1 else None
        vals = [v for v in got.values() if v is not None]
        same = len(vals) >= 2 and all([x[:6] for x in v] == [x[:6] for x in vals[0]] for v in vals) and len(vals) == len(got)
        rows0 = vals[0] if vals else []
        clean = all(not x[7] for v in vals for x in v) and len({(x[0], x[1]) for x in rows0}) == len(rows0)
        exp = None
        for c in scan["rows"]:
            if [c[0], c[1]] == [a, b]:
                exp = c[2]
        pw = [[x[2], x[1], x[0], iso(x[6]) if x[6] else None, x[3], x[4], str(x[5])] for x in rows0 if x[3] in watched or x[4] in watched]
        saved = [x for x in pay["rows"] if a <= x[2] <= b]
        times_ok = all(p_[3] is None or p_[3] == s_[3] for p_, s_ in zip(pw, saved))
        cond = same and clean and (exp is None or exp == len(rows0)) and [x[:3] + x[4:] for x in pw] == [x[:3] + x[4:] for x in saved] and times_ok
        r.ok("window %s %d-%d equal on %s" % (kind, a, b, ", ".join(sorted(got))), cond,
             "%d logs%s, %d payment rows equal payments.json" % (len(rows0), "" if exp is None else " (scan chunk %d)" % exp, len(pw)))
        for u in unavail:
            r.add("window %s %d-%d on %s" % (kind, a, b, u.split(": ")[0]), "unavailable", u.split(": ", 1)[1])

    # ---- payment token, recipient, direction
    cap = ev("imd-capabilities.json")
    eth = [x["payment"] for x in cap.get("response", {}).get("actions", []) if isinstance(x.get("payment"), dict)
           and x["payment"].get("network") == "eip155:1"]
    chains = cap.get("response", {}).get("onchain", {}).get("chains", [])
    intakes = {}
    for c in chains:
        for x in [c.get("intake")] + list(c.get("legacyIntakes", [])):
            if x:
                intakes.setdefault(x.lower(), set()).add(c["chainId"])
    pin = [x for x in pay["rows"] if x[5] == PAY_TO]
    pout = [x for x in pay["rows"] if x[4] == PAY_TO]
    r.ok("payments: API job.open on eip155:1 pays this token to payTo; rows are into (in) or out of payTo",
         bool(eth) and all(e["asset"].lower() == TOKEN and e["payTo"].lower() == PAY_TO for e in eth)
         and len(pin) + len(pout) == len(pay["rows"]) and len(pin) == 7384 and len(pout) == 15,
         "%d in (%d of 0.5 IMD), %d out; %s at %s" % (len(pin), sum(x[6] == "500000000000000000" for x in pin), len(pout), cap.get("url"), cap.get("utc")))
    other = [w for w in watched if w != PAY_TO]
    c1 = [c for c in chains if c.get("chainId") == 1]
    named = {c1[0]["intake"].lower(), *[x.lower() for x in c1[0].get("legacyIntakes", [])]} if c1 else set()
    r.ok("payments: API chain 1 entry names this token, payTo and exactly the watched intakes; no Transfer touches an intake",
         len(c1) == 1 and c1[0]["asset"].lower() == TOKEN and c1[0]["payTo"].lower() == PAY_TO and named == set(other)
         and not [x for x in pay["rows"] if x[4] in other or x[5] in other],
         "intakes %s: 0 transfers; IMD moves payer -> payTo directly, so payment rows are payer->payTo" % ", ".join(sorted(other)))
    return r


def evidence_hashes(root):
    out = {}
    for n in EV_FILES:
        out["sources/e04-chain-ethereum/" + n] = sha_file(os.path.join(root, "sources", "e04-chain-ethereum", n))
    for p in PINNED:
        out[p] = sha_file(os.path.join(root, p))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    ap.add_argument("--write-report", action="store_true")
    a = ap.parse_args()
    root = a.root
    rp = os.path.join(root, "tools", "e04-chain-ethereum", "report.json")
    try:
        r = build(root)
    except Exception as e:  # a missing or malformed evidence file is a failure, not a crash
        print("FAIL  evidence unreadable: %s: %s" % (type(e).__name__, e))
        return 1
    if a.write_report:
        old = json.load(open(rp)) if os.path.exists(rp) else {}
        rep = dict(old.get("narrative", {}) and {"narrative": old["narrative"]} or {})
        rep.update({"target": "genesis-repair-e04/ethereum", "block": BLOCK, "snapshotTime": T, "token": TOKEN,
                    "hashes": evidence_hashes(root), "results": r.items})
        with open(rp, "w") as f:
            json.dump(rep, f, indent=1)
            f.write("\n")
    rep = json.load(open(rp))
    fails = 0
    for x in r.items:
        print("%-11s %s  (%s)" % (x["status"].upper(), x["check"], x["detail"]))
        fails += x["status"] == "fail"
    hs = evidence_hashes(root)
    for p, h in rep["hashes"].items():
        if hs.get(p) != h:
            print("FAIL        pinned file changed: %s (report %s, now %s)" % (p, h[:16], (hs.get(p) or "missing")[:16]))
            fails += 1
    if set(rep["hashes"]) != set(hs):
        print("FAIL        report hash list differs from the evidence file list")
        fails += 1
    if rep["results"] != r.items:
        print("FAIL        report.json results differ from what the saved evidence gives")
        fails += 1
    if not any(x["status"] == "pass" and x["check"].startswith("tenderly replay: holders digest") for x in r.items):
        print("FAIL        no full independent replay passed")
        fails += 1
    n = {s: sum(x["status"] == s for x in r.items) for s in ("pass", "fail", "unavailable")}
    print("pass %d, fail %d, unavailable %d" % (n["pass"], n["fail"], n["unavailable"]))
    print("FAILED %d" % fails if fails else "all checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
