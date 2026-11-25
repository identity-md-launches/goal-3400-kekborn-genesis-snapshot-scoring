#!/usr/bin/env python3
"""Collect shard 05 using public GETs. Run from any directory."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from source_format import encode_earnings, json_bytes

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources/api-wallets-05"
API = "https://api.imd.fun"
T = datetime(2026, 11, 25, 11, tzinfo=timezone.utc)
BLOCKS = {"ethereum": 26491756, "robinhood": 122989778}
ADDRESS = re.compile(r"0x[0-9a-f]{40}", re.I)


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if moment.tzinfo else None
    except ValueError:
        return None


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load(path):
    return json.loads(path.read_bytes())


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def write_source(path, source, stamp, coverage, errors, columns, rows):
    header = dict(source=source, fetchedAt=stamp, coverage=coverage, errors=errors, columns=columns)
    temporary = path.with_suffix(".json.tmp")
    digest = hashlib.sha256()
    with temporary.open("wb") as stream:
        def write(data):
            stream.write(data)
            digest.update(data)

        write(json_bytes(header)[:-2] + b',"rows":[')
        for index, row in enumerate(rows):
            if index:
                write(b",")
            write(json_bytes(row)[:-1])
        write(b"]}\n")
    temporary.replace(path)
    return digest.hexdigest()


def holders(cache=None, offline=False):
    snapshot = load(ROOT / "snapshot.json")
    if instant(snapshot["snapshotTime"]) != T:
        raise ValueError("unexpected snapshot cutoff")
    wallets = set()
    provenance = {}
    for chain, block in BLOCKS.items():
        if snapshot["chains"][chain]["block"] != block:
            raise ValueError(f"unexpected {chain} snapshot block")
        relative = f"sources/chain-{chain}/holders.json"
        path = ROOT / relative
        mode = "checkout"
        if not path.exists():
            if cache is None:
                raise ValueError(f"missing {relative}; provide the collector cache")
            from rebuild import rebuild
            if offline:
                path = cache / "rebuild" / chain / snapshot["chains"][chain]["blockHash"] / "holders.json"
            else:
                path = rebuild(cache, chain, snapshot["chains"][chain])
            mode = "rebuilt from Transfer logs"
        data = load(path)
        pairs = {}
        wi = data["columns"].index("wallet")
        bi = data["columns"].index("balanceRaw")
        for row in data["rows"]:
            wallet, balance = row[wi].lower(), row[bi]
            if not ADDRESS.fullmatch(wallet) or wallet in pairs or int(balance) <= 0:
                raise ValueError(f"invalid or repeated {chain} holder {wallet}")
            pairs[wallet] = int(balance)
        digest = hashlib.sha256("".join(f"{w}:{pairs[w]}\n" for w in sorted(pairs)).encode()).hexdigest()
        provenance[chain] = dict(mode=mode, count=len(pairs), holdersDigest=digest, block=block)
        if mode == "checkout":
            summary = path.with_name("scan.json")
            if summary.exists():
                scan = load(summary)
                if scan["holdersDigest"] != digest or int(scan["supplyRaw"]) != sum(pairs.values()):
                    raise ValueError(f"{chain} holder digest or supply mismatch")
                source = scan.get("supplyFrom")
                if source is None:
                    source = scan.get("checks", {}).get("totalSupplySource")
                provenance[chain].update(supplyRaw=scan["supplyRaw"], supplySource=source)
        else:
            provenance[chain].update(data["coverage"])
            provenance[chain]["errors"] = data["errors"]
        wallets.update(pairs)
    return sorted(w for w in wallets if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 5), provenance


class Reader:
    def __init__(self, cache):
        self.cache = cache
        self.last = 0
        self.live = 0
        self.hits = 0
        self.failures = 0
        self.limits = 0
        self.read_times = []

    def get(self, path, errors):
        key = hashlib.sha256(path.encode()).hexdigest()
        cached = self.cache / "http" / (key + ".json.gz")
        if cached.exists():
            record = json.loads(gzip.decompress(cached.read_bytes()))
            self.hits += 1
            self.read_times.append(record["readAt"])
            return record["body"], record["readAt"]

        for attempt in range(5):
            # A rolling minute contains at most 80 starts, including retries.
            time.sleep(max(0, self.last + 0.76 - time.monotonic()))
            self.last = time.monotonic()
            self.live += 1
            try:
                request = Request(API + path, headers={"Accept": "application/json", "User-Agent": "imd-genesis-wallets-05/1"})
                with urlopen(request, timeout=35) as response:
                    body = json.load(response)
                read_at = now()
                if not isinstance(body, dict):
                    raise ValueError("expected JSON object")
                if body.get("error") and not any(k in body for k in ("jobId", "id")):
                    raise ValueError("API error: " + str(body["error"])[:180])
                atomic(cached, gzip.compress(json_bytes(dict(readAt=read_at, body=body)), mtime=0))
                self.read_times.append(read_at)
                return body, read_at
            except HTTPError as exc:
                reason = f"HTTP {exc.code}"
                if exc.code == 429:
                    self.limits += 1
                    retry = exc.headers.get("Retry-After")
                    delay = 60
                    if retry:
                        try:
                            delay = float(retry)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                            except (TypeError, ValueError, OverflowError):
                                pass
                    time.sleep(max(1, delay))
                exc.close()
                if exc.code != 429 and exc.code < 500:
                    break
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < 4:
                time.sleep(2 ** attempt)
        errors.append({"path": path, "readAt": now(), "reason": reason})
        self.failures += 1
        return None, None


ORDER_FIELDS = "wallet orderId action status createdAt paidAt paymentStatus paymentTx resultKind resultJobId resultId continues beforeT".split()
WALLET_FIELDS = "wallet orders truncated earnings seatsOwned errors".split()
SEAT_FIELDS = "tokenId owner attempts accepted rejected workBeforeT acceptedBeforeT jobsBeforeT readAt".split()
LATE_FIELDS = "wallet orderId jobId createdAt detail result".split()


def address_case(value):
    if isinstance(value, dict):
        return {k: address_case(v) for k, v in value.items()}
    if isinstance(value, list):
        return [address_case(v) for v in value]
    if isinstance(value, str) and ADDRESS.fullmatch(value):
        return value.lower()
    return value


def read_list(body, field, path, errors):
    if body is None:
        return None
    items = body.get(field)
    if not isinstance(items, list):
        errors.append(dict(path=path, reason=f"missing or invalid {field} list"))
        return None
    return items


def wallet_orders(reader, wallet, errors):
    path = f"/requests/paid-by/{wallet}"
    data, _ = reader.get(path, errors)
    orders = read_list(data, "orders", path, errors)
    if orders is None:
        return None
    rows = []
    for order in orders:
        paid = order.get("payment") or {}
        result = order.get("result") or {}
        created = instant(order.get("createdAt"))
        if created is None:
            errors.append(dict(path=path, orderId=order.get("orderId"), reason="missing or invalid createdAt"))
        job_id = result.get("jobId")
        result_id = result.get("oracleRequestId") or result.get("requestId") or result.get("workflowId")
        if result.get("kind") == "oracle-group" and isinstance(result.get("requests"), list):
            job_id = [member.get("jobId") for member in result["requests"]]
            result_id = [member.get("requestId") for member in result["requests"]]
        rows.append([
            wallet, order.get("orderId"), order.get("action"), order.get("status"),
            order.get("createdAt"), order.get("paidAt"), paid.get("status"),
            paid.get("transactionHash"), result.get("kind"), job_id, result_id,
            result.get("continues"), created < T if created is not None else None,
        ])
    return rows


def wallet_earnings(reader, wallet, errors):
    base = f"/wallets/{wallet}/earnings"
    cursor = None
    seen = set()
    rows = []
    while True:
        query = {"limit": 200}
        if cursor is not None:
            query["before"] = cursor
        path = base + "?" + urlencode(query)
        data, _ = reader.get(path, errors)
        page = read_list(data, "earnings", path, errors)
        if page is None:
            return rows, False
        rows.extend(address_case(item) for item in page)
        if "next" not in data:
            errors.append(dict(path=path, reason="missing next cursor"))
            return rows, False
        cursor = data["next"]
        if cursor is None:
            return rows, True
        if str(cursor) in seen:
            errors.append(dict(path=path, reason="repeated earnings cursor"))
            return rows, False
        seen.add(str(cursor))


def seat_index(reader, wallets, errors):
    owners_body, read_at = reader.get("/seats/owners", errors)
    records_body, _ = reader.get("/seats/records", errors)
    owners = read_list(owners_body, "owners", "/seats/owners", errors)
    records = read_list(records_body, "seats", "/seats/records", errors)
    owned = defaultdict(list)
    if owners is not None:
        for token, owner in enumerate(owners):
            if isinstance(owner, str) and owner.lower() in wallets:
                owned[owner.lower()].append(str(token))
    return owned, owners is not None, records, read_at, (owners_body or {}).get("at")


def seat_row(reader, token, owner, read_at, errors):
    base = f"/seats/{quote(token, safe='')}"
    first = None
    cursor = None
    seen = set()
    read_count = work_count = accepted = 0
    work_known = accepted_known = jobs_known = True
    jobs = set()
    tied_boundaries = []
    while True:
        query = dict(work=1000, reviews=0, collaborators=0)
        if cursor is not None:
            query["workBefore"] = cursor
        path = base + "?" + urlencode(query)
        data, _ = reader.get(path, errors)
        page = read_list(data, "work", path, errors)
        if first is None:
            first = data or {}
            detail_owner = first.get("owner")
            if detail_owner and detail_owner.lower() != owner:
                errors.append(dict(path=path, reason="detail owner differs from ownership index", detailOwner=detail_owner.lower()))
        if page is None:
            work_known = accepted_known = jobs_known = False
            break
        read_count += len(page)
        previous = instant(cursor)
        for index, item in enumerate(page):
            submitted = instant(item.get("submittedAt"))
            if submitted is None:
                work_known = accepted_known = jobs_known = False
                continue
            if previous is not None and (submitted > previous or (index == 0 and submitted == previous)):
                errors.append(dict(path=path, reason="work order or exclusive cursor violated"))
                work_known = accepted_known = jobs_known = False
            previous = submitted
            if submitted >= T:
                continue
            work_count += 1
            if item.get("jobId") is None:
                jobs_known = False
            else:
                jobs.add(item["jobId"])
            accepted_at = instant(item.get("acceptedAt"))
            if accepted_at is not None:
                accepted += accepted_at < T
            elif item.get("status") == "accepted":
                accepted_known = False
        if len(page) < 1000:
            break
        cursor = page[-1].get("submittedAt")
        if instant(cursor) is None or cursor in seen:
            errors.append(dict(path=path, reason="invalid or repeated seat work cursor"))
            work_known = accepted_known = jobs_known = False
            break
        if instant(cursor) == instant(page[-2].get("submittedAt")):
            tied_boundaries.append(path)
        seen.add(cursor)
    attempts = first.get("attempts")
    if attempts is not None and attempts != read_count:
        errors.append(dict(path=base, reason="paged work count differs from first-page lifetime attempts",
                           workRows=read_count, attempts=attempts))
        work_known = accepted_known = jobs_known = False
    if tied_boundaries and (attempts != read_count or not work_known):
        errors.extend(dict(path=path, reason="tied boundary timestamps may hide work rows") for path in tied_boundaries)
        work_known = accepted_known = jobs_known = False
    if not (work_known and accepted_known and jobs_known):
        errors.append(dict(path=base, reason="one or more cutoff metrics cannot be established from the work pages"))
    return [token, owner, first.get("attempts"), first.get("accepted"), first.get("rejected"),
            work_count if work_known else None, accepted if accepted_known else None,
            len(jobs) if jobs_known else None, read_at]


def late_jobs(reader, orders, errors, wallet_errors=None):
    candidates = defaultdict(list)
    for row in orders:
        created = instant(row[4])
        if created is not None and T - timedelta(days=1) <= created < T:
            jobs = row[9] if isinstance(row[9], list) else [row[9]]
            for job in sorted({item for item in jobs if item}):
                candidates[job].append((row[0], row[1]))
    rows = []
    for job, orders_for_job in sorted(candidates.items()):
        path = f"/jobs/{quote(job, safe='')}"
        failures = []
        detail, _ = reader.get(path, failures)
        if detail is not None:
            created_at = detail.get("createdAt")
            created = instant(created_at)
            if created is None:
                failures.append(dict(path=path, reason="missing or invalid job createdAt"))
            elif created >= T:
                result, _ = reader.get(path + "/result", failures)
                normalized_detail, normalized_result = address_case(detail), address_case(result)
                rows.extend([wallet, order, job, created_at, normalized_detail, normalized_result]
                            for wallet, order in orders_for_job)
        errors.extend(failures)
        if failures and wallet_errors is not None:
            for wallet in {w for w, _ in orders_for_job}:
                wallet_errors[wallet].extend(failures)
    print(f"Late-job candidates {len(candidates)}, matching order rows {len(rows)}", flush=True)
    return rows, len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "test/scratch/wallets-05-cache")
    args = parser.parse_args()
    wallets, provenance = holders(args.cache)
    print(f"Holders {json.dumps(provenance)}; shard {len(wallets)}", flush=True)
    reader = Reader(args.cache)
    errors = {key: [] for key in ("wallets", "orders", "earnings", "seats", "late-jobs")}
    owned, owners_known, records, read_at, source_at = seat_index(reader, set(wallets), errors["seats"])
    wallet_errors = defaultdict(list)
    seat_rows = []
    if records is not None:
        record_ids = {str(item["tokenId"]) for item in records}
        for owner, tokens in sorted(owned.items()):
            for token in tokens:
                if token in record_ids:
                    failures = []
                    seat_rows.append(seat_row(reader, token, owner, read_at, failures))
                    errors["seats"].extend(failures)
                    wallet_errors[owner].extend(failures)
    order_rows, wallet_rows, earning_pairs = [], [], []
    for index, wallet in enumerate(wallets, 1):
        failures = wallet_errors[wallet]
        start = len(errors["orders"])
        orders = wallet_orders(reader, wallet, errors["orders"])
        failures.extend(errors["orders"][start:])
        start = len(errors["earnings"])
        earnings, done = wallet_earnings(reader, wallet, errors["earnings"])
        failures.extend(errors["earnings"][start:])
        if not owners_known or records is None:
            failures.append(dict(reason="seat owners or records unavailable"))
        order_rows.extend(orders or [])
        earning_pairs.extend((wallet, item) for item in earnings)
        count = len(orders) if orders is not None else None
        wallet_rows.append([wallet, count, count >= 100 if count is not None else None,
                            len(earnings) if done else None, len(owned[wallet]) if owners_known else None, failures])
        if index % 100 == 0 or index == len(wallets):
            print(f"Wallets {index}/{len(wallets)}, live reads {reader.live}, cache hits {reader.hits}, final failures {reader.failures}, 429s {reader.limits}", flush=True)
    late_rows, candidates = late_jobs(reader, order_rows, errors["late-jobs"], wallet_errors)
    earning_fields = sorted({key for _, item in earning_pairs for key in item})
    earning_rows = [[wallet] + [item.get(key) for key in earning_fields] for wallet, item in earning_pairs]
    stamp = max(reader.read_times, default=None)
    cutoff = "2026-11-25T11:00:00Z"
    definitions = {
        "orders": ("/requests/paid-by/<wallet>", ORDER_FIELDS, order_rows,
                   dict(cutoff=cutoff, limit=100, note="All returned orders, including after T. Null fields are absent or null upstream. beforeT uses createdAt only. Oracle-group resultJobId and resultId are parallel arrays in API order.")),
        "earnings": ("/wallets/<wallet>/earnings?limit=200", ["wallet", *earning_fields], earning_rows,
                     dict(note="Unfiltered API earnings, paged by next as before. Missing upstream fields are null.")),
        "seats": ("/seats/owners, /seats/records, /seats/<tokenId>", SEAT_FIELDS, seat_rows,
                  dict(cutoff=cutoff, ownershipReadAt=read_at, ownershipSourceAt=source_at,
                       note="Current indexed ownership. Rows require a seat record. Work counts submission timestamps, accepted counts acceptance timestamps among pre-cutoff work, jobs counts distinct work job IDs. Lifetime totals are detail response values.")),
        "wallets": ("/requests/paid-by/<wallet>, /wallets/<wallet>/earnings, /seats/owners, /seats/records, /seats/<tokenId>",
                    WALLET_FIELDS, wallet_rows, dict(holders=provenance, shard=5, shards=6,
                    note="Counts are null when the corresponding read or pagination was incomplete. seatsOwned includes seats without work records.")),
        "late-jobs": ("/jobs/<id>, /jobs/<id>/result", LATE_FIELDS, late_rows,
                      dict(cutoff=cutoff, candidateJobs=candidates,
                           note="Orders created in [T minus 24h, T) naming a job created at or after T. One row per order.")),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (route, columns, rows, coverage) in definitions.items():
        coverage["wallets"] = len(wallets)
        if name == "earnings":
            rows, coverage = encode_earnings(columns, rows, coverage)
        digest = write_source(OUT / (name + ".json"), API + route, stamp, coverage, errors[name], columns, rows)
        print(f"{name}.json {len(rows)} rows {len(errors[name])} errors {digest}", flush=True)
    print(f"API live reads {reader.live}, cache hits {reader.hits}, final failures {reader.failures}, 429s {reader.limits}", flush=True)


def _e04_guard():
    """orders.json now carries the e04 merge (original rows + raw payment/result + observations).
    A plain re-run would rewrite it from capped newest-100 lists, so refresh orders only with
    tools/e04-orders/collect.py, which merges by wallet+orderId and keeps older history.
    Earnings and seats are not recollected by e04."""
    import json as _json
    from pathlib import Path as _Path
    _p = _Path(__file__).resolve().parents[3] / "sources" / "api-wallets-05" / "orders.json"
    if _p.exists() and "e04Observation" in _json.loads(_p.read_text(encoding="utf-8")).get("columns", []):
        raise SystemExit("orders.json holds the e04 merge; refresh it with tools/e04-orders/collect.py (paid-by, intake, build)")


if __name__ == "__main__":
    _e04_guard()
    main()
