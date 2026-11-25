#!/usr/bin/env python3
"""Collect job details/results for shard 02 (ids starting with hex 4 or 5).
Rerunnable: per-job raw reads are cached in a state dir and reused (resume).
Usage: python3 collect.py [--state DIR]  (default state: .cache/api-jobs-02 at repo root, not submitted)"""
import json, os, sys, time, hashlib, urllib.request, urllib.error, datetime

API = "https://api.imd.fun"
T = "2026-11-25T11:00:00Z"
T_MS = "2026-11-25T11:00:00.000Z"
PREFIXES = ("4", "5")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT = os.path.join(ROOT, "sources", "api-jobs-02")
STATE = os.path.join(ROOT, ".cache", "api-jobs-02")
if "--state" in sys.argv:
    STATE = sys.argv[sys.argv.index("--state") + 1]
MIN_INTERVAL = 60.0 / 85  # stay under 90 reads/min
_last = [0.0]
ERRORS = []

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def get(path):
    """Return (json, error). Three tries; 429 honours Retry-After (not counted as a try)."""
    err = None
    tries = 0
    while tries < 3:
        wait = MIN_INTERVAL - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(API + path, headers={"User-Agent": "kekborn-genesis-collector", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                try: ra = float(ra)
                except Exception: ra = 30.0
                time.sleep(max(ra, 1.0))
                continue
            err = "HTTP %d %s" % (e.code, path)
            if e.code == 404:
                break
        except Exception as e:
            err = "%s %s" % (type(e).__name__, path)
        tries += 1
        time.sleep(2 * tries)
    ERRORS.append(err)
    return None, err

def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)

def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def list_all():
    seen = {}
    before = T
    pages = 0
    while True:
        data, err = get("/jobs?exclude=oracle&limit=500&before=" + before)
        if data is None:
            raise SystemExit("listing failed: %s" % err)
        pages += 1
        jobs = data.get("jobs") or []
        new = 0
        for j in jobs:
            if j["id"] not in seen and j["createdAt"] < T_MS:
                seen[j["id"]] = j; new += 1
        if not jobs or new == 0:
            break
        oldest = min(parse_ts(j["createdAt"]) for j in jobs)
        before = fmt_ts(oldest + datetime.timedelta(milliseconds=1))
        print("page %d: %d jobs, %d new, next before=%s" % (pages, len(jobs), new, before), flush=True)
    return seen, pages

def cached(kind, jid):
    os.makedirs(STATE, exist_ok=True)
    p = os.path.join(STATE, "%s-%s.json" % (kind, jid))
    if os.path.exists(p):
        with open(p) as f:
            c = json.load(f)
        if c.get("data") is not None:
            return c
    path = "/jobs/%s" % jid + ("/result" if kind == "result" else "")
    data, err = get(path)
    c = {"fetchedAt": now(), "data": data, "error": err}
    with open(p, "w") as f:
        json.dump(c, f)
    return c

def g(o, *ks):
    for k in ks:
        if not isinstance(o, dict): return None
        o = o.get(k)
    return o

def count(v):
    return len(v) if isinstance(v, list) else None

COLUMNS = ["id","state","template","createdAt","updatedAt","objective","paidBy","parentJobId","projectId","projectVersions","workflowId","oracleRequestId","via","launchKind","launchStatus","launchChainId","launchId","siteUrl","blockedReason","deliveryRepoUrl","deliveryCommit","deliveredAt","mediaFiles","nodes","nodeRoles","nodesAccepted","revisions","judgeRevisions","seats","reviewsSent","reviewValues","resultComplete","resultFiles","resultFileNames","resultSourceNodes","fetchedAt","error"]

def site_url(d):
    s = d.get("site")
    if isinstance(s, str): return s
    if isinstance(s, dict): return s.get("url") or s.get("siteUrl")
    return None

def row(jid, listed, dc, rc):
    d, r = dc["data"], rc["data"]
    errs = [e for e in (dc["error"], rc["error"]) if e]
    out = {c: None for c in COLUMNS}
    out["id"] = jid
    out["fetchedAt"] = max(dc["fetchedAt"], rc["fetchedAt"])
    out["error"] = "; ".join(errs) if errs else None
    src = d if d is not None else listed
    out["state"] = src.get("state"); out["template"] = src.get("template")
    out["createdAt"] = src.get("createdAt"); out["updatedAt"] = src.get("updatedAt")
    if d is not None:
        ob = d.get("objective"); out["objective"] = ob[:300] if isinstance(ob, str) else None
        pb = d.get("paidBy"); out["paidBy"] = pb.lower() if isinstance(pb, str) else None
        out["parentJobId"] = d.get("parentJobId")
        out["projectId"] = g(d, "project", "id")
        vs = g(d, "project", "versions")
        if isinstance(vs, list):
            vs = sorted(vs, key=lambda v: v.get("createdAt") or "")
            out["projectVersions"] = [v.get("jobId") for v in vs]
        wf = d.get("workflow")
        out["workflowId"] = (wf.get("id") if isinstance(wf, dict) else wf) if wf else None
        out["oracleRequestId"] = d.get("oracleRequestId")
        out["via"] = d.get("via")
        out["launchKind"] = g(d, "launch", "kind"); out["launchStatus"] = g(d, "launch", "status")
        out["launchChainId"] = g(d, "launch", "chainId"); out["launchId"] = g(d, "launch", "id")
        out["siteUrl"] = site_url(d)
        out["blockedReason"] = d.get("blockedReason")
        out["deliveryRepoUrl"] = g(d, "delivery", "repoUrl"); out["deliveryCommit"] = g(d, "delivery", "commit")
        out["deliveredAt"] = g(d, "delivery", "deliveredAt")
        media = d.get("media") if d.get("media") is not None else g(d, "delivery", "media")
        out["mediaFiles"] = len(media) if isinstance(media, list) else (len(media.get("files") or []) if isinstance(media, dict) else None)
        nodes = d.get("nodes")
        if isinstance(nodes, list):
            out["nodes"] = len(nodes)
            roles = {}
            for n in nodes:
                roles[n.get("role")] = roles.get(n.get("role"), 0) + 1
            out["nodeRoles"] = ";".join("%s:%d" % (k, v) for k, v in sorted(roles.items(), key=lambda x: str(x[0])))
            out["nodesAccepted"] = sum(1 for n in nodes if n.get("state") == "accepted")
            out["revisions"] = sum(n.get("revisions") or 0 for n in nodes)
            out["judgeRevisions"] = sum(n.get("judgeRevisions") or 0 for n in nodes)
            seats = sorted({str(g(n, "seat", "tokenId")) for n in nodes if g(n, "seat", "tokenId") is not None}, key=lambda x: (len(x), x))
            out["seats"] = seats
        revs = d.get("reviews")
        if isinstance(revs, list):
            out["reviewsSent"] = sum(1 for x in revs if x.get("status") == "sent")
            out["reviewValues"] = [e.get("value") for x in revs for e in (x.get("entries") or [])]
    if r is not None:
        out["resultComplete"] = r.get("complete")
        files = r.get("files")
        if isinstance(files, list):
            out["resultFiles"] = len(files)
            out["resultFileNames"] = [(f.get("name") or f.get("path")) if isinstance(f, dict) else f for f in files]
        out["resultSourceNodes"] = count(r.get("source"))
    return [out[c] for c in COLUMNS]

def main():
    listed, pages = list_all()
    allids = sorted(listed)
    mine = sorted(i for i in allids if i[0] in PREFIXES)
    print("listed %d jobs in %d pages; shard has %d" % (len(allids), pages, len(mine)), flush=True)
    rows = []
    for k, jid in enumerate(mine):
        dc = cached("detail", jid); rc = cached("result", jid)
        rows.append(row(jid, listed[jid], dc, rc))
        if k % 25 == 0: print("%d/%d" % (k, len(mine)), flush=True)
    errs = [r[-1] for r in rows if r[-1]]
    ts = now()
    os.makedirs(OUT, exist_ok=True)
    jobs = {"source": "api.imd.fun GET /jobs/<id> and /jobs/<id>/result", "fetchedAt": ts,
            "coverage": "non-oracle jobs created strictly before %s whose id begins with 4 or 5 (shard 02); %d jobs" % (T, len(rows)),
            "errors": errs, "columns": COLUMNS, "rows": rows}
    index = {"source": "api.imd.fun GET /jobs?exclude=oracle&limit=500&before=<time>", "fetchedAt": ts,
             "coverage": "full non-oracle job list before %s paged by before=oldest createdAt+1ms, deduped by id; rows are shard 02 ids" % T,
             "errors": [], "listedCount": len(allids), "listedPages": pages,
             "listedIdsSha256": hashlib.sha256("\n".join(allids).encode()).hexdigest(),
             "listedIdsSha256Note": "sha256 of the sorted lowercase ids joined by \\n, no trailing newline",
             "columns": ["id", "createdAt", "template"],
             "rows": [[i, listed[i]["createdAt"], listed[i].get("template")] for i in mine]}
    for name, obj in (("jobs.json", jobs), ("index.json", index)):
        with open(os.path.join(OUT, name), "w") as f:
            json.dump(obj, f, separators=(",", ":"))
    print("wrote %d rows, %d with errors" % (len(rows), len(errs)))

if __name__ == "__main__":
    main()
