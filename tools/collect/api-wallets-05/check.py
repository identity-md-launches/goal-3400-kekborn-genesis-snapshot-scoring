#!/usr/bin/env python3
"""Verify shard 05 output offline against the snapshot holder inputs."""
import argparse
from collections import Counter
from datetime import timedelta
from pathlib import Path

from collect import (OUT, ROOT, LATE_FIELDS, ORDER_FIELDS, SEAT_FIELDS, T,
                     WALLET_FIELDS, holders, instant)
from source_format import load_source


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "test/scratch/wallets-05-cache")
    args = parser.parse_args()
    expected, provenance = holders(args.cache, offline=True)
    shard = set(expected)
    columns = {"orders": ORDER_FIELDS + ["payment", "result", "e04Observation", "e04Source", "e04Conflicts", "createdAtSource"], "wallets": WALLET_FIELDS, "seats": SEAT_FIELDS,
               "earnings": None, "late-jobs": LATE_FIELDS}
    data = {}
    for name, fields in columns.items():
        obj = load_source(OUT / (name + ".json"))
        require(set(obj) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"}, f"{name}: source shape")
        require(len(set(obj["columns"])) == len(obj["columns"]), f"{name}: duplicate columns")
        require(obj["columns"] == fields if fields else obj["columns"][0] == "wallet", f"{name}: columns")
        require(obj["coverage"]["wallets"] == len(shard), f"{name}: shard size")
        require(all(isinstance(row, list) and len(row) == len(obj["columns"]) for row in obj["rows"]), f"{name}: row lengths")
        data[name] = [dict(zip(obj["columns"], row)) for row in obj["rows"]]
        if name == "wallets":
            observed = obj["coverage"]["holders"]
            for chain in provenance:
                for key in ("count", "holdersDigest", "block"):
                    require(observed[chain][key] == provenance[chain][key], f"{chain}: {key} mismatch")
        print(f"PASS {name}.json schema and shard size ({len(obj['rows'])} rows, {len(obj['errors'])} recorded errors)")

    counts = Counter(row["wallet"] for row in data["wallets"])
    require(set(counts) == shard and all(count == 1 for count in counts.values()), "wallets: missing, foreign or duplicate wallet")
    print(f"PASS exact shard membership, one row per wallet ({len(shard)})")
    for name, field in (("orders", "wallet"), ("earnings", "wallet"), ("seats", "owner"), ("late-jobs", "wallet")):
        require(all(row[field] in shard for row in data[name]), f"{name}: wallet outside shard")
    orders = {(row["wallet"], row["orderId"]): row for row in data["orders"]}
    require(len(orders) == len(data["orders"]), "orders: duplicate wallet and order ID")
    order_count = Counter(row["wallet"] for row in data["orders"] if str(row.get("e04Observation", "original")).startswith("original"))
    earnings_count = Counter(row["wallet"] for row in data["earnings"])
    seats_count = Counter(row["owner"] for row in data["seats"])
    require(len({row["tokenId"] for row in data["seats"]}) == len(data["seats"]), "seats: duplicate token ID")
    for row in data["wallets"]:
        wallet = row["wallet"]
        count = row["orders"]
        require(count is None or count == order_count[wallet], f"{wallet}: order count")
        require(row["truncated"] is (None if count is None else count >= 100), f"{wallet}: truncation")
        require(row["earnings"] is None or row["earnings"] == earnings_count[wallet], f"{wallet}: earnings count")
        require(row["seatsOwned"] is None or row["seatsOwned"] >= seats_count[wallet], f"{wallet}: owned seats")
        require(all(row[key] is not None for key in ("orders", "earnings", "seatsOwned")) or row["errors"], f"{wallet}: missing reason")
    for row in data["earnings"]:
        amount = row.get("amount")
        require(amount is None or isinstance(amount, str) and amount.isascii() and amount.isdecimal(), f"{row['wallet']}: raw earnings amount")
    for row in data["seats"]:
        work = row["workBeforeT"]
        if work is not None:
            for metric in ("acceptedBeforeT", "jobsBeforeT"):
                value = row[metric]
                require(value is None or 0 <= value <= work, f"{row['tokenId']}: {metric} exceeds cutoff work")
    for row in data["orders"]:
        created = instant(row["createdAt"])
        require(row["beforeT"] is (created < T if created is not None else None), f"{row['orderId']}: cutoff flag")
    for row in data["late-jobs"]:
        order = orders[row["wallet"], row["orderId"]]
        jobs = order["resultJobId"]
        require(row["jobId"] in (jobs if isinstance(jobs, list) else [jobs]), f"{row['jobId']}: missing order link")
        require(T - timedelta(days=1) <= instant(order["createdAt"]) < T <= instant(row["createdAt"]), f"{row['jobId']}: late-job window")
    print("PASS shard ownership, unique IDs, wallet counts, truncation, raw earnings and cutoff joins")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"FAIL {exc}") from exc
