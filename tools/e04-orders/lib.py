"""Pure, offline merge logic for the e04 order-evidence repair.

Inputs (all saved under sources/e04-orders/):
  baseline-orders.json  the six shard orders.json files exactly as they were before e04
  paid-by.json          full raw /requests/paid-by/{wallet} response bodies (text + sha256)
  intake.json           full raw /intake/{chainId}/{tx} response bodies (text + sha256)

build() is deterministic: collect.py writes its output and check.py recomputes it and
compares byte-for-byte.  Standard library only.
"""
import hashlib
import json
from datetime import datetime, timezone

T = "2026-11-25T11:00:00Z"
T_DT = datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc)
SHARDS = ["00", "01", "02", "03", "04", "05"]
EXTRA = ["payment", "result", "e04Observation", "e04Source", "e04Conflicts", "createdAtSource"]
# Fields a re-read may fill when the original row held null: they were lost by a
# collector bug (payment.transactionHash read under another key, oracle-group ids).
FILLABLE = ("paymentTx", "resultJobId", "resultId", "paymentReason", "resultChildren", "continues")
# Fields compared between the original row and the re-read; the original value is kept.
COMPARED = ("action", "status", "createdAt", "paidAt", "paymentStatus", "paymentTx",
            "resultKind", "resultJobId", "resultId", "continues", "paymentReason")
INTAKE_ADDRESSES = ("0xa43e6f75ee006411f79ac1c84120606c2330de82", "0x1397434cd35e8a9c8ac312a61d3a285eb31dea56")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def before_t(value):
    """Strict createdAt < T; None when createdAt is missing or unparsable."""
    dt = parse_ts(value)
    return None if dt is None else dt < T_DT


def shard_of(wallet):
    return "%02d" % (int(hashlib.sha256(wallet.encode()).hexdigest()[:8], 16) % 6)


def result_ids(result):
    """(kind, jobId, resultId) from a raw result object, lists for oracle groups."""
    if not isinstance(result, dict):
        return None, None, None
    kind = result.get("kind")
    reqs = result.get("requests")
    if isinstance(reqs, list) and reqs:
        jobs = [q.get("jobId") for q in reqs if isinstance(q, dict)]
        rids = [q.get("requestId") for q in reqs if isinstance(q, dict)]
        return kind, jobs, rids
    rid = None
    for name in ("requestId", "workflowId", "goalId", "scheduleId", "id"):
        if result.get(name) is not None:
            rid = result.get(name)
            break
    return kind, result.get("jobId"), rid


def payment_tx(payment):
    if not isinstance(payment, dict):
        return None
    for name in ("transactionHash", "txHash", "paymentTx", "tx"):
        if payment.get(name):
            return payment.get(name)
    return None


def api_fields(wallet, order):
    """Column name -> value for one raw paid-by order (the corrected mapping)."""
    payment = order.get("payment") if isinstance(order.get("payment"), dict) else None
    result = order.get("result") if isinstance(order.get("result"), dict) else None
    kind, job_id, rid = result_ids(result)
    return {
        "wallet": wallet, "orderId": order.get("orderId"), "action": order.get("action"),
        "status": order.get("status"), "createdAt": order.get("createdAt"), "paidAt": order.get("paidAt"),
        "paymentStatus": (payment or {}).get("status"), "paymentReason": (payment or {}).get("reason"),
        "paymentTx": payment_tx(payment), "resultKind": kind, "resultJobId": job_id, "resultId": rid,
        "continues": (result or {}).get("continues"), "beforeT": before_t(order.get("createdAt")),
        "resultChildren": (result or {}).get("children") if kind == "goal" else None,
        "payment": payment, "result": result,
    }


def intake_fields(wallet, chain_id, tx, entry):
    admission = entry.get("admission") if isinstance(entry.get("admission"), dict) else {}
    result = admission.get("result") if isinstance(admission.get("result"), dict) else None
    order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
    kind, job_id, rid = result_ids(result)
    created = admission.get("createdAt")
    return {
        "wallet": wallet, "orderId": order.get("id"), "action": order.get("action") or entry.get("action"),
        "status": order.get("status"), "createdAt": created, "paidAt": None,
        "paymentStatus": entry.get("state"), "paymentReason": entry.get("reason"), "paymentTx": tx,
        "resultKind": kind, "resultJobId": job_id, "resultId": rid,
        "continues": (result or {}).get("continues"), "beforeT": before_t(created),
        "resultChildren": (result or {}).get("children") if kind == "goal" else None,
        "payment": dict(entry, chainId=chain_id, transactionHash=tx), "result": result,
    }


def same(a, b):
    if isinstance(a, str) and isinstance(b, str) and a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


def latest_paid_by(paid_rows):
    """wallet -> (fetchedAt, parsed body) of the newest successful read."""
    out = {}
    for wallet, _shard, _url, fetched, status, digest, text, _error in paid_rows:
        if status != 200 or text is None or sha256_text(text) != digest:
            continue
        body = json.loads(text)
        if not isinstance(body, dict) or not isinstance(body.get("orders"), list):
            continue
        if wallet not in out or fetched > out[wallet][0]:
            out[wallet] = (fetched, body)
    return out


def intake_orders(intake_rows):
    """List of (wallet, fetchedAt, chainId, tx, entry) with explicit order ids."""
    out = []
    for chain_id, tx, wallet, fetched, status, digest, text, _error in intake_rows:
        if status != 200 or text is None or sha256_text(text) != digest:
            continue
        body = json.loads(text)
        for entry in (body.get("entries") or []) if isinstance(body, dict) else []:
            if not isinstance(entry, dict):
                continue
            order = entry.get("order")
            payer = str(entry.get("payer") or "").lower()
            if entry.get("kind") == "request" and isinstance(order, dict) and order.get("id") and payer == wallet:
                out.append((wallet, fetched, chain_id, tx, entry))
    return out


def build(baseline, paid_rows, intake_rows):
    """Return ({shard: orders doc}, ledger rows)."""
    fresh = latest_paid_by(paid_rows)
    intake = intake_orders(intake_rows)
    ledger = []
    outputs = {}
    seen_global = {}
    for shard in SHARDS:
        base = baseline[shard]
        cols = list(base["columns"])
        out_cols = cols + [c for c in EXTRA if c not in cols]
        rows = []
        index = {}
        for raw in base["rows"]:
            row = dict(zip(cols, raw))
            key = (row["wallet"], row["orderId"])
            if key in index:
                prior = rows[index[key]]
                kind = "duplicate-identical" if [prior.get(c) for c in cols] == raw else "duplicate-conflict"
                ledger.append([shard, key[0], key[1], kind, None, None, raw, "baseline"])
                continue
            index[key] = len(rows)
            got = fresh.get(row["wallet"])
            order = None
            if got:
                order = next((o for o in got[1]["orders"] if isinstance(o, dict) and o.get("orderId") == row["orderId"]), None)
            conflicts = []
            if order is not None:
                api = api_fields(row["wallet"], order)
                for c in cols:
                    if c in FILLABLE and row.get(c) is None and api.get(c) is not None:
                        ledger.append([shard, key[0], key[1], "filled-null", c, None, api[c], "paid-by@" + got[0]])
                        row[c] = api[c]
                    elif c in COMPARED and not same(row.get(c), api.get(c)) and not (c in FILLABLE and api.get(c) is None):
                        conflicts.append([c, row.get(c), api.get(c)])
                        ledger.append([shard, key[0], key[1], "conflict-kept-original", c, row.get(c), api.get(c), "paid-by@" + got[0]])
                row["payment"] = api["payment"]
                if row.get("result") is None:
                    row["result"] = api["result"]
                row["e04Observation"] = "original+reread"
                row["e04Source"] = "baseline; paid-by@" + got[0]
            else:
                row["payment"] = None
                row.setdefault("result", None)
                if got:
                    capped = len(got[1]["orders"]) >= 100 or got[1].get("count") == 100
                    row["e04Observation"] = "original-only:omitted-by-newest-100" if capped else "original-only:absent-from-reread"
                    ledger.append([shard, key[0], key[1], row["e04Observation"], None, None, None, "paid-by@" + got[0]])
                else:
                    row["e04Observation"] = "original-only:not-reread"
                row["e04Source"] = "baseline"
            row["e04Conflicts"] = conflicts
            row["createdAtSource"] = "paid-by"
            rows.append(row)
        # New orders from re-read lists of this shard's wallets.
        new = []
        for wallet in sorted(w for w in fresh if shard_of(w) == shard):
            fetched, body = fresh[wallet]
            for order in body["orders"]:
                if not isinstance(order, dict) or (wallet, order.get("orderId")) in index:
                    continue
                row = api_fields(wallet, order)
                row.update(e04Observation="new-reread", e04Source="paid-by@" + fetched, e04Conflicts=[], createdAtSource="paid-by")
                index[(wallet, row["orderId"])] = -1
                new.append(row)
                ledger.append([shard, wallet, row["orderId"], "new-reread", "beforeT", None, row["beforeT"], "paid-by@" + fetched])
        all_rows = rows + new
        by_key = {(r["wallet"], r["orderId"]): r for r in all_rows}
        for wallet, fetched, chain_id, tx, entry in sorted(intake, key=lambda x: (x[0], x[2], x[3], x[4].get("logIndex") or 0)):
            if shard_of(wallet) != shard:
                continue
            src = "intake/%s/%s@%s" % (chain_id, tx, fetched)
            oid = entry["order"]["id"]
            if (wallet, oid) in by_key:
                existing = by_key[(wallet, oid)]
                if existing.get("paymentTx") is None:
                    existing["paymentTx"] = tx
                    ledger.append([shard, wallet, oid, "filled-null", "paymentTx", None, tx, src])
                elif not same(existing.get("paymentTx"), tx):
                    existing["e04Conflicts"].append(["paymentTx", existing.get("paymentTx"), tx])
                    ledger.append([shard, wallet, oid, "conflict-kept-original", "paymentTx", existing.get("paymentTx"), tx, src])
                else:
                    ledger.append([shard, wallet, oid, "intake-corroborates", "paymentTx", tx, tx, src])
                existing["e04Source"] += "; " + src
                continue
            row = intake_fields(wallet, chain_id, tx, entry)
            row.update(e04Observation="new-intake", e04Source=src, e04Conflicts=[], createdAtSource="intake.admission.createdAt")
            by_key[(wallet, oid)] = row
            all_rows.append(row)
            ledger.append([shard, wallet, oid, "new-intake", "beforeT", None, row["beforeT"], src])
        for r in all_rows:
            prev = seen_global.get(r["orderId"])
            if prev is not None and prev != (shard, r["wallet"]):
                ledger.append([shard, r["wallet"], r["orderId"], "orderId-in-two-places", None, list(prev), None, "merge"])
            seen_global.setdefault(r["orderId"], (shard, r["wallet"]))
        coverage = base["coverage"]
        note = ("e04 repair: original rows kept with original timestamps; re-read of /requests/paid-by/{wallet} "
                "fills null paymentTx from payment.transactionHash and null oracle-group ids, appends raw payment/result; "
                "rows absent from a newest-100 re-read are preserved; new orders (pre- and post-T, beforeT strict createdAt<T) "
                "and intake-recovered orders are appended with e04Observation; see sources/e04-orders/ledger.json")
        if isinstance(coverage, dict):
            coverage = dict(coverage, e04=note)
        else:
            coverage = str(coverage) + "; " + note
        outputs[shard] = {"source": base["source"], "fetchedAt": base["fetchedAt"], "coverage": coverage,
                          "errors": base["errors"], "columns": out_cols,
                          "rows": [[r.get(c) for c in out_cols] for r in all_rows]}
    return outputs, ledger


NO_TRAILING_NEWLINE = {"00"}  # matches each baseline file's own byte style (sha-verified)


def dump_shard(shard, doc):
    text = dumps(doc)
    return text[:-1] if shard in NO_TRAILING_NEWLINE else text


def dumps(doc):
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n"
