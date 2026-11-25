#!/usr/bin/env python3
"""Rebuild the IMD holder list on Ethereum at the snapshot block from Transfer logs alone.

  python3 tools/collect/chain-ethereum/collect.py --rpc URL [--rpc URL2 ...]
      [--rpc-label TEXT] [--cross-rpc URL ...] [--cache DIR] [--chunk N] [--workers N]

--rpc        endpoint that serves archive eth_getLogs, eth_getCode and eth_call. Repeat it to give
             fallbacks, tried in order. Needed: nothing is read from a default endpoint.
--rpc-label  what to write in the files for --rpc (default: the URLs). Use it when the URL holds a key.
--cross-rpc  another endpoint to repeat the state checks and sampled log windows on (repeatable).
--cache      directory for finished chunks, so that a stopped run resumes. Not a deliverable.

Reads only. Writes sources/chain-ethereum/{holders,payments,scan}.json. Exits 1 if a check failed
(the files are still written and scan.json says which), 2 if a read could not be completed.
Python 3 standard library only.
"""
import argparse, datetime, hashlib, json, os, random, re, sys, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

TOKEN = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
CHAIN_ID = 1
BLOCK = 26491756
T = "2026-11-25T11:00:00Z"
PAY_TO = "0x4e0fa57bde726079356537e2f34d671e9f41adbc"
CAPABILITIES = "https://api.imd.fun/requests/capabilities"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
PUBLIC = ["https://ethereum-rpc.publicnode.com", "https://gateway.tenderly.co/public/mainnet",
          "https://eth.drpc.org"]
LOG_CAP = 10000          # a reply this long may have been cut: halve and read again
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

RATE = re.compile(r"rate.?limit|too many requests|per second|throughput|capacity", re.I)


class RpcError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(text):
    """An error text with no URL in it, so a keyed endpoint never reaches a file."""
    return " ".join(re.sub(r"https?://\S+", "<url>", str(text)).split())[:200]


class Rpc:
    def __init__(self, urls, tries=6, timeout=90):
        self.urls, self.tries, self.timeout = urls, tries, timeout
        self.calls, self.lock = 0, threading.Lock()

    def _post(self, url, body):
        last = None
        for attempt in range(self.tries):
            wait = min(2 ** attempt, 20)
            req = urllib.request.Request(url, json.dumps(body).encode(),
                                         {"content-type": "application/json", "user-agent": "imd-snapshot/1"})
            with self.lock:
                self.calls += len(body) if isinstance(body, list) else 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    reply = json.loads(r.read())
            except urllib.error.HTTPError as e:
                text = e.read()[:500].decode("utf-8", "replace")
                last = RpcError("HTTP %d %s" % (e.code, clean(text)))
                if e.code == 429 or RATE.search(text):
                    ra = e.headers.get("Retry-After")
                    wait = min(int(ra), 120) if ra and ra.isdigit() else wait
                elif e.code < 500 or attempt >= 1:
                    raise last
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = RpcError("%s %s" % (type(e).__name__, clean(e)))
                if attempt >= 3:
                    raise last
            else:
                errs = [x.get("error") for x in (reply if isinstance(reply, list) else [reply]) if "error" in x]
                if not errs:
                    return reply
                last = RpcError(clean(json.dumps(errs[0])))
                if not RATE.search(str(errs[0])):
                    raise last
            time.sleep(wait)
        raise last

    def call(self, method, params):
        last = None
        for url in self.urls:
            try:
                reply = self._post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if "result" not in reply or reply["result"] is None:
                    raise RpcError("%s: no result" % method)
                return reply["result"]
            except RpcError as e:
                last = e
        raise last

    def batch(self, items):
        """[(method, params)] -> results in order, None for one that could not be read."""
        if not items:
            return []
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(items)]
        for url in self.urls:
            try:
                reply = self._post(url, body)
                got = {x["id"]: x.get("result") for x in reply}
                if len(got) == len(items) and None not in got.values():
                    return [got[i] for i in range(len(items))]
            except (RpcError, KeyError, TypeError, AttributeError):
                pass
        if len(items) == 1:
            return [None]
        mid = len(items) // 2
        return self.batch(items[:mid]) + self.batch(items[mid:])


def log_filter(lo, hi):
    return [{"address": TOKEN, "topics": [TRANSFER], "fromBlock": hex(lo), "toBlock": hex(hi)}]


def parse_log(l, lo, hi):
    """-> [block, logIndex, txHash, from, to, value, timestamp|None]; a log that is not ours is an error."""
    t = l["topics"]
    if l["address"].lower() != TOKEN or len(t) != 3 or t[0].lower() != TRANSFER or l.get("removed"):
        raise RpcError("unexpected log %s" % json.dumps(l)[:150])
    block = int(l["blockNumber"], 16)
    if not lo <= block <= hi or len(l["data"]) != 66 or any(int(x[2:26], 16) for x in t[1:]):
        raise RpcError("unexpected log %s" % json.dumps(l)[:150])
    ts = l.get("blockTimestamp")
    return [block, int(l["logIndex"], 16), l["transactionHash"].lower(), "0x" + t[1][26:].lower(),
            "0x" + t[2][26:].lower(), int(l["data"], 16), int(ts, 16) if ts else None]


def read_chunk(rpc, lo, hi):
    """-> [(lo, hi, rows)] covering lo..hi, halving whenever a range is refused or may be capped."""
    try:
        logs = rpc.call("eth_getLogs", log_filter(lo, hi))
        rows = sorted(parse_log(l, lo, hi) for l in logs)
        if len(rows) < LOG_CAP or lo == hi:
            return [(lo, hi, rows)]
    except RpcError as e:
        if lo == hi:
            raise RpcError("eth_getLogs block %d: %s" % (lo, e))
    mid = (lo + hi) // 2
    return read_chunk(rpc, lo, mid) + read_chunk(rpc, mid + 1, hi)


def word(x):
    return "%064x" % x


def balances_multicall(rpc, addrs):
    """balanceOf(addr) at BLOCK for many addresses in one eth_call to Multicall3.aggregate3."""
    n = len(addrs)
    d = "82ad56cb" + word(32) + word(n) + "".join(word(n * 32 + i * 192) for i in range(n))
    for a in addrs:
        d += word(int(TOKEN, 16)) + word(0) + word(96) + word(36)
        d += ("70a08231" + word(int(a, 16))).ljust(128, "0")
    out = rpc.call("eth_call", [{"to": MULTICALL3, "data": "0x" + d}, hex(BLOCK)])[2:]
    w = [int(out[i:i + 64], 16) for i in range(0, len(out), 64)]
    if len(w) < 2 or w[1] != n:
        raise RpcError("multicall returned %d results for %d" % (w[1] if len(w) > 1 else -1, n))
    res = []
    for i in range(n):
        p = 2 + w[2 + i] // 32
        if w[p] != 1 or w[p + 2] != 32:
            raise RpcError("multicall entry failed for %s" % addrs[i])
        res.append(w[p + 3])
    return res


def balance_of(rpc, a):
    return int(rpc.call("eth_call", [{"to": TOKEN, "data": "0x70a08231" + word(int(a, 16))}, hex(BLOCK)]), 16)


def attempt(fn):
    """(value, None) or (None, reason)"""
    try:
        return fn(), None
    except RpcError as e:
        return None, str(e)


def find_first_block(rpc):
    lo, hi = 0, BLOCK
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if rpc.call("eth_getCode", [TOKEN, hex(mid)]) != "0x":
            hi = mid
        else:
            lo = mid
    return hi


def read_intakes():
    """Every intake contract the API names for this chain: [(address, role)], raw entry."""
    req = urllib.request.Request(CAPABILITIES, headers={"user-agent": "imd-snapshot/1"})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                caps = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 3:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 10))
    out = []
    for c in caps["onchain"]["chains"]:
        if c["chainId"] == CHAIN_ID:
            out.append((c["intake"].lower(), "intake"))
            out += [(a.lower(), "legacyIntake") for a in c.get("legacyIntakes", [])]
            if c["asset"].lower() != TOKEN or c["payTo"].lower() != PAY_TO:
                raise SystemExit("capabilities name another asset or payTo for chain 1: %s" % c)
    if not out:
        raise SystemExit("capabilities name no intake for chain 1")
    return out


def dump(path, obj):
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data), hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rpc", action="append", required=True)
    ap.add_argument("--rpc-label")
    ap.add_argument("--cross-rpc", action="append", default=[])
    ap.add_argument("--cache")
    ap.add_argument("--chunk", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(ROOT, "sources", "chain-ethereum"))
    a = ap.parse_args()
    rpc, label = Rpc(a.rpc), a.rpc_label or ", ".join(a.rpc)
    started, errors = now(), []

    snap = json.load(open(os.path.join(ROOT, "snapshot.json")))["chains"]["ethereum"]
    assert snap["block"] == BLOCK and snap["token"] == TOKEN
    if int(rpc.call("eth_chainId", []), 16) != CHAIN_ID:
        raise SystemExit("not chain 1")
    head = rpc.call("eth_getBlockByNumber", [hex(BLOCK), False])
    checks = []

    def check(name, ok, detail):
        checks.append([name, "passed" if ok else "not run" if ok is None else "failed", detail])
        print("check", checks[-1][1], name, "-", detail, flush=True)

    check("snapshot block hash and time equal snapshot.json",
          head["hash"] == snap["blockHash"] and int(head["timestamp"], 16) == snap["blockTimestamp"],
          "%s %s" % (head["hash"], iso(int(head["timestamp"], 16))))
    intakes = read_intakes()
    caps_at = now()
    watched = [(PAY_TO, "payTo")] + intakes

    first = find_first_block(rpc)
    print("first block", first, flush=True)

    # ---- logs
    chunks, cachef, read_at = {}, None, []
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
        path = os.path.join(a.cache, "chunks.jsonl")
        if os.path.exists(path):
            for line in open(path):
                try:
                    lo, hi, rows, at = json.loads(line)
                except ValueError:
                    continue          # a line cut by a stopped run
                chunks[lo] = (lo, hi, rows)
                read_at.append(at)
        cachef = open(path, "a")
    todo = []
    for lo in range(first, BLOCK + 1, a.chunk):
        hi, p = min(lo + a.chunk - 1, BLOCK), lo
        while p <= hi and p in chunks and chunks[p][1] <= hi:
            p = chunks[p][1] + 1
        if p <= hi:
            todo.append((p, hi))
    for lo in [x for x in chunks if not first <= x <= BLOCK]:
        del chunks[lo]
    cached = len(chunks)       # kept in --cache by an earlier run; their calls are not counted in this one
    print("chunks to read", len(todo), "cached", cached, flush=True)
    lock, done = threading.Lock(), [0]

    def work(r):
        got = read_chunk(rpc, *r)
        with lock:
            for c in got:
                chunks[c[0]] = c
                read_at.append(now())
                if cachef:
                    cachef.write(json.dumps([*c, read_at[-1]], separators=(",", ":")) + "\n")
                    cachef.flush()
            done[0] += 1
            if done[0] % 100 == 0:
                print("read", done[0], "of", len(todo), flush=True)

    try:
        with ThreadPoolExecutor(a.workers) as ex:
            futs = [ex.submit(work, r) for r in todo]
            try:
                for f in futs:
                    f.result()
            except RpcError:
                for f in futs:
                    f.cancel()
                raise
    except RpcError as e:
        print("log scan incomplete, nothing written:", e, file=sys.stderr)
        return 2
    order = sorted(chunks)
    p = first
    for lo in order:                                       # contiguous or bust
        assert lo == p, (lo, p)
        p = chunks[lo][1] + 1
    assert p == BLOCK + 1
    scanned_at = max(read_at)          # the last log read, also when a cached run is resumed

    # ---- balances
    bal, total, keys, lh, minted, burned = {}, 0, set(), hashlib.sha256(), 0, 0
    nmint = nburn = 0
    for lo in order:
        for b, i, tx, f, t, v, ts in chunks[lo][2]:
            total += 1
            assert (b, i) not in keys, (b, i)
            keys.add((b, i))
            lh.update(("%d:%d:%s:%s:%s:%d\n" % (b, i, tx, f, t, v)).encode())
            bal[f] = bal.get(f, 0) - v
            bal[t] = bal.get(t, 0) + v
            if f == ZERO:
                minted, nmint = minted + v, nmint + 1
            if t == ZERO:
                burned, nburn = burned + v, nburn + 1
    bal.pop(ZERO, None)
    negative = sorted(w for w, v in bal.items() if v < 0)
    holders = sorted(w for w, v in bal.items() if v > 0)
    total_sum = sum(bal[w] for w in holders)
    digest = hashlib.sha256("".join("%s:%d\n" % (w, bal[w]) for w in holders).encode()).hexdigest()
    print("logs", total, "seen", len(bal), "holders", len(holders), "digest", digest, flush=True)

    # ---- isContract
    codes = []
    items = [("eth_getCode", [w, hex(BLOCK)]) for w in holders]
    parts = [items[i:i + 50] for i in range(0, len(items), 50)]
    with ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(rpc.batch, parts):
            codes += r
    is_contract = [None if c is None else c not in ("0x", "0x0") for c in codes]
    delegated = [[w, "0x" + c[8:48].lower()] for w, c in zip(holders, codes)
                 if c and c.lower().startswith("0xef0100") and len(c) == 48]
    for w, c in zip(holders, codes):
        if c is None:
            errors.append({"read": "eth_getCode %s at %d" % (w, BLOCK), "error": "unread, isContract null"})

    # ---- checks
    check("no negative balance", not negative, "%d negative of %d addresses seen" % (len(negative), len(bal)))
    supply, why = attempt(lambda: int(rpc.call("eth_call", [{"to": TOKEN, "data": "0x18160ddd"}, hex(BLOCK)]), 16))
    if supply is None:
        supply, supply_from = minted - burned, "mints less burns: totalSupply() at the block was not served (%s)" % why
        check("balances sum to mints less burns", total_sum == supply, "%d, %d" % (total_sum, supply))
    else:
        supply_from = "eth_call totalSupply() at block %d" % BLOCK
        check("balances sum to totalSupply() at the block", total_sum == supply, "sum %d, supply %d" % (total_sum, supply))
        check("mints less burns equal totalSupply() at the block", minted - burned == supply,
              "%d less %d is %d" % (minted, burned, minted - burned))
    ranked = sorted(holders, key=lambda w: (-bal[w], w))
    sample = ranked[:5] + [ranked[5 + i * (len(ranked) - 6) // 19] for i in range(20)]  # 5 largest, 20 spread by rank
    sample = list(dict.fromkeys(sample))[:25]

    def balances25(r):
        got = [(w, balance_of(r, w)) for w in sample]
        bad = [w for w, v in got if v != bal[w]]
        return not bad, "%d of %d equal%s" % (len(got) - len(bad), len(got), ", differ: " + ",".join(bad) if bad else "")

    def balances_all(r):
        ws, bad = sorted(bal), []
        for i in range(0, len(ws), 400):
            part = ws[i:i + 400]
            bad += [w for w, v in zip(part, balances_multicall(r, part)) if v != bal[w]]
        return not bad, "%d of %d addresses seen equal (zero balances too), through Multicall3 %s%s" % (
            len(ws) - len(bad), len(ws), MULTICALL3, ", differ: " + ",".join(bad[:20]) if bad else "")

    def run(name, fn, r):
        got, why = attempt(lambda: fn(r))
        check(name, *(got if got else (None, why)))

    run("balanceOf at the block equals ours for 25 holders", balances25, rpc)
    run("balanceOf at the block equals ours for every address seen", balances_all, rpc)

    # ---- payments
    wset = {w for w, _ in watched}
    pay, missing = [], set()
    for lo in order:
        for b, i, tx, f, t, v, ts in chunks[lo][2]:
            if f in wset or t in wset:
                pay.append([b, i, tx, f, t, v, ts])
                if ts is None:
                    missing.add(b)
    times = {}
    if missing:
        blocks = sorted(missing)
        for i in range(0, len(blocks), 50):
            part = blocks[i:i + 50]
            for b, r in zip(part, rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in part])):
                times[b] = int(r["timestamp"], 16) if r else None
                if r is None:
                    errors.append({"read": "eth_getBlockByNumber %d" % b, "error": "unread, blockTime null"})
    prow = []
    for b, i, tx, f, t, v, ts in pay:
        ts = ts if ts is not None else times.get(b)
        prow.append([tx, i, b, iso(ts) if ts is not None else None, f, t, str(v)])
    pblocks = sorted({r[2] for r in prow if r[3]})
    psample = [pblocks[i * (len(pblocks) - 1) // 24] for i in range(25)] if len(pblocks) > 25 else pblocks

    def ptimes(r):
        want = {x[2]: x[3] for x in prow}
        bad = [b for b in psample if iso(int(r.call("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)) != want[b]]
        return not bad, "%d of %d sampled payment blocks equal" % (len(psample) - len(bad), len(psample))

    run("payment blockTime equals eth_getBlockByNumber", ptimes, rpc)

    # ---- the same on other endpoints
    endpoints = [{"endpoint": label, "used": "every read: logs, code, state, blocks", "calls": rpc.calls,
                  "chunksReadByAnEarlierRun": cached}]
    allrows = {}
    for lo in order:
        for r in chunks[lo][2]:
            allrows.setdefault(r[0], []).append(r[:6])
    logblocks = sorted(allrows)
    rnd = random.Random(BLOCK)
    windows = sorted(rnd.sample(logblocks, min(40, len(logblocks))))

    def same_logs(r):
        bad = n = served = 0
        reasons = {}
        for b in windows:
            if not served and sum(reasons.values()) >= 5:     # it refuses them all: stop asking
                break
            lo, hi = b, min(b + 99, BLOCK)
            logs, why = attempt(lambda: r.call("eth_getLogs", log_filter(lo, hi)))
            if logs is None:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            served += 1
            got = sorted(parse_log(l, lo, hi)[:6] for l in logs)
            want = [x for k in range(lo, hi + 1) for x in allrows.get(k, [])]
            n += len(want)
            bad += got != want
        if not served:
            raise RpcError("; ".join(reasons))
        return not bad, "%d of %d served windows of 100 blocks equal (%d logs), %d windows not served%s" % (
            served - bad, served, n, len(windows) - served, ": " + "; ".join(reasons) if reasons else "")

    for url in a.cross_rpc:
        r = Rpc([url], tries=4)
        before = len(checks)
        run("%s: snapshot block hash" % url, lambda r: ((lambda h: (h == snap["blockHash"], h))(
            r.call("eth_getBlockByNumber", [hex(BLOCK), False])["hash"])), r)
        run("%s: first block has code, the one before none" % url, lambda r: (
            r.call("eth_getCode", [TOKEN, hex(first)]) != "0x" and r.call("eth_getCode", [TOKEN, hex(first - 1)]) == "0x",
            "blocks %d and %d" % (first, first - 1)), r)
        run("%s: totalSupply() at the block equals the sum" % url, lambda r: ((lambda s: (s == total_sum, str(s)))(
            int(r.call("eth_call", [{"to": TOKEN, "data": "0x18160ddd"}, hex(BLOCK)]), 16))), r)
        run("%s: balanceOf at the block equals ours for 25 holders" % url, balances25, r)
        run("%s: balanceOf at the block equals ours for every address seen" % url, balances_all, r)
        run("%s: sampled log windows equal ours" % url, same_logs, r)
        mine = [c[1] for c in checks[before:]]
        endpoints.append({"endpoint": url, "used": "cross-check only: %d passed, %d failed, %d not served" % (
            mine.count("passed"), mine.count("failed"), mine.count("not run")), "calls": r.calls})
    endpoints += [{"endpoint": u, "used": "not called in this run", "calls": 0}
                  for u in PUBLIC if u not in [e["endpoint"] for e in endpoints]]

    # ---- files
    os.makedirs(a.out, exist_ok=True)
    src = "Ethereum JSON-RPC (chain id 1), Transfer logs of %s by eth_getLogs; public endpoints: %s" % (TOKEN, ", ".join(PUBLIC))
    rng = "blocks %d (the token's first) through %d, every Transfer(address,address,uint256) log" % (first, BLOCK)
    written = {}
    n, h = dump(os.path.join(a.out, "holders.json"), {
        "source": src, "fetchedAt": scanned_at,
        "coverage": "every address with a balance above zero at block %d (%s), pools and contracts too; balance = received less sent over %s. "
                    "isContract: eth_getCode at block %d is non-empty, which is also true of an EIP-7702 delegated account: scan.json lists those in eip7702Delegated." % (
                        BLOCK, snap["blockTimeUtc"], rng, BLOCK),
        "errors": [e for e in errors if "getCode" in e["read"]],
        "columns": ["wallet", "balanceRaw", "isContract"],
        "rows": [[w, str(bal[w]), c] for w, c in zip(holders, is_contract)]})
    written["holders.json"] = (len(holders), n, h)
    n, h = dump(os.path.join(a.out, "payments.json"), {
        "source": src + "; watched addresses from " + CAPABILITIES + " (read " + caps_at + ")", "fetchedAt": scanned_at,
        "coverage": "every Transfer to or from a watched address in %s. A transfer between two watched addresses is one row. "
                    "blockTime is the block's UTC time. A row is a token transfer, not proof of an order: refunds, sweeps and "
                    "transfers that bought nothing are here too." % rng,
        "errors": [e for e in errors if "getBlock" in e["read"]],
        "watched": [{"address": w, "role": role,
                     "in": sum(1 for r in prow if r[5] == w), "inRaw": str(sum(int(r[6]) for r in prow if r[5] == w)),
                     "out": sum(1 for r in prow if r[4] == w), "outRaw": str(sum(int(r[6]) for r in prow if r[4] == w))}
                    for w, role in watched],
        "columns": ["txHash", "logIndex", "block", "blockTime", "from", "to", "valueRaw"], "rows": prow})
    written["payments.json"] = (len(prow), n, h)
    crow = [[lo, chunks[lo][1], len(chunks[lo][2])] for lo in order]
    n, h = dump(os.path.join(a.out, "scan.json"), {
        "source": src, "fetchedAt": scanned_at,
        "coverage": rng + "; rows are the ranges read, with no gap or overlap, and the logs each returned",
        "errors": errors, "runStartedAt": started, "firstLogReadAt": min(read_at), "chainId": CHAIN_ID, "token": TOKEN, "snapshotBlock": BLOCK,
        "snapshotBlockHash": snap["blockHash"], "firstBlock": first, "range": [first, BLOCK],
        "chunkCount": len(crow), "totalLogs": total, "addressesSeen": len(bal),
        "addressesSeenNote": "the zero address is not counted", "holderCount": len(holders),
        "contractHolders": sum(1 for c in is_contract if c), "unreadCode": is_contract.count(None),
        "eip7702Delegated": delegated,
        "mints": nmint, "mintedRaw": str(minted), "burns": nburn, "burnedRaw": str(burned),
        "supplyRaw": str(supply), "supplyFrom": supply_from, "sumRaw": str(total_sum), "negativeBalances": negative,
        "holdersDigest": digest, "logsDigest": lh.hexdigest(),
        "logsDigestOf": "sha256 of the lines 'block:logIndex:txHash:from:to:value\\n' in block, logIndex order",
        "paymentRows": len(prow), "balanceOfSample": sample, "endpoints": endpoints, "checks": checks,
        "columns": ["fromBlock", "toBlock", "logs"], "rows": crow})
    written["scan.json"] = (len(crow), n, h)
    for name, (rows, size, h) in written.items():
        print("wrote %s rows %d bytes %d sha256 %s" % (name, rows, size, h))
    bad = [c for c in checks if c[1] == "failed"]
    print("calls", rpc.calls, "checks", len(checks), "failed", len(bad), "not run", sum(c[1] == "not run" for c in checks))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
