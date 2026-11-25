#!/usr/bin/env python3
"""Resumable, read-only IMD API collection for holder shard 3."""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources" / "api-wallets-03"
STATE = OUT / ".state.json"
BASE = "https://api.imd.fun"
SNAPSHOT = "2026-11-25T11:00:00Z"
SNAPSHOT_TS = 1795604400
MIN_INTERVAL = 60 / 85


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class Client:
    def __init__(self, spacing=MIN_INTERVAL):
        self.spacing = spacing
        self.last = 0.0
        self.reads = 0

    def get(self, path, errors):
        url = BASE + path
        attempts = 0
        while attempts < 4:
            wait = self.spacing - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.reads += 1
            attempts += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "IdentityMD-genesis-research/1.0"})
                with urllib.request.urlopen(req, timeout=45) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempts < 4:
                    try:
                        delay = max(1, int(exc.headers.get("Retry-After", "10")))
                    except ValueError:
                        delay = 10
                    time.sleep(delay)
                    continue
                errors.append({"url": url, "attempt": attempts, "status": exc.code,
                               "error": str(exc), "at": now()})
                return None
            except Exception as exc:
                if attempts < 3:
                    time.sleep(min(2 ** attempts, 8))
                    continue
                errors.append({"url": url, "attempt": attempts, "error": repr(exc), "at": now()})
                return None
        return None


def get_wallets():
    found = set()
    balances = {}
    for chain in ("ethereum", "robinhood"):
        data = json.loads((ROOT / "sources" / f"chain-{chain}" / "holders.json").read_text())
        for wallet, raw, *_ in data["rows"]:
            wallet = wallet.lower()
            if int(raw) > 0:
                found.add(wallet)
                balances[wallet] = balances.get(wallet, 0) + int(raw)
    return sorted(w for w in found if int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 6 == 3), balances


def source(source_name, columns, rows, errors, coverage):
    return {"source": source_name, "fetchedAt": now(), "coverage": coverage,
            "errors": errors, "columns": columns, "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="collect only first N shard wallets (for a resumable partial run)")
    parser.add_argument("--spacing", type=float, default=MIN_INTERVAL, help="minimum seconds between API reads")
    parser.add_argument("--emit-only", action="store_true", help="rebuild output datasets from saved checkpoints without API reads")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    wallets, balances = get_wallets()
    holder_lists = {}
    for chain in ("ethereum", "robinhood"):
        source_doc = json.loads((ROOT / "sources" / f"chain-{chain}" / "holders.json").read_text())
        lines = "".join(f"{row[0].lower()}:{row[1]}\n" for row in sorted(source_doc["rows"], key=lambda r: r[0].lower()))
        holder_lists[chain] = {"count": len(source_doc["rows"]), "holdersDigest": hashlib.sha256(lines.encode()).hexdigest(),
                               "source": f"sources/chain-{chain}/holders.json", "method": "used landed holder list"}
    todo = [] if args.emit_only else (wallets[:args.limit] if args.limit else wallets)
    state = read_json(STATE, {"wallets": {}, "seatDetails": {}, "errors": [], "seatSources": None, "lateJobs": {}})
    state.setdefault("lateJobs", {})
    api = Client(max(MIN_INTERVAL, args.spacing))

    # The two seat indexes are shared for the shard; persist them before wallet work.
    if state["seatSources"] is None or not state["seatSources"].get("owners") or not state["seatSources"].get("records"):
        errs = []
        owners = api.get("/seats/owners", errs)
        records = api.get("/seats/records", errs)
        state["seatSources"] = {"owners": owners, "records": records, "readAt": now()}
        state["errors"].extend(errs)
        write_json(STATE, state)

    seat_src = state["seatSources"]
    owner_data = seat_src.get("owners") or {}
    rec_data = seat_src.get("records") or {}
    # Owners is indexed directly by tokenId; require a record too.
    owners = owner_data.get("owners", [])
    recs = rec_data.get("seats", [])
    record_ids = {str(r.get("tokenId")) for r in recs}
    owned_by = {}
    for w in wallets:
        owned_by[w] = [str(i) for i, owner in enumerate(owners)
                       if isinstance(owner, str) and owner.lower() == w and str(i) in record_ids]

    for index, wallet in enumerate(todo, 1):
        if wallet in state["wallets"] and state["wallets"][wallet].get("done"):
            continue
        item = state["wallets"].setdefault(wallet, {"orders": None, "earnings": [], "errors": [], "earningsDone": False})
        errs = item["errors"]
        if item["orders"] is None:
            result = api.get(f"/requests/paid-by/{wallet}", errs)
            if result is not None:
                orders = result.get("orders", []) if isinstance(result, dict) else []
                item["orders"] = orders[:100]
                item["truncated"] = len(orders) >= 100 or (isinstance(result, dict) and result.get("count", len(orders)) > 100)
                item["orderCountReported"] = result.get("count") if isinstance(result, dict) else None
        if not item["earningsDone"]:
            cursor = item.get("earningsNext")
            suffix = "&before=" + urllib.parse.quote(str(cursor), safe="") if cursor is not None else ""
            page = api.get(f"/wallets/{wallet}/earnings?limit=200{suffix}", errs)
            if page is not None:
                item["earnings"].extend(page.get("earnings", []) if isinstance(page, dict) else [])
                item["earningsNext"] = page.get("next") if isinstance(page, dict) else None
                item["earningsDone"] = item["earningsNext"] is None
                write_json(STATE, state)
                seen_cursors = set()
                while not item["earningsDone"]:
                    cursor = item["earningsNext"]
                    if str(cursor) in seen_cursors:
                        errs.append({"wallet": wallet, "what": "earnings cursor repeated; pagination stopped", "cursor": cursor, "at": now()})
                        break
                    seen_cursors.add(str(cursor))
                    page = api.get(f"/wallets/{wallet}/earnings?limit=200&before={urllib.parse.quote(str(cursor), safe='')}", errs)
                    if page is None:
                        break
                    item["earnings"].extend(page.get("earnings", []) if isinstance(page, dict) else [])
                    item["earningsNext"] = page.get("next") if isinstance(page, dict) else None
                    item["earningsDone"] = item["earningsNext"] is None
                    write_json(STATE, state)
        item["seatsOwned"] = owned_by.get(wallet, [])
        item["done"] = (item["orders"] is not None and item["earningsDone"])
        if index % 25 == 0 or item["done"]:
            write_json(STATE, state)
            print(f"wallet {index}/{len(todo)} {wallet}: orders={len(item['orders'] or [])}, earnings={len(item['earnings'])}, done={item['done']}, reads={api.reads}", flush=True)
    write_json(STATE, state)

    # Fetch each seat detail once, retaining all pages and filtering snapshot counts locally.
    for wallet in todo:
        for token_id in state["wallets"].get(wallet, {}).get("seatsOwned", []):
            if token_id in state["seatDetails"] and not state["seatDetails"][token_id].get("errors"):
                continue
            errs = []
            work = []
            cursor = None
            for _ in range(100):
                path = f"/seats/{token_id}?work=1000&reviews=0&collaborators=0"
                if cursor is not None:
                    path += "&workBefore=" + urllib.parse.quote(str(cursor), safe="")
                detail = api.get(path, errs)
                if detail is None:
                    break
                batch = detail.get("work", []) if isinstance(detail, dict) else []
                work.extend(batch)
                cursor = batch[-1].get("submittedAt") if batch else None
                if cursor is None or len(batch) < 1000:
                    break
            state["seatDetails"][token_id] = {"detail": detail, "work": work, "errors": errs, "readAt": now()}
            write_json(STATE, state)

    # Gather jobs for orders in the 24h immediately preceding T which identify a job.
    late = state["lateJobs"]
    if args.emit_only and not late and (OUT / "late-jobs.json").exists():
        previous = json.loads((OUT / "late-jobs.json").read_text())
        for row in previous.get("rows", []):
            if len(row) >= 6:
                late[str(row[0])] = {"jobId": row[0], "orderWallet": row[1], "order": row[2],
                                    "detail": row[3], "result": row[4], "errors": row[5]}
        state["lateJobs"] = late
    for wallet in todo:
        for order in state["wallets"].get(wallet, {}).get("orders", []) or []:
            created = order.get("createdAt")
            if not isinstance(created, str):
                continue
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if not (SNAPSHOT_TS - 86400 <= ts < SNAPSHOT_TS):
                continue
            res = order.get("result") if isinstance(order.get("result"), dict) else {}
            jobid = order.get("resultJobId") or order.get("resultId") or res.get("jobId") or res.get("id")
            if not jobid or str(jobid) in late:
                continue
            errs = []
            detail = api.get("/jobs/" + urllib.parse.quote(str(jobid), safe=""), errs)
            created_job = detail.get("createdAt") if isinstance(detail, dict) else None
            result = None
            if isinstance(created_job, str):
                try:
                    if datetime.fromisoformat(created_job.replace("Z", "+00:00")).timestamp() >= SNAPSHOT_TS:
                        result = api.get("/jobs/" + urllib.parse.quote(str(jobid), safe="") + "/result", errs)
                except ValueError:
                    pass
            late[str(jobid)] = {"jobId": str(jobid), "orderWallet": wallet, "order": order,
                                "detail": detail, "result": result, "errors": errs}
            state["lateJobs"] = late
            write_json(STATE, state)

    # Emit compact object datasets. Keep raw API records intact inside row arrays.
    errors = list(state["errors"])
    order_cols = ["wallet", "orderId", "action", "status", "createdAt", "paidAt", "paymentStatus", "paymentTx", "resultKind", "resultJobId", "resultId", "continues", "beforeT"]
    order_rows, earning_rows, wallet_rows = [], [], []
    for wallet in wallets:
        it = state["wallets"].get(wallet, {})
        for o in it.get("orders") or []:
            created = o.get("createdAt")
            if not isinstance(created, str):
                errors.append({"wallet": wallet, "orderId": o.get("orderId"), "what": "createdAt missing; beforeT cannot be established"})
                continue
            before_t = isinstance(created, str) and _before(created)
            if not before_t:
                continue
            payment = o.get("payment") if isinstance(o.get("payment"), dict) else {}
            result = o.get("result") if isinstance(o.get("result"), dict) else {}
            order_rows.append([wallet, o.get("orderId"), o.get("action"), o.get("status"), created,
                o.get("paidAt"), payment.get("status", o.get("paymentStatus")),
                payment.get("tx", payment.get("txHash", payment.get("transactionHash", o.get("paymentTx")))),
                result.get("kind", o.get("resultKind")), result.get("jobId", o.get("resultJobId")),
                result.get("requestId", result.get("workflowId", result.get("id", o.get("resultId")))),
                o.get("continues"), before_t])
        for e in it.get("earnings", []):
            at = e.get("at") if isinstance(e, dict) else None
            if isinstance(at, str) and _before(at):
                earning_rows.append([wallet, e])
            elif not isinstance(at, str):
                errors.append({"wallet": wallet, "what": "earnings row has no usable at timestamp", "row": e})
        count_before = sum(1 for o in (it.get("orders") or []) if isinstance(o.get("createdAt"), str) and _before(o["createdAt"]))
        wallet_rows.append([wallet, str(balances[wallet]), count_before, bool(it.get("truncated", False)),
                            sum(1 for e in it.get("earnings", []) if isinstance(e, dict) and isinstance(e.get("at"), str) and _before(e["at"])),
                            it.get("seatsOwned", []), it.get("errors", []), bool(it.get("done", False))])
        errors.extend(it.get("errors", []))
    seat_rows = []
    for wallet in wallets:
        for tid in owned_by.get(wallet, []):
            entry = state["seatDetails"].get(tid, {})
            detail = entry.get("detail") or {}
            work = entry.get("work", [])
            before = [x for x in work if _before(x.get("submittedAt") or "")]
            accepted = [x for x in before if _before(x.get("acceptedAt") or "")]
            rejected = [x for x in before if x.get("status") in ("rejected", "failed")]
            seat_rows.append([tid, detail.get("owner") if isinstance(detail, dict) else None,
                              detail.get("attempts") if isinstance(detail, dict) else None,
                              detail.get("accepted") if isinstance(detail, dict) else None,
                              detail.get("rejected") if isinstance(detail, dict) else None,
                              len(before), len(accepted), len({x.get("jobId") for x in before if x.get("jobId")}), entry.get("readAt")])
            errors.extend(entry.get("errors", []))
    write_json(OUT / "orders.json", source("GET /requests/paid-by/{wallet}", order_cols, order_rows, errors,
        {"shard": 3, "shards": 6, "walletCount": len(wallets), "ordersPerWalletCap": 100, "onlyOrdersBeforeT": True,
         "nullReason": "a null field was absent from the API response or was not represented in its payment/result object; no value inferred"}))
    # Earnings contain large launch and token objects repeated for every
    # recipient. Store those objects once and reference them from a flat table.
    # The fields and values remain losslessly reconstructible from this file.
    launch_ids, token_ids = {}, {}
    launch_catalog, token_catalog, compact_earnings = [], [], []
    for wallet, earning in earning_rows:
        launch_key = json.dumps(earning.get("launch"), sort_keys=True, separators=(",", ":"))
        token_key = json.dumps(earning.get("token"), sort_keys=True, separators=(",", ":"))
        if launch_key not in launch_ids:
            launch_ids[launch_key] = len(launch_catalog)
            launch_catalog.append(earning.get("launch"))
        if token_key not in token_ids:
            token_ids[token_key] = len(token_catalog)
            token_catalog.append(earning.get("token"))
        compact_earnings.append([wallet, earning.get("launchId"), earning.get("launchNumber"),
            earning.get("status"), earning.get("chainId"), earning.get("kind"),
            token_ids[token_key], earning.get("amount"), earning.get("at"), launch_ids[launch_key]])
    earnings_doc = source("GET /wallets/{wallet}/earnings?limit=200",
        ["wallet", "launchId", "launchNumber", "status", "chainId", "kind", "tokenRef", "amount", "at", "launchRef"],
        compact_earnings, errors,
        {"walletCount": len(wallets), "pagedBy": "next", "onlyRowsBeforeT": True,
         "encoding": "lossless normalized table; tokenRef and launchRef index the corresponding catalogs",
         "earningFields": ["launchId", "launchNumber", "status", "chainId", "kind", "token", "amount", "at", "launch"],
         "tokenCatalogCount": len(token_catalog), "launchCatalogCount": len(launch_catalog)})
    earnings_doc["tokens"] = token_catalog
    earnings_doc["launches"] = launch_catalog
    write_json(OUT / "earnings.json", earnings_doc)
    write_json(OUT / "seats.json", source("GET /seats/owners; /seats/records; GET /seats/{tokenId}?work=1000&reviews=0&collaborators=0", ["tokenId", "owner", "attempts", "accepted", "rejected", "workBeforeT", "acceptedBeforeT", "jobsBeforeT", "readAt"], seat_rows, errors,
        {"ownershipAsOf": "each GET /seats/{tokenId} readAt; current read, not historical T", "ownersAndRecordsReadAt": seat_src.get("readAt"), "recordRequired": True, "walletCount": len(wallets)}))
    write_json(OUT / "wallets.json", source("Shard wallet rollup from chain holders and IMD API", ["wallet", "balanceRaw", "orders", "truncated", "earnings", "seatsOwned", "errors", "complete"], wallet_rows, errors,
        {"shard": 3, "shards": 6, "walletCount": len(wallets), "holderLists": holder_lists, "snapshot": SNAPSHOT}))
    write_json(OUT / "late-jobs.json", source("GET /jobs/{id} for recent paid orders", ["jobId", "orderWallet", "order", "detail", "result", "errors"],
        [[k, v["orderWallet"], v["order"], v["detail"], v["result"], v["errors"]] for k, v in sorted(late.items())], errors,
        {"ordersCreatedIn": ["2026-11-24T11:00:00Z", SNAPSHOT], "lateJobResultReadWhenJobCreatedAtOrAfterT": True}))
    print(f"complete wallets={sum(1 for w in wallets if state['wallets'].get(w,{}).get('done'))}/{len(wallets)} seats={len(seat_rows)} lateJobs={len(late)} errors={len(errors)} reads={api.reads}")


def _before(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() < SNAPSHOT_TS
    except (ValueError, AttributeError):
        return False


def _count_before(items, field, jobs=False):
    return sum(1 for x in items if _before(x.get(field, "")))


if __name__ == "__main__":
    main()
