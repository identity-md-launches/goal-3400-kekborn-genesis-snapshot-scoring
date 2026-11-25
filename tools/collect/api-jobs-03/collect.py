#!/usr/bin/env python3
"""Collect job detail + result for shard 03 (non-oracle jobs created before T, id starts with 6 or 7).

Rerunnable: the paged list is cached in tools/collect/api-jobs-03/cache/list.json and
finished rows are kept in sources/api-jobs-03/jobs.json; a rerun fetches only what is missing
(rows with an error are retried). Standard library only. Read-only: GET requests only.

usage: collect.py [--max-seconds N] [--relist]
"""
import hashlib, json, os, sys, time, threading
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

API = "https://api.imd.fun"
T = "2026-11-25T11:00:00.000Z"
PREFIXES = ("6", "7")
RATE = 85  # reads per minute, under the 90 budget
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT = os.path.join(ROOT, "sources", "api-jobs-03")
CACHE = os.path.join(HERE, "cache", "list.json")

COLUMNS = ["id", "state", "template", "createdAt", "updatedAt", "objective", "paidBy", "parentJobId",
           "projectId", "projectVersions", "workflowId", "oracleRequestId", "via", "launchKind",
           "launchStatus", "launchChainId", "launchId", "siteUrl", "blockedReason", "deliveryRepoUrl",
           "deliveryCommit", "deliveredAt", "mediaFiles", "nodes", "nodeRoles", "nodesAccepted",
           "revisions", "judgeRevisions", "seats", "reviewsSent", "reviewValues", "resultComplete",
           "resultFiles", "resultFileNames", "resultSourceNodes", "fetchedAt", "error"]
INDEX_COLUMNS = ["id", "createdAt", "template"]

_times = []
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def throttle():
    with _lock:
        while True:
            t = time.time()
            while _times and t - _times[0] > 60:
                _times.pop(0)
            if len(_times) < RATE:
                _times.append(t)
                return
            time.sleep(max(0.05, 60 - (t - _times[0]) + 0.05))


def get(path, tries=3):
    """GET json. Returns (data, error). 429 waits Retry-After and does not use up a try (max 20 waits)."""
    err = None
    attempt = 0
    waits = 0
    while attempt < tries:
        throttle()
        try:
            req = urllib.request.Request(API + path, headers={"Accept": "application/json", "User-Agent": "imd-genesis-collect"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and waits < 20:
                waits += 1
                try:
                    ra = float(e.headers.get("Retry-After", "5"))
                except ValueError:
                    ra = 5.0
                time.sleep(min(max(ra, 1), 120))
                continue
            err = "HTTP %d" % e.code
        except Exception as e:  # noqa
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
        attempt += 1
        time.sleep(1.5 * attempt)
    return None, "%s (after %d tries)" % (err, tries)


def parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def list_all(errors):
    seen = {}
    before = T
    pages = 0
    while True:
        d, e = get("/jobs?exclude=oracle&limit=500&before=" + before)
        pages += 1
        if e:
            errors.append({"what": "list before=" + before, "error": e})
            return None
        jobs = d.get("jobs") or []
        if not jobs:
            break
        new = 0
        for j in jobs:
            if j["id"] not in seen:
                seen[j["id"]] = j
                new += 1
        oldest = min(j["createdAt"] for j in jobs)
        print("page %d before=%s got=%d new=%d total=%d" % (pages, before, len(jobs), new, len(seen)), flush=True)
        if new == 0:
            break
        nxt = fmt(parse(oldest) + timedelta(milliseconds=1))
        if nxt >= before and len(jobs) < 500:
            break
        before = nxt
        if len(jobs) < 1:
            break
    return seen


def build_row(jid, listed):
    row = {c: None for c in COLUMNS}
    row["id"] = jid
    row["createdAt"] = listed.get("createdAt")
    row["template"] = listed.get("template")
    errs = []
    d, e = get("/jobs/" + jid)
    if e:
        errs.append("detail: " + e)
    else:
        L = d.get("launch") or {}
        dl = d.get("delivery") or {}
        proj = d.get("project") or {}
        wf = d.get("workflow")
        nodes = d.get("nodes") or []
        site = d.get("site")
        if isinstance(site, dict):
            site = site.get("url") or site.get("siteUrl")
        media = d.get("media")
        if media is None:
            media = dl.get("media")
        vers = sorted(proj.get("versions") or [], key=lambda v: (v.get("createdAt") or ""))
        roles = {}
        for n in nodes:
            roles[n.get("role")] = roles.get(n.get("role"), 0) + 1
        seats = sorted({str((n.get("seat") or {}).get("tokenId")) for n in nodes if n.get("seat") and n["seat"].get("tokenId") is not None})
        reviews = d.get("reviews") or []
        row.update({
            "state": d.get("state"), "template": d.get("template"), "createdAt": d.get("createdAt"),
            "updatedAt": d.get("updatedAt"), "objective": (d.get("objective") or "")[:300],
            "paidBy": d.get("paidBy").lower() if isinstance(d.get("paidBy"), str) else None,
            "parentJobId": d.get("parentJobId"), "projectId": proj.get("id"),
            "projectVersions": [v.get("jobId") for v in vers],
            "workflowId": (wf.get("id") if isinstance(wf, dict) else wf),
            "oracleRequestId": d.get("oracleRequestId"), "via": d.get("via"),
            "launchKind": L.get("kind"), "launchStatus": L.get("status"), "launchChainId": L.get("chainId"),
            "launchId": L.get("id"), "siteUrl": site, "blockedReason": d.get("blockedReason"),
            "deliveryRepoUrl": dl.get("repoUrl"), "deliveryCommit": dl.get("commit"),
            "deliveredAt": dl.get("deliveredAt"),
            "mediaFiles": (len(media) if isinstance(media, (list, dict)) else (0 if media is None else None)),
            "nodes": len(nodes), "nodeRoles": ["%s:%d" % (k, roles[k]) for k in sorted(roles, key=str)],
            "nodesAccepted": sum(1 for n in nodes if n.get("state") == "accepted"),
            "revisions": sum(n.get("revisions") or 0 for n in nodes),
            "judgeRevisions": sum(n.get("judgeRevisions") or 0 for n in nodes),
            "seats": seats,
            "reviewsSent": sum(1 for r in reviews if r.get("status") == "sent"),
            "reviewValues": [en.get("value") for r in reviews for en in (r.get("entries") or [])],
        })
    r, e = get("/jobs/%s/result" % jid)
    if e:
        errs.append("result: " + e)
    else:
        files = r.get("files") or []
        names = []
        for f in files:
            names.append(f.get("name") or f.get("path") or f.get("filename") if isinstance(f, dict) else str(f))
        row["resultComplete"] = r.get("complete")
        row["resultFiles"] = len(files)
        row["resultFileNames"] = names
        row["resultSourceNodes"] = len(r.get("source") or [])
    row["fetchedAt"] = now()
    row["error"] = "; ".join(errs) if errs else None
    return row


def main():
    max_s = None
    if "--max-seconds" in sys.argv:
        max_s = float(sys.argv[sys.argv.index("--max-seconds") + 1])
    start = time.time()
    errors = []
    listing = None
    if os.path.exists(CACHE) and "--relist" not in sys.argv:
        listing = json.load(open(CACHE))
    if listing is None:
        seen = list_all(errors)
        if seen is None:
            print("listing failed", errors)
            sys.exit(2)
        ids = sorted(seen)
        listing = {"fetchedAt": now(), "count": len(ids),
                   "idsSha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
                   "jobs": {i: {"createdAt": seen[i].get("createdAt"), "template": seen[i].get("template")} for i in ids
                            if seen[i].get("createdAt", "") < T}}
        atomic_write(CACHE, listing)
    mine = sorted(i for i in listing["jobs"] if i.startswith(PREFIXES))
    print("list=%d mine=%d" % (listing["count"], len(mine)), flush=True)

    jpath = os.path.join(OUT, "jobs.json")
    rows = {}
    if os.path.exists(jpath):
        old = json.load(open(jpath))
        for a in old["rows"]:
            rec = dict(zip(old["columns"], a))
            rows[rec["id"]] = rec
    todo = [i for i in mine if i not in rows or rows[i]["error"]]
    print("todo=%d" % len(todo), flush=True)

    def save():
        out_rows = [[rows[i][c] for c in COLUMNS] for i in mine if i in rows]
        errs = [{"id": i, "error": rows[i]["error"]} for i in mine if i in rows and rows[i]["error"]]
        atomic_write(jpath, {"source": "api.imd.fun /jobs/<id> + /jobs/<id>/result, shard 03",
                             "fetchedAt": now(),
                             "coverage": "non-oracle jobs created before 2026-11-25T11:00:00Z with id starting 6 or 7; %d of %d listed" % (len(out_rows), len(mine)),
                             "errors": errs, "columns": COLUMNS, "rows": out_rows})

    n = 0
    for i in todo:
        if max_s and time.time() - start > max_s:
            print("time limit; rerun to resume")
            break
        rows[i] = build_row(i, listing["jobs"][i])
        n += 1
        if n % 20 == 0:
            save()
            print("done %d/%d" % (n, len(todo)), flush=True)
    save()
    complete = all(i in rows for i in mine)
    atomic_write(os.path.join(OUT, "index.json"), {
        "source": "api.imd.fun /jobs?exclude=oracle list, shard 03 ids",
        "fetchedAt": listing["fetchedAt"],
        "coverage": "ids starting 6 or 7 among non-oracle jobs created before 2026-11-25T11:00:00Z; listCount is the whole paged list (all shards); listIdsSha256 is sha256 of the sorted ids of the whole list joined by newline, no trailing newline",
        "errors": errors, "listCount": listing["count"], "listIdsSha256": listing["idsSha256"],
        "columns": INDEX_COLUMNS,
        "rows": [[i, listing["jobs"][i]["createdAt"], listing["jobs"][i]["template"]] for i in mine]})
    bad = sum(1 for i in mine if i in rows and rows[i]["error"])
    print("rows=%d complete=%s rows_with_error=%d" % (len(mine), complete, bad))


if __name__ == "__main__":
    main()
