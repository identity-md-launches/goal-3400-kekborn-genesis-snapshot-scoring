#!/usr/bin/env python3
"""Write fixtures/cases.json: a miniature repository root exercising lib.select()."""
import csv
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lib  # noqa: E402


def pick(prefix, shard, n=1):
    out, i = [], 0
    while len(out) < n:
        c = f"{prefix}-{i:04d}-0000-4000-8000-000000000000"
        if lib.shard_of(c) == shard:
            out.append(c)
        i += 1
    return out


H = "0x" + "a1" * 20      # holder
N = "0x" + "b2" * 20      # non-holder
G1, G2 = pick("aaaa0001", 1, 2)          # oracle-group members by direct jobId
G3, = pick("aaaa0002", 1)                # oracle-group member resolved only via requestId catalog
POST, = pick("bbbb0001", 1)              # job admitted after T, order before T -> included
POSTLATE, = pick("bbbb0002", 1)          # order created after T -> excluded
GAP, = pick("cccc0001", 1)               # jobs.csv holder gap row -> included
GAP0, = pick("cccc0002", 0)              # jobs.csv gap row in shard 0 -> excluded
SAVED, = pick("dddd0001", 1)             # has saved detail -> excluded
NONH, = pick("eeee0001", 1)              # non-holder order -> excluded
INTAKE, = pick("ffff0001", 1)            # intake admission with explicit payer -> included
INTAKEX, = pick("ffff0002", 1)           # intake entry whose payer != wallet -> excluded
REQ3 = "req-3"


def table(cols, rows, **extra):
    return {"source": "fixture", "fetchedAt": "2026-11-25T00:00:00Z", "coverage": "fixture",
            "errors": [], "columns": cols, "rows": rows, **extra}


def csv_text(cols, rows):
    b = io.StringIO()
    w = csv.writer(b, lineterminator="\n")
    w.writerow(cols)
    w.writerows(rows)
    return b.getvalue()


jobs_cols = ["job_id", "wallet", "is_holder", "order_id", "data_gaps"]
gap = json.dumps({"detail": "no detailed job record in saved job shards or late-jobs; index-only fields retained"})
files = {
    "holders.csv": csv_text(["wallet"], [[H]]),
    "jobs.csv": csv_text(jobs_cols, [[GAP, H, "true", "o-gap", gap], [GAP0, H, "true", "o-gap0", gap],
                                     [SAVED, H, "true", "o-saved", "{}"]]),
    "sources/api-jobs-00/jobs.json": table(["id", "error"], [[SAVED, None]]),
    "sources/api-wallets-00/late-jobs.json": table(["jobId", "detail"], []),
    "sources/api-catalog/oracle-requests.json": table(["id", "jobId"], [[REQ3, G3]]),
    "sources/api-wallets-00/orders.json": table(
        ["wallet", "orderId", "createdAt", "resultJobId", "result"],
        [[H, "o-group", "2026-11-14T17:00:53.316Z", [G1, G2],
          {"kind": "oracle-group", "requests": [{"jobId": G1, "requestId": "req-1"},
                                                {"jobId": G2, "requestId": "req-2"},
                                                {"requestId": REQ3}]}],
         [H, "o-post", "2026-11-25T10:59:59.999Z", POST, {"kind": "job", "jobId": POST}],
         [H, "o-late", "2026-11-25T11:00:00.000Z", POSTLATE, {"kind": "job", "jobId": POSTLATE}],
         [H, "o-saved", "2026-10-01T00:00:00Z", SAVED, {"kind": "job", "jobId": SAVED}],
         [H, "o-req", "2026-10-02T00:00:00Z", None, {"kind": "oracle", "requestId": "req-unknown"}],
         [N, "o-nonh", "2026-10-01T00:00:00Z", NONH, {"kind": "job", "jobId": NONH}]]),
    "sources/e04-orders/paid-by.json": table(
        ["wallet", "shard", "url", "fetchedAt", "httpStatus", "bodySha256", "bodyText", "error"],
        [[H, "00", "u", "t", 200, "x", json.dumps({"orders": [
            {"orderId": "o-post", "createdAt": "2026-11-25T10:59:59.999Z", "result": {"jobId": POST}}]}), None]]),
    "sources/e04-orders/intake.json": table(
        ["chainId", "transactionHash", "wallet", "fetchedAt", "httpStatus", "bodySha256", "bodyText", "error"],
        [[1, "0x1", H, "t", 200, "x", json.dumps({"entries": [
            {"payer": H, "order": {"id": "o-intake"},
             "admission": {"createdAt": "2026-11-13T04:49:12.232Z", "result": {"jobId": INTAKE, "requestId": "req-9"}}},
            {"payer": N, "order": {"id": "o-intakex"},
             "admission": {"createdAt": "2026-11-13T04:49:12.232Z", "result": {"jobId": INTAKEX}}}]}), None]]),
}
cases = {
    "files": files,
    "expected": sorted([G1, G2, G3, POST, GAP, INTAKE]),
    "excluded": {POSTLATE: "order created at/after T", GAP0: "shard 0", SAVED: "saved detail",
                 NONH: "non-holder payer", INTAKEX: "intake payer differs from wallet"},
    "unresolvedRequests": ["req-unknown"],
    "oracleGroupOrder": "o-group",
    "postTAdmission": POST,
}
out = HERE / "fixtures/cases.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(cases, indent=1) + "\n", encoding="utf-8")
print("wrote", out)
