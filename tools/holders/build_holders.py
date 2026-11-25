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
    "eth_code_kind", "eth_code_block", "eth_code_read_started_at", "eth_code_read_finished_at",
    "rh_code_kind", "rh_code_block_tag", "rh_code_source_fetched_at",
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
    return positive, source, scan


def decimal_text(raw):
    whole, fractional = divmod(raw, SCALE)
    return f"{whole}.{fractional:018d}"


def flag(value):
    if value is None:
        return ""
    return "true" if value else "false"


def verify_scan(scan, block):
    start = scan['firstBlock']
    total = 0
    for first, last, count, *_ in scan['rows']:
        if first != start or last < first or type(count) is not int or count < 0:
            raise ValueError('Invalid or noncontiguous Transfer scan range')
        start = last + 1
        total += count
    if start != block + 1 or total != scan['totalLogs']:
        raise ValueError('Transfer scan coverage does not match the snapshot')


def verify_ethereum(root, holders, scan, block):
    evidence = root / 'sources/e04-chain-ethereum'
    balances = json.loads((evidence / 'balances.json').read_bytes())
    code = json.loads((evidence / 'code.json').read_bytes())
    replay = json.loads((evidence / 'replay-tenderly.json').read_bytes())
    for doc in (balances, code):
        if doc['block'] != block or doc['unread']:
            raise ValueError('Incomplete Ethereum snapshot evidence')
        if len({row[0] for row in doc['rows']}) != len(doc['rows']):
            raise ValueError('Duplicate wallet in Ethereum evidence')
    if (len(balances['rows']) != scan['addressesSeen']
            or {w: int(raw) for w, raw, _ in balances['rows'] if raw != '0'}
            != {w: balance for w, (balance, _) in holders.items()}
            or any(not isinstance(raw, str) or not re.fullmatch(r'0|[1-9][0-9]*', raw)
                   for _, raw, _ in balances['rows'])):
        raise ValueError('Ethereum balanceOf and Transfer balances differ')
    if (replay['holderMismatchCount'] or replay['chunkCounts'] != [r[2] for r in scan['rows']]
            or any(replay['totals'][k] != scan[k] for k in ('holdersDigest', 'logsDigest', 'sumRaw'))):
        raise ValueError('Ethereum replay evidence differs from saved scan')
    kinds = {}
    delegates = {}
    for wallet, kind, size, value in code['rows']:
        if kind == 'eip7702' and size == 23 and re.fullmatch(r'0x[0-9a-f]{40}', value or ''):
            delegates[wallet] = value
        elif not ((kind == 'none' and size == 0 and value is None) or
                  (kind == 'contract' and size > 0 and re.fullmatch(r'[0-9a-f]{64}', value or ''))):
            raise ValueError(f'Invalid code evidence for {wallet}')
        kinds[wallet] = kind
    if (kinds.keys() != holders.keys() or delegates != dict(scan['eip7702Delegated'])
            or any(flag != (kinds[w] != 'none') for w, (_, flag) in holders.items())):
        raise ValueError('Ethereum account-code evidence disagrees with holder flags')
    return {wallet: (kind, block, code['utc'], code['finishedUtc']) for wallet, kind in kinds.items()}


def verify_robinhood(root, scan, block):
    evidence = json.loads((root / 'sources/e04-chain-robinhood/rpc-observations.json').read_bytes())
    if evidence['snapshot']['block'] != block:
        raise ValueError('Robinhood corroboration has the wrong snapshot block')
    for name in ('holders.json', 'scan.json', 'payments.json'):
        path = root / 'sources/chain-robinhood' / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sourceFileSha256'][name]:
            raise ValueError(f'Robinhood source digest mismatch: {name}')
    expected = {'holders': scan['holderCount'], 'holdersDigest': scan['holdersDigest'],
                'logs': scan['totalLogs'], 'supplyRaw': scan['supplyRaw']}
    if any(evidence['savedDatasetSummary'][k] != v for k, v in expected.items()):
        raise ValueError('Robinhood corroboration disagrees with saved scan')


def build_csv(root, verify_evidence=True):
    ethereum, _, eth_scan = load_chain(root / 'sources/chain-ethereum')
    robinhood, rh_source, rh_scan = load_chain(root / 'sources/chain-robinhood')
    eth_code = {}
    if verify_evidence:
        snapshot = json.loads((root / 'snapshot.json').read_bytes())
        eth_block, rh_block = (snapshot['chains'][chain]['block'] for chain in CHAINS)
        if (snapshot['snapshotTime'], eth_block, rh_block) != ('2026-11-25T11:00:00Z', 26491756, 122989778):
            raise ValueError('Holder inputs must use the fixed batch snapshot')
        verify_scan(eth_scan, eth_block)
        verify_scan(rh_scan, rh_block)
        eth_code = verify_ethereum(root, ethereum, eth_scan, eth_block)
        verify_robinhood(root, rh_scan, rh_block)
        if rh_source['coverage']['isContractReadAt'] != 'latest':
            raise ValueError('Unexpected Robinhood account-code observation tag')
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
            *eth_code.get(wallet, ('', '', '', '')),
            ('nonempty_unclassified' if rh_contract else 'none') if rh_contract is not None else '',
            rh_source.get('coverage', {}).get('isContractReadAt', '') if rh_contract is not None else '',
            rh_source.get('fetchedAt', '') if rh_contract is not None else '',
        ))
    print(f"Both chains: {len(ethereum.keys() & robinhood.keys())}; CSV data rows: {len(wallets)}")
    return output.getvalue().encode("ascii")


def main(root=ROOT, argv=None, *, verify_evidence=True):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare rebuilt bytes without writing")
    arguments = parser.parse_args(argv)
    path = root / "holders.csv"
    try:
        rebuilt = build_csv(root, verify_evidence)
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
