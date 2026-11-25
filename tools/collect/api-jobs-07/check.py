#!/usr/bin/env python3
"""Verify that the local shard has exactly one data row per indexed job."""
from collections import Counter
import json
import re
import sys

sys.dont_write_bytecode = True
from collect import INDEX_COLUMNS, JOB_COLUMNS, OUTPUT, T, instant


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(filename, columns):
    path = OUTPUT / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    require(set(data) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"},
            f"{filename}: unexpected source envelope")
    require(data["columns"] == columns, f"{filename}: unexpected columns")
    require(data["coverage"]["before"] == T, f"{filename}: wrong cutoff")
    require(data["coverage"]["idPrefixes"] == ["e", "f"], f"{filename}: wrong shard")
    require(isinstance(data["rows"], list), f"{filename}: rows must be an array")
    seen = Counter()
    for row in data["rows"]:
        require(isinstance(row, list) and len(row) == len(columns),
                f"{filename}: row does not match columns")
        job_id = row[0]
        require(isinstance(job_id, str) and job_id.startswith(("e", "f")),
                f"{filename}: invalid shard ID {job_id!r}")
        seen[job_id] += 1
    duplicates = sorted(job_id for job_id, count in seen.items() if count > 1)
    require(not duplicates, f"{filename}: duplicate IDs {duplicates}")
    return data, {row[0]: dict(zip(columns, row)) for row in data["rows"]}


def check():
    index, indexed = read("index.json", INDEX_COLUMNS)
    jobs, collected = read("jobs.json", JOB_COLUMNS)
    coverage = index["coverage"]
    require(coverage["complete"] is True, "index is incomplete")
    require(coverage["wholeListCount"] >= len(indexed), "index exceeds whole-list count")
    require(re.fullmatch("[0-9a-f]{64}", coverage["wholeListSha256"]),
            "invalid whole-list SHA-256")
    missing, extra = indexed.keys() - collected.keys(), collected.keys() - indexed.keys()
    require(not missing and not extra, f"ID mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    require(jobs["coverage"]["collected"] == jobs["coverage"]["indexed"] == len(indexed),
            "incorrect jobs coverage counts")
    for job_id, row in indexed.items():
        require(instant(row["createdAt"]) < instant(T), f"{job_id}: outside cutoff")
        for column in INDEX_COLUMNS[1:]:
            require(collected[job_id][column] == row[column], f"{job_id}: {column} differs from index")
        payer = collected[job_id]["paidBy"]
        require(payer is None or re.fullmatch("0x[0-9a-f]{40}", payer),
                f"{job_id}: invalid payer address")
        require(payer is not None or (collected[job_id]["error"] or {}).get("unlinked", {}).get("paidBy"),
                f"{job_id}: missing reason for unlinked payer")
    print(f"OK: {len(indexed)} indexed jobs, exactly one row each; "
          f"{coverage['wholeListCount']} whole-list IDs; "
          f"{len(jobs['errors'])} recorded endpoint failures")


if __name__ == "__main__":
    try:
        check()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.exit(f"FAIL: {exc}")
