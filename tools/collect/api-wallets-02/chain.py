"""Read-only holder reconstruction when an upstream holder file is absent."""
from collections import Counter
import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
# The Ethereum token's first code block was established by the landed chain scan.
# Robinhood is scanned from genesis because the token is present at block zero.
FIRST = {1: 23501863, 4663: 0}
PUBLIC = {
    1: "https://ethereum-rpc.publicnode.com",
    4663: "https://rpc.mainnet.chain.robinhood.com",
}


class RangeError(RuntimeError):
    pass


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = Request(url, body, {"Content-Type": "application/json"})
    for delay in (1, 2, 4, None):
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
            if "error" in result:
                if method == "eth_getLogs":
                    raise RangeError("RPC refused log range")
                raise RuntimeError(f"RPC refused {method}")
            return result["result"]
        except (URLError, TimeoutError, ValueError) as exc:
            if delay is None:
                if method == "eth_getLogs" and getattr(exc, "code", None) in (400, 413):
                    raise RangeError("HTTP refused log range") from None
                raise RuntimeError(f"RPC transport failed for {method}") from None
            time.sleep(delay)


def transfer_logs(url, token, start, end):
    query = {"address": token, "topics": [TRANSFER], "fromBlock": hex(start), "toBlock": hex(end)}
    try:
        logs = rpc(url, "eth_getLogs", [query])
        # Split busy ranges before accepting a possibly capped result.
        if len(logs) >= 1000 and start != end:
            raise RangeError("Potentially capped log range")
    except RangeError:
        if start == end:
            raise
        middle = (start + end) // 2
        yield from transfer_logs(url, token, start, middle)
        yield from transfer_logs(url, token, middle + 1, end)
    else:
        yield from logs


def reconstruct(chain_id, block, token, url):
    if int(rpc(url, "eth_chainId", []), 16) != chain_id:
        raise ValueError(f"RPC chain ID mismatch for {chain_id}")
    first = FIRST[chain_id]
    if first and rpc(url, "eth_getCode", [token, hex(first - 1)]) != "0x":
        raise ValueError(f"Token was present before the expected deployment on {chain_id}")
    if rpc(url, "eth_getCode", [token, hex(first)]) == "0x":
        raise ValueError(f"Token code absent at first scan block on {chain_id}")
    supply = int(rpc(url, "eth_call", [{"to": token, "data": "0x18160ddd"}, hex(block)]), 16)
    balances = Counter()
    seen = set()
    step = 5000 if chain_id == 1 else 500000
    for start in range(first, block + 1, step):
        end = min(start + step - 1, block)
        for log in transfer_logs(url, token, start, end):
            key = (log["blockHash"], int(log["logIndex"], 16))
            if key in seen:
                raise ValueError(f"Duplicate Transfer log on {chain_id}")
            seen.add(key)
            if (log.get("removed") or not start <= int(log["blockNumber"], 16) <= end
                    or log["address"].lower() != token or log["topics"][0].lower() != TRANSFER
                    or len(log["topics"]) != 3):
                raise ValueError(f"Unexpected Transfer log on {chain_id}")
            sender, recipient = ("0x" + topic[-40:].lower() for topic in log["topics"][1:])
            amount = int(log["data"], 16)
            if sender != ZERO:
                balances[sender] -= amount
            if recipient != ZERO:
                balances[recipient] += amount
    if any(amount < 0 for amount in balances.values()) or sum(balances.values()) != supply:
        raise ValueError(f"Replayed balances do not reconcile with snapshot totalSupply on {chain_id}")
    return {"chainId": chain_id, "token": token, "fromBlock": first, "toBlock": block,
            "totalSupplyRaw": str(supply), "transferLogs": len(seen)}, [
                [wallet, str(amount)] for wallet, amount in sorted(balances.items()) if amount > 0
            ]
