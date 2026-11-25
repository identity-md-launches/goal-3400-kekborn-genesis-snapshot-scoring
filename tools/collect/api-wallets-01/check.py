#!/usr/bin/env python3
"""Offline structural check for api-wallets shard 01."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources" / "api-wallets-01"


def expected():
    wallets = set()
    for chain in ("ethereum", "robinhood"):
        doc = json.loads((ROOT / "sources" / ("chain-" + chain) / "holders.json").read_text())
        wi = doc["columns"].index("wallet")
        wallets.update(str(row[wi]).lower() for row in doc["rows"])
    return {w for w in wallets if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 1}


def read(name):
    path = OUT / name
    if not path.exists():
        raise ValueError("missing " + str(path.relative_to(ROOT)))
    doc = json.loads(path.read_text())
    for key in ("source", "fetchedAt", "coverage", "errors", "columns", "rows"):
        if key not in doc:
            raise ValueError(name + " missing key " + key)
    if not isinstance(doc["rows"], list) or not all(isinstance(row, list) for row in doc["rows"]):
        raise ValueError(name + " rows must be arrays")
    return doc


def main():
    try:
        docs = {name: read(name) for name in ("orders.json", "earnings.json", "seats.json", "late-jobs.json", "wallets.json")}
        wanted = expected()
        rows = docs["wallets.json"]["rows"]
        got = [row[0] for row in rows if len(row) >= 6]
        if len(got) != len(set(got)):
            raise ValueError("wallets.json contains duplicate wallets")
        if set(got) != wanted:
            raise ValueError("wallets.json wallets=%d expected=%d missing=%d extra=%d" %
                             (len(got), len(wanted), len(wanted - set(got)), len(set(got) - wanted)))
        if any(not isinstance(row[0], str) or row[0] != row[0].lower() for row in rows):
            raise ValueError("wallets.json has non-lowercase wallet")
        print("PASS shard wallets=%d; orders=%d earnings=%d seats=%d late-jobs=%d" %
              (len(got), len(docs["orders.json"]["rows"]), len(docs["earnings.json"]["rows"]),
               len(docs["seats.json"]["rows"]), len(docs["late-jobs.json"]["rows"])))
        return 0
    except Exception as exc:
        print("FAIL", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
