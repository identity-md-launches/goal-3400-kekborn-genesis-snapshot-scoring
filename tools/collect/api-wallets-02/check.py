"""Verify the shard's saved datasets without making network requests."""
from collections import Counter
from datetime import timedelta
import sys

from collect import (LATE_COLUMNS, ORDER_COLUMNS, OUTPUT, SEAT_COLUMNS,
                     T, WALLET_COLUMNS, holder_shard, read_json, timestamp,
                     timestamp_flag)
from source_format import decode_source


E04_EXTRA = ["payment", "result", "e04Observation", "e04Source", "e04Conflicts", "createdAtSource"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(name, columns, evidence, count):
    data = read_json(OUTPUT / f"{name}.json")
    require(set(data) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"},
            f"{name}: invalid source envelope")
    data = decode_source(data)
    require(timestamp(data["fetchedAt"]) is not None, f"{name}: invalid fetchedAt")
    if columns is not None:
        require(data["columns"] == columns, f"{name}: unexpected columns")
    else:
        require(data["columns"][:1] == ["wallet"], f"{name}: first column must be wallet")
    require(len(set(data["columns"])) == len(data["columns"]), f"{name}: duplicate columns")
    require(all(isinstance(row, list) and len(row) == len(data["columns"]) for row in data["rows"]),
            f"{name}: row width differs from columns")
    recorded = data["coverage"]["holders"]
    holder_fields = ("block", "count", "holdersDigest")
    require(recorded.keys() == evidence.keys() and all(
                recorded[chain][key] == evidence[chain][key]
                for chain in evidence for key in holder_fields)
            and data["coverage"]["walletCount"] == count
            and data["coverage"]["shard"] == 2 and data["coverage"]["shards"] == 6
            and timestamp(data["coverage"]["snapshotTime"]) == T,
            f"{name}: holder inputs or cutoff differ from this shard")
    return [dict(zip(data["columns"], row)) for row in data["rows"]]


def check():
    wallets, evidence = holder_shard()
    wallet_rows = load("wallets", WALLET_COLUMNS, evidence, len(wallets))
    counts = Counter(row["wallet"] for row in wallet_rows)
    require(counts == Counter(wallets),
            f"wallet coverage mismatch: {len(set(wallets) - counts.keys())} missing, "
            f"{len(counts.keys() - set(wallets))} extra, "
            f"{sum(count > 1 for count in counts.values())} duplicated")
    print(f"PASS exact wallet coverage ({len(wallets)} wallets)")

    orders = load("orders", list(ORDER_COLUMNS) + E04_EXTRA, evidence, len(wallets))
    earnings = load("earnings", None, evidence, len(wallets))
    seats = load("seats", SEAT_COLUMNS, evidence, len(wallets))
    late = load("late-jobs", LATE_COLUMNS, evidence, len(wallets))
    # e04 repair appends observations; wallets.json counts the original read only.
    order_counts = Counter(row["wallet"] for row in orders if str(row.get("e04Observation", "original")).startswith("original"))
    earning_counts = Counter(row["wallet"] for row in earnings)
    require((order_counts.keys() | earning_counts.keys()) <= counts.keys(),
            "orders or earnings contain out-of-shard wallets")
    require(all(timestamp_flag(row["createdAt"], T) is row["beforeT"] for row in orders),
            "order beforeT flag differs from the fixed cutoff")
    ids = [(row["wallet"], row["orderId"]) for row in orders]
    require(len(ids) == len(set(ids)), "duplicate order for a wallet")
    wallet_index = {row["wallet"]: row for row in wallet_rows}
    for row in wallet_rows:
        wallet = row["wallet"]
        require(row["orders"] == order_counts[wallet] or
                (row["orders"] is None and order_counts[wallet] == 0 and row["errors"]),
                f"{wallet}: incorrect order count")
        require(row["truncated"] is (None if row["orders"] is None else row["orders"] == 100),
                f"{wallet}: incorrect truncation flag")
        require(row["earnings"] == earning_counts[wallet] or
                (row["earnings"] is None and row["errors"]), f"{wallet}: incorrect earnings count")
    for row in earnings:
        amount = row.get("amount")
        require(amount is None or (isinstance(amount, str) and amount.isascii() and amount.isdecimal()),
                f"{row['wallet']}: earnings amount is not a raw integer string")
    print(f"PASS order cutoff, order and earnings counts ({len(orders)} orders, {len(earnings)} earnings)")

    seen = set()
    for row in seats:
        owner, token = row["owner"], row["tokenId"]
        require(owner in wallet_index and token in (wallet_index[owner]["seatsOwned"] or []),
                f"seat {token}: owner missing from wallet index")
        require(token not in seen, f"duplicate seat {token}")
        seen.add(token)
        work, accepted, jobs = row["workBeforeT"], row["acceptedBeforeT"], row["jobsBeforeT"]
        require(work is None or (work >= 0 and (accepted is None or 0 <= accepted <= work)),
                f"seat {token}: inconsistent work counts")
        require(jobs is None or (work is not None and jobs == sorted(set(jobs)) and len(jobs) <= work),
                f"seat {token}: inconsistent job IDs")
    print(f"PASS seat ownership and observed counts ({len(seats)} seats)")

    index = dict(zip(ids, orders))
    links = set()
    for row in late:
        job_id = row["jobId"]
        link = (row["wallet"], row["orderId"], job_id)
        order = index.get(link[:2])
        require(order is not None and order["beforeT"] and
                timestamp(order["createdAt"]) >= T - timedelta(hours=24),
                f"late job {job_id}: missing recent order")
        require(order["resultJobId"] == job_id or order["resultKind"] == "oracle-group",
                f"late job {job_id}: order link differs")
        require(row["detail"].get("id") == job_id and row["detail"].get("createdAt") == row["createdAt"]
                and timestamp_flag(row["createdAt"], T) is False,
                f"late job {job_id}: detail does not establish post-cutoff creation")
        require(link not in links, f"duplicate late job link {link}")
        links.add(link)
    print(f"PASS late job order links and creation cutoff ({len(late)} links)")


if __name__ == "__main__":
    try:
        check()
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        sys.exit(1)
