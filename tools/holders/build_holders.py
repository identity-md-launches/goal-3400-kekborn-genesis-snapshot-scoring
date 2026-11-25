#!/usr/bin/env python3
"""Validate the saved chain holder data and rebuild holders.csv offline."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
CHAINS = ("ethereum", "robinhood")
COLUMNS = (
    "wallet", "eth_balance_raw", "rh_balance_raw", "total_balance_raw",
    "eth_balance", "rh_balance", "total_balance", "on_ethereum", "on_robinhood",
    "is_contract_eth", "is_contract_rh",
)
HOLDER_COLUMNS = ("wallet", "balanceRaw", "isContract")
SCALE = 10**18


def load_chain(source_dir):
    source = json.loads((source_dir / "holders.json").read_bytes())
    scan = json.loads((source_dir / "scan.json").read_bytes())
    columns = source["columns"]
    positions = [columns.index(column) for column in HOLDER_COLUMNS]
    records = {}
    digest_lines = {}
    for number, row in enumerate(source["rows"], start=1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"{source_dir.name}: invalid row shape at row {number}")
        wallet, raw, is_contract = (row[index] for index in positions)
        if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
            raise ValueError(f"{source_dir.name}: invalid wallet at row {number}")
        address = wallet.lower()
        if address in records:
            raise ValueError(f"{source_dir.name}: duplicate wallet {address}")
        if not isinstance(raw, str) or not re.fullmatch(r"0|[1-9][0-9]*", raw):
            raise ValueError(f"{source_dir.name}: invalid or negative balance for {address}")
        if is_contract is not None and not isinstance(is_contract, bool):
            raise ValueError(f"{source_dir.name}: invalid contract flag for {address}")
        records[address] = (int(raw), is_contract)
        digest_lines[wallet] = f"{wallet}:{raw}\n"

    balance_sum = sum(balance for balance, _ in records.values())
    if str(balance_sum) != scan["supplyRaw"]:
        raise ValueError(f"{source_dir.name}: sum {balance_sum} differs from supply {scan['supplyRaw']}")
    digest = hashlib.sha256(
        "".join(digest_lines[wallet] for wallet in sorted(digest_lines)).encode("ascii")
    ).hexdigest()
    if digest != scan["holdersDigest"]:
        raise ValueError(f"{source_dir.name}: holdersDigest mismatch (computed {digest})")

    positive = {wallet: entry for wallet, entry in records.items() if entry[0] > 0}
    print(
        f"{source_dir.name}: {len(records)} source rows, {len(positive)} positive holders, "
        f"sum {balance_sum} raw; supply, uniqueness, nonnegative balances and digest PASS ({digest})"
    )
    return positive


def decimal_text(raw):
    whole, fractional = divmod(raw, SCALE)
    return f"{whole}.{fractional:018d}"


def flag(value):
    if value is None:
        return ""
    return "true" if value else "false"


def build_csv(root):
    ethereum, robinhood = (
        load_chain(root / "sources" / f"chain-{chain}") for chain in CHAINS
    )
    wallets = sorted(ethereum.keys() | robinhood.keys())
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(COLUMNS)
    for wallet in wallets:
        eth, eth_contract = ethereum.get(wallet, (0, None))
        rh, rh_contract = robinhood.get(wallet, (0, None))
        total = eth + rh
        writer.writerow((
            wallet, eth, rh, total, decimal_text(eth), decimal_text(rh), decimal_text(total),
            flag(eth > 0), flag(rh > 0), flag(eth_contract), flag(rh_contract),
        ))
    print(f"Both chains: {len(ethereum.keys() & robinhood.keys())}; CSV data rows: {len(wallets)}")
    return output.getvalue().encode("ascii")


def main(root=ROOT, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare rebuilt bytes without writing")
    arguments = parser.parse_args(argv)
    path = root / "holders.csv"
    try:
        rebuilt = build_csv(root)
        if arguments.check:
            if path.read_bytes() != rebuilt:
                print("FAIL: holders.csv differs from the offline rebuild", file=sys.stderr)
                return 1
            print("PASS: holders.csv matches the offline rebuild")
        else:
            path.write_bytes(rebuilt)
            print("Wrote holders.csv")
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
