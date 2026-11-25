#!/usr/bin/env python3
"""Offline check for sources/e04-jobs-00 (standard library only)."""
import json, sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as c


def fixtures():
    # shard rule is the exact-jobId sha256 rule
    a, b = "0011ea8f-dce3-4a06-9b61-41bb5d8067b8", "00043615-ddff-4d50-9991-0b29ef2676b0"
    assert c.shard_of(a) == c.shard_of(a) and c.in_shard(a) == (c.shard_of(a) == 0)
    # oracle-group expansion: both member jobs referenced, via jobId and via catalog request id
    group = {"result": {"kind": "oracle-group", "requests": [{"jobId": "j-direct"}, {"requestId": "r-1"}]}}
    refs = c.refs_from_order(group, {"r-1": "j-cat"})
    assert {r[0] for r in refs} == {"j-direct", "j-cat"}, refs
    # single oracle order resolves by request id when jobId is absent
    assert c.refs_from_order({"result": {"kind": "oracle", "requestId": "r-1"}}, {"r-1": "j-cat"})[0][0] == "j-cat"
    # post-T admission: order created before T counts, order created at/after T does not
    orders = [("o1", "2026-11-25T10:59:59.999Z", {"result": {"jobId": "post-T-job"}}),
              ("o2", "2026-11-25T11:00:00Z", {"result": {"jobId": "late-order-job"}})]
    got = {r[0] for r in c.order_refs(orders, {})}
    assert got == {"post-T-job"}, got
    # selection: skips saved details, dedupes, keeps only shard 0, sorted
    ids = [f"id-{i}" for i in range(40)]
    sel = c.select([(i, "x", "y") for i in ids + ids], {"id-0"})
    assert all(c.in_shard(r[0]) for r in sel) and "id-0" not in [r[0] for r in sel]
    assert [r[0] for r in sel] == sorted(set(r[0] for r in sel))
    assert 0 < len(sel) < 40


def check():
    fixtures()
    idx = json.load(open(c.OUT / "index.json", encoding="utf-8"))
    rec = json.load(open(c.OUT / "records.json", encoding="utf-8"))
    assert idx["columns"] == c.INDEX_COLUMNS and rec["columns"] == c.RECORD_COLUMNS
    for d in (idx, rec):
        for k in ("source", "fetchedAt", "coverage", "errors", "columns", "rows"):
            assert k in d, k
    want = [r[0] for r in idx["rows"]]
    assert len(want) == len(set(want)) == idx["coverage"]["expected"], "index IDs not unique"
    for j in want:
        assert c.in_shard(j), f"{j} not in shard 0"
    got = Counter(r[0] for r in rec["rows"])
    assert set(got) == set(want) and all(v == 1 for v in got.values()), "records must cover each selected ID exactly once"
    unavailable = 0
    for r in rec["rows"]:
        assert len(r) == len(c.RECORD_COLUMNS)
        row = dict(zip(c.RECORD_COLUMNS, r))
        for part in ("detail", "result"):
            ok = row[part] is not None and row[part + "HttpStatus"] == 200
            assert ok or row[part + "Error"], f"{row['jobId']}: {part} has neither response nor error"
            assert row[part + "FetchedAt"], f"{row['jobId']}: no {part} timestamp"
        if row["detail"] is not None:
            assert row["detail"].get("id") == row["jobId"], f"{row['jobId']}: detail ID mismatch"
        if row["result"] is not None:  # no lost raw result
            assert row["resultHttpStatus"] == 200 and isinstance(row["result"], dict)
        if row["detail"] is None or row["result"] is None:
            unavailable += 1
    # every recorded failure is listed
    failed = {e["jobId"] for e in rec["errors"]}
    assert failed == {r[0] for r in rec["rows"] if r[5] or r[9]}, "errors list incomplete"
    # expected set is a superset-free subset of the recomputed plan unless details have since landed
    plan = {r[0] for r in c.expected()[0]}
    assert plan <= set(want), f"{len(plan - set(want))} planned IDs missing from index"
    print(f"OK: expected={len(want)} read={len(rec['rows'])} with-detail-and-result={len(want)-unavailable} "
          f"unavailable={unavailable}; fixtures passed")


if __name__ == "__main__":
    try:
        check()
    except (AssertionError, OSError, KeyError, ValueError) as e:
        print(f"Check failed: {e!r}", file=sys.stderr); sys.exit(1)
