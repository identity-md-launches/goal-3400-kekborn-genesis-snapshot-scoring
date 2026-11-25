#!/usr/bin/env python3
"""Collect the public IMD catalogs using only GET requests and the standard library."""

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE = "https://api.imd.fun"
CUTOFF = "2026-11-25T11:00:00Z"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "sources/api-catalog"
LIMIT = 500
JOB_COLUMNS = ["id", "state", "template", "createdAt", "updatedAt", "launchKind",
               "projectId", "projectVersion"]
GOAL_COLUMNS = ["jobId", "state", "orchestrated", "workers", "itemsTotal",
                "itemsAccepted", "itemsFailed", "writeup", "judge", "createdAt", "children"]
CHILD_COLUMNS = ["key", "action", "jobId", "state"]
WORKFLOW_COLUMNS = ["id", "status", "contractsJobId", "frontendJobId"]
ORACLE_COLUMNS = ["id", "status", "jobId", "answerType", "chainId", "createdAt", "attestedAt"]
SEAT_COLUMNS = ["tokenId", "agentId", "attempts", "accepted", "rejected", "failed",
                "pending", "lastWorkedAt"]
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
MISSING = object()
NULL_POLICY = {
    "apiNull": "The API explicitly returned null; it supplied no further value or cause.",
    "omitted": "The API did not supply this field; no value is inferred.",
    "unavailable": "The endpoint or parent object was unavailable; see errors.",
}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now():
    return iso(datetime.now(timezone.utc))


def ids_sha256(ids):
    # UTF-8 sorted IDs, separated by LF, with NO trailing LF.
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def normalize(value):
    """Lowercase address-valued strings without altering arbitrary text or hex hashes."""
    if isinstance(value, str) and ADDRESS.fullmatch(value):
        return value.lower()
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def wait_for(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining > 30:
            print("Waiting %.1fs for Retry-After/backoff" % remaining, flush=True)
        time.sleep(min(30, max(0, remaining)))


def retry_after(value, current=None):
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - (current or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class Client:
    def __init__(self, reads_per_minute=60, attempts=5, timeout=45,
                 opener=urlopen, sleeper=wait_for, clock=time.monotonic):
        if not 0 < reads_per_minute < 90:
            raise ValueError("reads-per-minute must be positive and below 90")
        if attempts < 1 or timeout <= 0:
            raise ValueError("attempts and timeout must be positive")
        self.interval = 60.0 / reads_per_minute
        self.attempts = attempts
        self.timeout = timeout
        self.opener, self.sleeper, self.clock = opener, sleeper, clock
        self.next_read = 0.0
        self.reads = []

    def get(self, path, errors):
        url = BASE + path
        for attempt in range(1, self.attempts + 1):
            self.sleeper(max(0, self.next_read - self.clock()))
            self.next_read = self.clock() + self.interval
            read = {"url": url, "startedAt": now(), "attempt": attempt}
            delay = None
            retryable = False
            try:
                request = Request(url, headers={"Accept": "application/json",
                                  "User-Agent": "Kekborn-Genesis-catalog/1.0"}, method="GET")
                with self.opener(request, timeout=self.timeout) as response:
                    body = response.read()
                    read.update(status=response.status, responseSha256=hashlib.sha256(body).hexdigest(),
                                bytes=len(body))
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("expected a JSON object")
                read["fetchedAt"] = now()
                self.reads.append(read)
                return value, read
            except HTTPError as exc:
                body = exc.read()
                read.update(status=exc.code, error="http_error",
                            responseSha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
                if exc.headers.get("Retry-After") is not None:
                    read["retryAfter"] = exc.headers.get("Retry-After")
                    delay = retry_after(read["retryAfter"])
                retryable = exc.code in (408, 425, 429) or 500 <= exc.code < 600
                exc.close()
            except (URLError, TimeoutError, OSError) as exc:
                # Do not include machine/proxy configuration from exception messages.
                read.update(status=None, error=type(exc).__name__)
                retryable = True
            except (ValueError, UnicodeError):
                read["error"] = "invalid_json_object"
                retryable = True
            read["fetchedAt"] = now()
            self.reads.append(read)
            if not retryable or attempt == self.attempts:
                errors.append({"kind": "read_failed", "url": url, "at": read["fetchedAt"],
                               "status": read.get("status"), "attempts": attempt,
                               "reason": read["error"], "fatal": True})
                print("FAILED GET %s: %s (%s attempts)" % (path, read["error"], attempt), flush=True)
                return None, read
            delay = delay if delay is not None else min(30, 2 ** attempt)
            read["retryDelaySeconds"] = delay
            print("Retry GET %s after %.1fs (%s)" % (path, delay, read["error"]), flush=True)
            self.sleeper(delay)
        raise AssertionError("unreachable")


def envelope(source, selection):
    return {"source": BASE + source, "fetchedAt": None,
            "coverage": {"cutoffExclusive": CUTOFF, "selection": selection,
                         "stateAsOfSnapshot": False,
                         "stateSemantics": "States and mutable fields are observed at read time, not reconstructed at T.",
                         "nullPolicy": NULL_POLICY, "nulls": {}, "complete": False},
            "errors": [], "columns": [], "rows": []}


def error(doc, kind, reason, **extra):
    doc["errors"].append({"kind": kind, "reason": reason, "fatal": True, **extra})


def field(doc, obj, key, column=None, unavailable=False):
    value = obj.get(key, MISSING) if isinstance(obj, dict) else MISSING
    if value is MISSING or value is None:
        why = "unavailable" if unavailable else "omitted" if value is MISSING else "apiNull"
        counts = doc["coverage"]["nulls"].setdefault(column or key, {})
        counts[why] = counts.get(why, 0) + 1
        return None
    return normalize(value)


def finish(doc, client, start):
    reads = client.reads[start:]
    doc["coverage"]["reads"] = reads
    doc["coverage"]["readStartedAt"] = reads[0]["startedAt"] if reads else None
    doc["fetchedAt"] = reads[-1]["fetchedAt"] if reads else now()
    doc["coverage"]["readFinishedAt"] = doc["fetchedAt"]
    doc["coverage"]["rowCount"] = len(doc["rows"])
    doc["coverage"]["complete"] = not any(e.get("fatal") for e in doc["errors"])
    return doc


def paginate(client, endpoint, list_key):
    """Overlap the oldest millisecond; never advance past a saturated timestamp."""
    start = len(client.reads)
    doc = envelope(endpoint, "All API-visible rows with createdAt strictly before T; no state or template filter.")
    coverage = doc["coverage"]
    coverage.update(limit=LIMIT, countMeaning="API count is this page's size, never the overall total.",
                    pagination="before = oldest createdAt on the preceding page + 1 millisecond; deduplicate by id",
                    pages=[], termination=None, excludedAtOrAfterCutoff=[], changedDuplicates=[])
    before = timestamp(CUTOFF)
    selected = {}
    # A guard against a broken endpoint; never present hitting this as exhaustion.
    for page_number in range(1, 10001):
        path = endpoint + "?" + urlencode({"limit": LIMIT, "before": iso(before)})
        data, read = client.get(path, doc["errors"])
        if data is None:
            coverage["termination"] = "read_failed"
            break
        page = data.get(list_key)
        if not isinstance(page, list):
            error(doc, "invalid_page", "Response has no array named " + list_key, url=read["url"])
            coverage["termination"] = "invalid_page"
            break
        meta = {"page": page_number, "url": read["url"], "fetchedAt": read["fetchedAt"],
                "before": iso(before), "count": data.get("count"), "size": len(page),
                "newIds": 0, "duplicates": 0}
        coverage["pages"].append(meta)
        extra = {k: normalize(v) for k, v in data.items() if k not in (list_key, "count")}
        if extra:
            meta["responseMetadata"] = extra
        if type(data.get("count")) is not int or data["count"] != len(page) or len(page) > LIMIT:
            error(doc, "page_count_mismatch", "API count or limit disagrees with the returned array", url=read["url"])
        if not page:
            coverage["termination"] = "empty_page"
            break
        times = []
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                error(doc, "invalid_row", "A page entry has no nonempty string id", url=read["url"])
                continue
            try:
                created = timestamp(item.get("createdAt"))
            except (ValueError, OverflowError):
                error(doc, "invalid_created_at", "Cannot establish this row's cutoff or cursor", id=item["id"], url=read["url"])
                continue
            times.append(created)
            if created >= before:
                error(doc, "cursor_ignored", "A row is not strictly before the requested cursor", id=item["id"], url=read["url"])
            if created >= timestamp(CUTOFF):
                coverage["excludedAtOrAfterCutoff"].append(item["id"])
                continue
            if item["id"] in selected:
                meta["duplicates"] += 1
                if item != selected[item["id"]]:
                    coverage["changedDuplicates"].append({"id": item["id"], "page": page_number,
                                                          "policy": "Keep the first observation."})
            else:
                selected[item["id"]] = item
                meta["newIds"] += 1
        if not times:
            error(doc, "missing_cursor", "No usable createdAt in a nonempty page", url=read["url"])
            coverage["termination"] = "missing_cursor"
            break
        if times != sorted(times, reverse=True):
            error(doc, "page_not_ordered", "Rows are not ordered by descending createdAt", url=read["url"])
        oldest = min(times)
        following = oldest + timedelta(milliseconds=1)
        meta.update(oldestCreatedAt=iso(oldest), newestCreatedAt=iso(max(times)), nextBefore=iso(following))
        print("GET %s page=%d size=%d new=%d total=%d" %
              (endpoint, page_number, len(page), meta["newIds"], len(selected)), flush=True)
        if meta["newIds"] == 0:
            if len(page) < LIMIT:
                coverage["termination"] = "overlap_only_short_page"
            else:
                error(doc, "timestamp_saturation", "A full page repeats without progress; time-only pagination cannot prove complete coverage.", url=read["url"])
                coverage["termination"] = "timestamp_saturation"
            break
        if following >= before:
            if len(page) < LIMIT:
                coverage["termination"] = "short_page_exhausted"
            else:
                error(doc, "cursor_stalled", "Full page cannot move the overlapping time cursor backwards", url=read["url"])
                coverage["termination"] = "cursor_stalled"
            break
        before = following
    else:
        error(doc, "page_limit", "Page safety limit reached; collection is incomplete")
        coverage["termination"] = "page_limit"
    coverage["total"] = len(selected)
    coverage["sortedIdsSha256"] = ids_sha256(selected)
    coverage["sortedIdsEncoding"] = "sha256(UTF-8 of lexicographically sorted IDs joined with LF; no trailing LF)"
    coverage["rowCreatedAt"] = [[ident, item["createdAt"]] for ident, item in sorted(selected.items())]
    finish(doc, client, start)
    return doc, [selected[ident] for ident in sorted(selected)]


def collect_jobs(client):
    doc, items = paginate(client, "/jobs", "jobs")
    doc["columns"] = JOB_COLUMNS
    doc["coverage"]["oraclePanelJobsIncluded"] = True
    doc["coverage"]["projectMapping"] = "projectId = project.id and projectVersion = project.version; null project stays null."
    for item in items:
        row = [field(doc, item, col) for col in JOB_COLUMNS[:6]]
        project = item.get("project", MISSING)
        if isinstance(project, dict):
            row += [field(doc, project, "id", "projectId"), field(doc, project, "version", "projectVersion")]
        else:
            parent = {"id": None, "version": None} if project is None else {}
            row += [field(doc, parent, "id", "projectId"), field(doc, parent, "version", "projectVersion")]
            if project is not None and project is not MISSING:
                error(doc, "invalid_project", "Non-null project is not an object", id=item["id"])
        doc["rows"].append(row)
    doc["coverage"]["rowCount"] = len(doc["rows"])
    doc["coverage"]["complete"] = not doc["errors"]
    return doc


def absent_references(doc, job_ids, columns):
    absent = []
    for row in doc["rows"]:
        item = dict(zip(doc["columns"], row))
        for col in columns:
            ident = item.get(col)
            if ident is not None and ident not in job_ids:
                absent.append({"id": item["id"], "field": col, "jobId": ident,
                               "reason": "Not returned by the pre-cutoff /jobs index. The API lists alone do not establish why; no link inferred."})
    doc["coverage"]["absentJobIds"] = absent


def collect_list(client, endpoint, list_key, columns, job_ids, references):
    doc, items = paginate(client, endpoint, list_key)
    doc["columns"] = columns
    doc["rows"] = [[field(doc, item, col) for col in columns] for item in items]
    doc["coverage"]["rowCount"] = len(doc["rows"])
    absent_references(doc, job_ids, references)
    return doc


def collect_goals(client, jobs):
    start = len(client.reads)
    ids = sorted(row[0] for row in jobs["rows"] if row[2] == "goal")
    doc = envelope("/goals/{jobId}", "Exactly the indexed jobs whose template equals goal; both detail and children endpoints read.")
    doc["columns"] = GOAL_COLUMNS
    doc["coverage"].update(expectedGoalCount=len(ids), expectedGoalIdsSha256=ids_sha256(ids),
                           childrenSource=BASE + "/goals/{jobId}/children", childrenColumns=CHILD_COLUMNS,
                           childJobIdMapping="child.jobId when non-null, otherwise child.job.id when supplied; no inferred link. State is child.state, not child.job.state.",
                           additionalChildFields=[], absentChildJobIds=[])
    indexed = {r[0] for r in jobs["rows"]}
    for number, ident in enumerate(ids, 1):
        path = "/goals/" + quote(ident, safe="")
        detail, _ = client.get(path, doc["errors"])
        child_data, _ = client.get(path + "/children", doc["errors"])
        for label, body in (("detail", detail), ("children", child_data)):
            if body is not None and body.get("jobId") != ident:
                error(doc, "goal_id_mismatch", "Response jobId differs from requested goal", jobId=ident, endpoint=label)
                if label == "detail":
                    detail = None
                else:
                    child_data = None
        # The row's jobId is sourced from /jobs even if its detail cannot be read.
        row = [ident] + [field(doc, detail, col, unavailable=detail is None)
                         for col in ("state", "orchestrated", "workers")]
        items = detail.get("items") if detail is not None else None
        for key, col in (("total", "itemsTotal"), ("accepted", "itemsAccepted"), ("failed", "itemsFailed")):
            row.append(field(doc, items, key, col, unavailable=detail is None))
        row += [field(doc, detail, col, unavailable=detail is None) for col in ("writeup", "judge", "createdAt")]
        children = None
        if child_data is not None:
            values = child_data.get("children")
            if not isinstance(values, list):
                error(doc, "invalid_children", "No children array in successful response", jobId=ident)
            else:
                children = []
                for child in values:
                    if not isinstance(child, dict):
                        error(doc, "invalid_child", "Child is not an object", jobId=ident)
                        continue
                    nested_job = child.get("job")
                    direct_job = child.get("jobId")
                    if isinstance(nested_job, dict) and nested_job.get("id") is not None:
                        if direct_job is not None and direct_job != nested_job["id"]:
                            error(doc, "conflicting_child_job", "child.jobId differs from child.job.id", jobId=ident, key=child.get("key"))
                        job_value = direct_job if direct_job is not None else nested_job["id"]
                        projected = {**child, "jobId": job_value}
                    else:
                        projected = child
                    children.append([field(doc, projected, col, "children." + col) for col in CHILD_COLUMNS])
                    extras = {k: normalize(v) for k, v in child.items() if k not in CHILD_COLUMNS}
                    if extras:
                        doc["coverage"]["additionalChildFields"].append({"jobId": ident, "key": child.get("key"), "fields": extras})
                    child_job = children[-1][2]
                    if child_job is not None and child_job not in indexed:
                        doc["coverage"]["absentChildJobIds"].append({"jobId": child_job, "parentJobId": ident, "key": child.get("key"),
                            "reason": "Child endpoint returned this reference, but it is absent from the pre-cutoff /jobs index; cause not established."})
        if children is None:
            field(doc, None, "children", unavailable=True)
        row.append(children)
        doc["rows"].append(row)
        print("Goal %d/%d %s children=%s" % (number, len(ids), ident, len(children) if children is not None else "null"), flush=True)
    return finish(doc, client, start)


def collect_single(client, kind):
    endpoint = {"seats-owners": "/seats/owners", "seats-records": "/seats/records",
                "capabilities": "/requests/capabilities"}[kind]
    start = len(client.reads)
    doc = envelope(endpoint, "Current API response at read time, not historical state at T; no cutoff filter available.")
    data, read = client.get(endpoint, doc["errors"])
    doc["coverage"].update(observedAt=read["fetchedAt"], historicalAtCutoff=False)
    if kind == "seats-owners":
        doc["columns"] = ["tokenId", "owner"]
        doc["coverage"]["tokenIdMapping"] = "Zero-based index in API owners array, per https://imd.fun/docs/llms.txt."
        if data is not None:
            doc["coverage"]["responseMetadata"] = normalize({k: v for k, v in data.items() if k != "owners"})
            doc["coverage"]["at"] = data.get("at")
            if not isinstance(data.get("owners"), list):
                error(doc, "invalid_owners", "Response has no owners array")
            else:
                for index, owner in enumerate(data["owners"]):
                    if owner is not None and (not isinstance(owner, str) or not ADDRESS.fullmatch(owner)):
                        error(doc, "invalid_owner", "Owner is not an address or null", tokenId=str(index))
                    doc["rows"].append([str(index), field(doc, {"owner": owner}, "owner")])
    elif kind == "seats-records":
        doc["columns"] = SEAT_COLUMNS.copy()
        doc["coverage"]["cacheNote"] = "The documented API cache can be up to 10 minutes old; observedAt is the retrieval time."
        if data is not None:
            doc["coverage"]["responseMetadata"] = normalize({k: v for k, v in data.items() if k != "seats"})
            records = data.get("seats")
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                error(doc, "invalid_records", "Response has no array of seat objects")
            else:
                # Keep source order, all fields, and all values (except lowercase addresses).
                extras = sorted(set().union(*(item.keys() for item in records)) - set(SEAT_COLUMNS)) if records else []
                doc["columns"] += extras
                doc["rows"] = [[field(doc, item, col) for col in doc["columns"]] for item in records]
    else:
        doc["columns"] = ["response"]
        doc["coverage"]["encoding"] = "One row with the entire API JSON object, without dropping fields; address-valued strings lowercased."
        if data is not None:
            doc["rows"] = [[normalize(data)]]
    if kind != "capabilities" and data is not None and isinstance(data.get("count"), int):
        if data["count"] != len(doc["rows"]):
            error(doc, "count_mismatch", "API count differs from saved rows")
    return finish(doc, client, start)


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    print("SAVED %s rows=%d sha256=%s errors=%d" %
          (path.name, len(doc["rows"]), hashlib.sha256(payload).hexdigest(), len(doc["errors"])), flush=True)


def verify_snapshot(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("snapshotTime") != CUTOFF or
            data.get("chains", {}).get("ethereum", {}).get("block") != 26491756 or
            data.get("chains", {}).get("robinhood", {}).get("block") != 122989778):
        raise ValueError("snapshot.json does not match the immutable batch snapshot")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--reads-per-minute", type=float, default=60,
                        help="Per-process read pace, must be below 90; default 60. Reduce when sharing an IP.")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args(argv)
    verify_snapshot(ROOT / "snapshot.json")
    client = Client(args.reads_per_minute, args.attempts, args.timeout)
    docs = {}

    def save(name, doc):
        docs[name] = doc
        write_json(args.output_dir / (name + ".json"), doc)

    save("job-index", collect_jobs(client))
    job_ids = {row[0] for row in docs["job-index"]["rows"]}
    save("workflows", collect_list(client, "/workflows", "workflows", WORKFLOW_COLUMNS,
                                   job_ids, ["contractsJobId", "frontendJobId"]))
    save("oracle-requests", collect_list(client, "/oracle/requests", "requests", ORACLE_COLUMNS,
                                         job_ids, ["jobId"]))
    save("goals", collect_goals(client, docs["job-index"]))
    for name in ("seats-owners", "seats-records", "capabilities"):
        save(name, collect_single(client, name))
    failures = sum(len(doc["errors"]) for doc in docs.values())
    print("COLLECTION files=%d reads=%d failedChecks=%d" % (len(docs), len(client.reads), failures), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
