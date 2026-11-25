#!/usr/bin/env python3
"""e04 order-evidence repair collector (resumable, bounded).

  python3 -B tools/e04-orders/collect.py baseline           # freeze the six orders.json once
  python3 -B tools/e04-orders/collect.py paid-by  --stop-at ISO --max-seconds N
  python3 -B tools/e04-orders/collect.py receipts --rpc-1 URL --rpc-4663 URL
  python3 -B tools/e04-orders/collect.py intake   --stop-at ISO --max-seconds N
  python3 -B tools/e04-orders/collect.py build              # offline: write orders.json, ledger, report

API reads are spaced 2.05 s apart (< 30 reads/minute) and honour 429 Retry-After.
Receipts are read over JSON-RPC (public endpoints: https://ethereum-rpc.publicnode.com,
https://rpc.mainnet.chain.robinhood.com) only to tell which saved payment txs emitted an
intake-contract log; read-only eth_getTransactionReceipt.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "sources" / "e04-orders"
API = "https://api.imd.fun"
GAP = 2.05
PUBLIC_RPC = {1: "https://ethereum-rpc.publicnode.com", 4663: "https://rpc.mainnet.chain.robinhood.com"}
PAID_COLS = ["wallet", "shard", "url", "fetchedAt", "httpStatus", "bodySha256", "bodyText", "error"]
INTAKE_COLS = ["chainId", "transactionHash", "wallet", "fetchedAt", "httpStatus", "bodySha256", "bodyText", "error"]
RECEIPT_COLS = ["chainId", "transactionHash", "wallet", "fetchedAt", "receiptStatus", "to", "intakeLogIndexes", "logCount", "error"]


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def envelope(path, source, coverage, errors, columns, rows):
    SRC.mkdir(parents=True, exist_ok=True)
    doc = {"source": source, "fetchedAt": now(), "coverage": coverage, "errors": errors, "columns": columns, "rows": rows}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(lib.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class Reader:
    def __init__(self):
        self.last = 0.0
        self.count = 0

    def get(self, url):
        """Return (status, text, error).  Text is the exact body, also for 4xx."""
        for attempt in range(5):
            wait = self.last + GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.count += 1
            try:
                req = Request(url, headers={"Accept": "application/json", "User-Agent": "imd-genesis-e04-orders/1"})
                with urlopen(req, timeout=40) as resp:
                    return resp.status, resp.read().decode("utf-8"), None
            except HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code == 429 and attempt < 4:
                    try:
                        time.sleep(max(2.0, float(exc.headers.get("Retry-After", "5"))))
                    except ValueError:
                        time.sleep(5)
                    continue
                return exc.code, body, "HTTP %s" % exc.code
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < 4:
                    time.sleep(2 + attempt)
                    continue
                return None, None, str(exc)
        return None, None, "retry exhaustion"


def baseline_doc():
    return load(SRC / "baseline-orders.json")


def baseline_map():
    doc = baseline_doc()
    out = {}
    for row in doc["rows"]:
        rec = dict(zip(doc["columns"], row))
        out[rec["shard"]] = rec
    return out


def cmd_baseline():
    if (SRC / "baseline-orders.json").exists():
        print("baseline exists; unchanged")
        return
    rows = []
    for shard in lib.SHARDS:
        path = ROOT / "sources" / ("api-wallets-" + shard) / "orders.json"
        data = path.read_bytes()
        doc = json.loads(data)
        rows.append([shard, "sources/api-wallets-%s/orders.json" % shard, lib.sha256_bytes(data), doc["source"],
                     doc["fetchedAt"], doc["coverage"], doc["errors"], doc["columns"], doc["rows"]])
    envelope(SRC / "baseline-orders.json", "git trunk 3041659afad50f140316c33d320d4677898a4eb7, sources/api-wallets-0X/orders.json",
             "the six shard orders.json documents byte-hashed and copied verbatim before the e04 repair", [],
             ["shard", "path", "fileSha256", "source", "fetchedAt", "coverage", "errors", "columns", "rows"], rows)
    print("baseline frozen", [(r[0], len(r[8])) for r in rows])


def wallets_with_orders():
    out = set()
    for rec in baseline_map().values():
        out.update(row[rec["columns"].index("wallet")] for row in rec["rows"])
    return sorted(out)


def cmd_paid_by(stop_at, max_seconds):
    path = SRC / "paid-by.json"
    doc = load(path)
    rows = doc["rows"] if doc else []
    ok = {r[0] for r in rows if r[4] == 200}
    wallets = wallets_with_orders()
    reader, start = Reader(), time.monotonic()
    for i, wallet in enumerate(wallets):
        if wallet in ok:
            continue
        if time.monotonic() - start > max_seconds or now() >= stop_at:
            break
        url = "%s/requests/paid-by/%s" % (API, wallet)
        status, text, error = reader.get(url)
        rows.append([wallet, lib.shard_of(wallet), url, now(), status, lib.sha256_text(text) if text is not None else None, text, error])
        if reader.count % 25 == 0:
            save_paid(rows, wallets)
    save_paid(rows, wallets)
    done = {r[0] for r in rows if r[4] == 200}
    print("paid-by ok=%d/%d reads=%d" % (len(done & set(wallets)), len(wallets), reader.count))


def save_paid(rows, wallets):
    envelope(SRC / "paid-by.json", API + "/requests/paid-by/{wallet} (public GET, newest first, at most 100)",
             "re-read for every wallet with saved orders in the six api-wallets shards (%d wallets); bodyText is the exact response body, bodySha256 its sha256" % len(wallets),
             [r[0] + ": " + str(r[7]) for r in rows if r[7]], PAID_COLS, rows)


def truncated_wallets():
    """Wallets whose saved list may be capped: flagged truncated, or a newest-100 list."""
    out = set()
    for shard in lib.SHARDS:
        doc = load(ROOT / "sources" / ("api-wallets-" + shard) / "wallets.json")
        cols = doc["columns"]
        for row in doc["rows"]:
            if row[cols.index("truncated")]:
                out.add(row[0])
    for wallet, (_f, body) in lib.latest_paid_by(load(SRC / "paid-by.json")["rows"]).items():
        if len(body["orders"]) >= 100 or body.get("count") == 100:
            out.add(wallet)
    return sorted(out)


def candidates():
    """Saved chain payment txs from truncated wallets, before T, not tied to a known order."""
    trunc = truncated_wallets()
    outputs, _ = lib.build({s: dict(r) for s, r in baseline_map().items()}, load(SRC / "paid-by.json")["rows"], [])
    known = {}
    for doc in outputs.values():
        ci = doc["columns"]
        for row in doc["rows"]:
            rec = dict(zip(ci, row))
            if rec["paymentTx"]:
                known.setdefault(rec["wallet"], set()).add(rec["paymentTx"].lower())
    oldest = {}
    for doc in outputs.values():
        for row in doc["rows"]:
            rec = dict(zip(doc["columns"], row))
            if rec["wallet"] in trunc and rec["createdAt"]:
                oldest[rec["wallet"]] = min(oldest.get(rec["wallet"], "9"), rec["createdAt"])
    out = []
    for chain, cid in (("ethereum", 1), ("robinhood", 4663)):
        doc = load(ROOT / "sources" / ("chain-" + chain) / "payments.json")
        c = doc["columns"]
        seen = set()
        for row in doc["rows"]:
            rec = dict(zip(c, row))
            w = rec["from"].lower()
            if w in trunc and rec["blockTime"] < lib.T and (cid, rec["txHash"]) not in seen:
                seen.add((cid, rec["txHash"]))
                linked = rec["txHash"].lower() in known.get(w, set())
                out.append({"chainId": cid, "transactionHash": rec["txHash"], "wallet": w, "block": rec["block"],
                            "blockTime": rec["blockTime"], "to": rec["to"], "valueRaw": rec["valueRaw"],
                            "linkedToSavedOrder": linked, "olderThanOldestSavedOrder": rec["blockTime"] < oldest.get(w, "")})
    return trunc, out


def rpc_batch(url, calls):
    body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]).encode()
    for attempt in range(4):
        try:
            req = Request(url, data=body, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return {d["id"]: d for d in data}
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                return {i: {"error": str(exc)} for i in range(len(calls))}
            time.sleep(2 + attempt)


def cmd_receipts(rpcs):
    trunc, cands = candidates()
    todo = [c for c in cands if not c["linkedToSavedOrder"]]
    path = SRC / "rpc-receipts.json"
    doc = load(path)
    rows = doc["rows"] if doc else []
    have = {(r[0], r[1]) for r in rows if r[8] is None}
    rows = [r for r in rows if (r[0], r[1]) in have]
    pending = [c for c in todo if (c["chainId"], c["transactionHash"]) not in have]
    for cid in (1, 4663):
        sub = [c for c in pending if c["chainId"] == cid]
        for i in range(0, len(sub), 10):
            chunk = sub[i:i + 10]
            time.sleep(0.5)
            res = rpc_batch(rpcs[cid], [("eth_getTransactionReceipt", [c["transactionHash"]]) for c in chunk])
            for j, c in enumerate(chunk):
                r = res.get(j, {})
                rec = r.get("result")
                if not isinstance(rec, dict):
                    rows.append([cid, c["transactionHash"], c["wallet"], now(), None, None, None, None, str(r.get("error") or "no receipt")])
                    continue
                idx = [int(l["logIndex"], 16) for l in rec.get("logs", []) if str(l.get("address", "")).lower() in lib.INTAKE_ADDRESSES]
                rows.append([cid, c["transactionHash"], c["wallet"], now(), rec.get("status"), (rec.get("to") or "").lower(), idx, len(rec.get("logs", [])), None])
    envelope(path, "eth_getTransactionReceipt via JSON-RPC; public endpoints %s" % json.dumps(PUBLIC_RPC),
             "receipts of saved pre-T payment txs (sources/chain-*/payments.json) from the truncated wallets %s that are not the paymentTx of a saved order; intakeLogIndexes lists logs emitted by the intake contracts %s" % (trunc, list(lib.INTAKE_ADDRESSES)),
             [r[1] + ": " + r[8] for r in rows if r[8]], RECEIPT_COLS, rows)
    print("receipts rows=%d intake-tx=%d errors=%d" % (len(rows), sum(1 for r in rows if r[6]), sum(1 for r in rows if r[8])))


def intake_queue():
    trunc, cands = candidates()
    rec = {(r[0], r[1]): r for r in load(SRC / "rpc-receipts.json")["rows"]}
    queue = [c for c in cands if not c["linkedToSavedOrder"] and rec.get((c["chainId"], c["transactionHash"]), [None] * 9)[6]]
    # Oldest-saved-order boundary first (those are the capped-away orders), newest first within.
    queue.sort(key=lambda c: (not c["olderThanOldestSavedOrder"], [-ord(ch) for ch in c["blockTime"]]))
    by_wallet = {}
    for c in queue:
        by_wallet.setdefault(c["wallet"], []).append(c)
    ordered, i = [], 0
    while any(by_wallet.values()):
        for w in sorted(by_wallet):
            if by_wallet[w]:
                ordered.append(by_wallet[w].pop(0))
    return trunc, cands, rec, ordered


def cmd_intake(stop_at, max_seconds):
    _t, _c, _r, queue = intake_queue()
    path = SRC / "intake.json"
    doc = load(path)
    rows = doc["rows"] if doc else []
    done = {(r[0], r[1]) for r in rows if r[4] in (200, 404)}
    reader, start = Reader(), time.monotonic()
    for c in queue:
        key = (c["chainId"], c["transactionHash"])
        if key in done:
            continue
        if time.monotonic() - start > max_seconds or now() >= stop_at:
            break
        url = "%s/intake/%s/%s" % (API, c["chainId"], c["transactionHash"])
        status, text, error = reader.get(url)
        rows.append([c["chainId"], c["transactionHash"], c["wallet"], now(), status, lib.sha256_text(text) if text is not None else None, text, error])
        done.add(key) if status in (200, 404) else None
        if reader.count % 25 == 0:
            save_intake(rows)
    save_intake(rows)
    print("intake rows=%d ok=%d 404=%d queue=%d reads=%d" % (len(rows), sum(1 for r in rows if r[4] == 200),
          sum(1 for r in rows if r[4] == 404), len(queue), reader.count))


def save_intake(rows):
    envelope(SRC / "intake.json", API + "/intake/{chainId}/{transactionHash} (public GET)",
             "probed only saved pre-T payment txs of truncated wallets whose receipt carries an intake-contract log; bodyText is the exact body",
             [r[1] + ": " + str(r[7]) for r in rows if r[7] and r[4] != 404], INTAKE_COLS, rows)


def cmd_build():
    base = baseline_map()
    paid = load(SRC / "paid-by.json")["rows"]
    intake_doc = load(SRC / "intake.json")
    intake = intake_doc["rows"] if intake_doc else []
    outputs, ledger = lib.build({s: dict(r) for s, r in base.items()}, paid, intake)
    for shard, doc in outputs.items():
        (ROOT / "sources" / ("api-wallets-" + shard) / "orders.json").write_text(lib.dump_shard(shard, doc), encoding="utf-8")
    envelope(SRC / "ledger.json", "derived: tools/e04-orders/lib.py build() over baseline-orders.json, paid-by.json, intake.json",
             "one row per correction, conflict, preserved capped order, new observation and duplicate; original values are kept in orders.json rows, observed values are here",
             [], ["shard", "wallet", "orderId", "kind", "field", "original", "observed", "source"], ledger)
    trunc, cands, rec, queue = intake_queue()
    probed = {(r[0], r[1]): r for r in intake if r[4] in (200, 404)}
    recovered = {(r[2]["payment"]["chainId"], r[2]["paymentTx"]) for r in []}
    new_intake_tx = {}
    for doc in outputs.values():
        for row in doc["rows"]:
            d = dict(zip(doc["columns"], row))
            if d["e04Observation"] == "new-intake" or "intake/" in (d["e04Source"] or ""):
                new_intake_tx.setdefault(d["paymentTx"], []).append(d["orderId"])
    out = []
    for c in cands:
        key = (c["chainId"], c["transactionHash"])
        r = rec.get(key)
        if c["linkedToSavedOrder"]:
            disp = "linked-to-saved-order"
        elif r is None or r[8]:
            disp = "unresolved:no-receipt-read" + ((": " + r[8]) if r else "")
        elif not r[6]:
            disp = "unresolved:no-intake-log (x402 or plain transfer; no public per-transaction order endpoint)"
        elif key not in probed:
            disp = "unattempted:intake-not-read-within-45-minute-bound"
        elif probed[key][4] == 404:
            disp = "unresolved:intake-404-unknown_transaction"
        elif c["transactionHash"] in new_intake_tx:
            disp = "intake-order:" + ",".join(new_intake_tx[c["transactionHash"]])
        else:
            disp = "unresolved:intake-200-without-order-for-this-payer"
        out.append(dict(c, disposition=disp))
    cols = ["chainId", "transactionHash", "wallet", "block", "blockTime", "to", "valueRaw", "linkedToSavedOrder", "olderThanOldestSavedOrder", "disposition"]
    envelope(SRC / "candidates.json", "derived from sources/chain-ethereum/payments.json, sources/chain-robinhood/payments.json, rpc-receipts.json, intake.json",
             "every saved pre-T payment tx from the truncated wallets %s with its disposition; no order is inferred from amount or time" % trunc,
             [], cols, [[c[k] for k in cols] for c in out])
    write_report(outputs, ledger, out)
    print("built", {s: len(d["rows"]) for s, d in outputs.items()}, "ledger", len(ledger), "candidates", len(out))


def file_hash(rel):
    return lib.sha256_bytes((ROOT / rel).read_bytes())


def write_report(outputs, ledger, cands):
    import collections
    kinds = collections.Counter("%s|%s|%s" % (r[0], r[3], r[4] or "") for r in ledger)
    per_shard = {}
    for shard, doc in outputs.items():
        rows = [dict(zip(doc["columns"], r)) for r in doc["rows"]]
        per_shard[shard] = {"rows": len(rows), "baselineRows": len(baseline_map()[shard]["rows"]),
                            "byObservation": dict(collections.Counter(r["e04Observation"] for r in rows)),
                            "beforeT": dict(collections.Counter(str(r["beforeT"]) for r in rows)),
                            "nullPaymentTx": sum(1 for r in rows if r["paymentTx"] is None)}
    tx_filled = [r for r in ledger if r[3] == "filled-null" and r[4] == "paymentTx"]
    pre_t = {(r[0], r[1]) for doc in outputs.values() for r in doc["rows"]
             if dict(zip(doc["columns"], r))["beforeT"] is True}
    unresolved = collections.Counter(c["disposition"].split(" (")[0] for c in cands if not c["disposition"].startswith(("linked", "intake-order")))
    paid = load(SRC / "paid-by.json")["rows"]
    intake = load(SRC / "intake.json")["rows"]
    report = {
        "target": "genesis-repair-e04/orders", "T": lib.T, "builtAt": now(),
        "commands": ["collect.py baseline", "collect.py paid-by (2 runs)", "collect.py receipts (2 runs; first hit RPC 429, retried)",
                     "collect.py intake (2 runs)", "collect.py build", "check.py"],
        "collectionWindow": {"paidByFirst": min(r[3] for r in paid), "paidByLast": max(r[3] for r in paid),
                             "intakeFirst": min((r[3] for r in intake), default=None), "intakeLast": max((r[3] for r in intake), default=None),
                             "boundMinutes": 45},
        "reads": {"paidBy": len(paid), "paidByOk": sum(1 for r in paid if r[4] == 200), "intake": len(intake),
                  "intakeOk": sum(1 for r in intake if r[4] == 200), "intake404": sum(1 for r in intake if r[4] == 404)},
        "recoveredPaymentTx": {"total": len(tx_filled), "byShard": dict(collections.Counter(r[0] for r in tx_filled)),
                               "beforeT": sum(1 for r in tx_filled if (r[1], r[2]) in pre_t)},
        "recoveredOrders": {"newIntake": sum(1 for r in ledger if r[3] == "new-intake"),
                            "newIntakeBeforeT": sum(1 for r in ledger if r[3] == "new-intake" and r[6] is True),
                            "newReread": sum(1 for r in ledger if r[3] == "new-reread"),
                            "newRereadBeforeT": sum(1 for r in ledger if r[3] == "new-reread" and r[6] is True)},
        "preservedOmittedByCap": [[r[1], r[2]] for r in ledger if r[3].startswith("original-only")],
        "conflicts": [r for r in ledger if r[3] == "conflict-kept-original"],
        "duplicates": [r for r in ledger if r[3].startswith("duplicate") or r[3] == "orderId-in-two-places"],
        "truncatedWallets": truncated_wallets(),
        "unresolvedCandidates": dict(unresolved),
        "unresolvedListedIn": "sources/e04-orders/candidates.json (disposition column)",
        "ledgerKinds": dict(kinds), "perShard": per_shard,
        "inputs": {rel: file_hash(rel) for rel in ["sources/e04-orders/baseline-orders.json", "sources/e04-orders/paid-by.json",
                   "sources/e04-orders/rpc-receipts.json", "sources/e04-orders/intake.json",
                   "sources/chain-ethereum/payments.json", "sources/chain-robinhood/payments.json"]},
        "baselineFileHashes": {s: r["fileSha256"] for s, r in baseline_map().items()},
        "outputs": {rel: file_hash(rel) for rel in ["sources/api-wallets-%s/orders.json" % s for s in lib.SHARDS]
                    + ["sources/e04-orders/ledger.json", "sources/e04-orders/candidates.json"]},
    }
    (ROOT / "tools" / "e04-orders" / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")


def main(argv):
    args = dict(zip(argv[2::2], argv[3::2]))
    cmd = argv[1]
    stop_at = args.get("--stop-at", "9999")
    max_seconds = float(args.get("--max-seconds", "560"))
    if cmd == "baseline":
        cmd_baseline()
    elif cmd == "paid-by":
        cmd_paid_by(stop_at, max_seconds)
    elif cmd == "receipts":
        cmd_receipts({1: args["--rpc-1"], 4663: args["--rpc-4663"]})
    elif cmd == "intake":
        cmd_intake(stop_at, max_seconds)
    elif cmd == "build":
        cmd_build()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
