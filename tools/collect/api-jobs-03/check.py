#!/usr/bin/env python3
"""Offline check: every indexed job has exactly one row in jobs.json, and nothing else. Exit 1 on failure."""
import json, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
OUT = os.path.join(ROOT, "sources", "api-jobs-03")


def main():
    bad = []
    idx = json.load(open(os.path.join(OUT, "index.json")))
    jobs = json.load(open(os.path.join(OUT, "jobs.json")))
    ids = [r[0] for r in idx["rows"]]
    if len(ids) != len(set(ids)):
        bad.append("duplicate ids in index")
    for i in ids:
        if i[0] not in "67":
            bad.append("index id outside shard: " + i)
    ci = jobs["columns"].index("id")
    cr = [r[ci] for r in jobs["rows"]]
    for r in jobs["rows"]:
        if len(r) != len(jobs["columns"]):
            bad.append("row width wrong: " + str(r[ci]))
    for i in ids:
        c = cr.count(i)
        if c != 1:
            bad.append("%s has %d rows" % (i, c))
    for i in set(cr) - set(ids):
        bad.append("row not in index: " + i)
    nerr = sum(1 for r in jobs["rows"] if r[jobs["columns"].index("error")])
    print("indexed=%d rows=%d rows_with_error=%d listCount=%s" % (len(ids), len(cr), nerr, idx.get("listCount")))
    for b in bad[:30]:
        print("FAIL", b)
    sys.exit(1 if bad else 0)


main()
