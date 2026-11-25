"""Deterministic selection of shard-1 job IDs lacking saved detail (e04-jobs-01).

Pure functions over saved repository files; Python standard library only.
"""
import csv
import glob
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
T = "2026-11-25T11:00:00Z"
SHARD, SHARDS = 1, 2


def shard_of(job_id):
    return int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % SHARDS


def before_t(ts):
    # ISO-8601 UTC strings; compare as instants (normalise trailing Z / millis).
    from datetime import datetime
    a = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    b = datetime.fromisoformat(T.replace("Z", "+00:00"))
    return a < b


def load(rel, root=ROOT):
    with open(Path(root) / rel, encoding="utf-8") as f:
        return json.load(f)


def rows_as_dicts(doc):
    cols = doc["columns"]
    return [dict(zip(cols, r)) for r in doc["rows"]]


def saved_detail_ids(root=ROOT):
    """IDs that already have a saved /jobs/{id} detail in landed shards."""
    ids = set()
    for p in sorted(glob.glob(str(Path(root) / "sources/api-jobs-0*/jobs.json"))):
        for r in rows_as_dicts(json.load(open(p, encoding="utf-8"))):
            if r.get("id") and not r.get("error"):
                ids.add(r["id"])
    for p in sorted(glob.glob(str(Path(root) / "sources/api-wallets-0*/late-jobs.json"))):
        for r in rows_as_dicts(json.load(open(p, encoding="utf-8"))):
            if r.get("jobId") and r.get("detail"):
                ids.add(r["jobId"])
    return ids


def holder_set(root=ROOT):
    with open(Path(root) / "holders.csv", encoding="utf-8") as f:
        return {r["wallet"].lower() for r in csv.DictReader(f)}


def oracle_catalog(root=ROOT):
    """requestId -> jobId from the saved oracle request catalog."""
    return {r["id"]: r["jobId"] for r in rows_as_dicts(load("sources/api-catalog/oracle-requests.json", root))
            if r.get("jobId")}


def refs_from_result(result):
    """Yield (kind, id) references in an order's result object: direct jobId,
    oracle requestId and every result.requests[] member (oracle-group expansion).
    A requestId is resolved through the catalog only when its object carries no jobId."""
    if not isinstance(result, dict):
        return
    if result.get("jobId"):
        yield ("job", result["jobId"])
    if result.get("requestId") and not result.get("jobId"):
        yield ("request", result["requestId"])
    for i, req in enumerate(result.get("requests") or []):
        if isinstance(req, dict):
            if req.get("jobId"):
                yield ("job", req["jobId"])
            if req.get("requestId") and not req.get("jobId"):
                yield ("request", req["requestId"])


def order_observations(root=ROOT):
    """Every saved order observation: (wallet, orderId, createdAt, result, source)."""
    out = []
    for p in sorted(glob.glob(str(Path(root) / "sources/api-wallets-0*/orders.json"))):
        rel = str(Path(p).relative_to(root))
        for r in rows_as_dicts(json.load(open(p, encoding="utf-8"))):
            res = r.get("result") if isinstance(r.get("result"), dict) else {}
            res = dict(res)
            rj = r.get("resultJobId")
            extra = rj if isinstance(rj, list) else ([rj] if rj else [])
            known = {x for _, x in refs_from_result(res)}
            missing = [x for x in extra if isinstance(x, str) and x not in known]
            if missing:
                res["requests"] = list(res.get("requests") or []) + [{"jobId": x} for x in missing]
            out.append((r["wallet"].lower(), r["orderId"], r.get("createdAt"), res, rel))
    pb = load("sources/e04-orders/paid-by.json", root)
    for r in rows_as_dicts(pb):
        if r.get("httpStatus") != 200 or not r.get("bodyText"):
            continue
        body = json.loads(r["bodyText"])
        for o in body.get("orders") or []:
            out.append((r["wallet"].lower(), o.get("orderId"), o.get("createdAt"), o.get("result"),
                        "sources/e04-orders/paid-by.json"))
    it = load("sources/e04-orders/intake.json", root)
    for r in rows_as_dicts(it):
        if r.get("httpStatus") != 200 or not r.get("bodyText"):
            continue
        body = json.loads(r["bodyText"])
        for e in body.get("entries") or []:
            if not isinstance(e, dict):
                continue
            payer = (e.get("payer") or "").lower()
            if payer != r["wallet"].lower():
                continue  # never attribute by anything but the explicit payer
            order = e.get("order") if isinstance(e.get("order"), dict) else {}
            adm = e.get("admission") if isinstance(e.get("admission"), dict) else {}
            out.append((payer, order.get("id"), adm.get("createdAt"), adm.get("result"),
                        "sources/e04-orders/intake.json"))
    return out


def jobs_csv_gap_rows(root=ROOT):
    with open(Path(root) / "jobs.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["is_holder"] == "true" and "no detailed job record" in r["data_gaps"]:
                yield r


def select(root=ROOT, extra_request_map=None):
    """Return (expected, unresolved_requests).

    expected: {jobId: {"basis": [...], "wallets": [...], "orders": [...]}} for shard 1 only.
    unresolved_requests: {requestId: [basis]} whose jobId is not in the saved catalog
    (resolved later through public request detail, passed via extra_request_map).
    """
    saved = saved_detail_ids(root)
    holders = holder_set(root)
    cat = oracle_catalog(root)
    if extra_request_map:
        cat = {**cat, **extra_request_map}
    cand = {}

    def add(jid, basis, wallet=None, order=None):
        e = cand.setdefault(jid, {"basis": set(), "wallets": set(), "orders": set()})
        e["basis"].add(basis)
        if wallet:
            e["wallets"].add(wallet)
        if order:
            e["orders"].add(order)

    for r in jobs_csv_gap_rows(root):
        add(r["job_id"], "jobs.csv:holder-linked-no-detail", r["wallet"] or None, r["order_id"] or None)

    unresolved = {}
    for wallet, oid, created, res, src in order_observations(root):
        if wallet not in holders or not created or not before_t(created):
            continue  # only evidenced pre-T orders of holders
        for kind, ref in refs_from_result(res):
            if kind == "job":
                add(ref, "order-ref:" + src, wallet, oid)
            else:
                jid = cat.get(ref)
                if jid:
                    add(jid, "oracle-request:" + src, wallet, oid)
                else:
                    unresolved.setdefault(ref, set()).add((wallet, oid, src))

    expected = {}
    for jid, e in cand.items():
        if jid in saved and not any(b.startswith("jobs.csv") for b in e["basis"]):
            continue  # already has saved detail: do not refetch
        if jid in saved:
            continue
        if shard_of(jid) != SHARD:
            continue
        expected[jid] = {k: sorted(v) for k, v in e.items()}
    unresolved = {k: sorted(map(list, v)) for k, v in unresolved.items()}
    return expected, unresolved


def expected_digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def input_files(root=ROOT):
    rels = ["jobs.csv", "holders.csv", "sources/api-catalog/oracle-requests.json",
            "sources/e04-orders/paid-by.json", "sources/e04-orders/intake.json"]
    for pat in ("sources/api-jobs-0*/jobs.json", "sources/api-wallets-0*/late-jobs.json",
                "sources/api-wallets-0*/orders.json"):
        rels += [str(Path(p).relative_to(root)) for p in glob.glob(str(Path(root) / pat))]
    return sorted(rels)


def input_hashes(root=ROOT):
    return {r: hashlib.sha256((Path(root) / r).read_bytes()).hexdigest() for r in input_files(root)}
