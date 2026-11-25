#!/usr/bin/env python3
"""Shard 0 of 2 evidence collector: fill missing job detail/result for holder-linked jobs.

Public GET only. `python3 collect.py plan` is offline (writes nothing); `python3 collect.py fetch`
reads /jobs/{id} and /jobs/{id}/result for each selected ID (<=30 reads/min, 429 honoured) and
writes sources/e04-jobs-00/{index,records}.json. Status/result values are read-time, not at T.
"""
import csv, glob, json, sys, time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources/e04-jobs-00"
API = "https://api.imd.fun"
T = "2026-11-25T11:00:00Z"
SHARD, SHARDS = 0, 2
SPACING = 2.1  # seconds between reads -> <=28/min
RECORD_COLUMNS = ["jobId", "selectionBasis", "detail", "detailFetchedAt", "detailHttpStatus", "detailError",
                  "result", "resultFetchedAt", "resultHttpStatus", "resultError"]
INDEX_COLUMNS = ["jobId", "basis", "reference"]


def shard_of(job_id):
    return int(sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % SHARDS


def in_shard(job_id):
    return shard_of(job_id) == SHARD


def before_t(ts):
    return bool(ts) and datetime.fromisoformat(ts.replace("Z", "+00:00")) < datetime.fromisoformat(T.replace("Z", "+00:00"))


def load(path):
    with open(ROOT / path, encoding="utf-8") as f:
        return json.load(f)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def saved_detail_ids(root=ROOT):
    """IDs whose detail was already saved without error in other sources."""
    have = set()
    for p in glob.glob(str(root / "sources/api-jobs-0*/jobs.json")):
        d = json.load(open(p, encoding="utf-8")); c = d["columns"]
        for r in d["rows"]:
            if r[c.index("error")] is None:
                have.add(r[0])
    p = root / "sources/api-jobs-04/details.json"
    if p.exists():
        for r in json.load(open(p, encoding="utf-8"))["rows"]:
            if r[2] == 200:
                have.add(r[0])
    return have


def refs_from_order(order, request_to_job):
    """Job IDs referenced by one paid-by order (direct jobId, oracle-group requests[], request catalog)."""
    out = []
    res = order.get("result") or {}
    if res.get("jobId"):
        out.append((res["jobId"], "result.jobId"))
    for q in res.get("requests") or []:
        if q.get("jobId"):
            out.append((q["jobId"], "result.requests[].jobId"))
        elif q.get("requestId") in request_to_job:
            out.append((request_to_job[q["requestId"]], "result.requests[].requestId via catalog"))
    if not res.get("jobId") and res.get("requestId") in request_to_job:
        out.append((request_to_job[res["requestId"]], "result.requestId via catalog"))
    return out


def order_refs(orders, request_to_job):
    """orders: iterable of (orderId, createdAt, order dict). Post-T admissions count only if the order is pre-T."""
    out = []
    for oid, created, order in orders:
        if not before_t(created):
            continue
        for jid, how in refs_from_order(order, request_to_job):
            out.append((jid, "order-ref", f"order:{oid} {how}"))
    return out


def select(candidates, have):
    """candidates: (jobId, basis, reference). Deterministic: unique, lacking detail, in shard, sorted."""
    chosen = {}
    for jid, basis, ref in sorted(candidates):
        if jid in have or not in_shard(jid) or jid in chosen:
            continue
        chosen[jid] = (basis, ref)
    return [[j, b, r] for j, (b, r) in sorted(chosen.items())]


def expected(root=ROOT):
    have = saved_detail_ids(root)
    cat = json.load(open(root / "sources/api-catalog/oracle-requests.json", encoding="utf-8"))
    request_to_job = {r[0]: r[2] for r in cat["rows"] if r[2]}
    cands = []
    with open(root / "jobs.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["is_holder"] == "true" and r["job_id"] not in have and before_t(r["order_created_at"] or r["created_at"]):
                cands.append((r["job_id"], "jobs.csv-holder-lacking-detail", f"order:{r['order_id']}"))
    orders = []
    for row in json.load(open(root / "sources/e04-orders/paid-by.json", encoding="utf-8"))["rows"]:
        if row[4] == 200 and row[6]:
            for o in json.loads(row[6]).get("orders", []):
                orders.append((o["orderId"], o.get("createdAt"), o))
    for row in json.load(open(root / "sources/e04-orders/intake.json", encoding="utf-8"))["rows"]:
        if row[4] == 200 and row[6]:
            for e in json.loads(row[6]).get("entries", []):
                adm = e.get("admission") or {}
                if adm.get("result"):
                    orders.append(((e.get("order") or {}).get("id"), adm.get("createdAt"), {"result": adm["result"]}))
    cands += order_refs(orders, request_to_job)
    return select(cands, have), len(have)


NETFAIL = [0]
LAST = [0.0]  # monotonic time of the last answered read; failed connections do not count as reads


def get(path):
    """One public GET with up to 3 tries; returns (status, parsed_or_None, error, fetchedAt)."""
    err = None
    if NETFAIL[0] >= 6:
        return None, None, "not attempted: network unreachable (6 consecutive connection failures this run)", now()
    for _ in range(3):
        time.sleep(max(0, LAST[0] + SPACING - time.monotonic()))
        at = now()
        try:
            with urlopen(Request(API + path, headers={"Accept": "application/json"}), timeout=30) as r:
                LAST[0] = time.monotonic(); NETFAIL[0] = 0
                return r.status, json.loads(r.read().decode("utf-8")), None, at
        except HTTPError as e:
            LAST[0] = time.monotonic()
            body = e.read(2000).decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = int(e.headers.get("Retry-After", "60"))
                except ValueError:
                    wait = 60
                err = f"HTTP 429"
                time.sleep(min(wait, 120)); continue
            return e.code, None, f"HTTP {e.code}: {body[:300]}", at
        except Exception as e:  # network/proxy/json
            err = f"{type(e).__name__}: {e}"
            at = now()
            NETFAIL[0] += 1
            if "Proxy" in err or "Tunnel" in err or "URLError" in err:
                break
    return None, None, err, at


def result_requests(result):
    return [q for q in ((result or {}).get("requests") or []) if isinstance(q, dict)]


def fetch_all():
    ids, nhave = expected()
    queue = [r[0] for r in ids]; sel = {r[0]: r for r in ids}
    records, seen, discovered = [], set(), []
    while queue:
        jid = queue.pop(0)
        if jid in seen:
            continue
        seen.add(jid)
        ds, dp, de, dt = get(f"/jobs/{jid}")
        rs, rp, re_, rt = get(f"/jobs/{jid}/result")
        records.append([jid, sel[jid][0], dp, dt, ds, de, rp, rt, rs, re_])
        for q in result_requests(rp):  # oracle-group / result.requests[] expansion
            nid = q.get("jobId")
            if nid and nid not in sel and in_shard(nid) and nid not in saved_detail_ids():
                sel[nid] = [nid, "result.requests[] expansion", f"job:{jid}"]; queue.append(nid); discovered.append(nid)
    return ids, nhave, records, sel, discovered


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if mode == "plan":
        ids, nhave, *_ = expected(); print(len(ids), "selected;", nhave, "already saved"); return
    ids, nhave, records, sel, discovered = fetch_all()
    OUT.mkdir(parents=True, exist_ok=True)
    errs = [{"jobId": r[0], "detailError": r[5], "resultError": r[9]} for r in records if r[5] or r[9]]
    index = {"source": "derived: jobs.csv, sources/e04-orders/{paid-by,intake}.json, sources/api-catalog/oracle-requests.json",
             "fetchedAt": now(),
             "coverage": {"shard": f"{SHARD} of {SHARDS}", "shardRule": "int(sha256(jobId UTF-8).hexdigest()[:8],16)%2==0",
                          "snapshotTime": T, "selectionBasis": "holder-linked jobs.csv rows lacking saved detail; jobs referenced by pre-T orders in e04-orders (direct jobId, request IDs via catalog, result.requests[]); post-T admitted jobs only through pre-T orders; IDs with saved detail excluded",
                          "expected": len(sel), "alreadySavedDetails": nhave, "expandedByResult": discovered,
                          "readTimeNote": "status/result are read-time observations, not historical at T"},
             "errors": errs, "columns": INDEX_COLUMNS, "rows": [[j, sel[j][1], sel[j][2]] for j in sorted(sel)]}
    rec = {"source": ["GET /jobs/{id}", "GET /jobs/{id}/result"], "fetchedAt": now(), "coverage": index["coverage"],
           "errors": errs, "columns": RECORD_COLUMNS, "rows": sorted(records, key=lambda r: r[0])}
    for name, d in (("index.json", index), ("records.json", rec)):
        (OUT / name).write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(len(sel), "expected;", len(records), "read;", len(errs), "with errors")


if __name__ == "__main__":
    main()
