"""Fallback snapshot holder reconstruction using public read-only JSON-RPC."""
from collections import defaultdict
import json
from urllib.request import Request, urlopen

from collect import atomic, json_bytes, load, now

RPCS = {
    "ethereum": ("https://ethereum-rpc.publicnode.com", "https://gateway.tenderly.co/public/mainnet", "https://eth.drpc.org"),
    "robinhood": ("https://rpc.mainnet.chain.robinhood.com", "https://robinhood-rpc.publicnode.com"),
}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40


class Rpc:
    def __init__(self, chain):
        self.urls = RPCS[chain]

    def call(self, method, params):
        request = json_bytes(dict(jsonrpc="2.0", id=1, method=method, params=params))
        errors = []
        for url in self.urls:
            try:
                with urlopen(Request(url, request, {"Content-Type": "application/json"}), timeout=30) as response:
                    reply = json.load(response)
                if "result" in reply and reply["result"] is not None:
                    return reply["result"]
                errors.append(f"{url}: {reply.get('error')}")
            except (OSError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
        raise RuntimeError(f"{method}: " + " | ".join(errors))


def transfer_logs(rpc, token, start, end, directory, topics=None, label="all"):
    path = directory / f"{label}-{start}-{end}.json"
    if path.exists():
        yield from load(path)
        return
    try:
        rows = rpc.call("eth_getLogs", [dict(address=token, fromBlock=hex(start), toBlock=hex(end),
                                          topics=topics or [TRANSFER])])
    except RuntimeError:
        if start == end:
            raise
        rows = None
    if rows is not None and len(rows) < 1000:
        atomic(path, json_bytes(rows))
        yield from rows
        return
    if start == end:
        raise RuntimeError(f"cannot certify an uncapped Transfer response for block {start}")
    middle = (start + end) // 2
    yield from transfer_logs(rpc, token, start, middle, directory, topics, label)
    yield from transfer_logs(rpc, token, middle + 1, end, directory, topics, label)


def transfers(rpc, token, start, end, directory, size, topics=None, label="all"):
    for low in range(start, end + 1, size):
        yield from transfer_logs(rpc, token, low, min(end, low + size - 1), directory, topics, label)


def supply_at(rpc, token, block, directory, size):
    args = {"to": token, "data": "0x18160ddd"}
    try:
        return int(rpc.call("eth_call", [args, hex(block)]), 16), "historical totalSupply()", []
    except (RuntimeError, ValueError) as exc:
        errors = [dict(what="historical totalSupply()", reason=str(exc))]
    head = rpc.call("eth_getBlockByNumber", ["latest", False])
    latest = int(head["number"], 16)
    if latest < block:
        raise ValueError("RPC head precedes the snapshot")
    supply = int(rpc.call("eth_call", [args, hex(latest)]), 16)
    zero_topic = "0x" + "0" * 64
    for label, sign, topics in (
        ("mint", -1, [TRANSFER, zero_topic]), ("burn", 1, [TRANSFER, None, zero_topic]),
    ):
        for row in transfers(rpc, token, block + 1, latest, directory / head["hash"], size, topics, label):
            supply += sign * int(row["data"], 16)
    if rpc.call("eth_getBlockByNumber", [hex(latest), False])["hash"] != head["hash"]:
        raise ValueError("head changed while verifying supply")
    return supply, f"totalSupply() at {latest} rolled back using mint and burn logs", errors


def rebuild(cache, chain, snapshot):
    block, token = snapshot["block"], snapshot["token"]
    directory = cache / "rebuild" / chain / snapshot["blockHash"]
    path = directory / "holders.json"
    if path.exists():
        return path
    rpc = Rpc(chain)
    if int(rpc.call("eth_chainId", []), 16) != snapshot["chainId"]:
        raise ValueError(f"wrong {chain} network")
    if rpc.call("eth_getBlockByNumber", [hex(block), False])["hash"] != snapshot["blockHash"]:
        raise ValueError(f"wrong {chain} snapshot hash")
    size = 10_000 if chain == "ethereum" else 5_000_000
    balances = defaultdict(int)
    seen = set()
    for row in transfers(rpc, token, 0, block, directory, size):
        key = (row["blockHash"], row["logIndex"])
        if key in seen:
            continue
        seen.add(key)
        if row.get("removed") or row["address"].lower() != token or row["topics"][0] != TRANSFER:
            raise ValueError("unexpected Transfer log")
        _, sender, recipient = row["topics"]
        sender, recipient = "0x" + sender[-40:].lower(), "0x" + recipient[-40:].lower()
        amount = int(row["data"], 16)
        if sender != ZERO:
            balances[sender] -= amount
        if recipient != ZERO:
            balances[recipient] += amount
    supply, source, errors = supply_at(rpc, token, block, directory, size)
    if any(value < 0 for value in balances.values()) or sum(balances.values()) != supply:
        raise ValueError(f"{chain} holder balances failed the supply check")
    rows = [[wallet, str(balance)] for wallet, balance in sorted(balances.items()) if balance]
    atomic(path, json_bytes(dict(source=list(rpc.urls), fetchedAt=now(),
                                 coverage=dict(block=block, supplyRaw=str(supply), supplySource=source),
                                 errors=errors, columns=["wallet", "balanceRaw"], rows=rows)))
    return path
