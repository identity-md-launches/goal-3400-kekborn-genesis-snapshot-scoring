"""Verify the saved jobs-05 index and its matching detail rows offline."""

import re
import sys
from collections import Counter

sys.dont_write_bytecode = True

from collect import INDEX_COLUMNS, JOB_COLUMNS, OUT, SNAPSHOT, read_json, timestamp


def load(name, columns):
    document = read_json(OUT / name)
    required = {"source", "fetchedAt", "coverage", "errors", "columns", "rows"}
    if set(document) != required or document["columns"] != columns:
        raise ValueError(f"{name}: unexpected source schema")
    if not isinstance(document["rows"], list) or any(
        not isinstance(row, list) or len(row) != len(columns) for row in document["rows"]
    ):
        raise ValueError(f"{name}: unexpected row shape")
    if document["coverage"]["snapshot"] != SNAPSHOT:
        raise ValueError(f"{name}: unexpected snapshot")
    return document


def verify():
    index = load("index.json", INDEX_COLUMNS)
    jobs = load("jobs.json", JOB_COLUMNS)
    indexed = Counter(row[0] for row in index["rows"])
    collected = Counter(row[0] for row in jobs["rows"])
    if any(count != 1 for count in indexed.values()):
        raise ValueError("index.json: duplicate job ID")
    if indexed != collected:
        missing = sum((indexed - collected).values())
        extra = sum((collected - indexed).values())
        raise ValueError(f"jobs.json: missing {missing}, extra or duplicated {extra}")
    if any(
        not re.fullmatch(r"[ab][0-9a-f-]*", job_id) or timestamp(created_at) >= timestamp(SNAPSHOT)
        for job_id, created_at, _ in index["rows"]
    ):
        raise ValueError("index.json: job outside shard or snapshot")
    created = {row[0]: row[1] for row in index["rows"]}
    if any(row[3] != created[row[0]] for row in jobs["rows"]):
        raise ValueError("jobs.json: creation time disagrees with index")
    coverage = index["coverage"]
    if coverage["listCount"] < len(indexed) or not re.fullmatch(r"[a-f0-9]{64}", coverage["sortedIdsSha256"]):
        raise ValueError("index.json: invalid whole-list metadata")
    if jobs["errors"] != [error for row in jobs["rows"] for error in (row[-1] or [])]:
        raise ValueError("jobs.json: row errors disagree with source errors")
    print(f"PASS: {len(indexed)} indexed jobs, exactly one row each; "
          f"{len(index['errors'])} listing errors, {len(jobs['errors'])} detail/result errors")


if __name__ == "__main__":
    try:
        verify()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
