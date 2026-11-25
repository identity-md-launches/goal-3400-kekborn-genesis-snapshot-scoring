#!/usr/bin/env python3
"""Rebuild the IMD holder list on Robinhood Chain (4663) from Transfer logs.
Usage: python3 collect.py [RPC_URL ...]   (first URL is primary; others are cross-check endpoints)
Read-only JSON-RPC: sends no transaction, signs nothing."""
from concurrent.futures import ThreadPoolExecutor
import json, sys, time, hashlib, urllib.request, urllib.error, datetime, os

TOKEN = "0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127"
SNAP = 122989778
CHAIN_ID = 4663
TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
PAY = ["0x4e0fa57bde726079356537e2f34d671e9f41adbc"]  # payTo
INTAKES = ["0xa43e6f75ee006411f79ac1c84120606c2330de82", "0x1397434cd35e8a9c8ac312a61d3a285eb31dea56"]  # fallback if capabilities unreachable
CAPS = "https://api.imd.fun/requests/capabilities"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "sources", "chain-robinhood")
URLS = sys.argv[1:] or ["https://rpc.mainnet.chain.robinhood.com"]
ERRORS = []
_id = [0]

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def rpc(method, params, url=None, tries=5):
    url = url or URLS[0]
    for a in range(tries):
        _id[0] += 1
        body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}).encode()
        req = urllib.request.Request(url, body, {"content-type": "application/json", "user-agent": "imd-snapshot-collector"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            if "error" in d:
                raise RuntimeError("rpc error %s" % json.dumps(d["error"])[:300])
            return d["result"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After") or 2)); continue
            err = "http %s" % e.code
        except RuntimeError as e:
            raise
        except Exception as e:
            err = repr(e)[:200]
        time.sleep(1 + a)
    raise RuntimeError("failed after retries: %s" % err)

def code_at(addr, block):
    return rpc("eth_getCode", [addr, hex(block)])

def first_block():
    lo, hi = 0, SNAP
    if code_at(TOKEN, hi) in ("0x", "", None):
        raise SystemExit("token has no code at snapshot block")
    while lo < hi:
        mid = (lo + hi) // 2
        if code_at(TOKEN, mid) not in ("0x", "", None):
            hi = mid
        else:
            lo = mid + 1
    return lo

def get_logs(a, b, url=None):
    return rpc("eth_getLogs", [{"address": TOKEN, "topics": [TOPIC], "fromBlock": hex(a), "toBlock": hex(b)}], url, tries=2)

def scan(start):
    chunks, logs = [], []
    a, size = start, 10000000
    while a <= SNAP:
        b = min(SNAP, a + size - 1)
        try:
            res = get_logs(a, b)
        except RuntimeError as e:
            if size == 1:
                raise
            size = max(1, size // 2); continue
        ch = {"from": a, "to": b, "logs": len(res), "endpoint": URLS[0]}
        if not res and len(URLS) > 1:
            confirms = []
            for u in URLS[1:]:
                try:
                    confirms.append({"endpoint": u, "logs": len(get_logs(a, b, u))})
                except Exception as e:
                    confirms.append({"endpoint": u, "error": str(e)[:200]})
            ch["crossCheck"] = confirms
        chunks.append(ch); logs += res
        print("chunk %d-%d logs=%d size=%d" % (a, b, len(res), size), flush=True)
        a = b + 1
        if len(res) < 2000: size = min(size * 2, 10000000)
    return chunks, logs

def call(data, block, to=TOKEN):
    return rpc("eth_call", [{"to": to, "data": data}, block if isinstance(block, str) else hex(block)])

def main():
    fetched = now()
    try:
        req = urllib.request.Request(CAPS, headers={"user-agent": "imd-snapshot-collector"})
        caps = json.load(urllib.request.urlopen(req, timeout=30))
        intakes = []
        for c in caps["onchain"]["chains"]:
            if c.get("chainId") == CHAIN_ID:
                intakes = [c["intake"].lower()] + [x.lower() for x in c.get("legacyIntakes", [])]
                PAY[:] = [c["payTo"].lower()]
        intake_src = CAPS
    except Exception as e:
        intakes = INTAKES; intake_src = "built-in fallback (capabilities unreachable: %r)" % e
        ERRORS.append({"what": "capabilities", "error": repr(e)[:200]})
    watch = set(PAY + intakes)

    try:
        fb = first_block(); fb_src = "eth_getCode bisection"
    except RuntimeError as e:
        fb = 0; fb_src = "eth_getCode bisection impossible (historical state not served: %s); scanned from genesis block 0" % str(e)[:160]
        ERRORS.append({"what": "firstBlockBisection", "error": str(e)[:200]})
    print("first block", fb, fb_src[:60], flush=True)
    chunks, logs = scan(fb)
    seen = set(); bal = {}; mint = burn = 0; dups = 0; pays = []
    for l in logs:
        key = (l["transactionHash"].lower(), int(l["logIndex"], 16))
        if key in seen: dups += 1; continue
        seen.add(key)
        if len(l["topics"]) != 3: ERRORS.append({"what": "non-standard Transfer log", "tx": key[0]}); continue
        f = "0x" + l["topics"][1][-40:].lower(); t = "0x" + l["topics"][2][-40:].lower()
        v = int(l["data"], 16) if l["data"] not in ("0x", "") else 0
        bal[f] = bal.get(f, 0) - v; bal[t] = bal.get(t, 0) + v
        if f == ZERO: mint += v
        if t == ZERO: burn += v
        if f in watch or t in watch:
            pays.append([key[0], key[1], int(l["blockNumber"], 16), None, f, t, str(v)])
    bal.pop(ZERO, None)
    negative = {k: str(v) for k, v in bal.items() if v < 0}
    holders = {k: v for k, v in bal.items() if v > 0}
    # block times for payment rows
    def btime(b):
        try:
            return b, datetime.datetime.fromtimestamp(int(rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as e:
            ERRORS.append({"what": "blockTime", "block": b, "error": str(e)[:200]}); return b, None
    with ThreadPoolExecutor(8) as ex:
        bt = dict(ex.map(btime, sorted({p[2] for p in pays})))
    for p in pays:
        p[3] = bt[p[2]]
    pays.sort(key=lambda p: (p[2], p[1]))
    # contract flags
    code_block = [SNAP]
    try:
        code_at(TOKEN, SNAP)
    except RuntimeError as e:
        code_block[0] = "latest"
        ERRORS.append({"what": "getCodeAtSnapshot", "error": "state at snapshot not served; isContract read at latest block instead: " + str(e)[:150]})
    def iscon(w):
        try:
            c = rpc("eth_getCode", [w, code_block[0] if isinstance(code_block[0], str) else hex(code_block[0])])
            return w, c not in ("0x", "", None)
        except Exception as e:
            ERRORS.append({"what": "getCode", "wallet": w, "error": str(e)[:200]}); return w, None
    with ThreadPoolExecutor(8) as ex:
        ic = dict(ex.map(iscon, sorted(holders)))
    rows = [[w, str(holders[w]), ic[w]] for w in sorted(holders)]
    total = sum(holders.values())
    try:
        ts = int(call("0x18160ddd", SNAP), 16); ts_src = "eth_call totalSupply() at block %d" % SNAP
    except Exception as e:
        ts = mint - burn; ts_src = "mints less burns (state not served: %s)" % str(e)[:150]
    try:
        ts_latest = str(int(call("0x18160ddd", "latest"), 16))
    except Exception as e:
        ts_latest = None
    # supply movement after the snapshot (mints/burns), to relate latest totalSupply to the snapshot
    try:
        post = rpc("eth_getLogs", [{"address": TOKEN, "topics": [TOPIC], "fromBlock": hex(SNAP + 1), "toBlock": "latest"}])
        pz = [l for l in post if l["topics"][1][-40:] == "0"*40 or l["topics"][2][-40:] == "0"*40]
        post_mb = len(pz)
    except Exception as e:
        post_mb = None
    # balanceOf spot check: top 25 by balance
    top = sorted(holders, key=lambda w: -holders[w])[:25]
    spot = []
    for w in top:
        try:
            r = int(call("0x70a08231" + w[2:].rjust(64, "0"), SNAP), 16)
            spot.append({"wallet": w, "onchain": str(r), "computed": str(holders[w]), "match": r == holders[w]})
        except Exception as e:
            spot.append({"wallet": w, "onchain": None, "computed": str(holders[w]), "match": None, "error": str(e)[:150]})
    digest = hashlib.sha256("".join("%s:%d\n" % (w, holders[w]) for w in sorted(holders)).encode()).hexdigest()
    checks = {
        "noNegativeBalance": not negative, "negativeBalances": negative,
        "sumEqualsTotalSupply": total == ts, "totalSupplySource": ts_src,
        "mintsLessBurnsEqualsSum": (mint - burn) == total,
        "balanceOfSpotCheck": {"checked": len(spot), "matched": sum(1 for s in spot if s["match"]), "rows": spot},
        "duplicateLogsDropped": dups,
        "totalSupplyLatest": ts_latest, "mintBurnLogsAfterSnapshot": post_mb,
        "latestSupplyEqualsSum": (ts_latest == str(total)) if ts_latest else None,
    }
    empty = [c for c in chunks if c["logs"] == 0]
    cov = {"chainId": CHAIN_ID, "token": TOKEN, "fromBlock": fb, "toBlock": SNAP}
    def dump(name, obj):
        with open(os.path.join(OUT, name), "w") as fh:
            json.dump(obj, fh, separators=(",", ":"), sort_keys=False)
    os.makedirs(OUT, exist_ok=True)
    dump("holders.json", {"source": "Transfer logs via eth_getLogs, %s" % ", ".join(URLS), "fetchedAt": fetched,
        "coverage": dict(cov, isContractReadAt=code_block[0]), "errors": [e for e in ERRORS if e["what"] in ("getCode", "getCodeAtSnapshot")],
        "columns": ["wallet", "balanceRaw", "isContract"], "rows": rows})
    dump("payments.json", {"source": "Transfer logs via eth_getLogs, %s; watched addresses from %s" % (", ".join(URLS), intake_src),
        "fetchedAt": fetched, "coverage": dict(cov, payTo=PAY, intakes=intakes),
        "errors": [e for e in ERRORS if e["what"] in ("blockTime", "capabilities")],
        "columns": ["txHash", "logIndex", "block", "blockTime", "from", "to", "valueRaw"], "rows": pays})
    dump("scan.json", {"source": "eth_getCode bisection + eth_getLogs scan", "fetchedAt": fetched, "coverage": cov, "errors": ERRORS,
        "columns": ["from", "to", "logs", "endpoint"], "rows": [[c["from"], c["to"], c["logs"], c["endpoint"]] for c in chunks],
        "firstBlock": fb, "firstBlockMethod": fb_src, "range": [fb, SNAP], "chunkCrossChecks": [c for c in chunks if "crossCheck" in c],
        "emptyChunks": len(empty), "firstLogBlock": min((int(l["blockNumber"], 16) for l in logs), default=None),
        "emptyChunkNote": "empty chunks are proven only by the supply check (mintsLessBurnsEqualsSum and latestSupplyEqualsSum) or crossCheck entries; no second endpoint was reachable unless crossCheck is present", "totalLogs": len(seen), "addressesSeen": len(bal),
        "holderCount": len(holders), "mintedRaw": str(mint), "burnedRaw": str(burn), "supplyRaw": str(ts),
        "sumRaw": str(total), "endpoints": URLS, "holdersDigest": digest, "checks": checks})
    print(json.dumps({"firstBlock": fb, "logs": len(seen), "holders": len(holders), "payments": len(pays),
        "sum": str(total), "supply": str(ts), "spot": checks["balanceOfSpotCheck"]["matched"], "neg": len(negative), "digest": digest}))

if __name__ == "__main__":
    main()
