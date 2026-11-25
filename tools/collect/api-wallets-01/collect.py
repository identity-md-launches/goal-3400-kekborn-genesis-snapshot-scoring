#!/usr/bin/env python3
"""Collect Genesis holder-history shard 01 from the public IMD API.

The output format intentionally keeps API values unmodified inside array rows.  This
is a resumable collector: a wallet with a row in wallets.json is not read again.
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources" / "api-wallets-01"
API = "https://api.imd.fun"
T = "2026-11-25T11:00:00Z"
T_EPOCH = datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc).timestamp()
GAP = 0.70  # <= 86 reads/minute, below the task's 90/minute ceiling


class Reader:
    def __init__(self):
        self.last = 0.0
        self.errors = []

    def get(self, path):
        url = path if path.startswith("http") else urljoin(API + "/", path.lstrip("/"))
        for attempt in range(4):
            delay = self.last + GAP - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                self.last = time.monotonic()
                req = Request(url, headers={"Accept": "application/json", "User-Agent": "imd-genesis-collector/1"})
                with urlopen(req, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8")), None
            except HTTPError as exc:
                body = exc.read(600).decode("utf-8", "replace")
                if exc.code == 429 and attempt < 3:
                    retry = exc.headers.get("Retry-After", "1")
                    try:
                        time.sleep(max(1.0, float(retry)))
                    except ValueError:
                        time.sleep(1.0)
                    continue
                return None, "HTTP %s %s: %s" % (exc.code, url, body)
            except (URLError, TimeoutError, ValueError) as exc:
                if attempt < 3:
                    time.sleep(1 + attempt)
                    continue
                return None, "%s: %s" % (url, exc)
        return None, "retry exhaustion: " + url


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dump(path, source, coverage, errors, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"source": source, "fetchedAt": now(), "coverage": coverage,
             "errors": errors, "columns": columns, "rows": rows}
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"), ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, path)


def load_rows(name):
    path = OUT / name
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("rows", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def holders():
    inputs = []
    wallets = set()
    for chain in ("ethereum", "robinhood"):
        path = ROOT / "sources" / ("chain-" + chain) / "holders.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        columns = data["columns"]
        wi, bi = columns.index("wallet"), columns.index("balanceRaw")
        pairs = sorted((str(row[wi]).lower(), str(row[bi])) for row in data["rows"])
        digest = hashlib.sha256("".join("%s:%s\n" % p for p in pairs).encode()).hexdigest()
        inputs.append({"chain": chain, "count": len(pairs), "holdersDigest": digest})
        wallets.update(wallet for wallet, _ in pairs)
    shard = sorted(w for w in wallets if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 1)
    return shard, inputs


def before_t(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() < T_EPOCH
    except ValueError:
        return None


def getpath(data, *names):
    for name in names:
        if isinstance(data, dict) and name in data:
            return data[name]
    return None


def order_row(wallet, order):
    payment = order.get("payment") if isinstance(order.get("payment"), dict) else {}
    result = order.get("result") if isinstance(order.get("result"), dict) else {}
    result_id = getpath(result, "requestId", "workflowId", "goalId", "scheduleId", "id")
    job_id = getpath(result, "jobId")
    if job_id is None and isinstance(result.get("requests"), list):
        job_id = [x.get("jobId") for x in result["requests"] if isinstance(x, dict) and x.get("jobId") is not None]
    return [wallet, order.get("orderId"), order.get("action"), order.get("status"),
            order.get("createdAt"), order.get("paidAt"), getpath(payment, "status"),
            getpath(payment, "transactionHash", "txHash", "paymentTx"), result.get("kind"), job_id, result_id,
            result.get("continues"), before_t(order.get("createdAt"))]


def collect_earnings(reader, wallet):
    path = "/wallets/%s/earnings?limit=200" % wallet
    all_rows = []
    seen = set()
    while path and path not in seen:
        seen.add(path)
        data, error = reader.get(path)
        if error:
            return all_rows, error
        if not isinstance(data, dict):
            return all_rows, "earnings response was not an object"
        entries = data.get("earnings")
        if not isinstance(entries, list):
            return all_rows, "earnings response has no earnings array"
        all_rows.extend([wallet, entry] for entry in entries)
        nxt = data.get("next")
        if not nxt:
            break
        # The service may return either an absolute URL, a relative URL, or a
        # cursor token.  Preserve each of those documented shapes.
        path = str(nxt) if str(nxt).startswith(("http", "/")) else "/wallets/%s/earnings?%s" % (wallet, urlencode({"next": nxt}))
    return all_rows, None


def seat_owners(data):
    """Return tokenId -> owner for both current owners response shapes."""
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("owners"), dict):
        return {str(k): str(v).lower() for k, v in data["owners"].items() if isinstance(v, str)}
    owners = data.get("owners", [])
    if isinstance(owners, list):
        # /seats/owners lists token ids in collection order, zero based.
        return {str(i): str(owner).lower() for i, owner in enumerate(owners) if isinstance(owner, str)}
    return {}


def seat_record_map(data):
    if not isinstance(data, dict):
        return {}
    records = data.get("records", data.get("seats", data.get("rows", [])))
    if not isinstance(records, list):
        return {}
    return {str(row.get("tokenId")): row for row in records if isinstance(row, dict) and row.get("tokenId") is not None}


def collect_seat(reader, token_id, owner, record):
    work_before_t = 0
    accepted_before_t = 0
    jobs_before_t = set()
    read_at = now()
    work_before = None
    seen = set()
    while True:
        suffix = "?work=1000&reviews=0&collaborators=0"
        if work_before:
            suffix += "&workBefore=" + urlencode({"x": work_before})[2:]
        path = "/seats/%s%s" % (token_id, suffix)
        if path in seen:
            return [], "repeated workBefore cursor"
        seen.add(path)
        data, error = reader.get(path)
        if error:
            return [], error
        if not isinstance(data, dict):
            return [], "seat response was not an object"
        work = data.get("work", [])
        if not isinstance(work, list):
            return [], "seat response has no work array"
        for item in work:
            if isinstance(item, dict):
                # Work entries use submittedAt, rather than createdAt.  The
                # fallback preserves a future API shape without inventing data.
                created = item.get("submittedAt", item.get("createdAt"))
                accepted = item.get("acceptedAt")
                if before_t(created) is True:
                    work_before_t += 1
                    if item.get("jobId") is not None:
                        jobs_before_t.add(str(item["jobId"]))
                if before_t(accepted) is True:
                    accepted_before_t += 1
        work_before = data.get("workBefore")
        if not work_before:
            # A zero-work seat is deliberately retained as one auditable row.
            return [[str(token_id), owner, record.get("attempts"), record.get("accepted"),
                     record.get("rejected"), work_before_t, accepted_before_t,
                     len(jobs_before_t), read_at]], None


def job_ids(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def main():
    shard, inputs = holders()
    coverage = "holder inputs: " + json.dumps(inputs, separators=(",", ":")) + "; merged unique shard 1/6: %d wallets; T=%s" % (len(shard), T)
    reader = Reader()
    orders = load_rows("orders.json")
    earnings = load_rows("earnings.json")
    wallet_rows = load_rows("wallets.json")
    done = {row[0] for row in wallet_rows if isinstance(row, list) and row}
    order_by_wallet = {}
    for row in orders:
        if isinstance(row, list) and row:
            order_by_wallet.setdefault(row[0], []).append(row)
    earning_by_wallet = {}
    for row in earnings:
        if isinstance(row, list) and row:
            earning_by_wallet.setdefault(row[0], []).append(row)

    for number, wallet in enumerate(shard, 1):
        if wallet in done:
            continue
        errors = []
        data, error = reader.get("/requests/paid-by/" + wallet)
        fetched_orders = []
        count = None
        if error:
            errors.append(error)
        elif not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            errors.append("paid-by response has no orders array")
        else:
            count = data.get("count")
            fetched_orders = [order_row(wallet, row) for row in data["orders"] if isinstance(row, dict)]
            orders.extend(fetched_orders)
            order_by_wallet[wallet] = fetched_orders
        fetched_earnings, error = collect_earnings(reader, wallet)
        if error:
            errors.append(error)
        else:
            earnings.extend(fetched_earnings)
            earning_by_wallet[wallet] = fetched_earnings
        wallet_rows.append([wallet, len(fetched_orders), count == 100, len(fetched_earnings), [], errors])
        done.add(wallet)
        if number % 10 == 0 or errors:
            dump(OUT / "orders.json", API + "/requests/paid-by/:wallet", coverage, reader.errors,
                 ["wallet", "orderId", "action", "status", "createdAt", "paidAt", "paymentStatus", "paymentTx", "resultKind", "resultJobId", "resultId", "continues", "beforeT"], orders)
            dump(OUT / "earnings.json", API + "/wallets/:wallet/earnings?limit=200", coverage, reader.errors,
                 ["wallet", "earning"], earnings)
            dump(OUT / "wallets.json", API + " wallet routes", coverage, reader.errors,
                 ["wallet", "orders", "truncated", "earnings", "seatsOwned", "errors"], wallet_rows)
            print("wallets", len(done), "/", len(shard), flush=True)

    # Current seat ownership is deliberately separate from snapshot-time ownership.
    owner_data, owner_error = reader.get("/seats/owners")
    records_data, records_error = reader.get("/seats/records")
    owners = seat_owners(owner_data) if not owner_error else {}
    records = seat_record_map(records_data) if not records_error else {}
    seat_rows = load_rows("seats.json")
    # Version 1 stored a row for every work item.  Regenerate that collector's
    # own output as one aggregate row per seat, the documented output shape.
    seat_path = OUT / "seats.json"
    if seat_path.exists():
        try:
            if "job" in json.loads(seat_path.read_text(encoding="utf-8")).get("columns", []):
                seat_rows = []
        except (OSError, json.JSONDecodeError):
            seat_rows = []
    old_seats = {row[0] for row in seat_rows if isinstance(row, list) and row}
    global_errors = [x for x in (owner_error, records_error) if x]
    seats_by_owner = {}
    for token_id, owner in owners.items():
        if owner in done:
            seats_by_owner.setdefault(owner, []).append(token_id)
            if token_id in records and token_id not in old_seats:
                rows, error = collect_seat(reader, token_id, owner, records[token_id])
                seat_rows.extend(rows)
                if error:
                    global_errors.append("seat %s: %s" % (token_id, error))
    for row in wallet_rows:
        if row[0] in seats_by_owner:
            row[4] = sorted(seats_by_owner[row[0]], key=lambda x: int(x) if x.isdigit() else x)

    # Jobs are intentionally read only for the narrow 24-hour pre-snapshot case.
    late = load_rows("late-jobs.json")
    seen_late = {str(row[1]) for row in late if isinstance(row, list) and len(row) > 1}
    lower = T_EPOCH - 86400
    for row in orders:
        if not isinstance(row, list) or len(row) < 13:
            continue
        created = row[4]
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        if not (lower <= stamp < T_EPOCH):
            continue
        for jid in job_ids(row[9]):
            if jid in seen_late:
                continue
            data, error = reader.get("/jobs/" + jid)
            if error:
                late.append([row[0], jid, created, None, None, error])
            else:
                job_created = data.get("createdAt") if isinstance(data, dict) else None
                detail = data if before_t(job_created) is False else None
                result = data.get("result") if isinstance(data, dict) and before_t(job_created) is False else None
                late.append([row[0], jid, created, job_created, detail, result])
            seen_late.add(jid)

    dump(OUT / "orders.json", API + "/requests/paid-by/:wallet", coverage, reader.errors,
         ["wallet", "orderId", "action", "status", "createdAt", "paidAt", "paymentStatus", "paymentTx", "resultKind", "resultJobId", "resultId", "continues", "beforeT"], orders)
    dump(OUT / "earnings.json", API + "/wallets/:wallet/earnings?limit=200", coverage, reader.errors,
         ["wallet", "earning"], earnings)
    dump(OUT / "seats.json", API + "/seats/owners, /seats/records, /seats/:tokenId", coverage, global_errors,
         ["tokenId", "owner", "attempts", "accepted", "rejected", "workBeforeT", "acceptedBeforeT", "jobsBeforeT", "readAt"], seat_rows)
    dump(OUT / "late-jobs.json", API + "/jobs/:id", coverage, reader.errors,
         ["wallet", "jobId", "orderCreatedAt", "jobCreatedAt", "detail", "resultOrError"], late)
    dump(OUT / "wallets.json", API + " wallet routes", coverage, global_errors + reader.errors,
         ["wallet", "orders", "truncated", "earnings", "seatsOwned", "errors"], wallet_rows)
    print("complete wallets=%d orders=%d earnings=%d seats=%d lateJobs=%d errors=%d" %
          (len(wallet_rows), len(orders), len(earnings), len(seat_rows), len(late), len(global_errors) + len(reader.errors)))


def _e04_guard():
    """orders.json now carries the e04 merge (original rows + raw payment/result + observations).
    A plain re-run would rewrite it from capped newest-100 lists, so refresh orders only with
    tools/e04-orders/collect.py, which merges by wallet+orderId and keeps older history.
    Earnings and seats are not recollected by e04."""
    import json as _json
    from pathlib import Path as _Path
    _p = _Path(__file__).resolve().parents[3] / "sources" / "api-wallets-01" / "orders.json"
    if _p.exists() and "e04Observation" in _json.loads(_p.read_text(encoding="utf-8")).get("columns", []):
        raise SystemExit("orders.json holds the e04 merge; refresh it with tools/e04-orders/collect.py (paid-by, intake, build)")


if __name__ == "__main__":
    _e04_guard()
    main()
