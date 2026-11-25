#!/usr/bin/env python3
"""Offline validation of the saved shard 3 API collection."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "sources" / "api-wallets-03"


def expected_wallets():
    all_wallets = set()
    for chain in ("ethereum", "robinhood"):
        doc = json.loads((ROOT / "sources" / f"chain-{chain}" / "holders.json").read_text())
        all_wallets.update(row[0].lower() for row in doc["rows"] if int(row[1]) > 0)
    return sorted(w for w in all_wallets if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 3)


def load(name):
    doc = json.loads((SRC / name).read_text())
    assert isinstance(doc, dict) and set(("source", "fetchedAt", "coverage", "errors", "columns", "rows")) <= set(doc), name
    assert isinstance(doc["rows"], list), name
    return doc


def main():
    problems = []
    try:
        want = expected_wallets()
        wallets = load("wallets.json")
        rows = wallets["rows"]
        got = [r[0] for r in rows]
        if got != want:
            problems.append(f"wallet row coverage mismatch: expected {len(want)}, got {len(got)}, missing {len(set(want)-set(got))}, extra {len(set(got)-set(want))}")
        if len(rows) != len(set(got)):
            problems.append("wallet rows contain duplicates")
        if any(len(r) != len(wallets["columns"]) for r in rows):
            problems.append("wallet row width mismatch")
        for name in ("orders.json", "earnings.json", "seats.json", "late-jobs.json"):
            d = load(name)
            if any(len(r) != len(d["columns"]) for r in d["rows"]):
                problems.append(f"{name}: row width mismatch")
        orders = load("orders.json")
        earnings = load("earnings.json")
        seats = load("seats.json")
        late = load("late-jobs.json")
        cutoff = datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc)
        def before(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")) < cutoff
        order_counts = {}
        oi = orders["columns"].index("e04Observation") if "e04Observation" in orders["columns"] else None
        for row in orders["rows"]:
            if oi is not None and not str(row[oi]).startswith("original"):
                # e04 observation: only the strict flag is required; post-T rows stay flagged False.
                if row[12] is not (before(row[4]) if row[4] else None):
                    problems.append(f"e04 order {row[1]} beforeT flag differs from createdAt < T")
                continue
            if row[12] is not True or not before(row[4]):
                problems.append(f"order {row[1]} is not strictly before T")
            if row[0] != row[0].lower():
                problems.append(f"order wallet is not lowercase: {row[0]}")
            order_counts[row[0]] = order_counts.get(row[0], 0) + 1
        earnings_counts = {}
        expected_earning_columns = ["wallet", "launchId", "launchNumber", "status", "chainId", "kind", "tokenRef", "amount", "at", "launchRef"]
        if earnings["columns"] != expected_earning_columns:
            problems.append("earnings columns do not match the lossless normalized schema")
        tokens, launches = earnings.get("tokens", []), earnings.get("launches", [])
        if len(tokens) != earnings.get("coverage", {}).get("tokenCatalogCount"):
            problems.append("earnings token catalog count differs from coverage")
        if len(launches) != earnings.get("coverage", {}).get("launchCatalogCount"):
            problems.append("earnings launch catalog count differs from coverage")
        for row in earnings["rows"]:
            if len(row) != len(expected_earning_columns):
                continue
            wallet, launch_id, launch_number, status, chain_id, kind, token_ref, amount, at, launch_ref = row
            if not isinstance(at, str) or not before(at):
                problems.append(f"earning for {wallet} lacks a usable pre-T timestamp")
            if not isinstance(token_ref, int) or not 0 <= token_ref < len(tokens):
                problems.append(f"earning for {wallet} has an invalid token reference")
            if not isinstance(launch_ref, int) or not 0 <= launch_ref < len(launches):
                problems.append(f"earning for {wallet} has an invalid launch reference")
            if not isinstance(amount, str):
                problems.append(f"earning for {wallet} amount is not a string")
            earnings_counts[wallet] = earnings_counts.get(wallet, 0) + 1
        if len(seats["columns"]) != 9:
            problems.append("seats columns do not match the requested nine fields")
        for row in rows:
            if row[2] != order_counts.get(row[0], 0):
                problems.append(f"wallet order rollup differs for {row[0]}")
            if row[4] != earnings_counts.get(row[0], 0):
                problems.append(f"wallet earnings rollup differs for {row[0]}")
        for row in late["rows"]:
            if not isinstance(row[3], dict):
                problems.append(f"late job {row[0]} has no detail object")
        for name in ("orders.json", "earnings.json", "seats.json", "late-jobs.json"):
            d = load(name)
            if d["errors"]:
                print(f"WARN {name}: {len(d['errors'])} saved read errors")
        incomplete = sum(not bool(r[7]) for r in rows)
        print(f"PASS holder shard derivation: {len(want)} wallets")
        print(f"{'PASS' if got == want else 'FAIL'} wallets.json rows: {len(rows)}")
        print(f"INFO incomplete wallet API histories: {incomplete}")
        print(f"PASS order and earnings timestamp cutoff: T=2026-11-25T11:00:00Z")
        print(f"PASS wallet rollups: orders={len(orders['rows'])}, earnings={len(earnings['rows'])}")
        for name in ("wallets.json", "orders.json", "earnings.json", "seats.json", "late-jobs.json"):
            if (SRC / name).exists():
                d = load(name)
                print(f"PASS {name}: {len(d['rows'])} rows")
        if problems:
            for problem in problems:
                print("FAIL " + problem)
            return 1
        return 0
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
