#!/usr/bin/env python3
"""Collect fixed-cutoff IMD jobs beginning 8/9, using GET only and Python stdlib.

Run from any directory; data lives at <repo>/sources/api-jobs-04. Successful
reads and finished pagination are reused. --retry-errors retries exhausted reads.
No response text is executed. No transaction, signing or payment routes are used.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import http.client
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.imd.fun"
CUTOFF = "2026-11-25T11:00:00Z"
PREFIXES = "89"
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources" / "api-jobs-04"
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
COLUMNS = [
    "id", "state", "template", "createdAt", "updatedAt", "objective", "paidBy",
    "parentJobId", "projectId", "projectVersions", "workflowId", "oracleRequestId",
    "via", "launchKind", "launchStatus", "launchChainId", "launchId", "siteUrl",
    "blockedReason", "deliveryRepoUrl", "deliveryCommit", "deliveredAt", "mediaFiles",
    "nodes", "nodeRoles", "nodesAccepted", "revisions", "judgeRevisions", "seats",
    "reviewsSent", "reviewValues", "resultComplete", "resultFiles", "resultFileNames",
    "resultSourceNodes", "fetchedAt", "error",
]
RAW_COLUMNS = ["id", "fetchedAt", "httpStatus", "payload", "error", "attempts"]
PAGE_COLUMNS = ["before", "fetchedAt", "httpStatus", "count", "jobs", "error", "attempts"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def lower_addresses(value):
    if isinstance(value, str):
        return value.lower() if ADDRESS.fullmatch(value) else value
    if isinstance(value, list):
        return [lower_addresses(v) for v in value]
    if isinstance(value, dict):
        return {k: lower_addresses(v) for k, v in value.items()}
    return value


def ids_hash(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def source(url, columns, rows, coverage=None, errors=None, fetched_at=None):
    if fetched_at is None and "fetchedAt" in columns:
        position = columns.index("fetchedAt")
        times = [row[position] for row in rows if row[position]]
        fetched_at = max(times) if times else None
    return {"source": url, "fetchedAt": fetched_at or now(), "coverage": coverage or {},
            "errors": errors or [], "columns": columns, "rows": rows}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class Client:
    def __init__(self, interval=1.1):
        if interval < 0.7:
            raise ValueError("interval must be >=0.7 seconds (strictly under 90 reads/minute)")
        self.interval = interval
        self.last = None

    @staticmethod
    def wait(seconds):
        # Small waits let an operator inspect progress without a long blocking call.
        while seconds > 0:
            part = min(seconds, 30)
            time.sleep(part)
            seconds -= part

    def get(self, url, validate):
        attempts = []
        status = None
        for attempt in range(1, 4):
            if self.last is not None:
                self.wait(max(0, self.last + self.interval - time.monotonic()))
            self.last = time.monotonic()
            at = now()
            retry_after = None
            status = None
            try:
                request = urllib.request.Request(url, headers={
                    "Accept": "application/json", "User-Agent": "IMD-Genesis-jobs-04/1.0"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    status = response.status
                    payload = json.load(response)
                validate(payload)
                attempts.append({"at": at, "httpStatus": status, "error": None})
                return {"fetchedAt": now(), "httpStatus": status,
                        "payload": lower_addresses(payload), "error": None, "attempts": attempts}
            except urllib.error.HTTPError as exc:
                status = exc.code
                message = "HTTP " + str(exc.code) + " " + str(exc.reason)
                header = exc.headers.get("Retry-After")
                if header:
                    try:
                        retry_after = max(0.0, float(header))
                    except ValueError:
                        try:
                            retry_after = max(0.0, (parsedate_to_datetime(header)
                                                   - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                if status == 429:
                    self.interval = min(10.0, self.interval * 1.5)
            except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as exc:
                message = type(exc).__name__ + ": " + str(exc)
            attempts.append({"at": at, "httpStatus": status, "error": message,
                             "retryAfterSeconds": retry_after})
            print(json.dumps({"readFailed": url, "attempt": attempt, "error": message,
                              "retryAfterSeconds": retry_after}), flush=True)
            if attempt < 3:
                self.wait(max(retry_after or 0, min(2 ** attempt, 10)))
            elif status == 429:
                # Apply Retry-After even when the next read is a different endpoint.
                self.last = time.monotonic() + (retry_after or 0)
        return {"fetchedAt": now(), "httpStatus": status, "payload": None,
                "error": message, "attempts": attempts}


def validate_page(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("list response is not an object with a jobs array")
    if payload.get("count") != len(payload["jobs"]):
        raise ValueError("page count is not the jobs array size")
    for job in payload["jobs"]:
        if not isinstance(job.get("id"), str) or not isinstance(job.get("createdAt"), str):
            raise ValueError("list entry lacks id/createdAt")
        timestamp(job["createdAt"])


def validate_job(payload, job_id, result=False):
    key = "jobId" if result else "id"
    if not isinstance(payload, dict) or payload.get(key) != job_id:
        raise ValueError("response does not name requested " + key)


def page_records(document):
    seen = {}
    for row in document["rows"]:
        entry = dict(zip(PAGE_COLUMNS, row))
        if entry["error"] is None:
            for job in entry["jobs"]:
                if timestamp(job["createdAt"]) < timestamp(CUTOFF):
                    seen.setdefault(job["id"], job)
    return seen


def collect_pages(client, retry_errors=False):
    path = OUT / "pages.json"
    doc = read(path) or source(API + "/jobs", PAGE_COLUMNS, [], {
        "snapshotTime": CUTOFF, "exclude": "oracle", "limit": 500,
        "complete": False, "nextBefore": CUTOFF,
        "pagination": "before = oldest createdAt of preceding page + 1ms; deduplicate by id"})
    if doc["coverage"].get("complete"):
        return doc
    if doc["errors"] and not retry_errors:
        raise RuntimeError("listing has exhausted read; use --retry-errors to resume")
    doc["errors"] = []
    seen = page_records(doc)
    before = doc["coverage"]["nextBefore"]
    while True:
        url = API + "/jobs?" + urllib.parse.urlencode({
            "exclude": "oracle", "limit": 500, "before": before})
        response = client.get(url, validate_page)
        payload = response["payload"] or {}
        doc["rows"].append([before, response["fetchedAt"], response["httpStatus"],
                            payload.get("count"), payload.get("jobs"), response["error"],
                            response["attempts"]])
        doc["fetchedAt"] = now()
        if response["error"]:
            doc["errors"].append({"url": url, "error": response["error"], "tries": 3})
            write(path, doc)
            raise RuntimeError("list read exhausted; incomplete coverage saved")
        jobs = payload["jobs"]
        additions = 0
        for job in jobs:
            if timestamp(job["createdAt"]) >= timestamp(CUTOFF):
                continue
            if job["id"] not in seen:
                additions += 1
                seen[job["id"]] = job
        print(json.dumps({"page": len(doc["rows"]), "count": len(jobs),
                          "newIds": additions, "uniqueListed": len(seen)}), flush=True)
        doc["coverage"]["uniqueListedJobs"] = len(seen)
        if not jobs:
            doc["coverage"].update(complete=True, nextBefore=None, termination="empty page")
            write(path, doc)
            return doc
        next_before = stamp(min(timestamp(j["createdAt"]) for j in jobs) + timedelta(milliseconds=1))
        if timestamp(next_before) >= timestamp(before):
            if len(jobs) < 500:
                doc["coverage"].update(complete=True, nextBefore=None,
                                       termination="short terminal page repeats only oldest timestamp")
                write(path, doc)
                return doc
            doc["errors"].append({"url": url, "error": "pagination stalled on a full timestamp boundary; completeness unknown"})
            write(path, doc)
            raise RuntimeError("pagination stalled: cannot safely skip equal timestamps")
        before = next_before
        doc["coverage"]["nextBefore"] = before
        write(path, doc)


def raw_map(filename):
    doc = read(OUT / filename)
    if not doc:
        return {}
    return {row[0]: dict(zip(RAW_COLUMNS, row)) for row in doc["rows"]}


def save_raw(filename, records, result=False):
    errors = [{"id": key, "error": value["error"]} for key, value in sorted(records.items())
              if value["error"]]
    doc = source(API + ("/jobs/{id}/result" if result else "/jobs/{id}"), RAW_COLUMNS,
                 [[entry.get(c) for c in RAW_COLUMNS] for _, entry in sorted(records.items())],
                 {"snapshotTime": CUTOFF, "idPrefixes": list(PREFIXES),
                  "rateLimit": "GET only; minimum spacing 1.1s by default; >=0.7s enforced; three tries; 429 honors Retry-After",
                  "note": "Values are live at fetchedAt, not a historical state at cutoff."}, errors)
    write(OUT / filename, doc)


def extract(indexed, detail, result):
    values = dict.fromkeys(COLUMNS)
    values.update(id=indexed["id"], createdAt=indexed.get("createdAt"), template=indexed.get("template"))
    missing = {}
    failures = []

    def missing_value(field, reason):
        missing[field] = reason
        return None

    def get(obj, key, field=None):
        if not isinstance(obj, dict) or obj.get(key) is None:
            return missing_value(field or key, "API field is absent or null: " + key)
        return obj[key]

    def array(obj, key, field=None):
        value = get(obj, key, field)
        if value is not None and not isinstance(value, list):
            return missing_value(field or key, "API field is not an array: " + key)
        return value

    for entry, label in ((detail, "detail"), (result, "result")):
        if not entry:
            failures.append({"endpoint": label, "error": "not fetched"})
        elif entry.get("error"):
            failures.append({"endpoint": label, "error": entry["error"]})
    d = detail.get("payload") if detail and not detail.get("error") else None
    r = result.get("payload") if result and not result.get("error") else None
    if d is not None:
        for field in ("state", "template", "createdAt", "updatedAt", "paidBy", "parentJobId",
                      "oracleRequestId", "via", "blockedReason"):
            values[field] = get(d, field)
        objective = get(d, "objective")
        values["objective"] = objective[:300] if isinstance(objective, str) else None
        project = d.get("project")
        values["projectId"] = get(project, "id", "projectId")
        versions = array(project, "versions", "projectVersions")
        if versions is not None:
            if all(isinstance(v, dict) and isinstance(v.get("jobId"), str) and v.get("createdAt") for v in versions):
                values["projectVersions"] = [v["jobId"] for v in sorted(versions, key=lambda v: timestamp(v["createdAt"]))]
            else:
                missing_value("projectVersions", "project version lacks jobId or createdAt")
        values["workflowId"] = get(d.get("workflow"), "id", "workflowId")
        for field, key in (("launchKind", "kind"), ("launchStatus", "status"),
                           ("launchChainId", "chainId"), ("launchId", "id")):
            values[field] = get(d.get("launch"), key, field)
        values["siteUrl"] = get(d.get("site"), "url", "siteUrl")
        delivery = d.get("delivery")
        for field, key in (("deliveryRepoUrl", "repoUrl"), ("deliveryCommit", "commit"), ("deliveredAt", "deliveredAt")):
            values[field] = get(delivery, key, field)
        media = d.get("media")
        if not isinstance(media, dict):
            media = delivery.get("media") if isinstance(delivery, dict) else None
        files = array(media, "files", "mediaFiles")
        values["mediaFiles"] = len(files) if files is not None else None
        nodes = array(d, "nodes")
        if nodes is not None:
            values["nodes"] = len(nodes)
            if all(isinstance(n, dict) and isinstance(n.get("role"), str) for n in nodes):
                values["nodeRoles"] = [[k, v] for k, v in sorted(Counter(n["role"] for n in nodes).items())]
            else:
                missing_value("nodeRoles", "one or more nodes lack role")
            if all(isinstance(n, dict) and n.get("state") is not None for n in nodes):
                values["nodesAccepted"] = sum(n["state"] == "accepted" for n in nodes)
            else:
                missing_value("nodesAccepted", "one or more nodes lack state")
            for field in ("revisions", "judgeRevisions"):
                if all(isinstance(n, dict) and isinstance(n.get(field), int) and not isinstance(n[field], bool) for n in nodes):
                    values[field] = sum(n[field] for n in nodes)
                else:
                    missing_value(field, "one or more nodes lack an integer " + field)
            seats = []
            for n in nodes:
                seat = n.get("seat") if isinstance(n, dict) else None
                if isinstance(seat, dict) and seat.get("tokenId") is not None:
                    seats.append(str(seat["tokenId"]))
                elif seat is not None:
                    missing_value("seats", "one or more non-null seats lack tokenId")
            if "seats" not in missing:
                values["seats"] = sorted(set(seats))
        reviews = array(d, "reviews")
        if reviews is not None:
            if all(isinstance(review, dict) and review.get("status") is not None for review in reviews):
                values["reviewsSent"] = sum(review["status"] == "sent" for review in reviews)
            else:
                missing_value("reviewsSent", "one or more reviews lack status")
            if all(isinstance(review, dict) and isinstance(review.get("entries"), list) for review in reviews):
                values["reviewValues"] = [entry.get("value") if isinstance(entry, dict) else None
                                          for review in reviews for entry in review["entries"]]
                if any(value is None for value in values["reviewValues"]):
                    missing_value("reviewValues", "one or more review entries lack value; null retained in array")
            else:
                missing_value("reviewValues", "one or more reviews lack entries array")
    else:
        missing["detail"] = "detail unavailable; only id/createdAt/template retained from list"
    if r is not None:
        values["resultComplete"] = get(r, "complete", "resultComplete")
        files = array(r, "files", "resultFiles")
        if files is not None:
            values["resultFiles"] = len(files)
            values["resultFileNames"] = [f.get("name") if isinstance(f, dict) else None for f in files]
            if any(n is None for n in values["resultFileNames"]):
                missing_value("resultFileNames", "one or more files lack name; null retained in array")
        sources = array(r, "source", "resultSourceNodes")
        values["resultSourceNodes"] = len(sources) if sources is not None else None
    else:
        missing["result"] = "result unavailable"
    times = [entry["fetchedAt"] for entry in (detail, result) if entry and entry.get("fetchedAt")]
    values["fetchedAt"] = max(times) if times else None
    values["error"] = failures or None
    return [values[c] for c in COLUMNS], missing


def save_outputs(pages, details, results):
    listed = page_records(pages)
    selected = [entry for key, entry in sorted(listed.items()) if key[0].lower() in PREFIXES]
    coverage = {"snapshotTime": CUTOFF, "createdAtRule": "strictly before snapshotTime",
                "exclude": "oracle", "idPrefixes": list(PREFIXES),
                "wholeListCount": len(listed), "wholeListSortedIdsSha256": ids_hash(listed),
                "sortedIdsHashEncoding": "UTF-8, lexicographically sorted unique ids joined by LF, no trailing LF",
                "listingComplete": pages["coverage"].get("complete", False),
                "shardCount": len(selected), "listingSource": "pages.json"}
    index = source(API + "/jobs?exclude=oracle&limit=500&before=" + CUTOFF,
                   ["id", "createdAt", "template"],
                   [[j["id"], j["createdAt"], j.get("template")] for j in selected],
                   coverage.copy(), pages["errors"], fetched_at=pages["fetchedAt"])
    write(OUT / "index.json", index)
    rows, missing, errors = [], {}, []
    for j in selected:
        row, reasons = extract(j, details.get(j["id"]), results.get(j["id"]))
        rows.append(row)
        if reasons:
            missing[j["id"]] = reasons
        if row[-1]:
            errors.append({"id": j["id"], "reads": row[-1]})
    coverage.update({"detailsRecorded": len(details), "resultsRecorded": len(results),
                     "allReadsAttempted": all(j["id"] in details and j["id"] in results for j in selected),
                     "nullReasonsByJob": missing,
                     "payerRule": "paidBy is exclusively the detail API's explicit paidBy; never inferred",
                     "observedState": "Live at endpoint fetchedAt, not reconstructed at snapshotTime",
                     "fieldSemantics": {
                         "nodeRoles": "sorted [role,count] pairs",
                         "nodesAccepted": "nodes whose state is accepted",
                         "seats": "distinct non-null nodes[].seat.tokenId strings; null seat denotes no named assignment",
                         "reviewsSent": "number of review batches whose status is sent",
                         "reviewValues": "every reviews[].entries[].value, retaining order and nulls",
                         "mediaFiles": "count of detail.media.files, or delivery.media.files when detail.media is not an object; null when API supplies no array",
                         "resultSourceNodes": "number of entries in result.source",
                         "fetchedAt": "latest detail/result read completion time; individual times in details/results.json",
                         "error": "null or endpoint read failures; missing/null field explanations are in nullReasonsByJob"}})
    write(OUT / "jobs.json", source([API + "/jobs/{id}", API + "/jobs/{id}/result"],
                                    COLUMNS, rows, coverage, errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.1, help="minimum seconds between GET starts (>=0.7)")
    parser.add_argument("--retry-errors", action="store_true", help="retry previously exhausted reads, retaining attempt history")
    parser.add_argument("--max-jobs", type=int, help="process at most this many indexed jobs in this run")
    parser.add_argument("--rebuild-only", action="store_true", help="rebuild tables offline from saved pages/details/results")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    client = Client(args.interval)
    pages = read(OUT / "pages.json") if args.rebuild_only else collect_pages(client, args.retry_errors)
    if pages is None:
        raise RuntimeError("no saved pages")
    details, results = raw_map("details.json"), raw_map("results.json")
    save_outputs(pages, details, results)
    selected = [j for key, j in sorted(page_records(pages).items()) if key[0].lower() in PREFIXES]
    if not args.rebuild_only:
        processed = 0
        for position, job in enumerate(selected, 1):
            if args.max_jobs is not None and processed >= args.max_jobs:
                break
            changed = False
            for records, filename, is_result in ((details, "details.json", False), (results, "results.json", True)):
                job_id = job["id"]
                previous = records.get(job_id)
                if previous and (not previous["error"] or not args.retry_errors):
                    continue
                url = API + "/jobs/" + urllib.parse.quote(job_id, safe="") + ("/result" if is_result else "")
                response = client.get(url, lambda payload: validate_job(payload, job_id, is_result))
                response["id"] = job_id
                if previous:
                    response["attempts"] = previous["attempts"] + response["attempts"]
                records[job_id] = response
                save_raw(filename, records, is_result)
                changed = True
            if changed:
                processed += 1
                save_outputs(pages, details, results)
                print(json.dumps({"processed": position, "shardJobs": len(selected), "id": job["id"],
                                  "detailError": details[job["id"]]["error"],
                                  "resultError": results[job["id"]]["error"]}), flush=True)
    save_outputs(pages, details, results)
    print(json.dumps({"status": "saved", "listed": len(page_records(pages)), "shardJobs": len(selected),
                      "detailsRecorded": len(details), "resultsRecorded": len(results),
                      "wholeListSortedIdsSha256": ids_hash(page_records(pages))}), flush=True)


if __name__ == "__main__":
    try:
        # The source inode is shared across process namespaces. Locking it avoids
        # a separate lock/cache file and prevents concurrent checkpoint writers.
        with Path(__file__).open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another jobs-04 collector/rebuild holds the lock")
            main()
    except (RuntimeError, ValueError, OSError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
