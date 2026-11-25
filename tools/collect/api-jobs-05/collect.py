"""Collect the public IMD job-detail shard for the fixed Genesis snapshot."""

import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "sources/api-jobs-05"
API = "https://api.imd.fun"
SNAPSHOT = "2026-11-25T11:00:00Z"
LIMIT = 500
INDEX_COLUMNS = "id createdAt template".split()
JOB_COLUMNS = (
    "id state template createdAt updatedAt objective paidBy parentJobId projectId "
    "projectVersions workflowId oracleRequestId via launchKind launchStatus launchChainId "
    "launchId siteUrl blockedReason deliveryRepoUrl deliveryCommit deliveredAt mediaFiles "
    "nodes nodeRoles nodesAccepted revisions judgeRevisions seats reviewsSent reviewValues "
    "resultComplete resultFiles resultFileNames resultSourceNodes fetchedAt error"
).split()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc(value=None):
    return (value or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest_ids(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(name, document):
    path = OUT / name
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(path)


def source(url, coverage, errors, columns, rows):
    return dict(source=url, fetchedAt=utc(), coverage=coverage, errors=errors, columns=columns, rows=rows)


class ReadError(Exception):
    pass


class Client:
    next_read = 0.0

    def get(self, path, validate):
        url = API + path
        errors = []
        for attempt in range(3):
            time.sleep(max(0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + 0.80
            response = subprocess.run(
                ["curl", "-sS", "-A", "Mozilla/5.0", "--max-time", "45",
                 "-D", "-", "-w", "\n%{http_code}", url],
                capture_output=True,
            )
            delay = 3
            try:
                if response.returncode:
                    raise ValueError(response.stderr.decode(errors="replace").strip())
                payload, code = response.stdout.rsplit(b"\n", 1)
                headers = {}
                while payload.startswith(b"HTTP/"):
                    header, payload = payload.split(b"\r\n\r\n", 1)
                    headers = dict(line.decode("latin1").lower().split(":", 1) for line in header.splitlines()[1:] if b":" in line)
                if code == b"429" and "retry-after" in headers:
                    raw = headers["retry-after"].strip()
                    try:
                        delay = max(0, int(raw))
                    except ValueError:
                        try:
                            delay = max(0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError):
                            pass
                if code != b"200":
                    raise ValueError(f"HTTP {code.decode()} ({payload[:120].decode(errors='replace').strip()})")
                document = json.loads(payload)
                if not validate(document):
                    raise ValueError("response failed schema or identity validation")
                return document
            except (ValueError, TypeError, AttributeError) as error:
                errors.append(str(error))
            if attempt < 2:
                time.sleep(delay)
        raise ReadError(dict(url=url, at=utc(), attempts=errors))


def list_page(document, before):
    rows = document.get("jobs")
    return (isinstance(rows, list) and document.get("count") == len(rows) <= LIMIT
            and all(re.fullmatch(r"[0-9a-f-]+", row.get("id", ""))
                    and timestamp(row["createdAt"]) < timestamp(before) for row in rows))


def index_jobs(client):
    before = SNAPSHOT
    jobs = {}
    errors = []
    while True:
        query = urlencode(dict(exclude="oracle", limit=LIMIT, before=before))
        try:
            page = client.get("/jobs?" + query, lambda doc: list_page(doc, before))["jobs"]
        except ReadError as error:
            errors.append(error.args[0])
            break
        for job in page:
            jobs.setdefault(job["id"], job)
        print(f"List before {before}: {len(page)} rows, {len(jobs)} unique", flush=True)
        if not page:
            break
        oldest = min(timestamp(job["createdAt"]) for job in page)
        next_before = utc(oldest + timedelta(milliseconds=1))
        if timestamp(next_before) >= timestamp(before):
            if len(page) == LIMIT or before == SNAPSHOT:
                errors.append(dict(url=API + "/jobs?" + query, at=utc(), reason="Inclusive overlap cannot advance safely"))
            break
        before = next_before
    rows = [[job.get(key) for key in INDEX_COLUMNS] for job in sorted(jobs.values(), key=lambda job: job["id"])
            if job["id"].startswith(("a", "b")) and timestamp(job["createdAt"]) < timestamp(SNAPSHOT)]
    coverage = dict(snapshot=SNAPSHOT, exclude="oracle", prefixes=["a", "b"], complete=not errors,
                    listCount=len(jobs), sortedIdsSha256=digest_ids(jobs), digestEncoding="UTF-8 sorted IDs joined by LF, no trailing LF")
    return source(API + "/jobs", coverage, errors, INDEX_COLUMNS, rows)


def nested(obj, key):
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def array(obj, key):
    value = obj.get(key)
    return value if isinstance(value, list) else None


def size(values):
    return len(values) if values is not None else None


def total(nodes, field):
    if nodes is not None and all(type(node.get(field)) is int for node in nodes):
        return sum(node[field] for node in nodes)
    return None


def extract(indexed, detail, result, errors):
    job_id, created_at, template = indexed
    project = nested(detail, "project")
    workflow = nested(detail, "workflow")
    launch = nested(detail, "launch")
    delivery = nested(detail, "delivery") or nested(result, "delivery")
    media = nested(delivery, "media") or nested(detail, "media")
    versions = array(project, "versions")
    nodes = array(detail, "nodes")
    reviews = array(detail, "reviews")
    files = array(result, "files")
    objective = detail.get("objective")
    payer = detail.get("paidBy")
    values = {field: detail.get(field) for field in JOB_COLUMNS}
    values.update(
        id=job_id, createdAt=created_at, template=template if not detail else detail.get("template"),
        objective=objective[:300] if isinstance(objective, str) else None,
        paidBy=payer.lower() if isinstance(payer, str) else None,
        projectId=project.get("id"),
        projectVersions=[version.get("jobId") for version in versions] if versions is not None else None,
        workflowId=workflow.get("id", workflow.get("workflowId", detail.get("workflowId"))),
        launchKind=launch.get("kind"), launchStatus=launch.get("status"),
        launchChainId=launch.get("chainId"), launchId=launch.get("id"),
        siteUrl=nested(detail, "site").get("url"), deliveryRepoUrl=delivery.get("repoUrl"),
        deliveryCommit=delivery.get("commit"), deliveredAt=delivery.get("deliveredAt"),
        mediaFiles=size(array(media, "files")), nodes=size(nodes),
        nodeRoles=dict(sorted(Counter(node["role"] for node in nodes).items()))
        if nodes is not None and all(isinstance(node.get("role"), str) for node in nodes) else None,
        nodesAccepted=sum(node.get("state") == "accepted" for node in nodes)
        if nodes is not None and all(isinstance(node.get("state"), str) for node in nodes) else None,
        revisions=total(nodes, "revisions"), judgeRevisions=total(nodes, "judgeRevisions"),
        seats=list(dict.fromkeys(nested(node, "seat").get("tokenId") for node in nodes if nested(node, "seat").get("tokenId") is not None))
        if nodes is not None and all("seat" in node and (node["seat"] is None or nested(node, "seat").get("tokenId") is not None) for node in nodes) else None,
        reviewsSent=sum(review.get("status") == "sent" for review in reviews)
        if reviews is not None and all(isinstance(review.get("status"), str) for review in reviews) else None,
        reviewValues=[entry.get("value") for review in reviews for entry in review["entries"]]
        if reviews is not None and all(isinstance(review.get("entries"), list) for review in reviews) else None,
        resultComplete=result.get("complete"), resultFiles=size(files),
        resultFileNames=[entry.get("name") for entry in files] if files is not None else None,
        resultSourceNodes=size(array(result, "source")), fetchedAt=utc(), error=errors or None,
    )
    return [values[key] for key in JOB_COLUMNS]


def collect():
    OUT.mkdir(parents=True, exist_ok=True)
    client = Client()
    index_path = OUT / "index.json"
    index = read_json(index_path) if index_path.exists() else None
    if index is None or not index["coverage"]["complete"]:
        index = index_jobs(client)
        save("index.json", index)
    job_path = OUT / "jobs.json"
    old = read_json(job_path) if job_path.exists() else None
    previous = {row[0]: row for row in old["rows"]} if old and old["columns"] == JOB_COLUMNS else {}
    rows = {row[0]: previous[row[0]] for row in index["rows"] if row[0] in previous}

    def checkpoint():
        records = [rows[job_id] for job_id in sorted(rows)]
        errors = [error for row in records for error in (row[-1] or [])]
        coverage = dict(snapshot=SNAPSHOT, prefixes=["a", "b"], indexed=len(index["rows"]),
                        indexComplete=index["coverage"]["complete"], complete=index["coverage"]["complete"] and
                        len(rows) == len(index["rows"]) and not errors,
                        semantics="Current API state for pre-snapshot jobs. Null means the relevant source did not expose a value or the read failed. paidBy is only the explicit detail payer. Seats are seat.tokenId. Media counts require an explicit files array.")
        save("jobs.json", source(API + "/jobs/{id} and /jobs/{id}/result", coverage, errors, JOB_COLUMNS, records))

    for position, indexed in enumerate(index["rows"], 1):
        job_id = indexed[0]
        if job_id in rows and rows[job_id][-1] is None:
            continue
        documents = []
        errors = []
        for suffix, id_field in (("", "id"), ("/result", "jobId")):
            try:
                document = client.get(f"/jobs/{job_id}{suffix}", lambda doc: doc.get(id_field) == job_id)
            except ReadError as error:
                document = {}
                errors.append(dict(jobId=job_id, **error.args[0]))
            documents.append(document)
        rows[job_id] = extract(indexed, *documents, errors)
        checkpoint()
        if position % 25 == 0 or errors:
            print(f"Jobs {position}/{len(index['rows'])}: {len(errors)} failed reads for {job_id}", flush=True)
    checkpoint()
    print(f"Saved {len(rows)} jobs, {sum(len(row[-1] or []) for row in rows.values())} failed detail/result reads, "
          f"{len(index['errors'])} listing errors", flush=True)
    return 0 if index["coverage"]["complete"] and all(row[-1] is None for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(collect())
