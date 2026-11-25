#!/usr/bin/env python3
"""Check the saved shard without making network requests."""

import argparse
import json
import re
from pathlib import Path

from collect import CUTOFF, INDEX_COLUMNS, JOB_COLUMNS, PREFIXES, SOURCE_DIR, instant


def load_rows(directory, filename, columns):
    document = json.loads((directory / filename).read_text(encoding="utf-8"))
    if document["columns"] != columns or not isinstance(document["rows"], list):
        raise ValueError(f"{filename}: invalid columns or rows")
    rows = {}
    for row in document["rows"]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"{filename}: invalid row width")
        job = dict(zip(columns, row))
        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id.startswith(tuple(PREFIXES)):
            raise ValueError(f"{filename}: job outside shard: {job_id!r}")
        if instant(job["createdAt"]) >= instant(CUTOFF):
            raise ValueError(f"{filename}: job at or after cutoff: {job_id}")
        if job_id in rows:
            raise ValueError(f"{filename}: duplicate job: {job_id}")
        rows[job_id] = job
    return document, rows


def check(directory):
    index, indexed = load_rows(directory, "index.json", INDEX_COLUMNS)
    jobs, records = load_rows(directory, "jobs.json", JOB_COLUMNS)
    if indexed.keys() != records.keys():
        missing = sorted(indexed.keys() - records.keys())
        extra = sorted(records.keys() - indexed.keys())
        raise ValueError(f"Job IDs differ. Missing: {missing}. Extra: {extra}")

    for key in ("wholeListCount", "wholeListSortedIdsSha256", "idDigestEncoding",
                "cutoffExclusive", "exclude", "idPrefixes"):
        if index["coverage"][key] != jobs["coverage"][key]:
            raise ValueError(f"Coverage differs for {key}")
    coverage = index["coverage"]
    if (coverage["cutoffExclusive"] != CUTOFF or coverage["exclude"] != "oracle"
            or coverage["idPrefixes"] != list(PREFIXES)
            or type(coverage["wholeListCount"]) is not int
            or coverage["wholeListCount"] < len(indexed)
            or not re.fullmatch(r"[0-9a-f]{64}", coverage["wholeListSortedIdsSha256"])):
        raise ValueError("Invalid index coverage")
    errors = []
    for job_id, row in records.items():
        for key in ("createdAt", "template"):
            if indexed[job_id][key] != row[key]:
                raise ValueError(f"{job_id}: index and job disagree on {key}")
        payer = row["paidBy"]
        if payer is not None and not re.fullmatch(r"0x[0-9a-f]{40}", payer):
            raise ValueError(f"{job_id}: invalid or non-lowercase paidBy")
        if row["error"] is not None:
            errors.extend(row["error"])
    if jobs["errors"] != errors:
        raise ValueError("Failed endpoint reads differ from row errors")
    return len(indexed), coverage["wholeListCount"], len(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=SOURCE_DIR)
    args = parser.parse_args()
    try:
        shard_count, list_count, failures = check(args.directory)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(f"PASS: {shard_count} indexed jobs have exactly one matching row "
          f"({list_count} whole-list IDs, {failures} failed detail/result reads).")


if __name__ == "__main__":
    main()
