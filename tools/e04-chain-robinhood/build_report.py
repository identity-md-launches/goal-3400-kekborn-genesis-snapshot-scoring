#!/usr/bin/env python3
"""Build the human-readable machine JSON report from saved Robinhood evidence."""
import hashlib
import json
import os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SRC = os.path.join(ROOT, "sources", "chain-robinhood")
E04 = os.path.join(ROOT, "sources", "e04-chain-robinhood")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.json")

def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

def main():
    scan = json.load(open(os.path.join(SRC, "scan.json")))
    evidence = json.load(open(os.path.join(E04, "rpc-observations.json")))
    sample_ok = all(x["comparison"] == "match" for x in evidence["independentSelectedWindows"])
    sample_bad = any(x["comparison"] == "discrepancy" for x in evidence["independentSelectedWindows"])
    replay_ok = all(x["comparison"] == "match" for x in evidence["primaryReplayChunkResponses"])
    replay_bad = any(x["comparison"] == "discrepancy" for x in evidence["primaryReplayChunkResponses"])
    header_values = [x["observation"].get("result") for x in evidence["snapshotHeaderObservations"]]
    header_ok = all(x and x.get("timestamp") == hex(1795604400) for x in header_values) and len({x.get("hash") for x in header_values}) == 1
    archive = evidence["archiveReadAttempts"]
    balance_rows = archive["balanceOfSelection"]["rows"]
    unavailable = sum(1 for x in balance_rows if x["observation"]["outcome"] != "ok")
    report = {
        "schema": "imd.e04.chain-robinhood.report.v1",
        "observedAt": evidence["fetchedAt"],
        "scope": "Read-only independent corroboration of saved Robinhood Chain IMD replay; no transaction, signature, deployment, mint, or purchase was made.",
        "snapshot": evidence["snapshot"],
        "commands": [
            "python3 -B tools/collect/chain-robinhood/collect.py https://rpc.mainnet.chain.robinhood.com",
            "python3 -B tools/e04-chain-robinhood/collect_evidence.py",
            "python3 -B tools/e04-chain-robinhood/build_report.py",
            "python3 -B tools/e04-chain-robinhood/check.py",
        ],
        "fullReplay": {
            "status": "pass" if replay_ok else ("fail" if replay_bad else "unavailable"),
            "endpoint": evidence["endpoints"]["primary"],
            "exactRanges": [[x["range"][0], x["range"][1]] for x in evidence["primaryReplayChunkResponses"]],
            "firstBlock": scan["firstBlock"], "lastBlock": scan["coverage"]["toBlock"],
            "chunks": len(evidence["primaryReplayChunkResponses"]), "logs": scan["totalLogs"],
            "holders": scan["holderCount"], "payments": len(json.load(open(os.path.join(SRC, "payments.json")))["rows"]),
            "supplyRaw": scan["supplyRaw"], "mintedRaw": scan["mintedRaw"], "burnedRaw": scan["burnedRaw"],
            "noNegativeBalances": scan["checks"]["noNegativeBalance"], "duplicateLogsDropped": scan["checks"]["duplicateLogsDropped"],
        },
        "independentSampling": {
            "status": "pass" if sample_ok and header_ok else ("fail" if sample_bad or not header_ok else "unavailable"),
            "endpoint": evidence["endpoints"]["independent"],
            "whatItEstablishes": "Exact canonical Transfer-log agreement for three fixed windows and the same snapshot block header. This is sampling, not a second full replay.",
            "windows": [{"range": x["range"], "comparison": x["comparison"], "primaryCount": x["primary"].get("logCount"), "independentCount": x["independent"].get("logCount"), "digest": x["primary"].get("canonicalLogsSha256")} for x in evidence["independentSelectedWindows"]],
            "snapshotHeaderMatch": header_ok,
        },
        "archiveReads": {
            "status": "unavailable" if archive["totalSupply"]["outcome"] != "ok" or unavailable else "pass",
            "totalSupplyAtSnapshot": archive["totalSupply"],
            "balanceOfAtSnapshot": {"attempted": len(balance_rows), "unavailable": unavailable, "selection": archive["balanceOfSelection"]["rule"]},
            "note": "Unavailable archive reads are retained as errors and are not replaced with latest values.",
        },
        "latestCorroboration": {
            "status": "pass" if evidence["latestCorroboration"]["totalSupply"].get("result") == hex(int(scan["supplyRaw"])) and evidence["latestCorroboration"]["postSnapshotTransfers"].get("mintOrBurnCount") == 0 else "unavailable",
            "note": "Latest supply corroborates the snapshot supply only because the recorded post-snapshot window has no mint/burn Transfer logs; it does not establish historical holder membership.",
            "observation": evidence["latestCorroboration"],
        },
        "limitations": [
            "The public primary endpoint did not serve archive state at block 122989778 for totalSupply or the 25 deterministic balanceOf reads.",
            "Independent endpoint validation is fixed-window sampling; only the primary endpoint performed the complete saved chunk replay.",
            "Latest isContract values in sources/chain-robinhood/holders.json are read-time observations, not historical contract-state claims.",
        ],
        "savedFilesSha256": {p: digest(os.path.join(ROOT, p)) for p in ["sources/chain-robinhood/holders.json", "sources/chain-robinhood/payments.json", "sources/chain-robinhood/scan.json", "sources/e04-chain-robinhood/rpc-observations.json"]},
    }
    with open(OUT, "w") as f:
        json.dump(report, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    print(json.dumps({"out": OUT, "fullReplay": report["fullReplay"]["status"], "sampling": report["independentSampling"]["status"], "archive": report["archiveReads"]["status"]}))

if __name__ == "__main__":
    main()
