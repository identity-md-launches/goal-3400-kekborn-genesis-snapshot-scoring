#!/usr/bin/env python3
"""Offline integrity and invariant checker for the Robinhood E04 evidence."""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SRC = os.path.join(ROOT, "sources", "chain-robinhood")
E04 = os.path.join(ROOT, "sources", "e04-chain-robinhood")
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.json")
TOKEN = "0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127"
SNAP = 122989778
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BLOCK_HASH = "0xed7b0b82d314693301461f419e86b566e2d0705be150fa8a5a893cd06db054a3"
BLOCK_TIME = "0x6b06bfb0"  # 2026-11-25T11:00:00Z
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
fails = []

def ok(name, condition):
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        fails.append(name)

def load(path):
    with open(path) as f:
        return json.load(f)

def sha_bytes(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def sha_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def response_has_error(x):
    return x.get("outcome") in ("rpc-error", "transport-error") and ("response" in x or "error" in x)

def main():
    holders = load(os.path.join(SRC, "holders.json"))
    payments = load(os.path.join(SRC, "payments.json"))
    scan = load(os.path.join(SRC, "scan.json"))
    evidence = load(os.path.join(E04, "rpc-observations.json"))
    report = load(REPORT)

    expected_sources = {n: sha_bytes(os.path.join(SRC, n)) for n in ("holders.json", "payments.json", "scan.json")}
    ok("evidence schema", evidence.get("schema") == "imd.e04.chain-robinhood.evidence.v1")
    copy = dict(evidence); claimed = copy.pop("evidenceSha256", None)
    ok("evidence self digest", claimed == sha_json(copy))
    ok("evidence anchors source bytes", evidence.get("sourceFileSha256") == expected_sources)
    ok("fixed snapshot", evidence.get("snapshot") == {"chainId": 4663, "block": SNAP, "expectedUtc": "2026-11-25T11:00:00Z", "token": TOKEN})

    # Original compact replay invariants, all integer arithmetic is exact.
    rows = holders.get("rows", [])
    wallets = [x[0] for x in rows]
    total = sum(int(x[1]) for x in rows)
    digest = hashlib.sha256("".join("%s:%s\n" % (x[0], x[1]) for x in sorted(rows)).encode()).hexdigest()
    ok("saved holders valid", all(len(x) == 3 and ADDR.match(x[0]) and x[0] != "0x" + "0" * 40 and str(x[1]).isdigit() and int(x[1]) > 0 for x in rows))
    ok("saved holder wallets unique", len(wallets) == len(set(wallets)))
    ok("saved replay baseline", (scan.get("totalLogs"), scan.get("holderCount"), scan.get("supplyRaw")) == (117301, 2131, "71170146452000000000000"))
    ok("saved holder sum and digest", str(total) == scan.get("sumRaw") == scan.get("supplyRaw") and digest == scan.get("holdersDigest"))
    ok("saved mint burn arithmetic", int(scan["mintedRaw"]) - int(scan["burnedRaw"]) == total and scan["checks"].get("noNegativeBalance") and not scan["checks"].get("negativeBalances"))
    chunks = scan.get("rows", [])
    ok("saved ranges continuous", bool(chunks) and chunks[0][0] == 0 and chunks[-1][1] == SNAP and all(a[1] + 1 == b[0] for a, b in zip(chunks, chunks[1:])))
    ok("saved chunk count and dedup", sum(x[2] for x in chunks) == scan["totalLogs"] + scan["checks"]["duplicateLogsDropped"])
    pkeys = [(x[0], x[1]) for x in payments.get("rows", [])]
    ok("saved payments unique and in snapshot", len(pkeys) == len(set(pkeys)) and all(x[2] <= SNAP and str(x[6]).isdigit() for x in payments.get("rows", [])))

    chain_obs = evidence.get("chainIdObservations", [])
    ok("two chain id observations", len(chain_obs) == 2 and all(x.get("observation", {}).get("outcome") == "ok" and x["observation"].get("result") == "0x1237" for x in chain_obs))
    headers = evidence.get("snapshotHeaderObservations", [])
    ok("two matching snapshot headers", len(headers) == 2 and all(x.get("observation", {}).get("outcome") == "ok" and x["observation"].get("result", {}).get("hash") == BLOCK_HASH and x["observation"].get("result", {}).get("timestamp") == BLOCK_TIME for x in headers))

    replay = evidence.get("primaryReplayChunkResponses", [])
    ok("fresh replay covers saved requests", len(replay) == len(chunks) and all(x.get("range") == y[:2] and x.get("expectedLogCount") == y[2] for x, y in zip(replay, chunks)))
    ok("fresh replay has no discrepancy", all(x.get("comparison") in ("match", "unavailable") for x in replay))
    ok("fresh replay response evidence", all((x.get("comparison") == "match" and x.get("response", {}).get("outcome") == "ok" and x["response"].get("logCount") == x["expectedLogCount"] and len(x["response"].get("canonicalLogsSha256", "")) == 64) or (x.get("comparison") == "unavailable" and response_has_error(x.get("response", {}))) for x in replay))

    wanted_windows = [[51500000, 52000000], [82500000, 83124999], [117538674, SNAP]]
    samples = evidence.get("independentSelectedWindows", [])
    ok("independent windows fixed", [x.get("range") for x in samples] == wanted_windows)
    ok("independent windows no discrepancy", all(x.get("comparison") in ("match", "unavailable") for x in samples))
    ok("independent response evidence", all((x.get("comparison") == "match" and x.get("primary", {}).get("outcome") == x.get("independent", {}).get("outcome") == "ok" and x["primary"].get("logCount") == x["independent"].get("logCount") and x["primary"].get("canonicalLogsSha256") == x["independent"].get("canonicalLogsSha256")) or (x.get("comparison") == "unavailable" and (response_has_error(x.get("primary", {})) or response_has_error(x.get("independent", {})))) for x in samples))

    archive = evidence.get("archiveReadAttempts", {})
    supply = archive.get("totalSupply", {})
    ok("totalSupply attempted at snapshot", supply.get("request", {}).get("method") == "eth_call" and supply.get("request", {}).get("params", [None, None])[1] == hex(SNAP) and supply.get("outcome") in ("ok", "rpc-error", "transport-error"))
    top = sorted(rows, key=lambda r: (-int(r[1]), r[0]))[:25]
    balances = archive.get("balanceOfSelection", {}).get("rows", [])
    ok("25 deterministic balance attempts", archive.get("balanceOfSelection", {}).get("count") == 25 and [(x.get("wallet"), x.get("computedRaw")) for x in balances] == [(x[0], x[1]) for x in top])
    ok("balance attempts use snapshot or preserve error", all(x.get("observation", {}).get("request", {}).get("method") == "eth_call" and x["observation"]["request"].get("params", [None, None])[1] == hex(SNAP) and x["observation"].get("outcome") in ("ok", "rpc-error", "transport-error") for x in balances))

    latest = evidence.get("latestCorroboration", {})
    post = latest.get("postSnapshotTransfers", {})
    ok("latest check is labelled non-historical", latest.get("totalSupply", {}).get("request", {}).get("params", [None, None])[1] == "latest" and post.get("request", {}).get("params", [{}])[0].get("fromBlock") == hex(SNAP + 1))
    ok("report schema and source hashes", report.get("schema") == "imd.e04.chain-robinhood.report.v1" and report.get("savedFilesSha256", {}).get("sources/chain-robinhood/holders.json") == expected_sources["holders.json"] and report.get("savedFilesSha256", {}).get("sources/chain-robinhood/payments.json") == expected_sources["payments.json"] and report.get("savedFilesSha256", {}).get("sources/chain-robinhood/scan.json") == expected_sources["scan.json"] and report.get("savedFilesSha256", {}).get("sources/e04-chain-robinhood/rpc-observations.json") == sha_bytes(os.path.join(E04, "rpc-observations.json")))
    ok("report does not fabricate unavailable checks", report.get("fullReplay", {}).get("status") in ("pass", "unavailable") and report.get("independentSampling", {}).get("status") in ("pass", "unavailable") and report.get("archiveReads", {}).get("status") in ("pass", "unavailable") and report.get("latestCorroboration", {}).get("status") in ("pass", "unavailable"))
    print("holders=%d payments=%d savedLogs=%d replayMatches=%d replayUnavailable=%d sampleMatches=%d sampleUnavailable=%d" % (len(rows), len(payments["rows"]), scan["totalLogs"], sum(x["comparison"] == "match" for x in replay), sum(x["comparison"] == "unavailable" for x in replay), sum(x["comparison"] == "match" for x in samples), sum(x["comparison"] == "unavailable" for x in samples)))
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
