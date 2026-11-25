"""Collect public IMD histories for the fixed snapshot's wallet shard."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from chain import PUBLIC, reconstruct
from source_format import DICTIONARY_COLUMNS, encode_source, json_text

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "sources/api-wallets-02"
API = "https://api.imd.fun"
T = datetime.fromisoformat("2026-11-25T11:00:00+00:00")
CHAINS = {
    "ethereum": (1, 26491756, "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"),
    "robinhood": (4663, 122989778, "0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127"),
}
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
RATE_LOCK = threading.Lock()
NEXT_READ = 0.0


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json_text(value), encoding="utf-8")
    temporary.replace(path)


def holder_shard(fallback=None):
    union = set()
    evidence = {}
    for chain, (_, block, _) in CHAINS.items():
        path = ROOT / f"sources/chain-{chain}/holders.json"
        if not path.exists():
            path = OUTPUT / f"holders-{chain}.json"
            if not path.exists() and fallback:
                fallback(chain, path)
        data = read_json(path)
        wallet_index = data["columns"].index("wallet")
        balance_index = data["columns"].index("balanceRaw")
        balances = {row[wallet_index].lower(): str(row[balance_index]) for row in data["rows"]}
        method = str(path.relative_to(ROOT))
        if path.parent == OUTPUT:
            if int(data["coverage"]["totalSupplyRaw"]) != sum(map(int, balances.values())):
                raise ValueError(f"Fallback holder supply mismatch for {chain}")
            method += " (Transfer logs verified against totalSupply())"
        if len(balances) != len(data["rows"]):
            raise ValueError(f"Duplicate holders in {method}")
        if any(not ADDRESS.fullmatch(wallet) or int(amount) <= 0
               for wallet, amount in balances.items()):
            raise ValueError(f"Invalid holder in {chain}")
        digest = hashlib.sha256()
        for wallet, amount in sorted(balances.items()):
            digest.update(f"{wallet}:{amount}\n".encode())
        evidence[chain] = {"method": method, "block": block, "count": len(balances),
                           "holdersDigest": digest.hexdigest()}
        union.update(balances)
    return sorted(w for w in union if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 2), evidence


class Client:
    def __init__(self, cache):
        self.cache = cache
        self.observed = {}

    def last_read(self, *prefixes):
        return max((at for path, at in self.observed.items() if path.startswith(prefixes)),
                   default=None, key=timestamp)

    def get(self, path):
        global NEXT_READ
        filename = self.cache / (hashlib.sha256(path.encode()).hexdigest() + ".json")
        if filename.exists():
            entry = read_json(filename)
            self.observed[path] = entry["readAt"]
            return entry["data"], entry["readAt"], None
        for attempt in range(5):
            while True:
                with RATE_LOCK:
                    delay = NEXT_READ - time.monotonic()
                    if delay <= 0:
                        NEXT_READ = time.monotonic() + 0.76
                        break
                time.sleep(delay)
            try:
                with urlopen(API + path, timeout=30) as response:
                    data = json.load(response)
                read_at = now()
                write_json(filename, {"path": path, "data": data, "readAt": read_at})
                self.observed[path] = read_at
                return data, read_at, None
            except HTTPError as exc:
                error = {"path": path, "httpStatus": exc.code}
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After", "60")
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            delay = 60
                    with RATE_LOCK:
                        NEXT_READ = max(NEXT_READ, time.monotonic() + max(1, delay))
                elif exc.code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    break
            except (URLError, TimeoutError, ValueError) as exc:
                error = {"path": path, "reason": str(exc)}
                time.sleep(2 ** attempt)
        return None, None, error


ORDER_COLUMNS = ("wallet orderId action status createdAt paidAt paymentStatus paymentTx "
                 "resultKind resultJobId resultId continues beforeT").split()
SEAT_COLUMNS = ("tokenId owner attempts accepted rejected workBeforeT acceptedBeforeT "
                "jobsBeforeT readAt").split()
WALLET_COLUMNS = "wallet orders truncated earnings seatsOwned errors".split()
LATE_COLUMNS = "wallet orderId jobId createdAt detail result".split()


def source_file(path, source, coverage, errors, columns, rows, fetched_at=None):
    data = {"source": source, "fetchedAt": fetched_at or now(), "coverage": coverage,
            "errors": errors, "columns": columns, "rows": rows}
    write_json(path, encode_source(data, DICTIONARY_COLUMNS.get(path.name)))
    print(f"{path.name}: {len(rows)} rows, {len(errors)} recorded issues", flush=True)


def read_object(client, path, errors):
    data, read_at, error = client.get(path)
    if error:
        errors.append(error)
        return None, read_at
    if not isinstance(data, dict):
        errors.append({"path": path, "reason": "response is not a JSON object"})
        return None, read_at
    return data, read_at


def read_list(data, key, path, errors):
    if data is None:
        return None
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        errors.append({"path": path, "reason": f"{key} is absent or is not a list of objects"})
        return None
    return rows


def normalize_addresses(value):
    if isinstance(value, dict):
        return {key: normalize_addresses(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_addresses(item) for item in value]
    if isinstance(value, str) and ADDRESS.fullmatch(value.lower()):
        return value.lower()
    return value


def timestamp_flag(value, cutoff):
    parsed = timestamp(value)
    return parsed < cutoff if parsed is not None else None


def collect_orders(client, wallet):
    path = f"/requests/paid-by/{wallet}"
    errors = []
    data, _ = read_object(client, path, errors)
    records = read_list(data, "orders", path, errors)
    if records is None:
        return None, [], [], errors
    if str(data.get("payer")).lower() != wallet:
        errors.append({"path": path, "reason": "payer did not match the order request"})
        return None, [], [], errors
    if data.get("count") != len(records):
        errors.append({"path": path, "reason": "reported count differs from the saved order list"})
    rows = []
    job_candidates = []
    for order in records:
        order_id = order.get("orderId")
        payment = order.get("payment") or {}
        result = order.get("result") or {}
        if not isinstance(payment, dict) or not isinstance(result, dict):
            errors.append({"path": path, "orderId": order_id, "reason": "payment or result is not an object"})
            payment = payment if isinstance(payment, dict) else {}
            result = result if isinstance(result, dict) else {}
        created_at = order.get("createdAt")
        before = timestamp_flag(created_at, T)
        if before is None:
            errors.append({"path": path, "orderId": order_id, "reason": "createdAt absent or invalid"})
        rows.append([wallet, order_id, order.get("action"), order.get("status"), created_at,
                     order.get("paidAt"), payment.get("status"), payment.get("transactionHash"),
                     result.get("kind"), result.get("jobId"),
                     result.get("requestId") or result.get("workflowId"), result.get("continues"),
                     before])
        if before and timestamp(created_at) >= T - timedelta(hours=24):
            refs = [result.get("jobId")]
            group = result.get("requests", [])
            if isinstance(group, list):
                refs.extend(item.get("jobId") for item in group if isinstance(item, dict))
            for job in refs:
                if job is None:
                    continue
                if not isinstance(job, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", job):
                    errors.append({"path": path, "orderId": order_id, "reason": "invalid result job ID"})
                    continue
                job_candidates.append((wallet, order_id, job))
    return len(records), rows, job_candidates, errors


def collect_earnings(client, wallet):
    cursor = None
    seen = set()
    rows = []
    errors = []
    while True:
        query = {"limit": 200}
        if cursor is not None:
            query["before"] = cursor
        path = f"/wallets/{wallet}/earnings?{urlencode(query)}"
        data, _ = read_object(client, path, errors)
        records = read_list(data, "earnings", path, errors)
        if records is None:
            return None, rows, errors
        if str(data.get("wallet")).lower() != wallet:
            errors.append({"path": path, "reason": "wallet did not match the earnings response"})
            return None, rows, errors
        for row in records:
            rows.append({**normalize_addresses(row), "wallet": wallet})
        cursor = data.get("next")
        if "next" not in data:
            errors.append({"path": path, "reason": "next field absent"})
            return None, rows, errors
        if cursor is None:
            return len(rows), rows, errors
        if str(cursor) in seen or not isinstance(cursor, (str, int)):
            errors.append({"path": path, "reason": "invalid or repeating next cursor"})
            return None, rows, errors
        seen.add(str(cursor))


def seat_index(client, wallets):
    errors = []
    owners, owners_read = read_object(client, "/seats/owners", errors)
    data, _ = read_object(client, "/seats/records", errors)
    records = read_list(data, "seats", "/seats/records", errors)
    owners_array = owners.get("owners") if owners is not None else None
    if not isinstance(owners_array, list) or any(
            owner is not None and (not isinstance(owner, str) or not ADDRESS.fullmatch(owner.lower()))
            for owner in owners_array):
        if owners is not None:
            errors.append({"path": "/seats/owners", "reason": "missing or malformed owners array"})
        return None, [], None, errors
    wallet_set = set(wallets)
    owned = defaultdict(list)
    for token_id, owner in enumerate(owners_array):
        if owner and owner.lower() in wallet_set:
            owned[owner.lower()].append(str(token_id))
    with_record = {str(row["tokenId"]) for row in records if row.get("tokenId") is not None} if records is not None else set()
    targets = [(token_id, wallet) for wallet in sorted(owned) for token_id in owned[wallet]
               if token_id in with_record]
    return owned, targets, owners.get("at") or owners_read, errors


def collect_seat(client, token_id, owner):
    errors = []
    seen_cursors = set()
    seen_work = set()
    cursor = None
    first = {}
    read_at = None
    work_count = 0
    accepted_count = 0
    jobs = set()
    complete = True
    work_known = True
    acceptance_known = True
    jobs_known = True
    while True:
        query = {"work": 1000, "reviews": 0, "collaborators": 0}
        if cursor is not None:
            query["workBefore"] = cursor
        path = f"/seats/{token_id}?{urlencode(query)}"
        data, page_read = read_object(client, path, errors)
        records = read_list(data, "work", path, errors)
        if cursor is None:
            first = data or {}
            read_at = page_read
            detail_owner = first.get("owner")
            if first and (not isinstance(detail_owner, str) or detail_owner.lower() != owner):
                errors.append({"path": path, "reason": "detail owner differs from owners index",
                               "indexOwner": owner, "detailOwner": detail_owner})
        if records is None:
            complete = False
            break
        for record in records:
            key = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if key in seen_work:
                errors.append({"path": path, "reason": "duplicate work row"})
                complete = False
                continue
            seen_work.add(key)
            submitted_at = timestamp(record.get("submittedAt"))
            if submitted_at is None:
                work_known = False
                errors.append({"path": path, "reason": "work submittedAt absent or invalid"})
                continue
            if submitted_at >= T:
                continue
            work_count += 1
            job_id = record.get("jobId")
            if job_id is None:
                jobs_known = False
                errors.append({"path": path, "reason": "jobId absent for pre-cutoff work"})
            else:
                jobs.add(job_id)
            accepted_at = timestamp(record.get("acceptedAt"))
            if accepted_at is not None:
                accepted_count += accepted_at < T
            elif record.get("status") in (None, "accepted") or record.get("acceptedAt") is not None:
                acceptance_known = False
                errors.append({"path": path, "reason": "acceptance time unavailable for pre-cutoff work"})
        if len(records) < 1000:
            break
        cursor = records[-1].get("submittedAt")
        if timestamp(cursor) is None or cursor in seen_cursors:
            errors.append({"path": path, "reason": "invalid or repeating workBefore cursor"})
            complete = False
            break
        seen_cursors.add(cursor)
    # If a timestamp cursor skipped tied items, a discrepancy may be visible in the full attempt count.
    if complete and isinstance(first.get("attempts"), int) and len(seen_work) != first["attempts"]:
        errors.append({"path": f"/seats/{token_id}", "reason": "work list size differs from attempts",
                       "workRows": len(seen_work), "attempts": first["attempts"]})
        # Endpoint attempts can include work types absent from work; retain observed counts.
    return [token_id, owner, first.get("attempts"), first.get("accepted"), first.get("rejected"),
            work_count if complete and work_known else None,
            accepted_count if complete and work_known and acceptance_known else None,
            sorted(jobs) if complete and work_known and jobs_known else None, read_at], errors


def collect_late_job(client, job_id):
    errors = []
    path = f"/jobs/{quote(job_id, safe='')}"
    detail, _ = read_object(client, path, errors)
    if detail is None:
        return None, errors
    if detail.get("id") != job_id:
        errors.append({"path": path, "reason": "job ID did not match the detail response"})
        return None, errors
    created_at = detail.get("createdAt")
    before = timestamp_flag(created_at, T)
    if before is None:
        errors.append({"path": path, "reason": "job createdAt absent or invalid"})
    if before is not False:
        return None, errors
    result, _ = read_object(client, path + "/result", errors)
    return [job_id, created_at, normalize_addresses(detail), normalize_addresses(result)], errors


def collect_wallet(client, wallet):
    return collect_orders(client, wallet), collect_earnings(client, wallet)


def rebuild_holder_file(chain, path, rpc_url):
    chain_id, block, token = CHAINS[chain]
    coverage, rows = reconstruct(chain_id, block, token, rpc_url or PUBLIC[chain_id])
    source_file(path, f"{PUBLIC[chain_id]} Transfer logs and historical totalSupply()", coverage,
                [], ["wallet", "balanceRaw"], rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "test/scratch/api-wallets-02",
                        help="cache of successful API pages for resumption; choose a new path to refresh")
    for chain in CHAINS:
        parser.add_argument(f"--{chain}-rpc", help=f"archive RPC override for {chain} fallback")
    args = parser.parse_args()
    wallets, evidence = holder_shard(lambda chain, path: rebuild_holder_file(
        chain, path, getattr(args, f"{chain}_rpc")))
    print(json.dumps({"wallets": len(wallets), "holders": evidence}), flush=True)
    client = Client(args.cache)
    order_rows, earning_rows, wallet_rows, candidates = [], [], [], []
    order_errors, earning_errors = [], []
    common = {"shard": 2, "shards": 6, "walletCount": len(wallets), "holders": evidence,
              "snapshotTime": T.isoformat().replace("+00:00", "Z")}
    with ThreadPoolExecutor(max_workers=8) as workers:
        owned, targets, owner_at, seat_errors = seat_index(client, wallets)
        index_errors = seat_errors.copy()
        for index, (wallet, (orders, earnings)) in enumerate(
                zip(wallets, workers.map(lambda wallet: collect_wallet(client, wallet), wallets)), 1):
            order_count, orders_for_wallet, refs, order_issues = orders
            earning_count, earnings_for_wallet, earning_issues = earnings
            order_rows.extend(orders_for_wallet)
            earning_rows.extend(earnings_for_wallet)
            candidates.extend(refs)
            order_errors.extend(order_issues)
            earning_errors.extend(earning_issues)
            wallet_seats = owned.get(wallet, []) if owned is not None else None
            wallet_issues = order_issues + earning_issues
            if wallet_seats is None or wallet_seats:
                wallet_issues.extend(index_errors)
            wallet_rows.append([wallet, order_count, order_count == 100 if order_count is not None else None,
                                earning_count, wallet_seats, wallet_issues])
            if index % 100 == 0:
                print(f"Processed {index}/{len(wallets)} wallets", flush=True)
        seat_rows = []
        seat_by_wallet = {row[0]: row for row in wallet_rows}
        for (token_id, wallet), (row, errors) in zip(targets, workers.map(
                lambda pair: collect_seat(client, *pair), targets)):
            seat_rows.append(row)
            seat_errors.extend(errors)
            seat_by_wallet[wallet][-1].extend(errors)
        late_rows, late_errors = [], []
        refs_by_job = defaultdict(list)
        for wallet, order_id, job_id in dict.fromkeys(candidates):
            refs_by_job[job_id].append((wallet, order_id))
        for job_id, (job, errors) in zip(refs_by_job, workers.map(
                lambda job_id: collect_late_job(client, job_id), refs_by_job)):
            late_errors.extend(errors)
            for wallet, order_id in refs_by_job[job_id]:
                seat_by_wallet[wallet][-1].extend(errors)
                if job is not None:
                    late_rows.append([wallet, order_id, *job])

    earnings_columns = ["wallet"] + sorted({key for row in earning_rows for key in row if key != "wallet"})
    source_file(OUTPUT / "orders.json", API + "/requests/paid-by/<wallet>",
                {**common, "orderLimit": 100, "beforeT": "createdAt strictly earlier than snapshotTime",
                 "nulls": "Optional values absent from the API stay null. No result means no admitted result was returned; resultId is only the top-level oracle requestId or workflowId; continues only applies to continuations."},
                order_errors, ORDER_COLUMNS, order_rows, client.last_read("/requests/paid-by/"))
    source_file(OUTPUT / "earnings.json", API + "/wallets/<wallet>/earnings?limit=200",
                {**common, "pagination": "next passed as before until null",
                 "nulls": "A null cell is null or absent in the API row."},
                earning_errors, earnings_columns,
                [[row.get(key) for key in earnings_columns] for row in earning_rows],
                client.last_read("/wallets/"))
    source_file(OUTPUT / "seats.json", API + "/seats/{owners,records,<tokenId>}",
                {**common, "ownerAt": owner_at, "ownerBasis": "owners index at read time",
                 "recordBearingOwnedSeats": len(targets),
                 "workBeforeT": "Work rows with submittedAt strictly before T",
                 "acceptedBeforeT": "Pre-T work with acceptedAt strictly before T",
                 "jobsBeforeT": "Distinct job IDs among work rows submitted strictly before T",
                 "nulls": "Unknown totals or pagination/timestamp information stay null. Attempt counters are current lifetime values. Cutoff counts cover work exposed by the endpoint."},
                seat_errors, SEAT_COLUMNS, seat_rows, client.last_read("/seats/"))
    source_file(OUTPUT / "late-jobs.json", API + "/jobs/<id> and /jobs/<id>/result",
                {**common, "candidateJobs": len(refs_by_job),
                 "candidateOrders": "createdAt in [T minus 24 hours, T), with explicit job ID",
                 "rows": "Job created at or after T; one row per qualifying order/job link",
                 "nulls": "Unavailable results stay null with the failed read recorded."},
                late_errors, LATE_COLUMNS, late_rows, client.last_read("/jobs/"))
    source_file(OUTPUT / "wallets.json", f"{API} /requests/paid-by, /wallets/earnings and /seats/owners",
                {**common, "truncated": "true at 100 returned orders",
                 "seatsOwned": "Token ID list from owners index at read time, including seats without work records",
                 "nulls": "Counts or ownership stay null if their required reads fail; see errors."},
                seat_errors + order_errors + earning_errors + late_errors, WALLET_COLUMNS, wallet_rows,
                client.last_read("/"))


def _e04_guard():
    """orders.json now carries the e04 merge (original rows + raw payment/result + observations).
    A plain re-run would rewrite it from capped newest-100 lists, so refresh orders only with
    tools/e04-orders/collect.py, which merges by wallet+orderId and keeps older history.
    Earnings and seats are not recollected by e04."""
    import json as _json
    from pathlib import Path as _Path
    _p = _Path(__file__).resolve().parents[3] / "sources" / "api-wallets-02" / "orders.json"
    if _p.exists() and "e04Observation" in _json.loads(_p.read_text(encoding="utf-8")).get("columns", []):
        raise SystemExit("orders.json holds the e04 merge; refresh it with tools/e04-orders/collect.py (paid-by, intake, build)")


if __name__ == "__main__":
    _e04_guard()
    main()
