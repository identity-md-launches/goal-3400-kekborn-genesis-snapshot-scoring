#!/usr/bin/env python3
"""Collect the pre-snapshot IMD job index and shard 00 using public GETs."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "sources/api-jobs-00"
API = "https://api.imd.fun"
SNAPSHOT = "2026-11-25T11:00:00Z"
LIMIT = 500
INDEX_COLUMNS = "id createdAt template".split()
JOB_COLUMNS = """
id state template createdAt updatedAt objective paidBy parentJobId projectId
projectVersions workflowId oracleRequestId via launchKind launchStatus launchChainId
launchId siteUrl blockedReason deliveryRepoUrl deliveryCommit deliveredAt mediaFiles
nodes nodeRoles nodesAccepted revisions judgeRevisions seats reviewsSent reviewValues
resultComplete resultFiles resultFileNames resultSourceNodes fetchedAt error
""".split()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def now():
    return iso(datetime.now(timezone.utc))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def digest(ids):
    return sha256("\n".join(sorted(ids)).encode()).hexdigest()


def in_shard(job_id):
    return job_id[:1] in ("0", "1")


def retry_delay(header):
    if not header:
        return 60
    try:
        return max(0, int(header))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(header) -
                           datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


class Reader:
    def __init__(self):
        self.last_start = 0

    def get(self, path, validate):
        for attempt in range(1, 4):
            time.sleep(max(0, self.last_start + 0.75 - time.monotonic()))
            self.last_start = time.monotonic()
            delay = attempt
            try:
                with urlopen(API + path, timeout=30) as response:
                    payload = json.load(response)
                validate(payload)
                return payload, None
            except HTTPError as exc:
                problem = f"HTTP {exc.code}"
                if exc.code == 429:
                    delay = retry_delay(exc.headers.get("Retry-After"))
                exc.close()
            except (OSError, HTTPException, ValueError, TypeError, KeyError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
            if attempt < 3 or problem == "HTTP 429":
                time.sleep(delay)
        error = {"path": path, "attempts": 3, "reason": problem, "fetchedAt": now()}
        print(f"{path}: {problem} after three attempts", flush=True)
        return None, error


def validate_page(payload):
    jobs = payload["jobs"]
    if not isinstance(jobs, list) or len(jobs) != payload["count"] or len(jobs) > LIMIT:
        raise ValueError("invalid page count")
    for job in jobs:
        if not isinstance(job["id"], str) or not job["id"]:
            raise ValueError("missing job ID")
        if timestamp(job["createdAt"]).tzinfo is None:
            raise ValueError("creation time has no timezone")


def collect_index(reader, checkpoint):
    path = checkpoint / "index.json"
    state = (json.loads(path.read_text()) if path.exists() else
             {"before": SNAPSHOT, "entries": {}, "pages": 0, "complete": False})
    errors = state.setdefault("errors", [])
    entries = state["entries"]
    while not state["complete"]:
        before = state["before"]
        query = urlencode({"exclude": "oracle", "limit": LIMIT, "before": before})
        payload, error = reader.get("/jobs?" + query, validate_page)
        if error:
            errors.append(error)
            save(path, state)
            raise RuntimeError("index incomplete; rerun to retry the failed page")
        jobs = payload["jobs"]
        for job in jobs:
            if timestamp(job["createdAt"]) < timestamp(SNAPSHOT):
                entries.setdefault(job["id"], [job["createdAt"], job.get("template")])
        if len(jobs) < LIMIT:
            state["complete"] = True
        else:
            oldest = min(timestamp(job["createdAt"]) for job in jobs)
            next_before = oldest + timedelta(milliseconds=1)
            if next_before >= timestamp(before):
                raise RuntimeError("full page made no cursor progress; timestamp coverage is uncertain")
            state["before"] = iso(next_before)
        state["pages"] += 1
        state["fetchedAt"] = now()
        save(path, state)
        print(f"Index page {state['pages']}: {len(jobs)} records, {len(entries)} unique so far",
              flush=True)
    return state


def object_value(value):
    return value if isinstance(value, dict) else {}


def mapped(items, key):
    if isinstance(items, list):
        return [object_value(item).get(key) for item in items]
    return None


def count(items):
    return len(items) if isinstance(items, list) else None


def total(items, key):
    values = mapped(items, key)
    if values is not None and all(type(value) is int for value in values):
        return sum(values)
    return None


def assemble(job_id, detail, result, fetched_at, errors):
    d = object_value(detail)
    r = object_value(result)
    project = object_value(d.get("project"))
    launch = object_value(d.get("launch"))
    detail_delivery = object_value(d.get("delivery"))
    delivery = object_value(r.get("delivery")).copy()
    delivery.update({
        key: value for key, value in detail_delivery.items() if value is not None
    })
    media = detail_delivery.get("media") or d.get("media")
    nodes = d.get("nodes")
    roles = mapped(nodes, "role")
    states = mapped(nodes, "state")
    node_seats = mapped(nodes, "seat")
    seat_ids = (sorted({str(seat["tokenId"]) for seat in node_seats
                        if isinstance(seat, dict) and seat.get("tokenId") is not None})
                if node_seats is not None else None)
    reviews = d.get("reviews")
    statuses = mapped(reviews, "status")
    entries = mapped(reviews, "entries")
    review_values = None
    if entries is not None and all(isinstance(group, list) for group in entries):
        review_values = [object_value(entry).get("value") for group in entries for entry in group]
    objective = d.get("objective")
    payer = d.get("paidBy")
    fields = {
        "id": job_id,
        **{key: d.get(key) for key in
           ("state", "template", "createdAt", "updatedAt", "parentJobId",
            "oracleRequestId", "via", "blockedReason")},
        "objective": objective[:300] if isinstance(objective, str) else None,
        "paidBy": payer.lower() if isinstance(payer, str) and payer else None,
        "projectId": project.get("id"),
        "projectVersions": mapped(project.get("versions"), "jobId"),
        "workflowId": object_value(d.get("workflow")).get("id"),
        "launchKind": launch.get("kind"),
        "launchStatus": launch.get("status"),
        "launchChainId": launch.get("chainId"),
        "launchId": launch.get("id"),
        "siteUrl": object_value(d.get("site")).get("url"),
        "deliveryRepoUrl": delivery.get("repoUrl"),
        "deliveryCommit": delivery.get("commit"),
        "deliveredAt": delivery.get("deliveredAt"),
        "mediaFiles": count(object_value(media).get("files")),
        "nodes": count(nodes),
        "nodeRoles": dict(sorted(Counter(roles).items()))
        if roles is not None and all(isinstance(role, str) for role in roles) else None,
        "nodesAccepted": states.count("accepted") if states is not None and None not in states else None,
        "revisions": total(nodes, "revisions"),
        "judgeRevisions": total(nodes, "judgeRevisions"),
        "seats": seat_ids,
        "reviewsSent": statuses.count("sent") if statuses is not None and None not in statuses else None,
        "reviewValues": review_values,
        "resultComplete": r.get("complete"),
        "resultFiles": count(r.get("files")),
        "resultFileNames": mapped(r.get("files"), "name"),
        "resultSourceNodes": count(r.get("source")),
        "fetchedAt": fetched_at,
        "error": "; ".join(f"{err['path']}: {err['reason']} after {err['attempts']} attempts"
                           for err in errors) if errors else None,
    }
    return [fields[key] for key in JOB_COLUMNS]


def require_id(payload, key, expected):
    if not isinstance(payload, dict) or payload.get(key) != expected:
        raise ValueError("response ID differs from requested ID")


def cached_read(reader, path, url, key, job_id):
    if path.exists():
        return json.loads(path.read_text())
    payload, error = reader.get(url, lambda data: require_id(data, key, job_id))
    record = {"payload": payload, "error": error, "fetchedAt": now()}
    save(path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path,
                        default=ROOT / "test/scratch/jobs-00/checkpoint")
    args = parser.parse_args()
    reader = Reader()
    state = collect_index(reader, args.checkpoint)
    entries = state["entries"]
    ids = sorted(job_id for job_id in entries if in_shard(job_id))
    coverage = {
        "createdAtBefore": SNAPSHOT,
        "exclude": "oracle",
        "idPrefixes": ["0", "1"],
        "allJobsCount": len(entries),
        "allJobsIdsSha256": digest(entries),
        "idHashEncoding": "Sorted IDs joined with LF, no trailing LF, UTF-8",
        "pages": state["pages"],
    }
    save(OUTPUT / "index.json", {
        "source": API + "/jobs?exclude=oracle&limit=500&before=<time>",
        "fetchedAt": state["fetchedAt"], "coverage": coverage, "errors": state.get("errors", []),
        "columns": INDEX_COLUMNS, "rows": [[job_id, *entries[job_id]] for job_id in ids],
    })
    rows, errors = [], []
    fetched_at = state["fetchedAt"]
    for number, job_id in enumerate(ids, 1):
        encoded = quote(job_id, safe="")
        endpoint = "/jobs/" + encoded
        detail = cached_read(reader, args.checkpoint / "detail" / (encoded + ".json"),
                             endpoint, "id", job_id)
        result = cached_read(reader, args.checkpoint / "result" / (encoded + ".json"),
                             endpoint + "/result", "jobId", job_id)
        job_errors = [item["error"] for item in (detail, result) if item["error"]]
        row_time = max(detail["fetchedAt"], result["fetchedAt"])
        fetched_at = max(fetched_at, row_time)
        rows.append(assemble(job_id, detail["payload"], result["payload"], row_time, job_errors))
        errors.extend(job_errors)
        if number % 50 == 0 or number == len(ids):
            print(f"Job pairs {number}/{len(ids)}; endpoint failures {len(errors)}", flush=True)
    save(OUTPUT / "jobs.json", {
        "source": [API + "/jobs/<id>", API + "/jobs/<id>/result"],
        "fetchedAt": fetched_at,
        "coverage": {**coverage, "stateTiming": "Live API state at fetchedAt",
                     "nulls": "API omitted or returned null for a field, or a required endpoint failed",
                     "unlinkedPayer": "When detail was read, null paidBy means API named no payer",
                     "delivery": "Detail takes precedence; result may name a repository before delivery",
                     "reviewsSent": "Count of review batches with status sent",
                     "resultSourceNodes": "Count of result source entries"},
        "errors": errors, "columns": JOB_COLUMNS, "rows": rows,
    })
    print(f"Wrote {len(rows)} indexed jobs from {len(entries)} unique jobs; "
          f"whole-list ID SHA-256 {coverage['allJobsIdsSha256']}", flush=True)


if __name__ == "__main__":
    main()
