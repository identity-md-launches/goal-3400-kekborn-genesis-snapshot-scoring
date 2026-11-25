#!/usr/bin/env python3
"""Offline structural, cutoff, pagination, digest, and cross-catalog validation."""

import argparse
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
from collect import (BASE, CUTOFF, DEFAULT_OUTPUT, ROOT, JOB_COLUMNS, GOAL_COLUMNS,
                     WORKFLOW_COLUMNS, ORACLE_COLUMNS, CHILD_COLUMNS, SEAT_COLUMNS,
                     ADDRESS, timestamp, iso, verify_snapshot)


FILES = {
    "job-index": JOB_COLUMNS,
    "goals": GOAL_COLUMNS,
    "workflows": WORKFLOW_COLUMNS,
    "oracle-requests": ORACLE_COLUMNS,
    "seats-owners": ["tokenId", "owner"],
    "seats-records": SEAT_COLUMNS,
    "capabilities": ["response"],
}
ENDPOINTS = {"job-index": "/jobs", "goals": "/goals/{jobId}", "workflows": "/workflows",
             "oracle-requests": "/oracle/requests", "seats-owners": "/seats/owners",
             "seats-records": "/seats/records", "capabilities": "/requests/capabilities"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
INTEGER = re.compile(r"[0-9]+\Z")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-JSON numeric constant: " + value)


def hash_ids(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


class Checker:
    def __init__(self):
        self.failures = []
        self.passed = []
        self.notices = []

    def require(self, condition, label):
        if not condition:
            self.failures.append(label)
        return bool(condition)

    def check_time(self, value, label):
        try:
            timestamp(value)
            return True
        except (ValueError, OverflowError, TypeError):
            self.failures.append(label + ": invalid timezone-aware timestamp")
            return False

    def walk(self, value, label):
        if isinstance(value, str) and ADDRESS.fullmatch(value):
            self.require(value == value.lower(), label + ": address is not lowercase")
        elif isinstance(value, dict):
            for key, item in value.items():
                self.walk(item, label + "." + key)
                if key in ("amount", "unitAmount", "intakePrice"):
                    self.require(isinstance(item, str) and INTEGER.fullmatch(item),
                                 label + "." + key + ": token amount is not a raw integer string")
                if key == "customCap" and isinstance(item, dict):
                    for bound in ("min", "max"):
                        self.require(isinstance(item.get(bound), str) and INTEGER.fullmatch(item[bound]),
                                     label + ".customCap." + bound + ": not a raw integer string")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.walk(item, "%s[%d]" % (label, index))

    def load(self, directory, name):
        path = directory / (name + ".json")
        raw = path.read_bytes()
        doc = json.loads(raw, object_pairs_hook=strict_object, parse_constant=reject_constant)
        self.require(set(doc) == {"source", "fetchedAt", "coverage", "errors", "columns", "rows"},
                     name + ": envelope must have exactly the six specified keys")
        self.require(raw == (json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"),
                     name + ": file is not compact UTF-8 JSON plus LF")
        self.require(doc["source"] == BASE + ENDPOINTS[name], name + ": unexpected source")
        self.check_time(doc["fetchedAt"], name + ".fetchedAt")
        columns = doc["columns"]
        self.require(isinstance(columns, list) and len(columns) == len(set(columns)), name + ": duplicate/invalid columns")
        expected = FILES[name]
        self.require(columns[:len(expected)] == expected if name == "seats-records" else columns == expected,
                     name + ": columns differ from required schema")
        rows = doc["rows"]
        self.require(isinstance(rows, list), name + ": rows are not an array")
        for index, row in enumerate(rows):
            self.require(isinstance(row, list) and len(row) == len(columns),
                         "%s row %d: wrong row width/type" % (name, index))
        coverage = doc["coverage"]
        self.require(coverage["cutoffExclusive"] == CUTOFF, name + ": changed cutoff")
        self.require(coverage["rowCount"] == len(rows), name + ": rowCount mismatch")
        self.require(coverage["complete"] is True, name + ": incomplete collection")
        self.require(coverage["stateAsOfSnapshot"] is False, name + ": falsely claims historical state")
        self.require(isinstance(doc["errors"], list) and not doc["errors"], name + ": collection errors present")
        self.require(bool(coverage.get("nullPolicy")), name + ": no null explanation")
        reads = coverage["reads"]
        self.require(isinstance(reads, list), name + ": reads ledger is not an array")
        for read in reads:
            self.require(read["url"].startswith(BASE + "/"), name + ": read outside IMD API")
            self.check_time(read["startedAt"], name + ": read startedAt")
            self.check_time(read["fetchedAt"], name + ": read fetchedAt")
            self.require(read.get("error") is not None or read["status"] == 200, name + ": unexplained HTTP failure")
            if read.get("status") is not None:
                self.require(bool(HEX64.fullmatch(read.get("responseSha256", ""))), name + ": invalid response digest")
                self.require(type(read.get("bytes")) is int and read["bytes"] >= 0, name + ": invalid response byte count")
            if read.get("error") is not None:
                self.require("retryDelaySeconds" in read, name + ": unrecovered failed read")
        if reads:
            self.require(doc["fetchedAt"] == reads[-1]["fetchedAt"], name + ": fetchedAt differs from final read")
            self.require(coverage["readStartedAt"] == reads[0]["startedAt"], name + ": readStartedAt mismatch")
        self.require(coverage["readFinishedAt"] == doc["fetchedAt"], name + ": readFinishedAt mismatch")
        for col_index, column in enumerate(columns):
            count = sum(row[col_index] is None for row in rows)
            explained = sum(coverage["nulls"].get(column, {}).values())
            self.require(count == explained, name + ": missing or incorrect null counts for " + column)
        self.walk(doc, name)
        self.passed.append("%s: rows=%d sha256=%s" % (name + ".json", len(rows), hashlib.sha256(raw).hexdigest()))
        return doc

    def identities(self, doc, name):
        ids = [row[0] for row in doc["rows"]]
        self.require(all(isinstance(ident, str) and ident for ident in ids), name + ": invalid identity")
        self.require(len(ids) == len(set(ids)), name + ": duplicate ids")
        return set(ids)

    def pages(self, doc, name):
        ids = self.identities(doc, name)
        cov = doc["coverage"]
        self.require(cov["total"] == len(ids), name + ": total mismatch")
        self.require(cov["sortedIdsSha256"] == hash_ids(ids), name + ": sorted IDs digest mismatch")
        dates = cov["rowCreatedAt"]
        self.require(len(dates) == len(ids) and {row[0] for row in dates} == ids, name + ": creation-time coverage differs from rows")
        for ident, created in dates:
            if self.check_time(created, name + ": " + ident):
                self.require(timestamp(created) < timestamp(CUTOFF), name + ": row not strictly before T: " + ident)
        if "createdAt" in doc["columns"]:
            index = doc["columns"].index("createdAt")
            self.require(dict(dates) == {row[0]: row[index] for row in doc["rows"]}, name + ": creation-time metadata mismatch")
        pages = cov["pages"]
        self.require(bool(pages), name + ": no pagination evidence")
        self.require(cov["limit"] == 500, name + ": limit differs from 500")
        self.require(cov["termination"] in ("empty_page", "overlap_only_short_page", "short_page_exhausted"), name + ": pagination did not exhaust")
        self.require(cov["excludedAtOrAfterCutoff"] == [], name + ": endpoint violated cutoff")
        self.require(sum(page["newIds"] for page in pages) == len(ids), name + ": page totals do not match ids")
        previous = iso(timestamp(CUTOFF))
        successful_reads = {read["url"] for read in cov["reads"] if "error" not in read}
        for index, page in enumerate(pages, 1):
            self.require(page["page"] == index, name + ": page sequence mismatch")
            self.require(page["url"] in successful_reads, name + ": page has no successful read")
            self.require(page["before"] == previous, name + ": pagination skipped an overlap")
            self.require(page["count"] == page["size"] and 0 <= page["size"] <= 500, name + ": count is not page size")
            self.require(page["newIds"] + page["duplicates"] == page["size"], name + ": unaccounted page rows")
            if page["size"]:
                expected = iso(timestamp(page["oldestCreatedAt"]) + timedelta(milliseconds=1))
                self.require(page["nextBefore"] == expected, name + ": cursor is not oldest+1ms")
                self.require(timestamp(page["oldestCreatedAt"]) <= timestamp(page["newestCreatedAt"]) < timestamp(page["before"]), name + ": page timestamp bounds invalid")
                previous = expected
        if pages:
            last = pages[-1]
            if cov["termination"] == "empty_page":
                self.require(last["size"] == 0, name + ": terminal page is not empty")
            else:
                self.require(last["size"] < 500, name + ": terminal page is full")
            if cov["termination"] == "overlap_only_short_page":
                self.require(last["newIds"] == 0, name + ": overlap termination includes new IDs")
        return ids

    def references(self, doc, name, columns, job_ids):
        missing = set()
        for row in doc["rows"]:
            item = dict(zip(doc["columns"], row))
            for column in columns:
                ident = item[column]
                self.require(ident is None or isinstance(ident, str) and bool(ident), name + ": invalid job reference")
                if ident is not None and ident not in job_ids:
                    missing.add((item["id"], column, ident))
        listed = doc["coverage"]["absentJobIds"]
        self.require(all(isinstance(item.get("reason"), str) and item["reason"] for item in listed), name + ": unexplained absent reference")
        declared = [(item["id"], item["field"], item["jobId"]) for item in listed]
        self.require(len(declared) == len(set(declared)), name + ": duplicate absent reference")
        self.require(set(declared) == missing, name + ": absent reference ledger is incomplete or contains false absences")
        self.notices.append("%s: %d references absent from index, all explicitly listed" % (name, len(missing)))

    def run(self, directory):
        docs = {}
        for name in FILES:
            try:
                docs[name] = self.load(directory, name)
            except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                self.failures.append(name + ": " + str(exc))
        if len(docs) != len(FILES):
            return
        try:
            jobs = docs["job-index"]
            job_ids = self.pages(jobs, "job-index")
            self.require(jobs["coverage"]["oraclePanelJobsIncluded"] is True, "job-index: oracle jobs excluded")
            self.require(all("exclude=" not in read["url"] for read in jobs["coverage"]["reads"]), "job-index: filtering URL found")
            for name, cols in (("workflows", ["contractsJobId", "frontendJobId"]), ("oracle-requests", ["jobId"])):
                self.pages(docs[name], name)
                self.references(docs[name], name, cols, job_ids)
            goals = docs["goals"]
            goal_ids = self.identities(goals, "goals")
            expected = {row[0] for row in jobs["rows"] if row[2] == "goal"}
            self.require(goal_ids == expected, "goals: not exactly one row for every indexed goal")
            self.require(goals["coverage"]["expectedGoalCount"] == len(expected), "goals: expected count mismatch")
            self.require(goals["coverage"]["expectedGoalIdsSha256"] == hash_ids(expected), "goals: expected IDs hash mismatch")
            self.require(goals["coverage"]["childrenColumns"] == CHILD_COLUMNS, "goals: child schema mismatch")
            reads = {read["url"] for read in goals["coverage"]["reads"] if "error" not in read}
            dates = {row[0]: row[3] for row in jobs["rows"]}
            missing_children = set()
            children_by_key = {}
            child_nulls = {column: 0 for column in CHILD_COLUMNS}
            for row in goals["rows"]:
                ident = row[0]
                self.require(BASE + "/goals/" + ident in reads and BASE + "/goals/" + ident + "/children" in reads, "goals: detail or children GET missing: " + ident)
                self.require(row[9] == dates.get(ident), "goals: createdAt differs from job index: " + ident)
                children = row[10]
                self.require(isinstance(children, list), "goals: unavailable children: " + ident)
                keys = []
                for child in children or []:
                    if self.require(isinstance(child, list) and len(child) == 4, "goals: invalid child row"):
                        keys.append(child[0])
                        children_by_key[(ident, child[0])] = child
                        for column, value in zip(CHILD_COLUMNS, child):
                            child_nulls[column] += value is None
                        if child[2] is not None and child[2] not in job_ids:
                            missing_children.add((ident, child[0], child[2]))
                self.require(len(keys) == len(set(keys)), "goals: duplicate child keys: " + ident)
            for column, count in child_nulls.items():
                explained = sum(goals["coverage"]["nulls"].get("children." + column, {}).values())
                self.require(explained == count, "goals: incorrect child null counts for " + column)
            for extra in goals["coverage"]["additionalChildFields"]:
                child = children_by_key.get((extra["jobId"], extra["key"]))
                self.require(child is not None, "goals: extra child fields without child row")
                nested_job = extra["fields"].get("job")
                if child is not None and isinstance(nested_job, dict) and nested_job.get("id") is not None:
                    self.require(child[2] == nested_job["id"], "goals: nested job.id link was lost or conflicts")
            declared_children = goals["coverage"]["absentChildJobIds"]
            self.require(all(item.get("reason") for item in declared_children), "goals: unexplained absent child")
            declared = [(item["parentJobId"], item["key"], item["jobId"]) for item in declared_children]
            self.require(len(declared) == len(set(declared)) and set(declared) == missing_children, "goals: absent child ledger mismatch")
            self.notices.append("goals: %d parents; %d absent child job references explicitly listed" % (len(goal_ids), len(missing_children)))
            for name in ("seats-owners", "seats-records", "capabilities"):
                doc = docs[name]
                cov = doc["coverage"]
                self.require(cov["historicalAtCutoff"] is False, name + ": missing current-state caveat")
                self.require(cov["observedAt"] == doc["fetchedAt"], name + ": missing actual read time")
                if name != "capabilities":
                    self.identities(doc, name)
                    self.require(cov["responseMetadata"]["count"] == len(doc["rows"]), name + ": API count mismatch")
                    self.require(all(isinstance(row[0], str) and INTEGER.fullmatch(row[0]) for row in doc["rows"]), name + ": tokenId is not a decimal string")
            owners = docs["seats-owners"]
            self.require([row[0] for row in owners["rows"]] == [str(i) for i in range(len(owners["rows"]))], "seats-owners: tokenId is not zero-based owners index")
            self.require(owners["coverage"]["at"] == owners["coverage"]["responseMetadata"]["at"], "seats-owners: source at timestamp changed")
            self.check_time(owners["coverage"]["at"], "seats-owners.at")
            self.require(all(row[1] is None or isinstance(row[1], str) and ADDRESS.fullmatch(row[1]) for row in owners["rows"]), "seats-owners: invalid owner")
            capability_rows = docs["capabilities"]["rows"]
            self.require(len(capability_rows) == 1 and isinstance(capability_rows[0][0], dict), "capabilities: full response missing")
            self.passed.append("unique IDs, exclusive cutoff, pagination overlap/exhaustion, ID digests, goal coverage, references, null accounting, address/amount formats, and seat observation caveats")
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            self.failures.append("cross-catalog validation: " + str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    checker = Checker()
    try:
        verify_snapshot(ROOT / "snapshot.json")
        checker.passed.append("fixed snapshot: T=%s Ethereum=26491756 Robinhood=122989778" % CUTOFF)
    except (OSError, ValueError, KeyError) as exc:
        checker.failures.append("snapshot: " + str(exc))
    checker.run(args.input_dir)
    for line in checker.notices:
        print("NOTE " + line)
    if checker.failures:
        for line in checker.failures:
            print("FAIL " + line)
        print("FAIL: %d checks failed" % len(checker.failures))
        return 1
    for line in checker.passed:
        print("PASS " + line)
    print("PASS: all 7 catalogs validated offline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
