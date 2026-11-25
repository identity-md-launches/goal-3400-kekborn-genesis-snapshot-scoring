#!/usr/bin/env python3
"""Offline check for the e04 order-evidence repair.  Python standard library only.

Verifies, from saved files only:
  - every saved raw response body matches its sha256
  - the six orders.json files equal a fresh deterministic rebuild from baseline + raw responses
  - raw-response-to-row mapping: paymentTx from payment.transactionHash, raw payment/result objects
  - no original order id lost; original values unchanged except null fields filled
  - strict createdAt < T flags on every row
  - no duplicate (wallet, orderId) rows and no order id in two places; duplicate/conflict handling
  - oracle-group result.requests[] preserved with requestId/jobId (sample wallet 0xc9d591...)
  - regression sample aaf44900-... has its public transactionHash
  - fixtures: transactionHash, newest-100 cap truncation, post-T orders, duplicates, conflicts, intake
  - report.json hashes match the files on disk
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import lib  # noqa: E402

FAILS = []
PASSES = []


def ok(cond, msg):
    (PASSES if cond else FAILS).append(msg)
    print(("PASS " if cond else "FAIL ") + msg)
    return cond


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def envelope_ok(rel):
    doc = load(rel)
    return ok(isinstance(doc, dict) and set(doc) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"}
              and all(isinstance(r, list) and len(r) == len(doc["columns"]) for r in doc["rows"]), rel + " is a source envelope"), doc


def fixtures():
    fx = json.loads((HERE / "fixtures" / "cases.json").read_text())
    shard = lib.shard_of(fx["wallet"])
    baseline = {s: {"source": "fixture", "fetchedAt": "x", "coverage": "x", "errors": [], "columns": fx["columns"],
                    "rows": fx["baselineRows"] if s == shard else []} for s in lib.SHARDS}
    out, ledger = lib.build(baseline, fx["paidBy"], fx["intake"])
    doc = out[shard]
    rows = {r[1]: dict(zip(doc["columns"], r)) for r in doc["rows"]}
    ok(len(doc["rows"]) == len(rows), "fixture: duplicate baseline row merged once")
    for oid, expect in fx["expect"].items():
        got = rows.get(oid)
        ok(got is not None and all(got.get(k) == v for k, v in expect.items()),
           "fixture %s -> %s" % (oid, json.dumps(expect)))
    kinds = {r[3] for r in ledger}
    ok(all(k in kinds for k in fx["expectLedgerKinds"]), "fixture ledger kinds %s" % fx["expectLedgerKinds"])
    ok(all(r["beforeT"] is lib.before_t(r["createdAt"]) for r in rows.values()), "fixture strict createdAt<T flags")
    # A collector reading txHash/paymentTx would lose this hash; the corrected mapping does not.
    ok(lib.payment_tx({"transactionHash": "0xab", "status": "confirmed"}) == "0xab", "fixture payment.transactionHash mapping")


def main():
    fixtures()
    _, base_doc = envelope_ok("sources/e04-orders/baseline-orders.json")
    _, paid = envelope_ok("sources/e04-orders/paid-by.json")
    _, intake = envelope_ok("sources/e04-orders/intake.json")
    envelope_ok("sources/e04-orders/rpc-receipts.json")
    _, ledger_doc = envelope_ok("sources/e04-orders/ledger.json")
    _, cand = envelope_ok("sources/e04-orders/candidates.json")
    ok(all(r[5] == lib.sha256_text(r[6]) for r in paid["rows"] if r[6] is not None), "paid-by bodies match bodySha256 (%d)" % len(paid["rows"]))
    ok(all(r[5] == lib.sha256_text(r[6]) for r in intake["rows"] if r[6] is not None), "intake bodies match bodySha256 (%d)" % len(intake["rows"]))
    baseline = {}
    for row in base_doc["rows"]:
        rec = dict(zip(base_doc["columns"], row))
        baseline[rec["shard"]] = {k: rec[k] for k in ("source", "fetchedAt", "coverage", "errors", "columns", "rows")}
    outputs, ledger = lib.build(baseline, paid["rows"], intake["rows"])
    for shard in lib.SHARDS:
        rel = "sources/api-wallets-%s/orders.json" % shard
        ok((ROOT / rel).read_text(encoding="utf-8") == lib.dump_shard(shard, outputs[shard]), rel + " equals deterministic rebuild")
    ok(ledger_doc["rows"] == json.loads(json.dumps(ledger)), "ledger.json equals rebuild (%d rows)" % len(ledger))

    fresh = lib.latest_paid_by(paid["rows"])
    lost, changed, mapping_bad, flag_bad, keys, ids = [], [], [], [], set(), {}
    for shard in lib.SHARDS:
        doc = load("sources/api-wallets-%s/orders.json" % shard)
        cols = doc["columns"]
        rows = [dict(zip(cols, r)) for r in doc["rows"]]
        by_key = {}
        for r in rows:
            k = (r["wallet"], r["orderId"])
            if k in keys:
                lost.append(("duplicate", shard, k))
            keys.add(k)
            by_key[k] = r
            if ids.setdefault(r["orderId"], shard) != shard:
                lost.append(("two-shards", shard, k))
            if r["beforeT"] is not lib.before_t(r["createdAt"]):
                flag_bad.append(k)
        bcols = baseline[shard]["columns"]
        for raw in baseline[shard]["rows"]:
            b = dict(zip(bcols, raw))
            r = by_key.get((b["wallet"], b["orderId"]))
            if r is None:
                lost.append(("lost", shard, b["orderId"]))
                continue
            for c in bcols:
                if b[c] != r[c] and not (b[c] is None and c in lib.FILLABLE):
                    changed.append((shard, b["orderId"], c))
        for r in rows:
            if "paid-by@" not in (r["e04Source"] or ""):
                continue
            body = fresh[r["wallet"]][1]
            o = next((x for x in body["orders"] if x.get("orderId") == r["orderId"]), None)
            if o is None or r["payment"] != o.get("payment"):
                mapping_bad.append((r["orderId"], "payment"))
                continue
            tx = (o.get("payment") or {}).get("transactionHash")
            if tx and not lib.same(r["paymentTx"], tx):
                mapping_bad.append((r["orderId"], "paymentTx"))
            if r["result"] is not None and o.get("result") is not None and r["result"] != o.get("result"):
                mapping_bad.append((r["orderId"], "result"))
            reqs = (o.get("result") or {}).get("requests")
            if isinstance(reqs, list) and reqs and (r["resultJobId"] != [q.get("jobId") for q in reqs]
                                                    or r["resultId"] != [q.get("requestId") for q in reqs]):
                mapping_bad.append((r["orderId"], "group requests"))
    ok(not lost, "no original order id lost, no duplicate or two-shard ids (%d keys) %s" % (len(keys), lost[:5]))
    ok(not changed, "original values unchanged except filled nulls %s" % changed[:5])
    ok(not mapping_bad, "raw paid-by response -> row mapping (payment, paymentTx, result, group requests) %s" % mapping_bad[:5])
    ok(not flag_bad, "strict createdAt<T flag on every row %s" % flag_bad[:5])

    rows01 = {r[1]: dict(zip(load("sources/api-wallets-01/orders.json")["columns"], r)) for r in load("sources/api-wallets-01/orders.json")["rows"]}
    s = rows01.get("aaf44900-ccf7-414a-8964-14c474703078")
    ok(s is not None and s["paymentTx"] == "0x04be3a7cb71babea389a169203bf3c577cfa6094e67c13b4ce5d79d738bacef8"
       and s["payment"]["transactionHash"] == s["paymentTx"], "regression: aaf44900 paymentTx from payment.transactionHash")
    filled = [r for r in ledger if r[0] == "01" and r[3] == "filled-null" and r[4] == "paymentTx"]
    ok(len(filled) >= 737, "shard 01 recovered paymentTx count %d (brief: 739, 737 pre-T)" % len(filled))
    c9 = "0xc9d591e91869d49dabf0021681e043e9b52f3ac4"
    shard = lib.shard_of(c9)
    doc = load("sources/api-wallets-%s/orders.json" % shard)
    groups = [dict(zip(doc["columns"], r)) for r in doc["rows"] if r[0] == c9 and r[2] == "oracle.group"]
    ok(len(groups) == 2 and all(len(g["result"]["requests"]) == 2 and all(q.get("requestId") and q.get("jobId") for q in g["result"]["requests"])
                                and len(g["resultJobId"]) == 2 and len(g["resultId"]) == 2 for g in groups),
       "oracle-group requests preserved for %s (%d orders)" % (c9, len(groups)))
    capped = [r for r in ledger if r[3] == "original-only:omitted-by-newest-100"]
    ok(all((r[1], r[2]) in keys for r in capped), "orders omitted by newest-100 lists preserved (%d)" % len(capped))
    ok(all(r[-1] for r in cand["rows"]), "every candidate payment tx has a disposition (%d)" % len(cand["rows"]))

    report = json.loads((HERE / "report.json").read_text())
    for rel, digest in list(report["inputs"].items()) + list(report["outputs"].items()):
        ok(lib.sha256_bytes((ROOT / rel).read_bytes()) == digest, "report hash " + rel)
    ok(report["baselineFileHashes"] == {r[0]: r[2] for r in base_doc["rows"]}, "report baseline hashes")
    print("SUMMARY passed=%d failed=%d" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
