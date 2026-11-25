#!/usr/bin/env python3
"""Verify jobs-04 offline, including index/row bijection and saved evidence.

Usage: python3 -B tools/collect/api-jobs-04/check.py [--data-dir DIRECTORY]
No network reads and no writes. Endpoint failures are allowed only when saved
after three attempts and faithfully represented by nulls and an error row.
"""

import argparse
from collections import Counter
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("jobs04_collect", Path(__file__).with_name("collect.py"))
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path, expected_columns):
    # Reject duplicate object keys rather than silently taking the last value.
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, str(path) + ": duplicate JSON key " + key)
            obj[key] = value
        return obj

    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    require(set(document) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"},
            str(path) + ": source envelope must have exactly six required keys")
    require(document["columns"] == expected_columns, str(path) + ": unexpected columns/order")
    require(isinstance(document["rows"], list), str(path) + ": rows is not an array")
    require(isinstance(document["coverage"], dict), str(path) + ": coverage is not an object")
    require(isinstance(document["errors"], list), str(path) + ": errors is not an array")
    COLLECT.timestamp(document["fetchedAt"])
    for number, row in enumerate(document["rows"], 1):
        require(isinstance(row, list) and len(row) == len(expected_columns),
                f"{path}: row {number} does not match column width")
    return document


def by_id(document, name):
    ids = [row[0] for row in document["rows"]]
    duplicates = [key for key, count in Counter(ids).items() if count > 1]
    require(not duplicates, name + ": duplicate ids: " + str(duplicates[:5]))
    return {row[0]: dict(zip(document["columns"], row)) for row in document["rows"]}


def validate_raw(records, selected, result):
    require(set(records) == set(selected), ("results" if result else "details") + ": requested/indexed id set mismatch")
    failures = 0
    for job_id, entry in records.items():
        COLLECT.timestamp(entry["fetchedAt"])
        attempts = entry["attempts"]
        require(isinstance(attempts, list) and attempts, job_id + ": no read attempt evidence")
        if entry["error"]:
            failures += 1
            require(entry["payload"] is None, job_id + ": failed read contains a payload")
            require(len(attempts) >= 3 and all(a["error"] for a in attempts[-3:]),
                    job_id + ": exhausted read lacks three failed attempts")
        else:
            require(entry["httpStatus"] == 200, job_id + ": success status is not HTTP 200")
            require(attempts[-1]["error"] is None, job_id + ": successful read has failed last attempt")
            COLLECT.validate_job(entry["payload"], job_id, result)
        require(entry == COLLECT.lower_addresses(entry), job_id + ": mixed-case wallet address")
    return failures


def check(directory):
    pages = load(directory / "pages.json", COLLECT.PAGE_COLUMNS)
    index = load(directory / "index.json", ["id", "createdAt", "template"])
    jobs = load(directory / "jobs.json", COLLECT.COLUMNS)
    details_doc = load(directory / "details.json", COLLECT.RAW_COLUMNS)
    results_doc = load(directory / "results.json", COLLECT.RAW_COLUMNS)
    require(pages["coverage"].get("complete") is True, "pagination is incomplete")
    require(not pages["errors"], "pagination has unresolved errors")
    require(pages["rows"], "no list pages saved")
    before = COLLECT.CUTOFF
    terminated = False
    for position, row in enumerate(pages["rows"]):
        page = dict(zip(COLLECT.PAGE_COLUMNS, row))
        require(not terminated, "page exists after pagination terminated")
        require(COLLECT.timestamp(page["before"]) == COLLECT.timestamp(before),
                "page cursor does not equal preceding oldest createdAt + 1ms")
        COLLECT.timestamp(page["fetchedAt"])
        if page["error"]:
            require(page["jobs"] is None, "failed list read contains jobs")
            require(len(page["attempts"]) >= 3, "failed list page lacks three attempts")
            continue  # A resumed run retries the same cursor; history is retained.
        require(page["httpStatus"] == 200, "successful page is not HTTP 200")
        COLLECT.validate_page({"count": page["count"], "jobs": page["jobs"]})
        entries = page["jobs"]
        require(len(entries) <= 500, "page exceeds requested limit")
        if not entries:
            terminated = True
        else:
            require(all(COLLECT.timestamp(j["createdAt"]) < COLLECT.timestamp(before) for j in entries),
                    "API returned entry at/after page's strict before cursor")
            times = [COLLECT.timestamp(j["createdAt"]) for j in entries]
            require(times == sorted(times, reverse=True), "list page not newest first")
            next_before = COLLECT.stamp(min(times) + timedelta(milliseconds=1))
            if COLLECT.timestamp(next_before) >= COLLECT.timestamp(before):
                require(len(entries) < 500, "full-page timestamp boundary stalled; coverage unknown")
                terminated = True
            before = next_before
    require(terminated, "no verified terminal page")
    listed = COLLECT.page_records(pages)
    require(pages["coverage"]["uniqueListedJobs"] == len(listed), "list unique count incorrect")
    selected = {key: entry for key, entry in listed.items() if key[0].lower() in COLLECT.PREFIXES}
    indexed = by_id(index, "index.json")
    job_rows = by_id(jobs, "jobs.json")
    require(set(indexed) == set(selected), "index is not exactly prefixes 8/9 from whole list")
    require(set(job_rows) == set(indexed), "each indexed job must have exactly one job row (missing/extra ids)")
    require(list(indexed) == sorted(indexed), "index rows not sorted by id")
    require(list(job_rows) == sorted(job_rows), "job rows not sorted by id")
    for doc, name in ((index, "index"), (jobs, "jobs")):
        coverage = doc["coverage"]
        require(coverage["snapshotTime"] == COLLECT.CUTOFF, name + ": wrong fixed cutoff")
        require(coverage["wholeListCount"] == len(listed), name + ": wrong whole-list count")
        require(coverage["wholeListSortedIdsSha256"] == COLLECT.ids_hash(listed), name + ": wrong whole-list hash")
        require(coverage["listingComplete"] is True, name + ": listing marked incomplete")
        require(coverage["shardCount"] == len(selected), name + ": wrong shard count")
    for job_id, entry in indexed.items():
        require(entry["createdAt"] == selected[job_id]["createdAt"] and entry["template"] == selected[job_id].get("template"),
                job_id + ": indexed time/template differs from list")
        require(COLLECT.timestamp(entry["createdAt"]) < COLLECT.timestamp(COLLECT.CUTOFF),
                job_id + ": created at/after snapshot")
    details = by_id(details_doc, "details.json")
    results = by_id(results_doc, "results.json")
    detail_failures = validate_raw(details, selected, False)
    result_failures = validate_raw(results, selected, True)
    require(jobs["coverage"]["allReadsAttempted"] is True, "not every read was attempted")
    expected_errors = []
    for job_id, indexed_job in indexed.items():
        row = job_rows[job_id]
        expected, null_reasons = COLLECT.extract(indexed_job, details[job_id], results[job_id])
        require([row[c] for c in COLLECT.COLUMNS] == expected,
                job_id + ": projected row differs from saved API evidence")
        require(jobs["coverage"]["nullReasonsByJob"].get(job_id, {}) == null_reasons,
                job_id + ": null explanations differ from saved API evidence")
        if details[job_id]["error"] is None:
            payload = details[job_id]["payload"]
            require(row["paidBy"] == payload.get("paidBy"), job_id + ": payer was inferred/changed")
            require(row["oracleRequestId"] is None, job_id + ": oracle panel leaked into non-oracle shard")
        if row["error"]:
            expected_errors.append({"id": job_id, "reads": row["error"]})
        if row["objective"] is not None:
            require(len(row["objective"]) <= 300, job_id + ": objective longer than 300 characters")
        if row["paidBy"] is not None:
            require(COLLECT.ADDRESS.fullmatch(row["paidBy"]) and row["paidBy"] == row["paidBy"].lower(),
                    job_id + ": payer is not a lowercase wallet address")
    require(jobs["errors"] == expected_errors, "jobs error summary differs from failed rows")
    for raw_doc, name in ((details_doc, "details"), (results_doc, "results")):
        expected = [{"id": row[0], "error": row[4]} for row in raw_doc["rows"] if row[4]]
        require(raw_doc["errors"] == expected, name + ": error summary differs from raw rows")
    print("PASS: five compact source envelopes, page cursor overlap, terminal pagination, cutoff and whole-list hash")
    print(f"PASS: {len(listed)} unique listed jobs; {len(indexed)} indexed shard jobs; exactly {len(job_rows)} unique job rows")
    print(f"PASS: {len(details)} detail reads and {len(results)} result reads; raw evidence and projections agree")
    print(f"PASS: explicit-only payers, non-oracle membership, lowercase addresses and null explanations")
    print(f"RECORDED FAILURES: detail={detail_failures}, result={result_failures} (retained as errors/nulls)")
    print("WHOLE_LIST_SORTED_IDS_SHA256: " + COLLECT.ids_hash(listed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=COLLECT.OUT)
    args = parser.parse_args()
    try:
        check(args.data_dir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
