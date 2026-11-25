#!/usr/bin/env python3
"""Collect the pre-snapshot e/f job shard using public IMD GET endpoints."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE = "https://api.imd.fun"
T = "2026-11-25T11:00:00Z"
READ_INTERVAL = 1  # At most 60 request starts per minute.
OUTPUT = Path(__file__).resolve().parents[3] / "sources/api-jobs-07"
INDEX_COLUMNS = "id createdAt template".split()
JOB_COLUMNS = """
id state template createdAt updatedAt objective paidBy parentJobId projectId
projectVersions workflowId oracleRequestId via launchKind launchStatus launchChainId
launchId siteUrl blockedReason deliveryRepoUrl deliveryCommit deliveredAt mediaFiles
nodes nodeRoles nodesAccepted revisions judgeRevisions seats reviewsSent reviewValues
resultComplete resultFiles resultFileNames resultSourceNodes fetchedAt error
""".split()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


class Reader:
    def __init__(self):
        self.next_read = 0

    def get(self, path, validate):
        failures = []
        for attempt in range(1, 4):
            time.sleep(max(0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + READ_INTERVAL
            delay = attempt
            try:
                request = Request(BASE + path, headers={"Accept": "application/json"})
                with urlopen(request, timeout=25) as response:
                    data = json.load(response)
                validate(data)
                return data, None
            except HTTPError as exc:
                failures.append(f"HTTP {exc.code}")
                retry_after = exc.headers.get("Retry-After")
                if exc.code == 429 and retry_after:
                    try:
                        delay = max(0, int(retry_after))
                    except ValueError:
                        try:
                            delay = max(0, (parsedate_to_datetime(retry_after) -
                                            datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                exc.close()
            except (URLError, OSError, ValueError, TypeError, KeyError) as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
            self.next_read = max(self.next_read, time.monotonic() + delay)
        return None, {"path": path, "attempts": failures}


def validate_page(page):
    if (not isinstance(page, dict) or not isinstance(page.get("jobs"), list)
            or page.get("count") != len(page["jobs"]) or len(page["jobs"]) > 500):
        raise ValueError("invalid listing envelope")
    for job in page["jobs"]:
        if not isinstance(job["id"], str) or not job["id"]:
            raise ValueError("invalid job ID")
        if not isinstance(job["createdAt"], str) or instant(job["createdAt"]).utcoffset() is None:
            raise ValueError("invalid job creation time")


def build_index(reader):
    checkpoint = OUTPUT / ".listing.json"
    progress = (json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists()
                else {"before": T, "jobs": {}, "pages": 0, "errors": []})
    while True:
        before = progress["before"]
        path = "/jobs?" + urlencode({"exclude": "oracle", "limit": 500, "before": before})
        page, error = reader.get(path, validate_page)
        if error:
            progress["errors"].append(error)
            save(checkpoint, progress)
            raise RuntimeError(f"listing failed at {before}: {error}")
        jobs = page["jobs"]
        for job in jobs:
            if instant(job["createdAt"]) < instant(T):
                progress["jobs"][job["id"]] = [job.get(column) for column in INDEX_COLUMNS]
        progress["pages"] += 1
        print(f"List page {progress['pages']}: {len(jobs)} rows, "
              f"{len(progress['jobs'])} unique before cutoff", flush=True)
        if len(jobs) < 500:
            break
        oldest = min(instant(job["createdAt"]) for job in jobs)
        cursor = oldest + timedelta(milliseconds=1)
        if cursor >= instant(before):
            raise RuntimeError(f"cursor cannot advance at {before}; timestamp tie may exceed page size")
        progress["before"] = cursor.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        save(checkpoint, progress)

    ids = sorted(progress["jobs"])
    rows = [progress["jobs"][job_id] for job_id in ids if job_id[0] in "ef"]
    index = {
        "source": BASE + "/jobs?exclude=oracle&limit=500",
        "fetchedAt": now(),
        "coverage": {
            "before": T,
            "idPrefixes": ["e", "f"],
            "pages": progress["pages"],
            "wholeListCount": len(ids),
            "wholeListSha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
            "wholeListHashFormat": "Sorted unique job IDs joined with LF (no trailing LF), UTF-8",
            "complete": True,
        },
        "errors": progress["errors"],
        "columns": INDEX_COLUMNS,
        "rows": rows,
    }
    save(OUTPUT / "index.json", index)
    checkpoint.unlink(missing_ok=True)
    return index


def count(values):
    return len(values) if isinstance(values, list) else None


def total(nodes, field):
    if nodes is not None and all(type(node.get(field)) is int for node in nodes):
        return sum(node[field] for node in nodes)
    return None


def collection(data, field):
    value = data.get(field)
    return value if isinstance(value, list) else None


def job_row(reader, indexed):
    job_id, created_at, template = indexed

    def validate_id(data, field):
        if not isinstance(data, dict) or data.get(field) != job_id:
            raise ValueError("response job ID does not match request")

    path = "/jobs/" + quote(job_id, safe="")
    detail, detail_error = reader.get(path, lambda data: validate_id(data, "id"))
    result, result_error = reader.get(path + "/result",
                                      lambda data: validate_id(data, "jobId"))
    job, result = detail or {}, result or {}
    project, workflow = job.get("project") or {}, job.get("workflow") or {}
    launch, delivery = job.get("launch") or {}, job.get("delivery") or {}
    media = delivery.get("media") or job.get("media") or {}
    nodes, reviews = collection(job, "nodes"), collection(job, "reviews")
    versions = collection(project, "versions")
    files = collection(result, "files")
    reasons = {}
    payer = job.get("paidBy")
    if payer is None:
        reasons["paidBy"] = ("Job detail unavailable" if detail_error else
                             "Job detail names no payer")
    objective = job.get("objective")
    row = {
        "id": job_id,
        "createdAt": created_at,
        "template": template,
        "objective": objective[:300] if isinstance(objective, str) else None,
        "paidBy": payer.lower() if isinstance(payer, str) else None,
        "projectId": project.get("id"),
        "projectVersions": [v.get("jobId") for v in versions] if versions is not None else None,
        "workflowId": workflow.get("id"),
        "launchKind": launch.get("kind"),
        "launchStatus": launch.get("status"),
        "launchChainId": launch.get("chainId"),
        "launchId": launch.get("id"),
        "siteUrl": (job.get("site") or {}).get("url"),
        "deliveryRepoUrl": delivery.get("repoUrl"),
        "deliveryCommit": delivery.get("commit"),
        "deliveredAt": delivery.get("deliveredAt"),
        "mediaFiles": count(media.get("files")),
        "nodes": count(nodes),
        "nodeRoles": sorted(Counter(node["role"] for node in nodes).items())
        if nodes is not None and all(isinstance(node.get("role"), str) for node in nodes) else None,
        "nodesAccepted": sum(node["state"] == "accepted" for node in nodes)
        if nodes is not None and all(isinstance(node.get("state"), str) for node in nodes) else None,
        "revisions": total(nodes, "revisions"),
        "judgeRevisions": total(nodes, "judgeRevisions"),
        "seats": sorted({seat["tokenId"] for node in nodes
                         if (seat := node.get("seat")) and seat.get("tokenId") is not None},
                        key=str) if nodes is not None else None,
        "reviewsSent": sum(review["status"] == "sent" for review in reviews)
        if reviews is not None and all(isinstance(r.get("status"), str) for r in reviews) else None,
        "reviewValues": [entry.get("value") for review in reviews for entry in review["entries"]]
        if reviews is not None and all(isinstance(r.get("entries"), list) for r in reviews) else None,
        "resultComplete": result.get("complete"),
        "resultFiles": count(files),
        "resultFileNames": [entry.get("name") for entry in files] if files is not None else None,
        "resultSourceNodes": count(result.get("source")),
        "fetchedAt": now(),
    }
    for field in ("state", "updatedAt", "parentJobId", "oracleRequestId", "via", "blockedReason"):
        row[field] = job.get(field)
    failures = [error for error in (detail_error, result_error) if error]
    row["error"] = {"reads": failures, "unlinked": reasons} if failures or reasons else None
    return [row[column] for column in JOB_COLUMNS]


def collect(retry_errors):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    reader = Reader()
    index_path = OUTPUT / "index.json"
    index = (json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists()
             else build_index(reader))
    jobs_path = OUTPUT / "jobs.json"
    rows = {}
    carried_errors = []
    if jobs_path.exists():
        previous = json.loads(jobs_path.read_text(encoding="utf-8"))
        if previous["columns"] != JOB_COLUMNS:
            raise RuntimeError("existing jobs file has different columns")
        rows = {row[0]: row for row in previous["rows"]}
        carried_errors = previous["errors"]
    wanted = {row[0] for row in index["rows"]}
    if rows.keys() - wanted:
        raise RuntimeError("existing jobs file has IDs outside the index")
    print(f"Shard index: {len(wanted)} jobs; {len(rows)} saved rows", flush=True)
    errors_by_path = {error["path"]: error for error in carried_errors}

    def save_rows(fetched_at):
        save(jobs_path, {
            "source": [BASE + "/jobs/<id>", BASE + "/jobs/<id>/result"],
            "fetchedAt": fetched_at,
            "coverage": {
                "before": T, "idPrefixes": ["e", "f"], "indexed": len(wanted),
                "collected": len(rows),
                "observedState": "Live detail/result reads for jobs created before cutoff",
                "nulls": "Unavailable or inapplicable in the cited response; terminal read failures are in error",
                "reviewsSent": "Count of review batches with status sent",
                "unlinked": "Only detail.paidBy is used; missing payers have a row-level reason",
            },
            "errors": list(errors_by_path.values()),
            "columns": JOB_COLUMNS,
            "rows": [rows[key] for key in sorted(rows)],
        })

    if not jobs_path.exists():
        save_rows(now())
    for position, indexed in enumerate(index["rows"], 1):
        job_id = indexed[0]
        existing = rows.get(job_id)
        if existing is not None and not (retry_errors and (existing[-1] or {}).get("reads")):
            continue
        row = job_row(reader, indexed)
        rows[job_id] = row
        for error in (row[-1] or {}).get("reads", []):
            errors_by_path[error["path"]] = error
        save_rows(row[-2])
        if position % 25 == 0 or position == len(wanted):
            print(f"Saved {len(rows)}/{len(wanted)} rows; "
                  f"{len(errors_by_path)} endpoint failures recorded", flush=True)
    print(f"Finished {len(rows)} jobs; {len(errors_by_path)} endpoint failures recorded", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-errors", action="store_true",
                        help="refetch rows with exhausted endpoint reads")
    args = parser.parse_args()
    try:
        collect(args.retry_errors)
    except (RuntimeError, KeyboardInterrupt) as exc:
        sys.exit(str(exc) or "Interrupted; run again to continue")
