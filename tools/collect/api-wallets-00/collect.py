#!/usr/bin/env python3
"""Collect the IMD API history of the holders in shard 00 of 6.

Shard rule: a wallet w (lowercase, with 0x) is in shard 0 when
    int(sha256(w).hexdigest()[:8], 16) % 6 == 0
over the union of the Ethereum and Robinhood holder lists.

Holders come from sources/chain-ethereum/holders.json and
sources/chain-robinhood/holders.json (landed in the repository).

Reads (all public GETs on https://api.imd.fun, nothing is signed or paid):
  GET /requests/paid-by/<w>                      -> orders.json
  GET /wallets/<w>/earnings?limit=200[&before=n] -> earnings.json (paged by next)
  GET /seats/owners, GET /seats/records (once)   -> seat ownership
  GET /seats/<id>?work=1000&reviews=0&collaborators=0[&workBefore=t]
                                                 -> seats.json (paged by workBefore)
  GET /jobs/<id> (+ /jobs/<id>/result)            -> late-jobs.json
  one row per wallet                              -> wallets.json

Resumable: every HTTP answer is cached on disk (default
test/scratch/api-wallets-00-cache, never submitted) keyed by URL; a rerun reuses
the cache and only reads what is missing or failed. Use --refresh-errors to
retry cached failures.

Rate: at most --rpm reads a minute (default 80, under the 90 asked; the API
allows 120 per IP). On 429 it waits Retry-After and retries; a read that still
fails after the retries is recorded in the output's errors.

Python 3 standard library only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.imd.fun"
T_ISO = "2026-11-25T11:00:00Z"
T = dt.datetime(2026, 11, 25, 11, 0, 0, tzinfo=dt.timezone.utc)
SHARD = 0
SHARDS = 6
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT = os.path.join(ROOT, "sources", "api-wallets-00")
HOLDER_FILES = {
    "ethereum": os.path.join(ROOT, "sources", "chain-ethereum", "holders.json"),
    "robinhood": os.path.join(ROOT, "sources", "chain-robinhood", "holders.json"),
}
UA = "imd-genesis-snapshot-collector/1.0"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        # fromisoformat in older Pythons dislikes some fractional widths
        try:
            base, frac = s.split(".", 1)
            tz = ""
            for sep in ("+", "-"):
                if sep in frac:
                    frac, tz = frac.split(sep, 1)
                    tz = sep + tz
                    break
            d = dt.datetime.fromisoformat(base + "." + (frac + "000000")[:6] + tz)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def in_shard(w):
    return int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % SHARDS == SHARD


def holders_digest(rows):
    lines = sorted("%s:%s\n" % (w, b) for w, b in rows)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def load_holders():
    info = {}
    wallets = {}
    for chain, path in HOLDER_FILES.items():
        with open(path) as f:
            d = json.load(f)
        cols = d["columns"]
        wi, bi = cols.index("wallet"), cols.index("balanceRaw")
        rows = [(r[wi].lower(), str(r[bi])) for r in d["rows"]]
        rows = [(w, b) for w, b in rows if int(b) > 0]
        info[chain] = {
            "file": os.path.relpath(path, ROOT),
            "count": len(rows),
            "holdersDigest": holders_digest(rows),
            "fileSha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
        }
        for w, b in rows:
            wallets.setdefault(w, {})[chain] = b
    return info, wallets


class Client:
    def __init__(self, cache, rpm, refresh_errors, offline=False):
        self.cache = cache
        os.makedirs(cache, exist_ok=True)
        self.interval = 60.0 / rpm
        self.last = 0.0
        self.refresh_errors = refresh_errors
        self.offline = offline
        self.reads = 0
        self.cached = 0
        self.max_at = None
        self.sections = []

    def take(self):
        """Latest readAt among the answers used since the last call (the section's fetch time)."""
        t, self.max_at = self.max_at, None
        t = t or now_iso()
        self.sections.append(t)
        return t

    def _seen(self, t):
        if t and (self.max_at is None or t > self.max_at):
            self.max_at = t

    def _path(self, url):
        return os.path.join(self.cache, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def get(self, path):
        """Return (status, body_json_or_None, error_or_None, readAt)."""
        url = API + path
        cp = self._path(url)
        if os.path.exists(cp):
            with open(cp) as f:
                c = json.load(f)
            if c.get("error") is None or not self.refresh_errors:
                self.cached += 1
                self._seen(c["readAt"])
                return c["status"], c["body"], c["error"], c["readAt"]
        if self.offline:
            return None, None, "not in cache (offline)", None
        status, body, err = None, None, None
        attempts = 0
        while True:
            attempts += 1
            wait = self.last + self.interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.reads += 1
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status = r.status
                    raw = r.read()
                body = json.loads(raw)
                err = None
                break
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read()
                try:
                    body = json.loads(raw)
                except Exception:
                    body = None
                if e.code == 429 and attempts <= 8:
                    ra = e.headers.get("Retry-After")
                    try:
                        s = float(ra)
                    except (TypeError, ValueError):
                        s = 30.0
                    sys.stderr.write("429 on %s, waiting %ss\n" % (path, s))
                    time.sleep(max(s, 1.0))
                    continue
                if e.code >= 500 and attempts <= 3:
                    time.sleep(5 * attempts)
                    continue
                err = "HTTP %s %s" % (e.code, (body if body is not None else raw[:200].decode("utf-8", "replace")))
                break
            except Exception as e:  # network, timeout, bad JSON
                if attempts <= 3:
                    time.sleep(5 * attempts)
                    continue
                status, body, err = None, None, "%s: %s" % (type(e).__name__, e)
                break
        read_at = now_iso()
        if err is not None and not isinstance(err, str):
            err = json.dumps(err)
        with open(cp + ".tmp", "w") as f:
            json.dump({"url": url, "status": status, "body": body, "error": err, "readAt": read_at}, f)
        os.replace(cp + ".tmp", cp)
        self._seen(read_at)
        return status, body, err, read_at


def write_source(name, source, coverage, errors, columns, rows, fetched_at):
    obj = {"source": source, "fetchedAt": fetched_at, "coverage": coverage,
           "errors": errors, "columns": columns, "rows": rows}
    p = os.path.join(OUT, name)
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(p + ".tmp", p)


def lower_addr(v):
    if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
        return v.lower()
    return v


RESULT_ID_KEYS = ("requestId", "oracleRequestId", "workflowId", "scheduleId", "goalId", "launchId", "id")
RESULT_ID_LIST_KEYS = ("requests", "requestIds", "questions")


def result_id(res):
    """The oracle request or workflow an order's result names, as the API gives it."""
    if not isinstance(res, dict):
        return None
    for k in RESULT_ID_KEYS:
        v = res.get(k)
        if isinstance(v, (str, int)) and v != "":
            return str(v)
    for k in RESULT_ID_LIST_KEYS:
        v = res.get(k)
        if isinstance(v, list) and v:
            ids = []
            for x in v:
                if isinstance(x, dict):
                    x = x.get("id") or x.get("requestId")
                if x is not None:
                    ids.append(str(x))
            if ids:
                return ",".join(ids)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, "test", "scratch", "api-wallets-00-cache"))
    ap.add_argument("--rpm", type=float, default=80.0)
    ap.add_argument("--refresh-errors", action="store_true")
    ap.add_argument("--offline", action="store_true", help="build outputs from the cache only")
    ap.add_argument("--limit", type=int, default=0, help="only the first N shard wallets (testing)")
    a = ap.parse_args()
    if a.rpm > 90:
        sys.exit("--rpm above 90 is not allowed")
    os.makedirs(OUT, exist_ok=True)
    cl = Client(a.cache, a.rpm, a.refresh_errors, a.offline)
    started = now_iso()

    hinfo, allw = load_holders()
    shard = sorted(w for w in allw if in_shard(w))
    if a.limit:
        shard = shard[: a.limit]
    print("holders: ethereum %d (%s), robinhood %d (%s); merged %d; shard %d of %d: %d wallets" % (
        hinfo["ethereum"]["count"], hinfo["ethereum"]["holdersDigest"],
        hinfo["robinhood"]["count"], hinfo["robinhood"]["holdersDigest"],
        len(allw), SHARD, SHARDS, len(shard)), flush=True)
    shard_set = set(shard)
    wallet_errors = {w: [] for w in shard}
    base_cov = {"shard": "%d of %d" % (SHARD, SHARDS),
                "shardRule": "int(sha256(w).hexdigest()[:8], 16) % 6 == 0, w lowercase with 0x",
                "holdersFrom": "landed lists (not rebuilt from Transfer logs)",
                "holders": hinfo, "mergedHolders": len(allw), "wallets": len(shard),
                "snapshotTime": T_ISO, "beforeT": "createdAt strictly before snapshotTime"}

    # ---------- orders ----------
    o_cols = ["wallet", "orderId", "action", "status", "createdAt", "paidAt", "paymentStatus",
              "paymentReason", "paymentTx", "resultKind", "resultJobId", "resultId", "continues",
              "beforeT", "result"]
    o_rows, o_err = [], []
    order_count, truncated = {}, {}
    late_candidates = []
    t_minus_24 = T - dt.timedelta(hours=24)
    for i, w in enumerate(shard):
        st, body, err, ra = cl.get("/requests/paid-by/" + w)
        if err or not isinstance(body, dict) or not isinstance(body.get("orders"), list):
            e = err or "unexpected body"
            o_err.append({"wallet": w, "read": "/requests/paid-by/" + w, "error": e})
            wallet_errors[w].append("orders: " + e)
            order_count[w] = None
            truncated[w] = None
            continue
        orders = body["orders"]
        order_count[w] = len(orders)
        truncated[w] = len(orders) >= 100
        for o in orders:
            pay = o.get("payment") or {}
            res = o.get("result")
            rd = res if isinstance(res, dict) else {}
            created = o.get("createdAt")
            cdt = parse_time(created)
            before = (cdt < T) if cdt else None
            cont = None
            for k in ("parentJobId", "continues", "continuesJobId"):
                if rd.get(k):
                    cont = rd.get(k)
                    break
            job_id = rd.get("jobId")
            o_rows.append([w, o.get("orderId"), o.get("action"), o.get("status"), created, o.get("paidAt"),
                           pay.get("status"), pay.get("reason"), lower_addr(pay.get("transactionHash")),
                           rd.get("kind"), job_id, result_id(rd), cont, before, res])
            if job_id and cdt and t_minus_24 <= cdt < T:
                late_candidates.append((w, o.get("orderId"), created, job_id))
        if (i + 1) % 100 == 0:
            print("orders %d/%d (reads %d, cached %d)" % (i + 1, len(shard), cl.reads, cl.cached), flush=True)
    write_source("orders.json", API + "/requests/paid-by/<wallet>",
                 dict(base_cov, note="newest first, at most 100 per wallet (truncated in wallets.json); "
                      "result is the API's result object unchanged; resultId is the request/workflow/schedule "
                      "id it names; continues is the parent job it names, null when the order body does not say; "
                      "paymentTx lowercased", ordersRead=len(shard) - len(o_err)),
                 o_err, o_cols, o_rows, cl.take())
    print("orders.json: %d rows, %d errors" % (len(o_rows), len(o_err)), flush=True)

    # ---------- earnings ----------
    e_rows_raw, e_err, earn_count = [], [], {}
    keys = []
    for i, w in enumerate(shard):
        before = None
        n = 0
        seen_pages = set()
        ok = True
        while True:
            q = "/wallets/%s/earnings?limit=200" % w + ("&before=%s" % urllib.parse.quote(str(before)) if before is not None else "")
            st, body, err, ra = cl.get(q)
            if err or not isinstance(body, dict) or not isinstance(body.get("earnings"), list):
                e = err or "unexpected body"
                e_err.append({"wallet": w, "read": q, "error": e})
                wallet_errors[w].append("earnings: " + e)
                ok = False
                break
            for row in body["earnings"]:
                for k in row:
                    if k not in keys:
                        keys.append(k)
                e_rows_raw.append((w, row))
                n += 1
            nxt = body.get("next")
            if nxt is None or nxt in seen_pages or not body["earnings"]:
                break
            seen_pages.add(nxt)
            before = nxt
        earn_count[w] = n if ok else None
        if (i + 1) % 100 == 0:
            print("earnings %d/%d (reads %d, cached %d)" % (i + 1, len(shard), cl.reads, cl.cached), flush=True)
    # The nested 'launch' object is the same for every wallet of a launch except
    # 'reward' (that wallet's share). To keep the file a sane size it is split
    # losslessly: earnings.json keeps every top-level field plus launch.reward;
    # earnings-launches.json keeps, once per launchId, the 'token' and the rest
    # of 'launch'. Row = earnings row + launches[launchId], with launch.reward put back.
    row_keys = [k for k in keys if k not in ("launch", "token")]
    e_at = cl.take()
    e_cols = ["wallet"] + row_keys + ["tokenAddress", "launchReward"]
    e_rows = []
    launches = {}
    launch_conflicts = []
    for w, row in e_rows_raw:
        lo = row.get("launch")
        tok = row.get("token")
        reward = lo.get("reward") if isinstance(lo, dict) else None
        shared = {k: v for k, v in lo.items() if k != "reward"} if isinstance(lo, dict) else lo
        lid = row.get("launchId")
        rec = {"token": tok, "launch": shared, "launchHasReward": isinstance(lo, dict) and "reward" in lo}
        if lid in launches and json.dumps(launches[lid], sort_keys=True) != json.dumps(rec, sort_keys=True):
            launch_conflicts.append({"launchId": lid, "wallet": w,
                                     "error": "launch/token object differs from the first wallet's; kept the first"})
        launches.setdefault(lid, rec)
        e_rows.append([w] + [lower_addr(row.get(k)) for k in row_keys] +
                      [lower_addr(tok.get("address")) if isinstance(tok, dict) else None, reward])
    write_source("earnings.json", API + "/wallets/<wallet>/earnings?limit=200, paged with before=<next>",
                 dict(base_cov, earningsFields=keys,
                      note="each earnings row's top-level fields as the API gives them (a field a row lacks is null), "
                      "amounts as raw integer strings; 'token' and 'launch' are moved to earnings-launches.json once per "
                      "launchId (identical across wallets), except launch.reward, the wallet's own share, kept here as "
                      "launchReward; tokenAddress = token.address. Earnings are read now, not as of T: each row's 'at' "
                      "says when"),
                 e_err, e_cols, e_rows, e_at)
    print("earnings.json: %d rows, %d errors" % (len(e_rows), len(e_err)), flush=True)
    el_cols = ["launchId", "token", "launch"]
    el_rows = [[lid, v["token"], v["launch"]] for lid, v in sorted(launches.items(), key=lambda x: str(x[0]))]
    write_source("earnings-launches.json", API + "/wallets/<wallet>/earnings (the token and launch objects of its rows)",
                 dict(base_cov, note="one row per launchId seen in earnings.json; launch is the API's launch object "
                      "without 'reward' (per wallet, in earnings.json launchReward)"),
                 launch_conflicts, el_cols, el_rows, e_at)
    print("earnings-launches.json: %d rows, %d conflicts" % (len(el_rows), len(launch_conflicts)), flush=True)

    # ---------- seats ----------
    s_err = []
    st, owners_b, err_o, owners_at = cl.get("/seats/owners")
    st, records_b, err_r, records_at = cl.get("/seats/records")
    owned = {w: [] for w in shard}
    owners = []
    if err_o or not isinstance(owners_b, dict):
        s_err.append({"read": "/seats/owners", "error": err_o or "unexpected body"})
    else:
        owners = owners_b.get("owners") or []
        for tid, ow in enumerate(owners):
            if isinstance(ow, str) and ow.lower() in shard_set:
                owned[ow.lower()].append(str(tid))
    recorded = set()
    if err_r or not isinstance(records_b, dict):
        s_err.append({"read": "/seats/records", "error": err_r or "unexpected body"})
    else:
        recorded = {str(s.get("tokenId")) for s in records_b.get("seats") or []}
    s_cols = ["tokenId", "owner", "agentId", "attempts", "accepted", "rejected", "failed", "pending",
              "workRows", "workBeforeT", "acceptedBeforeT", "jobsBeforeT", "readAt"]
    s_rows = []
    targets = sorted({t for w in shard for t in owned[w] if t in recorded}, key=int)
    for tid in targets:
        work = []
        wb = None
        detail = None
        failed = False
        read_at = None
        seen = set()
        while True:
            q = "/seats/%s?work=1000&reviews=0&collaborators=0" % tid + (
                "&workBefore=%s" % urllib.parse.quote(wb) if wb else "")
            st, body, err, ra = cl.get(q)
            read_at = read_at or ra
            if err or not isinstance(body, dict):
                s_err.append({"tokenId": tid, "read": q, "error": err or "unexpected body"})
                failed = True
                break
            if detail is None:
                detail = body
            page = body.get("work") or []
            work.extend(page)
            if len(page) < 1000:
                break
            last = page[-1].get("submittedAt")
            if not last or last in seen:
                break
            seen.add(last)
            wb = last
        if detail is None:
            ow = owners[int(tid)].lower() if int(tid) < len(owners) and owners[int(tid)] else None
            if ow in wallet_errors:
                wallet_errors[ow].append("seat %s: read failed" % tid)
            s_rows.append([tid, ow, None, None, None, None, None, None, None, None, None, None, read_at])
            continue
        # de-duplicate rows repeated at page edges
        uniq = {}
        for r in work:
            k = (r.get("submissionHash"), r.get("jobId"), r.get("nodeKey"), r.get("submittedAt"))
            uniq[k] = r
        work = list(uniq.values())
        bt = [r for r in work if (parse_time(r.get("submittedAt")) or T) < T]
        s_rows.append([tid, lower_addr(detail.get("owner")), detail.get("agentId"), detail.get("attempts"),
                       detail.get("accepted"), detail.get("rejected"), detail.get("failed"), detail.get("pending"),
                       len(work), len(bt), sum(1 for r in bt if r.get("status") == "accepted"),
                       sorted({r.get("jobId") for r in bt if r.get("jobId")}), read_at])
        if failed:
            ow = lower_addr(detail.get("owner"))
            if ow in wallet_errors:
                wallet_errors[ow].append("seat %s: a work page failed, counts partial" % tid)
    write_source("seats.json", API + "/seats/owners, /seats/records, /seats/<tokenId>?work=1000&reviews=0&collaborators=0 paged by workBefore",
                 dict(base_cov, ownersReadAt=owners_at, recordsReadAt=records_at,
                      seatsOwnedByShard=sum(len(v) for v in owned.values()), seatsWithRecord=len(targets),
                      note="ownership is as of the read (readAt), not of T; attempts/accepted/rejected/failed/pending "
                      "are the whole record now; workBeforeT/acceptedBeforeT/jobsBeforeT count work rows submitted "
                      "strictly before T; jobsBeforeT is the list of distinct job ids"),
                 s_err, s_cols, s_rows, cl.take())
    print("seats.json: %d rows, %d errors" % (len(s_rows), len(s_err)), flush=True)

    # ---------- late jobs ----------
    l_cols = ["wallet", "orderId", "orderCreatedAt", "jobId", "jobCreatedAt", "jobCreatedAtOrAfterT",
              "jobState", "detail", "result"]
    l_rows, l_err = [], []
    for w, oid, created, jid in late_candidates:
        st, body, err, ra = cl.get("/jobs/" + jid)
        if err or not isinstance(body, dict):
            l_err.append({"wallet": w, "orderId": oid, "jobId": jid, "read": "/jobs/" + jid, "error": err or "unexpected body"})
            l_rows.append([w, oid, created, jid, None, None, None, None, None])
            continue
        jc = body.get("createdAt")
        jdt = parse_time(jc)
        late = (jdt >= T) if jdt else None
        detail, result = None, None
        if late:
            detail = body
            st2, rb, err2, ra2 = cl.get("/jobs/%s/result" % jid)
            if err2:
                l_err.append({"wallet": w, "orderId": oid, "jobId": jid, "read": "/jobs/%s/result" % jid, "error": err2})
            else:
                result = rb
        l_rows.append([w, oid, created, jid, jc, late, body.get("state"), detail, result])
    write_source("late-jobs.json", API + "/jobs/<id> and /jobs/<id>/result",
                 dict(base_cov, window="orders created in [T-24h, T) that name a job",
                      candidates=len(late_candidates),
                      note="detail and result are filled only when the job was created at or after T; else null"),
                 l_err, l_cols, l_rows, cl.take())
    print("late-jobs.json: %d rows (%d created at or after T), %d errors" % (
        len(l_rows), sum(1 for r in l_rows if r[5]), len(l_err)), flush=True)

    # ---------- wallets ----------
    w_cols = ["wallet", "balanceEthereumRaw", "balanceRobinhoodRaw", "orders", "ordersBeforeT", "truncated",
              "earnings", "seatsOwned", "errors"]
    obt = {}
    for r in o_rows:
        if r[13]:
            obt[r[0]] = obt.get(r[0], 0) + 1
    w_rows = []
    for w in shard:
        w_rows.append([w, allw[w].get("ethereum"), allw[w].get("robinhood"), order_count[w],
                       (obt.get(w, 0) if order_count[w] is not None else None), truncated[w],
                       earn_count[w], owned[w] if owners else None, wallet_errors[w]])
    write_source("wallets.json", "derived from orders.json, earnings.json, seats.json of this shard",
                 dict(base_cov, note="orders = orders the API returned (null when the read failed); truncated = true at "
                      "100 orders (the list stops at the newest 100); earnings = earnings rows read; seatsOwned = "
                      "token ids owned now per /seats/owners (null when it failed)"),
                 [], w_cols, w_rows, max(cl.sections) if cl.sections else started)
    print("wallets.json: %d rows; truncated %d; wallets with errors %d" % (
        len(w_rows), sum(1 for r in w_rows if r[5]), sum(1 for r in w_rows if r[8])), flush=True)
    print("http reads this run %d, cached answers used %d" % (cl.reads, cl.cached))


if __name__ == "__main__":
    main()
