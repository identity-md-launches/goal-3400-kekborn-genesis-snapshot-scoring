#!/usr/bin/env python3
"""Independently verify sources/chain-ethereum/ against public Ethereum RPC endpoints at block 26491756.

  python3 -B tools/e04-chain-ethereum/verify_chain.py [--skip-replay LABEL ...] [--workers N]

Reads only (eth_getLogs, eth_call, eth_getCode, eth_getBlockByNumber, one public IMD API GET).
Writes sources/e04-chain-ethereum/*.json and tools/e04-chain-ethereum/report.json.
Never writes snapshot.json, sources/snapshot-blocks.json, holders.csv or sources/chain-ethereum/.
Python 3 standard library only. check.py verifies the written evidence offline.
"""
import argparse, datetime, hashlib, json, os, sys, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EV = os.path.join(ROOT, "sources", "e04-chain-ethereum")
SRC = os.path.join(ROOT, "sources", "chain-ethereum")
TOKEN = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
BLOCK = 26491756
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
CAPABILITIES = "https://api.imd.fun/requests/capabilities"
TENDERLY = "https://gateway.tenderly.co/public/mainnet"
MEVBLOCKER = "https://rpc.mevblocker.io"
DRPC = "https://eth.drpc.org"
PUBLICNODE = "https://ethereum-rpc.publicnode.com"
REPLAYS = {"tenderly": (TENDERLY, 25, 1), "mevblocker": (MEVBLOCKER, 5, 1)}  # url, scan chunks a call, workers
PACE = {MEVBLOCKER: 2.5, TENDERLY: 0.2, DRPC: 0.4}  # seconds between calls, to stay under public limits


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Err(Exception):
    pass


DEADLINE = [float("inf")]


def post(url, body, tries=5, timeout=120):
    """-> (raw bytes, parsed). Honors 429 / Retry-After; raises Err with the last error."""
    data = json.dumps(body).encode()
    last = None
    time.sleep(PACE.get(url, 0))
    for attempt in range(tries):
        if time.time() > DEADLINE[0]:
            raise Err("not attempted: this run's time budget (--budget) was used up")
        req = urllib.request.Request(url, data, {"content-type": "application/json", "user-agent": "imd-genesis-verify/1"})
        wait = min(2 ** attempt, 30)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            parsed = json.loads(raw)
            items = parsed if isinstance(parsed, list) else [parsed]
            errs = [x.get("error") for x in items if isinstance(x, dict) and x.get("error")]
            if errs and any("rate" in json.dumps(e).lower() or "limit exceeded" in json.dumps(e).lower() for e in errs):
                last = Err("rate limited: %s" % json.dumps(errs[0])[:200])
                time.sleep(wait)
                continue
            return raw, parsed
        except urllib.error.HTTPError as e:
            text = e.read()[:300].decode("utf-8", "replace")
            last = Err("HTTP %d %s" % (e.code, " ".join(text.split())[:200]))
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                wait = min(int(ra), 60) if ra and ra.isdigit() else max(wait, 15)
            elif e.code < 500:
                raise last
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = Err("%s %s" % (type(e).__name__, str(e)[:200]))
        time.sleep(wait)
    raise last


def rpc1(url, method, params, log=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    at = now()
    try:
        raw, p = post(url, body)
        rec = {"url": url, "utc": at, "request": body, "response": p, "responseSha256": sha(raw)}
    except Err as e:
        rec = {"url": url, "utc": at, "request": body, "error": str(e)}
    if log is not None:
        log.append(rec)
    return rec


def result(rec):
    r = rec.get("response")
    if isinstance(r, dict) and "result" in r and r["result"] is not None:
        return r["result"]
    return None


def canon_log(l):
    """-> (block, logIndex, tx, from, to, value, ts|None, problems[])"""
    probs = []
    t = l.get("topics", [])
    if l.get("address", "").lower() != TOKEN:
        probs.append("address")
    if len(t) != 3 or t[0].lower() != TRANSFER:
        probs.append("topics")
    if l.get("removed"):
        probs.append("removed")
    if len(l.get("data", "")) != 66:
        probs.append("data")
    ts = l.get("blockTimestamp")
    return (int(l["blockNumber"], 16), int(l["logIndex"], 16), l["transactionHash"].lower(),
            "0x" + t[1][26:].lower() if len(t) > 1 else "", "0x" + t[2][26:].lower() if len(t) > 2 else "",
            int(l["data"], 16) if l.get("data") not in (None, "0x") else 0, int(ts, 16) if ts else None, probs)


def line(r):
    return "%d:%d:%s:%s:%s:%d\n" % r[:6]


def load(name):
    return json.load(open(os.path.join(SRC, name + ".json")))


def dump(path, obj):
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print("wrote", os.path.relpath(path, ROOT), len(data), sha(data), flush=True)


def replay(label, scan, payments):
    url, per, workers = REPLAYS[label]
    chunks = scan["rows"]
    groups = [chunks[i:i + per] for i in range(0, len(chunks), per)]
    out, lock = {}, threading.Lock()
    started = now()

    def fetch(lo, hi):
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{"address": TOKEN, "topics": [TRANSFER], "fromBlock": hex(lo), "toBlock": hex(hi)}]}
        at = now()
        raw, p = post(url, body)
        if not isinstance(p, dict) or "result" not in p or not isinstance(p["result"], list):
            raise Err("eth_getLogs %d-%d: %s" % (lo, hi, json.dumps(p)[:200]))
        return at, raw, p["result"]

    def work(g):
        lo, hi = g[0][0], g[-1][1]
        try:
            res = [(lo, hi) + fetch(lo, hi)]
        except Err as e:
            if len(g) == 1:
                raise
            print(label, "split", lo, hi, e, flush=True)
            res = []
            for c in g:
                res.append((c[0], c[1]) + fetch(c[0], c[1]))
        with lock:
            for lo2, hi2, at, raw, logs in res:
                out[lo2] = (lo2, hi2, at, sha(raw), [canon_log(l) for l in logs])
            if len(out) % 20 == 0:
                print(label, "windows", len(out), flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(work, groups))
    finished = now()
    wins = [out[k] for k in sorted(out)]
    rows, windows = [], []
    for lo, hi, at, rsha, logs in wins:
        logs.sort()
        windows.append([lo, hi, len(logs), sha("".join(line(r) for r in logs).encode()), rsha, at])
        rows += logs
    # bucket into scan chunks
    starts = [c[0] for c in chunks]
    counts, ci = [0] * len(chunks), 0
    for r in rows:
        while ci + 1 < len(starts) and starts[ci + 1] <= r[0]:
            ci += 1
        counts[ci] += 1
    keys, dup, outside, probs, notime = set(), 0, 0, {}, 0
    bal, minted, burned, nm, nb = {}, 0, 0, 0, 0
    lh = hashlib.sha256()
    for r in rows:
        if (r[0], r[1]) in keys:
            dup += 1
        keys.add((r[0], r[1]))
        if not scan["firstBlock"] <= r[0] <= BLOCK:
            outside += 1
        for p in r[7]:
            probs[p] = probs.get(p, 0) + 1
        notime += r[6] is None
        lh.update(line(r).encode())
        b, i, tx, f, t, v = r[:6]
        bal[f] = bal.get(f, 0) - v
        bal[t] = bal.get(t, 0) + v
        if f == ZERO:
            minted, nm = minted + v, nm + 1
        if t == ZERO:
            burned, nb = burned + v, nb + 1
    bal.pop(ZERO, None)
    holders = sorted(w for w, v in bal.items() if v > 0)
    hd = sha("".join("%s:%d\n" % (w, bal[w]) for w in holders).encode())
    watched = {w["address"] for w in payments["watched"]}
    prow = [[r[2], r[1], r[0], iso(r[6]) if r[6] is not None else None, r[3], r[4], str(r[5])]
            for r in rows if r[3] in watched or r[4] in watched]
    saved_h = {w: int(v) for w, v, _ in load("holders")["rows"]}
    mism = [[w, str(bal.get(w, 0)), str(saved_h.get(w, 0))] for w in sorted(set(saved_h) | set(holders))
            if bal.get(w, 0) != saved_h.get(w, 0)]
    pd = lambda rs: sha("".join("%s:%d:%d:%s:%s:%s:%s\n" % tuple(x) for x in rs).encode())
    rec = {
        "endpoint": url, "method": "eth_getLogs address=token topics=[Transfer], windows of %d scan chunks" % per,
        "startedUtc": started, "finishedUtc": finished, "range": [chunks[0][0], chunks[-1][1]],
        "windowColumns": ["fromBlock", "toBlock", "logs", "linesSha256", "responseSha256", "readUtc"],
        "windows": windows, "chunkCounts": counts,
        "totals": {"logs": len(rows), "duplicateLogKeys": dup, "outsideRange": outside, "logProblems": probs,
                   "logsWithoutBlockTimestamp": notime, "mints": nm, "mintedRaw": str(minted), "burns": nb,
                   "burnedRaw": str(burned), "addressesSeen": len(bal), "holders": len(holders),
                   "negative": sum(v < 0 for v in bal.values()),
                   "sumRaw": str(sum(bal[w] for w in holders)), "holdersDigest": hd, "logsDigest": lh.hexdigest(),
                   "paymentRows": len(prow), "paymentsDigest": pd(prow)},
        "holderMismatches": mism[:200], "holderMismatchCount": len(mism),
        "paymentRowsEqualSaved": prow == payments["rows"],
        "paymentDiff": {"missingFromSaved": [x for x in prow if x not in payments["rows"]][:50],
                        "extraInSaved": [x for x in payments["rows"] if x not in prow][:50]},
    }
    return rec, sorted(bal)


MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def aggregate3(addrs):
    """calldata of Multicall3.aggregate3((address target, bool allowFailure, bytes callData)[])"""
    w = lambda x: "%064x" % x
    n = len(addrs)
    head, tails, off = [], [], 32 * n
    for a in addrs:
        cd = "70a08231" + "0" * 24 + a[2:]          # 36 bytes
        t = w(int(TOKEN, 16)) + w(0) + w(96) + w(36) + cd + "0" * 56
        head.append(w(off))
        tails.append(t)
        off += len(t) // 2
    return "0x82ad56cb" + w(32) + w(n) + "".join(head) + "".join(tails)


def decode_aggregate3(r):
    """-> balances as decimal strings, None where success is false or data is not 32 bytes"""
    b = bytes.fromhex(r[2:])
    u = lambda o: int.from_bytes(b[o:o + 32], "big")
    base = u(0)
    n = u(base)
    out = []
    for i in range(n):
        e = base + 32 + u(base + 32 + 32 * i)
        ok, doff = u(e), u(e + 32)
        ln = u(e + doff)
        out.append(str(u(e + doff + 32)) if ok and ln == 32 else None)
    return out


def state(scan, holders, seen):
    log = []
    S = {}
    sc = os.path.join(EV, "state-calls.json")
    if os.path.exists(sc):
        log = json.load(open(sc))["calls"]
        bad = [i for i, r in enumerate(log) if result(r) is None and PUBLICNODE != r["url"]]
        for i in bad:
            r = log[i]
            log[i] = rpc1(r["url"], r["request"]["method"], r["request"]["params"])
        log_done = True
    else:
        log_done = False
    # blocks
    for url in (TENDERLY, MEVBLOCKER, DRPC, PUBLICNODE) if not log_done else ():
        for n in (BLOCK, BLOCK + 1, scan["firstBlock"]):
            rpc1(url, "eth_getBlockByNumber", [hex(n), False], log)
        rpc1(url, "eth_getCode", [TOKEN, hex(scan["firstBlock"] - 1)], log)
        rpc1(url, "eth_getCode", [TOKEN, hex(scan["firstBlock"])], log)
        rpc1(url, "eth_call", [{"to": TOKEN, "data": "0x18160ddd"}, hex(BLOCK)], log)
    # 25 deterministic samples: the saved scan's own balanceOfSample list, single calls, on three endpoints
    for url in (TENDERLY, DRPC, MEVBLOCKER) if not log_done else ():
        for w in scan["balanceOfSample"]:
            rpc1(url, "eth_call", [{"to": TOKEN, "data": "0x70a08231" + "0" * 24 + w[2:]}, hex(BLOCK)], log)
            if url == TENDERLY:
                time.sleep(0.25)
    # every address seen by the replay: direct balanceOf batches (no multicall) on mevblocker
    bals, berr, via = {}, [], {}

    def bwork(part):
        body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                 "params": [{"to": TOKEN, "data": "0x70a08231" + "0" * 24 + w[2:]}, hex(BLOCK)]} for i, w in enumerate(part)]
        try:
            _, p = post(MEVBLOCKER, body)
            got = {x["id"]: x.get("result") for x in p}
            return [(w, got.get(i)) for i, w in enumerate(part)]
        except Err as e:
            return [(w, None) for w in part]

    old = os.path.join(EV, "balances.json")
    todo = seen
    if os.path.exists(old):
        o = json.load(open(old))
        for row in o["rows"]:
            bals[row[0]] = row[1]
            if len(row) > 2:
                via[row[0]] = row[2]
        todo = [w for w in seen if w not in bals]
    parts = [todo[i:i + 100] for i in range(0, len(todo), 100)]
    at = now() if todo == seen else o["utc"]
    with ThreadPoolExecutor(1) as ex:
        for res in ex.map(bwork, parts):
            for w, r in res:
                if r is None:
                    berr.append(w)
                else:
                    bals[w] = str(int(r, 16)) if r not in ("0x", "") else None
    left = [w for w in seen if w not in bals]
    mc = []
    for i in range(0, len(left), 400):
        part = left[i:i + 400]
        data = aggregate3(part)
        rec = rpc1(TENDERLY, "eth_call", [{"to": MULTICALL3, "data": data}, hex(BLOCK)])
        r = result(rec)
        if r is None:
            mc.append({"addresses": len(part), "utc": rec["utc"], "error": rec.get("error") or rec.get("response")})
            continue
        vals = decode_aggregate3(r)
        if len(vals) != len(part):
            mc.append({"addresses": len(part), "utc": rec["utc"], "error": "reply length"})
            continue
        for w, v in zip(part, vals):
            bals[w] = v
            via[w] = "multicall"
        mc.append({"addresses": len(part), "first": part[0], "last": part[-1], "utc": rec["utc"], "responseSha256": rec["responseSha256"],
                   "resultSha256": sha(r.encode())})
    berr = [w for w in seen if w not in bals]
    S["balances"] = {"endpoint": MEVBLOCKER, "utc": at, "finishedUtc": now(), "block": BLOCK,
                     "method": "JSON-RPC batches of 100 eth_call balanceOf(address) at block %d" % BLOCK,
                     "columns": ["address", "balanceRaw", "via"],
                     "via": {"direct": "%s JSON-RPC batch of eth_call balanceOf" % MEVBLOCKER,
                             "multicall": "%s eth_call Multicall3 %s aggregate3(balanceOf) at block %d, for addresses mevblocker did not serve" % (TENDERLY, MULTICALL3, BLOCK)},
                     "multicallCalls": mc,
                     "rows": [[w, bals[w], via.get(w, "direct")] for w in seen if w in bals],
                     "unread": berr}
    # code of every holder: classify EIP-7702 delegation
    codes, cerr = {}, []
    hw = [r[0] for r in holders]

    def cwork(part):
        body = [{"jsonrpc": "2.0", "id": i, "method": "eth_getCode", "params": [w, hex(BLOCK)]} for i, w in enumerate(part)]
        try:
            _, p = post(MEVBLOCKER, body)
            got = {x["id"]: x.get("result") for x in p}
            return [(w, got.get(i)) for i, w in enumerate(part)]
        except Err:
            return [(w, None) for w in part]

    old = os.path.join(EV, "code.json")
    todo, at = hw, now()
    if os.path.exists(old):
        o = json.load(open(old))
        for w, kind, n, x in o["rows"]:
            codes[w] = (kind, n, x)
        todo, at = [w for w in hw if w not in codes], o["utc"]
    with ThreadPoolExecutor(1) as ex:
        for res in ex.map(cwork, [todo[i:i + 50] for i in range(0, len(todo), 50)]):
            for w, c in res:
                if c is None:
                    cerr.append(w)
                else:
                    codes[w] = c.lower()
    crow = []
    for w in hw:
        if w not in codes:
            continue
        c = codes[w]
        if isinstance(c, tuple):
            crow.append([w, *c])
        elif c in ("0x", "0x0"):
            crow.append([w, "none", 0, None])
        elif c.startswith("0xef0100") and len(c) == 48:
            crow.append([w, "eip7702", 23, "0x" + c[8:48]])
        else:
            crow.append([w, "contract", (len(c) - 2) // 2, sha(bytes.fromhex(c[2:]))])
    S["code"] = {"endpoint": MEVBLOCKER, "utc": at, "finishedUtc": now(), "block": BLOCK,
                 "method": "JSON-RPC batches of 50 eth_getCode(address, block) for every positive holder",
                 "columns": ["address", "kind", "codeBytes", "delegateOrCodeSha256"], "rows": crow, "unread": cerr}
    S["calls"] = log
    return S


def windows(scan, payments):
    """Deterministic windows: whole first and last scan chunk, and 100-block windows at first/last/spread payments."""
    c = scan["rows"]
    p = payments["rows"]
    pick = [p[0], p[-1]] + [p[i * (len(p) - 1) // 9] for i in range(1, 9)]
    w100 = []
    for r in pick:
        lo = r[2] - 50
        if [lo, lo + 99] not in w100:
            w100.append([lo, lo + 99])
    first, last = c[0][:2], c[-1][:2]
    plan = []
    for url in (TENDERLY, MEVBLOCKER):
        for lo, hi in [first, last] + w100:
            plan.append((url, lo, hi))
    for lo, hi in [first, last]:
        for a in range(lo, hi + 1, 100):
            plan.append((DRPC, a, min(a + 99, hi)))
    for lo, hi in w100:
        plan.append((DRPC, lo, hi))
    log = []
    wp = os.path.join(EV, "windows.json")
    if os.path.exists(wp):
        log = json.load(open(wp))["calls"]
        for i, r in enumerate(log):
            if result(r) is None:
                log[i] = rpc1(r["url"], r["request"]["method"], r["request"]["params"])
        plan = []
    for url, lo, hi in plan:
        rpc1(url, "eth_getLogs", [{"address": TOKEN, "topics": [TRANSFER], "fromBlock": hex(lo), "toBlock": hex(hi)}], log)
        time.sleep(0.2 if url != DRPC else 0.4)
    return {"plan": {"firstChunk": first, "lastChunk": last, "paymentWindows": w100,
                     "drpcSplit": "drpc free plan serves at most 100 blocks a call: first and last chunk read in 100-block pieces"},
            "calls": log}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-replay", nargs="*", default=[])
    ap.add_argument("--budget", type=float, default=0, help="seconds; later reads are recorded as not attempted")
    ap.add_argument("--stages", nargs="*", default=["replay", "windows", "state", "api"])
    a = ap.parse_args()
    if a.budget:
        DEADLINE[0] = time.time() + a.budget
    scan, payments, holders = load("scan"), load("payments"), load("holders")["rows"]
    t0 = now()
    seen = None
    for label in REPLAYS:
        if label in a.skip_replay or "replay" not in a.stages:
            continue
        try:
            rec, s = replay(label, scan, payments)
            seen = seen or s
        except Err as e:
            rec = {"endpoint": REPLAYS[label][0], "unavailable": str(e), "utc": now()}
        dump(os.path.join(EV, "replay-%s.json" % label), rec)
    if seen is None:
        seen = seen_from_saved_replay() or sorted({r[0] for r in holders})
    if "windows" in a.stages:
        dump(os.path.join(EV, "windows.json"), windows(scan, payments))
    if "state" in a.stages:
        st = state(scan, holders, seen)
        dump(os.path.join(EV, "balances.json"), st["balances"])
        dump(os.path.join(EV, "code.json"), st["code"])
        dump(os.path.join(EV, "state-calls.json"), {"calls": st["calls"]})
    if "api" in a.stages:
        dump(os.path.join(EV, "imd-capabilities.json"), rpc_get(CAPABILITIES))
    print("collection", t0, "to", now())


def seen_from_saved_replay():
    """The addresses seen are the ones already in balances.json (written from the tenderly replay)."""
    p = os.path.join(EV, "balances.json")
    if os.path.exists(p):
        o = json.load(open(p))
        return sorted({r[0] for r in o["rows"]} | set(o["unread"]))
    return None


def rpc_get(url):
    at = now()
    try:
        req = urllib.request.Request(url, headers={"user-agent": "imd-genesis-verify/1", "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return {"url": url, "utc": at, "httpStatus": 200, "responseSha256": sha(raw), "response": json.loads(raw)}
    except Exception as e:
        return {"url": url, "utc": at, "error": "%s %s" % (type(e).__name__, str(e)[:200])}


if __name__ == "__main__":
    sys.exit(main())
