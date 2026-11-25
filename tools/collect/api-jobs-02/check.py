#!/usr/bin/env python3
"""Offline check: every indexed shard-02 job has exactly one row in jobs.json."""
import json, os, sys
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "sources", "api-jobs-02")
idx = json.load(open(os.path.join(D, "index.json")))
jobs = json.load(open(os.path.join(D, "jobs.json")))
ci = jobs["columns"].index("id")
ids = [r[ci] for r in jobs["rows"]]
want = [r[0] for r in idx["rows"]]
bad = 0
for i in want:
    n = ids.count(i)
    if n != 1: print("job %s has %d rows" % (i, n)); bad += 1
extra = set(ids) - set(want)
for i in sorted(extra): print("row %s not indexed" % i); bad += 1
for i in want:
    if i[0] not in "45": print("index id %s outside shard" % i); bad += 1
print("indexed %d, rows %d, problems %d" % (len(want), len(ids), bad))
sys.exit(1 if bad else 0)
