#!/usr/bin/env python3
"""Collect the c/d IMD job shard. An interrupted run resumes from its checkpoint."""

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = "https://api.imd.fun"
CUTOFF = "2026-11-25T11:00:00Z"
PREFIXES = "cd"
LIMIT = 500
SOURCE_DIR = Path(__file__).resolve().parents[3] / "sources" / "api-jobs-06"
CHECKPOINT = SOURCE_DIR / ".checkpoint.json"
INDEX_COLUMNS = "id createdAt template".split()
JOB_COLUMNS = (
    "id state template createdAt updatedAt objective paidBy parentJobId projectId "
    "projectVersions workflowId oracleRequestId via launchKind launchStatus launchChainId "
    "launchId siteUrl blockedReason deliveryRepoUrl deliveryCommit deliveredAt mediaFiles "
    "nodes nodeRoles nodesAccepted revisions judgeRevisions seats reviewsSent reviewValues "
    "resultComplete resultFiles resultFileNames resultSourceNodes fetchedAt error"
).split()


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path, document):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    temp.replace(path)


def ids_digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def retry_after(header):
    if header:
        try:
            return max(0, int(header))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 60


class Reader:
    def __init__(self):
        self.next_read = 0

    def get(self, path, validate):
        for attempt in range(1, 4):
            time.sleep(max(0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + 0.8
            try:
                request = Request(API + path, headers={"Accept": "application/json", "User-Agent": "IMD-Genesis-jobs-06/1"})
                with urlopen(request, timeout=30) as response:
                    data = json.load(response)
                validate(data)
                return data, now(), None
            except HTTPError as exc:
                reason = f"HTTP {exc.code}"
                if exc.code == 429:
                    self.next_read = max(self.next_read, time.monotonic() + retry_after(exc.headers.get("Retry-After")))
                exc.close()
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                reason = f"invalid response: {exc}"
            except (URLError, OSError, HTTPException) as exc:
                reason = f"read failed: {exc}"
            if attempt < 3:
                self.next_read = max(self.next_read, time.monotonic() + attempt)
        return None, now(), {"path": path, "attempts": 3, "reason": reason}


def validate_page(data, before):
    jobs = data["jobs"]
    if not isinstance(jobs, list) or data["count"] != len(jobs) or len(jobs) > LIMIT:
        raise ValueError("invalid list envelope")
    for job in jobs:
        if not isinstance(job["id"], str) or not job["id"]:
            raise ValueError("missing job id")
        if instant(job["createdAt"]) >= instant(before):
            raise ValueError("job outside exclusive cursor")


def list_jobs(reader, state):
    ids = state["ids"]
    while not state["listComplete"]:
        before = state["before"]
        path = "/jobs?" + urlencode({"exclude": "oracle", "limit": LIMIT, "before": before})
        data, fetched, error = reader.get(path, lambda value: validate_page(value, before))
        if error:
            state["listErrors"].append(error)
            write_json(CHECKPOINT, state)
            raise SystemExit(f"List incomplete at {before}: {error['reason']}")
        page = data["jobs"]
        for job in page:
            if instant(job["createdAt"]) < instant(CUTOFF):
                ids.setdefault(job["id"], [job[key] if key in job else None for key in INDEX_COLUMNS])
        state["listFetchedAt"] = fetched
        state["pages"] += 1
        if len(page) < LIMIT:
            state["listComplete"] = True
        else:
            oldest = min(instant(job["createdAt"]) for job in page)
            cursor = oldest + timedelta(milliseconds=1)
            if cursor >= instant(before):
                raise SystemExit(f"Cannot traverse a full page at timestamp boundary {before}")
            state["before"] = cursor.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        write_json(CHECKPOINT, state)
        print(f"List page {state['pages']}: {len(page)} records, {len(ids)} unique before cutoff", flush=True)


def obj(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else None


def size(values):
    return len(values) if values is not None else None


def sum_field(items, field):
    if items is not None and all(type(item.get(field)) is int for item in items):
        return sum(item[field] for item in items)
    return None


def detail_values(data):
    values = {key: data.get(key) for key in (
        "state", "updatedAt", "objective", "paidBy", "parentJobId",
        "oracleRequestId", "via", "blockedReason",
    )}
    if isinstance(values["objective"], str):
        values["objective"] = values["objective"][:300]
    payer = values["paidBy"]
    if payer is not None:
        if not isinstance(payer, str) or not re.fullmatch(r"0x[0-9a-f]{40}", payer, flags=re.IGNORECASE):
            raise ValueError("invalid paidBy address")
        values["paidBy"] = payer.lower()

    project = obj(data.get("project"))
    launch = obj(data.get("launch"))
    delivery = obj(data.get("delivery"))
    media = obj(delivery.get("media") or data.get("media"))
    versions = array(project.get("versions"))
    nodes = array(data.get("nodes"))
    reviews = array(data.get("reviews"))
    roles = None
    seats = None
    accepted = None
    if nodes is not None:
        roles = dict(sorted(Counter(node["role"] for node in nodes).items())) if all(
            isinstance(node.get("role"), str) for node in nodes) else None
        seat_ids = {obj(node.get("seat")).get("tokenId") for node in nodes}
        seats = sorted(seat_ids - {None}, key=str)
        if all(isinstance(node.get("state"), str) for node in nodes):
            accepted = sum(node["state"] == "accepted" for node in nodes)

    reviews_sent = None
    review_values = None
    if reviews is not None:
        if all(isinstance(review.get("status"), str) for review in reviews):
            reviews_sent = sum(review["status"] == "sent" for review in reviews)
        entries = [array(review.get("entries")) for review in reviews]
        if all(group is not None for group in entries):
            review_values = [entry.get("value") for group in entries for entry in group]

    values.update(
        projectId=project.get("id"),
        projectVersions=None if versions is None else [version.get("jobId") for version in versions],
        workflowId=obj(data.get("workflow")).get("id"),
        launchKind=launch.get("kind"),
        launchStatus=launch.get("status"),
        launchChainId=launch.get("chainId"),
        launchId=launch.get("id"),
        siteUrl=obj(data.get("site")).get("url"),
        deliveryRepoUrl=delivery.get("repoUrl"),
        deliveryCommit=delivery.get("commit"),
        deliveredAt=delivery.get("deliveredAt"),
        mediaFiles=size(array(media.get("files"))),
        nodes=size(nodes),
        nodeRoles=roles,
        nodesAccepted=accepted,
        revisions=sum_field(nodes, "revisions"),
        judgeRevisions=sum_field(nodes, "judgeRevisions"),
        seats=seats,
        reviewsSent=reviews_sent,
        reviewValues=review_values,
    )
    return values


def result_values(data):
    files = array(data.get("files"))
    return {
        "resultComplete": data.get("complete"),
        "resultFiles": size(files),
        "resultFileNames": None if files is None else [file.get("name") for file in files],
        "resultSourceNodes": size(array(data.get("source"))),
    }


def check_id(data, key, expected, project):
    if data[key] != expected:
        raise ValueError("response job id differs from requested id")
    project(data)


def fetch_job(reader, index_row):
    job_id, _, _ = index_row
    base = "/jobs/" + quote(job_id, safe="")
    values = dict(zip(INDEX_COLUMNS, index_row))
    errors = []
    for path, key, project in ((base, "id", detail_values), (base + "/result", "jobId", result_values)):
        data, fetched_at, error = reader.get(
            path, lambda data: check_id(data, key, job_id, project)
        )
        if error:
            errors.append(error)
        else:
            values.update(project(data))
    values.update(fetchedAt=fetched_at, error=errors or None)
    return [values.get(column) for column in JOB_COLUMNS]


def main():
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    if CHECKPOINT.exists():
        state = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    else:
        state = {
            "before": CUTOFF, "listComplete": False, "listErrors": [],
            "pages": 0, "ids": {}, "jobs": {},
        }
    reader = Reader()
    list_jobs(reader, state)
    ids = state["ids"]
    shard_ids = sorted(job_id for job_id in ids if job_id.startswith(tuple(PREFIXES)))
    coverage = {
        "cutoffExclusive": CUTOFF,
        "exclude": "oracle",
        "idPrefixes": list(PREFIXES),
        "wholeListCount": len(ids),
        "wholeListSortedIdsSha256": ids_digest(ids),
        "idDigestEncoding": "UTF-8, lexicographically sorted IDs joined by LF without a trailing LF",
    }
    write_json(SOURCE_DIR / "index.json", {
        "source": API + "/jobs?exclude=oracle&limit=500",
        "fetchedAt": state["listFetchedAt"],
        "coverage": {**coverage, "pages": state["pages"]},
        "errors": state["listErrors"],
        "columns": INDEX_COLUMNS,
        "rows": [ids[job_id] for job_id in shard_ids],
    })
    jobs = state["jobs"]
    print(f"Shard: {len(shard_ids)} jobs, {len(jobs)} already collected", flush=True)
    for position, job_id in enumerate(shard_ids, 1):
        if job_id in jobs:
            continue
        jobs[job_id] = fetch_job(reader, ids[job_id])
        write_json(CHECKPOINT, state)
        if position % 25 == 0 or position == len(shard_ids):
            print(f"Detail/result: {position}/{len(shard_ids)}", flush=True)

    rows = [jobs[job_id] for job_id in shard_ids]
    errors = [error for row in rows for error in (row[-1] or [])]
    write_json(SOURCE_DIR / "jobs.json", {
        "source": API + "/jobs/{id} and /jobs/{id}/result",
        "fetchedAt": max((row[-2] for row in rows), default=state["listFetchedAt"]),
        "coverage": {
            **coverage,
            "fieldTiming": "Current detail/result values at fetch time for jobs created strictly before the cutoff",
            "nullReason": "The API did not supply a value or the endpoint failed; per-row errors identify failed endpoints. A null aggregate can also indicate missing input fields.",
            "payer": "API detail paidBy only",
            "reductions": "nodeRoles maps roles to counts, nodesAccepted counts state=accepted, seats are distinct node seat tokenIds, reviewsSent counts review batches with status=sent, reviewValues includes all review entries",
        },
        "errors": errors,
        "columns": JOB_COLUMNS,
        "rows": rows,
    })
    CHECKPOINT.unlink(missing_ok=True)
    print(f"Wrote {len(rows)} jobs and {len(errors)} failed reads", flush=True)


if __name__ == "__main__":
    main()
