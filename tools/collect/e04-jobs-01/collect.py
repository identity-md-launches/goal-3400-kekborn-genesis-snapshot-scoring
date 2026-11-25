#!/usr/bin/env python3
"""Collect /jobs/{id} and /jobs/{id}/result for e04-jobs-01 (shard 1 of 2).

Read-only public GETs to https://api.imd.fun, at most ~28 reads/min, honours 429
Retry-After. Resumable: already-saved reads are never refetched.
Usage: python3 -B tools/collect/e04-jobs-01/collect.py [--max-seconds N]
       python3 -B tools/collect/e04-jobs-01/collect.py --finalize
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

API = "https://api.imd.fun"
OUT = lib.ROOT / "sources/e04-jobs-01"
PARTIAL = OUT / "partial.json"
MIN_GAP = 2.1  # seconds between reads: <= ~28.5 reads/min
COLUMNS = ["jobId", "shard", "basis", "wallets", "orders", "detailUrl", "detailFetchedAt",
           "detailHttpStatus", "detailSha256", "detailBodyText", "detailError", "detailIdMatches",
           "resultUrl", "resultFetchedAt", "resultHttpStatus", "resultSha256", "resultBodyText",
           "resultError", "template", "stateAtRead", "createdAt", "createdAtOrAfterT",
           "oracleRequestId", "attestation", "resultMeta", "detailAttempts", "resultAttempts"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Reader:
    def __init__(self):
        self.last = 0.0
        self.count = 0

    def get(self, url):
        """Return dict(url, fetchedAt, httpStatus, sha256, bodyText, error, attempts)."""
        attempts = []
        for attempt in range(1, 5):
            time.sleep(max(0.0, self.last + MIN_GAP - time.monotonic()))
            self.last = time.monotonic()
            self.count += 1
            at = now()
            try:
                req = Request(url, headers={"User-Agent": "imd-genesis-collector/1",
                                            "Accept": "application/json"})
                with urlopen(req, timeout=60) as r:
                    body = r.read()
                    status = r.status
                return {"url": url, "fetchedAt": at, "httpStatus": status,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bodyText": body.decode("utf-8"), "error": None, "attempts": attempts}
            except HTTPError as e:
                body = e.read() or b""
                attempts.append({"at": at, "httpStatus": e.code})
                if e.code == 429 or e.code >= 500:
                    ra = e.headers.get("Retry-After")
                    delay = 60.0
                    if ra:
                        try:
                            delay = float(ra)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds()
                            except Exception:
                                delay = 60.0
                    time.sleep(max(1.0, min(delay, 120.0)) if e.code == 429 else 5.0 * attempt)
                    continue
                return {"url": url, "fetchedAt": at, "httpStatus": e.code,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bodyText": body.decode("utf-8", "replace"),
                        "error": "HTTP %d" % e.code, "attempts": attempts}
            except (URLError, TimeoutError, OSError) as e:
                attempts.append({"at": at, "error": repr(e)[:200]})
                time.sleep(5.0 * attempt)
        return {"url": url, "fetchedAt": attempts[-1]["at"], "httpStatus": None, "sha256": None,
                "bodyText": None, "error": "gave up after %d attempts" % len(attempts),
                "attempts": attempts}


def load_partial():
    if PARTIAL.exists():
        return json.loads(PARTIAL.read_text(encoding="utf-8"))
    return {"reads": {}}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def collect(max_seconds):
    expected, unresolved = lib.select()
    if unresolved:
        print("unresolved oracle requests (no saved catalog jobId):", len(unresolved))
    part = load_partial()
    reads = part["reads"]
    reader = Reader()
    start = time.monotonic()
    todo = []
    for jid in sorted(expected):
        for kind, url in (("detail", f"{API}/jobs/{jid}"), ("result", f"{API}/jobs/{jid}/result")):
            r = reads.get(url)
            if r is None or (r.get("httpStatus") is None or r["httpStatus"] == 429 or r["httpStatus"] >= 500):
                todo.append(url)
    print("expected", len(expected), "reads pending", len(todo))
    done = 0
    for url in todo:
        if time.monotonic() - start > max_seconds:
            break
        reads[url] = reader.get(url)
        done += 1
        if done % 20 == 0:
            save_json(PARTIAL, part)
            print(done, "reads", now(), flush=True)
    save_json(PARTIAL, part)
    left = len(todo) - done
    print("done this run", done, "left", left)
    return left


ATTEST_KEYS = ("attestation", "attestationUrl", "attestedAt", "attestationHash", "signature",
               "signatures", "attestor", "attestors", "panel", "answer", "answerType", "oracle",
               "oracleRequest", "oracleRequestId", "requestId")


def pick_meta(obj):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ATTEST_KEYS or "attest" in k.lower():
                out[k] = v
    return out or None


def finalize():
    expected, unresolved = lib.select()
    part = load_partial()
    reads = part["reads"]
    rows, errors = [], []
    for jid in sorted(expected):
        e = expected[jid]
        d = reads.get(f"{API}/jobs/{jid}") or {}
        r = reads.get(f"{API}/jobs/{jid}/result") or {}
        detail = result = None
        if d.get("httpStatus") == 200:
            detail = json.loads(d["bodyText"])
        if r.get("httpStatus") == 200:
            result = json.loads(r["bodyText"])
        for kind, rd in (("detail", d), ("result", r)):
            if not rd:
                errors.append({"jobId": jid, "read": kind, "error": "not read"})
            elif rd.get("error"):
                errors.append({"jobId": jid, "read": kind, "url": rd["url"], "fetchedAt": rd["fetchedAt"],
                               "httpStatus": rd.get("httpStatus"), "error": rd["error"]})
        created = detail.get("createdAt") if isinstance(detail, dict) else None
        meta = None
        if isinstance(result, dict):
            meta = {k: result.get(k) for k in ("jobId", "projectId", "state", "complete", "launch",
                                                "delivery") if k in result}
            meta["files"] = [{k: f.get(k) for k in ("name", "path", "hash", "mediaType", "bytes",
                                                     "submissionHash", "url", "evaluation", "profile")}
                             for f in result.get("files") or [] if isinstance(f, dict)]
            meta["sourceCount"] = len(result.get("source") or [])
        att = {}
        for src in (detail, result):
            m = pick_meta(src)
            if m:
                att.update(m)
        rows.append([
            jid, lib.shard_of(jid), e["basis"], e["wallets"], e["orders"],
            d.get("url"), d.get("fetchedAt"), d.get("httpStatus"), d.get("sha256"), d.get("bodyText"),
            d.get("error"), (detail.get("id") == jid) if isinstance(detail, dict) else None,
            r.get("url"), r.get("fetchedAt"), r.get("httpStatus"), r.get("sha256"), r.get("bodyText"),
            r.get("error"),
            detail.get("template") if isinstance(detail, dict) else None,
            detail.get("state") if isinstance(detail, dict) else None,
            created, (not lib.before_t(created)) if created else None,
            (detail.get("oracleRequestId") or (detail.get("oracle") or {}).get("requestId")
             if isinstance(detail, dict) and isinstance(detail.get("oracle") or {}, dict) else None),
            att or None, meta, d.get("attempts", []), r.get("attempts", []),
        ])
    fetched_at = max([x for row in rows for x in (row[6], row[13]) if x] or [now()])
    ids = sorted(expected)
    ok_detail = sum(1 for row in rows if row[7] == 200)
    ok_result = sum(1 for row in rows if row[14] == 200)
    unavailable = sorted({row[0] for row in rows if row[7] != 200})
    coverage = {
        "snapshotTime": lib.T,
        "shard": "1 of 2",
        "shardRule": "int(sha256(jobId UTF-8).hexdigest()[:8],16) % 2 == 1",
        "selection": [
            "jobs.csv rows with is_holder=true whose data_gaps.detail says no detailed job record",
            "job references (result.jobId, resultJobId, result.requests[].jobId) and oracle requestIds "
            "(result.requestId, result.requests[].requestId resolved via sources/api-catalog/oracle-requests.json) "
            "in holder orders created strictly before T from sources/api-wallets-0*/orders.json (repaired), "
            "sources/e04-orders/paid-by.json bodies and sources/e04-orders/intake.json admissions (explicit payer only)",
            "minus IDs already with saved detail in sources/api-jobs-0*/jobs.json or sources/api-wallets-0*/late-jobs.json",
            "jobs admitted after T are kept only because an evidenced order was created before T",
        ],
        "expectedCount": len(ids),
        "expectedIdsSha256": lib.expected_digest(ids),
        "idHashEncoding": "sorted IDs joined with LF, no trailing LF, UTF-8",
        "basisCounts": {
            "jobsCsvHolderNoDetail": sum(1 for j in ids if any(b.startswith("jobs.csv") for b in expected[j]["basis"])),
            "newFromOrdersOnly": sum(1 for j in ids if not any(b.startswith("jobs.csv") for b in expected[j]["basis"])),
        },
        "unresolvedOracleRequests": unresolved,
        "inputsSha256": lib.input_hashes(),
        "detailFetched": ok_detail,
        "resultFetched": ok_result,
        "unavailableDetail": unavailable,
        "createdAtOrAfterT": sorted(row[0] for row in rows if row[21]),
        "stateTiming": "status/state/result observations are read-time (fetchedAt), not historical at T",
        "attribution": "wallets/orders are the explicit order payers from saved order evidence; no payer is inferred from seats, answer consumers or display names",
        "attestation": "attestation-like fields copied only when present in the responses; an oracle panel assessment is not treated as a successful oracle answer",
        "originalShards": "sources/api-jobs-0*, sources/api-wallets-0*, jobs.csv left unchanged as earlier observations",
        "noBinaries": "artifact URLs listed in resultMeta are not downloaded; no job result was executed",
    }
    doc = {"source": [f"{API}/jobs/<id>", f"{API}/jobs/<id>/result"], "fetchedAt": fetched_at,
           "coverage": coverage, "errors": errors, "columns": COLUMNS, "rows": rows}
    save_json(OUT / "records.json", doc)
    raw = (OUT / "records.json").read_bytes()
    index = {"source": "derived: tools/collect/e04-jobs-01/lib.py select() over saved repository files",
             "fetchedAt": fetched_at, "coverage": coverage, "errors": errors,
             "columns": ["jobId", "basis", "wallets", "orders"],
             "rows": [[j, expected[j]["basis"], expected[j]["wallets"], expected[j]["orders"]] for j in ids],
             "records": {"path": "sources/e04-jobs-01/records.json",
                         "sha256": hashlib.sha256(raw).hexdigest()}}
    save_json(OUT / "index.json", index)
    retried = sum(1 for row in rows if row[-1] or row[-2])
    report = {
        "target": "genesis-repair-e04/evidence-01",
        "snapshotTime": lib.T, "shard": "1 of 2",
        "counts": {"expected": len(ids), "fetchedDetail": ok_detail, "fetchedResult": ok_result,
                   "unavailable": len(unavailable), "readErrors": len(errors),
                   "readsWithRetries": retried,
                   "fromJobsCsvHolderGap": coverage["basisCounts"]["jobsCsvHolderNoDetail"],
                   "newFromOrders": coverage["basisCounts"]["newFromOrdersOnly"],
                   "createdAtOrAfterT": len(coverage["createdAtOrAfterT"]),
                   "unresolvedOracleRequests": len(unresolved)},
        "stateAtReadCounts": {},
        "templates": {},
        "expectedIdsSha256": coverage["expectedIdsSha256"],
        "files": {"sources/e04-jobs-01/records.json": hashlib.sha256(raw).hexdigest()},
        "readWindow": [min(x for row in rows for x in (row[6], row[13]) if x), fetched_at],
        "unavailableJobs": unavailable,
        "failures": errors,
        "note": "status/state/result observations are read-time (readWindow), not historical at T; "
                "selection is deterministic from saved inputs (coverage.inputsSha256)",
        "commands": ["python3 -B tools/collect/e04-jobs-01/make_fixtures.py",
                     "python3 -B tools/collect/e04-jobs-01/collect.py --max-seconds 550  (x4, resumable)",
                     "python3 -B tools/collect/e04-jobs-01/collect.py --finalize",
                     "python3 -B tools/collect/e04-jobs-01/check.py"],
    }
    for row in rows:
        report["stateAtReadCounts"][str(row[19])] = report["stateAtReadCounts"].get(str(row[19]), 0) + 1
        report["templates"][str(row[18])] = report["templates"].get(str(row[18]), 0) + 1
    report["files"]["sources/e04-jobs-01/index.json"] = hashlib.sha256((OUT / "index.json").read_bytes()).hexdigest()
    save_json(Path(__file__).resolve().parent / "report.json", report)
    print("expected", len(ids), "detail200", ok_detail, "result200", ok_result,
          "unavailable", len(unavailable), "errors", len(errors))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=float, default=540)
    ap.add_argument("--finalize", action="store_true")
    a = ap.parse_args()
    if a.finalize:
        finalize()
    else:
        collect(a.max_seconds)
