#!/usr/bin/env python3
"""Read-only corroboration for the Robinhood IMD snapshot.

This is deliberately separate from the original full replay collector.  It
replays the exact saved primary chunk requests, records canonical response
digests, probes a second public endpoint on deterministic windows, and records
archive-read outcomes without substituting a latest-state answer.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SRC = os.path.join(ROOT, "sources", "chain-robinhood")
OUT = os.path.join(ROOT, "sources", "e04-chain-robinhood")
TOKEN = "0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127"
SNAP = 122989778
CHAIN_ID = "0x1237"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PRIMARY = "https://rpc.mainnet.chain.robinhood.com"
INDEPENDENT = "https://robinhood-rpc.publicnode.com"


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def raw_sha(value):
    return hashlib.sha256(value).hexdigest()


def rpc(url, method, params, request_id=1):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    body = json.dumps(request, separators=(",", ":")).encode()
    try:
        req = urllib.request.Request(url, body, {"content-type": "application/json", "user-agent": "imd-genesis-evidence/1"})
        with urllib.request.urlopen(req, timeout=120) as response:
            wire = response.read()
        decoded = json.loads(wire)
        if "error" in decoded:
            return {"request": request, "outcome": "rpc-error", "response": decoded["error"], "responseSha256": raw_sha(wire)}
        return {"request": request, "outcome": "ok", "result": decoded.get("result"), "responseSha256": raw_sha(wire)}
    except Exception as exc:
        return {"request": request, "outcome": "transport-error", "error": repr(exc)}


def log_key(log):
    """Full result-independent canonical projection used for response hashing."""
    return {k: log.get(k) for k in ("address", "blockHash", "blockNumber", "data", "logIndex", "removed", "topics", "transactionHash", "transactionIndex")}


def compact_log_response(answer):
    out = {"request": answer["request"], "outcome": answer["outcome"]}
    if answer["outcome"] != "ok":
        out.update({k: answer[k] for k in answer if k not in ("request", "outcome")})
        return out
    logs = answer["result"]
    canonical = [log_key(x) for x in logs]
    tuples = [[x.get("transactionHash", "").lower(), x.get("logIndex", "").lower()] for x in canonical]
    out.update({
        "responseSha256": answer["responseSha256"],
        "logCount": len(canonical),
        "canonicalLogsSha256": sha(canonical),
        "dedupKeySha256": sha(sorted(tuples)),
        "firstKey": min(tuples) if tuples else None,
        "lastKey": max(tuples) if tuples else None,
    })
    return out


def compact_call(answer):
    out = {"request": answer["request"], "outcome": answer["outcome"]}
    if answer["outcome"] == "ok":
        out.update({"result": answer["result"], "responseSha256": answer["responseSha256"]})
    else:
        out.update({k: answer[k] for k in answer if k not in ("request", "outcome")})
    return out


def log_params(a, b):
    return [{"address": TOKEN, "topics": [TRANSFER], "fromBlock": hex(a), "toBlock": hex(b)}]


def main():
    os.makedirs(OUT, exist_ok=True)
    scan = json.load(open(os.path.join(SRC, "scan.json")))
    holders = json.load(open(os.path.join(SRC, "holders.json")))
    payments = json.load(open(os.path.join(SRC, "payments.json")))
    chunks = scan["rows"]
    if any(len(x) < 3 for x in chunks):
        raise SystemExit("invalid saved scan chunks")

    # The three windows include deployment/first transfer, a payment-containing
    # range, and the snapshot tail.  They are intentionally fixed, not chosen
    # after seeing the independent endpoint response.
    windows = [[51500000, 52000000], [82500000, 83124999], [117538674, SNAP]]
    chain_ids = [{"endpoint": u, "observation": compact_call(rpc(u, "eth_chainId", []))}
                 for u in (PRIMARY, INDEPENDENT)]
    headers = [{"endpoint": u, "observation": compact_call(rpc(u, "eth_getBlockByNumber", [hex(SNAP), False]))}
               for u in (PRIMARY, INDEPENDENT)]
    sample_windows = []
    for a, b in windows:
        p = compact_log_response(rpc(PRIMARY, "eth_getLogs", log_params(a, b)))
        q = compact_log_response(rpc(INDEPENDENT, "eth_getLogs", log_params(a, b)))
        if p.get("outcome") != "ok" or q.get("outcome") != "ok":
            comparison = "unavailable"
        else:
            comparison = "match" if (p.get("logCount") == q.get("logCount") and p.get("canonicalLogsSha256") == q.get("canonicalLogsSha256")) else "discrepancy"
        sample_windows.append({"range": [a, b], "primary": p, "independent": q, "comparison": comparison})

    # Reissue every saved full-replay request.  Only canonical response digests
    # are retained here: the authoritative compact holder/payment datasets are
    # already saved in sources/chain-robinhood, while this keeps the evidence
    # reasonably sized and makes each raw response reproducible from its request.
    replay = []
    for n, chunk in enumerate(chunks, 1):
        a, b, expected = chunk[:3]
        observed = compact_log_response(rpc(PRIMARY, "eth_getLogs", log_params(a, b), n))
        if observed.get("outcome") != "ok":
            comparison = "unavailable"
        else:
            comparison = "match" if observed.get("logCount") == expected else "discrepancy"
        replay.append({"range": [a, b], "expectedLogCount": expected, "response": observed,
                       "comparison": comparison})
        print("replay %d/%d %d-%d %s" % (n, len(chunks), a, b, replay[-1]["comparison"]), flush=True)

    # Exactly 25 deterministic balanceOf reads: descending computed balance,
    # then lexical address.  Archive errors are data and remain unavailable.
    top = sorted(holders["rows"], key=lambda r: (-int(r[1]), r[0]))[:25]
    supply = compact_call(rpc(PRIMARY, "eth_call", [{"to": TOKEN, "data": "0x18160ddd"}, hex(SNAP)]))
    balances = []
    for wallet, computed, _flag in top:
        data = "0x70a08231" + wallet[2:].rjust(64, "0")
        observation = compact_call(rpc(PRIMARY, "eth_call", [{"to": TOKEN, "data": data}, hex(SNAP)]))
        balances.append({"wallet": wallet, "computedRaw": computed, "observation": observation})

    tip = compact_call(rpc(PRIMARY, "eth_blockNumber", []))
    latest_supply = compact_call(rpc(PRIMARY, "eth_call", [{"to": TOKEN, "data": "0x18160ddd"}, "latest"]))
    post = None
    if tip["outcome"] == "ok":
        post = compact_log_response(rpc(PRIMARY, "eth_getLogs", log_params(SNAP + 1, int(tip["result"], 16))))
        if post["outcome"] == "ok":
            # mint/burn is assessed from the canonical transfer list, not from
            # a provider-specific aggregate.
            answer = rpc(PRIMARY, "eth_getLogs", log_params(SNAP + 1, int(tip["result"], 16)), 999)
            if answer["outcome"] == "ok":
                zero = "0x" + "0" * 64
                post["mintOrBurnCount"] = sum(1 for x in answer["result"] if x.get("topics", [None, None, None])[1] == zero or x.get("topics", [None, None, None])[2] == zero)

    evidence = {
        "schema": "imd.e04.chain-robinhood.evidence.v1",
        "fetchedAt": utcnow(),
        "readOnly": True,
        "snapshot": {"chainId": 4663, "block": SNAP, "expectedUtc": "2026-11-25T11:00:00Z", "token": TOKEN},
        "endpoints": {"primary": PRIMARY, "independent": INDEPENDENT},
        "sourceFileSha256": {n: raw_sha(open(os.path.join(SRC, n), "rb").read()) for n in ("holders.json", "payments.json", "scan.json")},
        "chainIdObservations": chain_ids,
        "snapshotHeaderObservations": headers,
        "independentSelectedWindows": sample_windows,
        "primaryReplayChunkResponses": replay,
        "archiveReadAttempts": {"totalSupply": supply, "balanceOfSelection": {"rule": "balance descending then wallet ascending", "count": len(balances), "rows": balances}},
        "latestCorroboration": {"tip": tip, "totalSupply": latest_supply, "postSnapshotTransfers": post},
        "savedDatasetSummary": {"logs": scan["totalLogs"], "holders": scan["holderCount"], "payments": len(payments["rows"]), "supplyRaw": scan["supplyRaw"], "holdersDigest": scan["holdersDigest"]},
    }
    evidence["evidenceSha256"] = sha(evidence)
    path = os.path.join(OUT, "rpc-observations.json")
    with open(path, "w") as f:
        json.dump(evidence, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    print(json.dumps({"out": path, "evidenceSha256": evidence["evidenceSha256"], "chunks": len(replay), "windows": len(sample_windows)}))


if __name__ == "__main__":
    main()
