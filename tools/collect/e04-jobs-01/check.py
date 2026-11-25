#!/usr/bin/env python3
"""Offline check for sources/e04-jobs-01 (Python standard library only, no network).

1. Fixtures: lib.select() on fixtures/cases.json reproduces oracle-group expansion,
   post-T admission, shard, saved-detail, holder and explicit-payer rules.
2. Saved data: every selected ID exactly once, in shard 1; explicit response or error
   per read; body sha256s; detail id == jobId; raw result kept; coverage counts consistent.
3. If the saved inputs are byte-identical to collection time, the expected ID set is
   recomputed from them and must match exactly.
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lib  # noqa: E402

OUT = lib.ROOT / "sources/e04-jobs-01"
failures = []


def fail(msg):
    failures.append(msg)


def check_fixtures():
    cases = json.loads((HERE / "fixtures/cases.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in cases["files"].items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        expected, unresolved = lib.select(root)
    got = sorted(expected)
    if got != cases["expected"]:
        fail(f"fixture expected set mismatch: got {got} want {cases['expected']}")
    for jid, why in cases["excluded"].items():
        if jid in expected:
            fail(f"fixture {jid} should be excluded ({why})")
    if sorted(unresolved) != sorted(cases["unresolvedRequests"]):
        fail(f"fixture unresolved requests {sorted(unresolved)}")
    grp = [j for j, e in expected.items() if cases["oracleGroupOrder"] in e["orders"]]
    if len(grp) != 3:
        fail(f"oracle-group expansion yielded {len(grp)} jobs, want 3 (2 direct jobId + 1 via requestId)")
    if cases["postTAdmission"] not in expected:
        fail("post-T admission with pre-T order not selected")
    for j in expected:
        if lib.shard_of(j) != 1:
            fail(f"fixture {j} not in shard 1")
    return len(cases["expected"])


def check_saved():
    index = json.loads((OUT / "index.json").read_text(encoding="utf-8"))
    raw = (OUT / "records.json").read_bytes()
    rec = json.loads(raw)
    for doc, name in ((index, "index"), (rec, "records")):
        for k in ("source", "fetchedAt", "coverage", "errors", "columns", "rows"):
            if k not in doc:
                fail(f"{name}.json lacks {k}")
    if index["records"]["sha256"] != hashlib.sha256(raw).hexdigest():
        fail("index.json records sha256 does not match records.json bytes")
    cov = rec["coverage"]
    if cov != index["coverage"]:
        fail("index and records coverage differ")
    ids = [r[0] for r in index["rows"]]
    rows = [dict(zip(rec["columns"], r)) for r in rec["rows"]]
    rids = [r["jobId"] for r in rows]
    if len(set(ids)) != len(ids):
        fail("duplicate IDs in index.json")
    if len(set(rids)) != len(rids):
        fail("duplicate IDs in records.json")
    if sorted(ids) != sorted(rids):
        fail("index and records ID sets differ")
    if lib.expected_digest(ids) != cov["expectedIdsSha256"] or len(ids) != cov["expectedCount"]:
        fail("expected ID digest/count mismatch")
    for j in ids:
        if lib.shard_of(j) != lib.SHARD:
            fail(f"{j} not in shard 1")
    errs = {(e["jobId"], e["read"]) for e in rec["errors"]}
    ok_d = ok_r = 0
    unavailable = []
    for r in rows:
        j = r["jobId"]
        if r["shard"] != 1:
            fail(f"{j} shard column {r['shard']}")
        if not r["orders"] and not r["basis"]:
            fail(f"{j} has no selection basis")
        for kind in ("detail", "result"):
            st, body, sha, at, err = (r[kind + "HttpStatus"], r[kind + "BodyText"], r[kind + "Sha256"],
                                      r[kind + "FetchedAt"], r[kind + "Error"])
            want_url = f"https://api.imd.fun/jobs/{j}" + ("/result" if kind == "result" else "")
            if r[kind + "Url"] != want_url:
                fail(f"{j} {kind} url {r[kind + 'Url']}")
            if not at:
                fail(f"{j} {kind} has no read timestamp")
            if st == 200:
                if body is None or hashlib.sha256(body.encode("utf-8")).hexdigest() != sha:
                    fail(f"{j} {kind} raw body missing or sha256 mismatch")
                    continue
                obj = json.loads(body)
                if kind == "detail":
                    ok_d += 1
                    if obj.get("id") != j or r["detailIdMatches"] is not True:
                        fail(f"{j} detail id {obj.get('id')} does not match")
                    if r["template"] != obj.get("template") or r["stateAtRead"] != obj.get("state"):
                        fail(f"{j} derived template/state differ from raw detail")
                    if obj.get("createdAt") and (not lib.before_t(obj["createdAt"])) != r["createdAtOrAfterT"]:
                        fail(f"{j} createdAtOrAfterT flag wrong")
                    if r["createdAtOrAfterT"] and not r["orders"]:
                        fail(f"{j} admitted after T without a pre-T order")
                else:
                    ok_r += 1
                    if obj.get("jobId") not in (None, j):
                        fail(f"{j} result jobId {obj.get('jobId')}")
                    meta = r["resultMeta"] or {}
                    if len(meta.get("files", [])) != len(obj.get("files") or []):
                        fail(f"{j} result metadata lost files")
            else:
                if not err or (j, kind) not in errs:
                    fail(f"{j} {kind} status {st} without an explicit listed error")
                if kind == "detail":
                    unavailable.append(j)
    if ok_d != cov["detailFetched"] or ok_r != cov["resultFetched"]:
        fail("coverage fetched counts mismatch")
    if sorted(unavailable) != sorted(cov["unavailableDetail"]):
        fail("coverage unavailable list mismatch")
    # recompute selection when the inputs are unchanged
    recomputed = None
    try:
        current = lib.input_hashes()
    except OSError as e:
        current = {"error": str(e)}
    if current == cov["inputsSha256"]:
        exp, unres = lib.select()
        recomputed = True
        if sorted(exp) != sorted(ids):
            fail(f"recomputed selection differs: {len(exp)} vs {len(ids)}")
        if unres != cov["unresolvedOracleRequests"]:
            fail("recomputed unresolved oracle requests differ")
        idx_basis = {r[0]: r[1:] for r in index["rows"]}
        for j, e in exp.items():
            if idx_basis.get(j) != [e["basis"], e["wallets"], e["orders"]]:
                fail(f"{j} basis differs on recompute")
                break
    else:
        recomputed = False
        changed = sorted(k for k in set(current) | set(cov["inputsSha256"])
                         if current.get(k) != cov["inputsSha256"].get(k))
        print("note: inputs changed since collection, saved expected set kept as observation:",
              ", ".join(changed[:8]))
    return len(ids), ok_d, ok_r, len(unavailable), recomputed


def main():
    nfx = check_fixtures()
    n, d, r, u, rc = check_saved()
    print(f"fixtures: {nfx} expected ids ok (oracle-group expansion, post-T admission)")
    print(f"expected {n}, detail fetched {d}, result fetched {r}, unavailable {u}, recomputed-from-inputs {rc}")
    print("status/result observations are read-time, not historical at T")
    if failures:
        for f in failures[:50]:
            print("FAIL:", f)
        print(f"{len(failures)} failure(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
