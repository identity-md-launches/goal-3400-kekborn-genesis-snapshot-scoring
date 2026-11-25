#!/usr/bin/env python3
"""Verify shard 00's saved index and job rows without network access."""

from collections import Counter
import json
import re
import sys

sys.dont_write_bytecode = True
from collect import INDEX_COLUMNS, JOB_COLUMNS, OUTPUT, SNAPSHOT, in_shard, timestamp


def load(name, columns):
    with (OUTPUT / name).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if data["columns"] != columns or columns[0] != "id":
        raise ValueError(f"{name}: unexpected columns")
    rows = data["rows"]
    if not isinstance(rows, list):
        raise ValueError(f"{name}: rows must be an array")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"{name}: invalid row width at row {number}")
        job_id = row[0]
        if not isinstance(job_id, str) or not in_shard(job_id):
            raise ValueError(f"{name}: invalid shard ID at row {number}: {job_id!r}")
    return data


def check():
    index = load("index.json", INDEX_COLUMNS)
    jobs = load("jobs.json", JOB_COLUMNS)
    indexed = Counter(row[0] for row in index["rows"])
    collected = Counter(row[0] for row in jobs["rows"])
    problems = []
    for name, counts in (("index.json", indexed), ("jobs.json", collected)):
        duplicates = sorted(job_id for job_id, count in counts.items() if count != 1)
        if duplicates:
            problems.append(f"{name}: {len(duplicates)} duplicate IDs, first five {duplicates[:5]}")
    for label, ids in (("missing", indexed.keys() - collected.keys()),
                       ("unindexed", collected.keys() - indexed.keys())):
        if ids:
            problems.append(f"jobs.json: {len(ids)} {label} IDs, first five {sorted(ids)[:5]}")
    if problems:
        raise ValueError("\n".join(problems))

    cutoff = timestamp(SNAPSHOT)
    for job_id, created_at, _ in index["rows"]:
        if timestamp(created_at) >= cutoff:
            raise ValueError(f"index.json: job {job_id} was created at or after the cutoff")
    index_coverage = index["coverage"]
    if index_coverage["createdAtBefore"] != SNAPSHOT:
        raise ValueError("index.json: unexpected snapshot time")
    for field in ("createdAtBefore", "allJobsCount", "allJobsIdsSha256", "idPrefixes", "pages"):
        if index_coverage[field] != jobs["coverage"][field]:
            raise ValueError(f"source coverage differs for {field}")
    if index_coverage["allJobsCount"] < len(indexed):
        raise ValueError("index.json: whole-list count is below the shard count")
    if not re.fullmatch(r"[0-9a-f]{64}", index_coverage["allJobsIdsSha256"]):
        raise ValueError("index.json: invalid whole-list ID digest")
    print(f"OK: {len(indexed)} indexed IDs; exactly one job row per ID; "
          f"{len(jobs['errors'])} recorded endpoint failures.")


if __name__ == "__main__":
    try:
        check()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Check failed: {exc}", file=sys.stderr)
        sys.exit(1)
