#!/usr/bin/env python3
"""Collect the IMD API history of the holders in shard 4 of 6 (Kekborn Genesis snapshot).

Shard rule: wallet w (lowercase, with 0x) over the union of both chains' holders,
int(sha256(w).hexdigest()[:8], 16) % 6 == 4.

Holders come from sources/chain-ethereum/holders.json and sources/chain-robinhood/holders.json.

Reads only public GET routes of https://api.imd.fun, at most ~85 a minute, honouring
Retry-After on 429. Every response is cached on disk (one file per URL), so the script
is resumable: run it again and it continues where it stopped. --max-seconds stops it
cleanly before a time limit; the output files are written only once every read is done
(or with --finalize, from whatever the cache holds, failed reads listed as errors).

Python 3, standard library only. Sends no transaction, signs and pays for nothing.

Usage (from the repository root):
  python3 tools/collect/api-wallets-04/collect.py [--cache DIR] [--max-seconds N] [--finalize]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SHARD = 4
SHARDS = 6
API = "https://api.imd.fun"
T_ISO = "2026-11-25T11:00:00Z"
T = dt.datetime(2026, 11, 25, 11, 0, 0, tzinfo=dt.timezone.utc)
LATE_FROM = T - dt.timedelta(hours=24)
MIN_INTERVAL = 60.0 / 85  # stay under 90 reads a minute
OUT = os.path.join("sources", "api-wallets-04")
HOLDER_FILES = {
    "ethereum": os.path.join("sources", "chain-ethereum", "holders.json"),
    "robinhood": os.path.join("sources", "chain-robinhood", "holders.json"),
}
UA = "kekborn-genesis-snapshot/1.0 (python urllib)"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if s is None:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def shard_of(w):
    return int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % SHARDS


def holders_digest(rows):
    lines = sorted("%s:%s\n" % (r[0].lower(), r[1]) for r in rows)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def load_holders():
    info = {}
    wallets = set()
    for chain, path in HOLDER_FILES.items():
        with open(path) as f:
            d = json.load(f)
        cols = d["columns"]
        iw, ib = cols.index("wallet"), cols.index("balanceRaw")
        rows = [(r[iw].lower(), str(r[ib])) for r in d["rows"]]
        info[chain] = {
            "file": path.replace(os.sep, "/"),
            "fileSha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
            "count": len(rows),
            "holdersDigest": holders_digest(rows),
            "positive": all(int(b) > 0 for _, b in rows),
        }
        wallets.update(w for w, _ in rows)
    mine = sorted(w for w in wallets if shard_of(w) == SHARD)
    return info, len(wallets), mine


class Reader:
    def __init__(self, cache, deadline):
        self.cache = cache
        self.deadline = deadline
        self.last = 0.0
        self.reads = 0
        self.cached = 0
        os.makedirs(cache, exist_ok=True)

    def _path(self, path):
        return os.path.join(self.cache, hashlib.sha256(path.encode()).hexdigest() + ".json")

    def get(self, path):
        """Return (status, body_or_None, fetchedAt, error). Cached results are reused,
        including 4xx answers other than 429 (they are the source's answer)."""
        cp = self._path(path)
        if os.path.exists(cp):
            with open(cp) as f:
                c = json.load(f)
            self.cached += 1
            return c["status"], c["body"], c["fetchedAt"], c["error"]
        if time.time() > self.deadline:
            raise TimeoutError("deadline")
        tries = 0
        err = None
        status = None
        body = None
        while tries < 6:
            tries += 1
            wait = self.last + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.reads += 1
            req = urllib.request.Request(API + path, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status = r.status
                    body = json.loads(r.read().decode())
                    err = None
                    break
            except urllib.error.HTTPError as e:
                status = e.code
                txt = e.read().decode(errors="replace")[:500]
                if e.code == 429:
                    ra = e.headers.get("Retry-After")
                    try:
                        sl = float(ra)
                    except (TypeError, ValueError):
                        sl = 30.0
                    err = "429 too_many_reads (Retry-After %s)" % ra
                    time.sleep(min(max(sl, 1.0), 120.0))
                    continue
                if e.code >= 500:
                    err = "HTTP %d: %s" % (e.code, txt)
                    time.sleep(5 * tries)
                    continue
                try:
                    body = json.loads(txt)
                except ValueError:
                    body = None
                err = "HTTP %d: %s" % (e.code, txt)
                break
            except Exception as e:  # network fault
                status = None
                err = "%s: %s" % (type(e).__name__, e)
                time.sleep(5 * tries)
                continue
        fetched = now_iso()
        if err is None or (status is not None and 400 <= status < 500 and status != 429):
            with open(cp + ".tmp", "w") as f:
                json.dump({"path": path, "status": status, "body": body, "fetchedAt": fetched, "error": err}, f)
            os.replace(cp + ".tmp", cp)
        return status, body, fetched, err


def compact(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


LAUNCH_FIELDS = ["launchId", "status", "chainId", "kind", "token"]
EARN_COLS = ["wallet", "launchNumber", "amount", "at", "rewardWork", "rewardVersion", "rewardOther"]
LAUNCH_COLS = ["launchNumber"] + LAUNCH_FIELDS + ["launch"]


def split_earnings(ecols, erows):
    """Split the API's earnings rows losslessly into per-wallet rows and one row per launch.

    The API repeats the whole launch (token, merkle root, pools, ...) in every earnings row,
    which made one file of ~43 MB. Fields that are the same for every row of a launch go to
    earnings-launches.json once. What stays per row: wallet, launchNumber, amount, at and
    launch.reward. launch.reward is {work, second, version} with work + second == amount;
    it is stored as rewardWork and rewardVersion (second = amount - work). A reward that is
    not of that shape is kept whole as rewardOther = {"reward": <as given>} (rewardWork and
    rewardVersion null then). join_earnings() rebuilds the rows as given.
    Raises ValueError if a launch-level field differs between rows of the same launch.
    """
    ix = {c: i for i, c in enumerate(ecols)}
    launches, rows = {}, []
    for r in erows:
        g = lambda c: r[ix[c]] if c in ix else None
        ln = g("launch")
        rest = dict(ln) if isinstance(ln, dict) else ln
        has_reward = isinstance(rest, dict) and "reward" in rest
        reward = rest.pop("reward") if has_reward else None
        key = g("launchNumber")
        lrow = [key] + [g(c) for c in LAUNCH_FIELDS] + [rest]
        if key in launches and launches[key] != lrow:
            raise ValueError("launch %r differs between earnings rows" % (key,))
        launches[key] = lrow
        rw = rv = other = None
        if (isinstance(reward, dict) and set(reward) == {"work", "second", "version"}
                and g("amount") is not None
                and int(reward["work"]) + int(reward["second"]) == int(g("amount"))
                and str(int(reward["work"])) == reward["work"] and str(int(reward["second"])) == reward["second"]):
            rw, rv = reward["work"], reward["version"]
        else:
            other = {"reward": reward} if has_reward else {"noReward": True}
        rows.append([r[ix["wallet"]], key, g("amount"), g("at"), rw, rv, other])
    extra = [c for c in ecols if c not in ["wallet", "launchNumber", "amount", "at", "launch"] + LAUNCH_FIELDS]
    if extra:
        raise ValueError("unexpected earnings fields %r" % extra)
    lrows = sorted(launches.values(), key=lambda x: (x[0] is None, x[0]))
    return rows, lrows


def join_earnings(rows, lrows):
    """Inverse of split_earnings: the API's rows (dicts) with wallet."""
    L = {lr[0]: lr for lr in lrows}
    out = []
    for w, num, amount, at, rw, rv, other in rows:
        lr = L[num]
        d = {"wallet": w, "launchNumber": num}
        d.update(zip(LAUNCH_FIELDS, lr[1:6]))
        d["amount"], d["at"] = amount, at
        ln = lr[6]
        if isinstance(ln, dict):
            ln = dict(ln)
            if other is None:
                ln["reward"] = {"work": rw, "second": str(int(amount) - int(rw)), "version": rv}
            elif "reward" in other:
                ln["reward"] = other["reward"]
        d["launch"] = ln
        out.append(d)
    return out


def write_source(name, source, coverage, errors, columns, rows):
    os.makedirs(OUT, exist_ok=True)
    doc = {"source": source, "fetchedAt": now_iso(), "coverage": coverage, "errors": errors,
           "columns": columns, "rows": rows}
    p = os.path.join(OUT, name)
    with open(p + ".tmp", "w") as f:
        f.write(compact(doc))
        f.write("\n")
    os.replace(p + ".tmp", p)
    return p


def order_row(w, o):
    pay = o.get("payment") or {}
    res = o.get("result") or {}
    kind = res.get("kind")
    job_id = res.get("jobId")
    rid = None
    children = None
    if kind == "oracle":
        rid = res.get("requestId")
    elif kind == "workflow":
        rid = res.get("workflowId")
    elif kind == "schedule":
        rid = res.get("scheduleId")
    elif kind == "oracle-group":
        reqs = res.get("requests") or []
        rid = [q.get("requestId") for q in reqs]
        job_id = [q.get("jobId") for q in reqs]
    if kind == "goal" and res.get("children") is not None:
        children = res.get("children")
    created = o.get("createdAt")
    ct = parse_ts(created)
    before = None if ct is None else ct < T
    return [w, o.get("orderId"), o.get("action"), o.get("status"), created, o.get("paidAt"),
            pay.get("status"), pay.get("transactionHash"), kind, job_id, rid, res.get("continues"),
            before, pay.get("reason"), children]


ORDER_COLS = ["wallet", "orderId", "action", "status", "createdAt", "paidAt", "paymentStatus", "paymentTx",
              "resultKind", "resultJobId", "resultId", "continues", "beforeT", "paymentReason", "resultChildren"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "imd-api-wallets-04-cache"))
    ap.add_argument("--max-seconds", type=float, default=1e9)
    ap.add_argument("--finalize", action="store_true", help="write outputs even if reads are left")
    a = ap.parse_args()
    t0 = time.time()
    rd = Reader(a.cache, t0 + a.max_seconds)
    hinfo, union, mine = load_holders()
    print("holders: ethereum %d (digest %s), robinhood %d (digest %s), union %d, shard %d wallets"
          % (hinfo["ethereum"]["count"], hinfo["ethereum"]["holdersDigest"], hinfo["robinhood"]["count"],
             hinfo["robinhood"]["holdersDigest"], union, len(mine)), flush=True)
    pending = False
    werr = {w: [] for w in mine}
    file_errors = {"orders": [], "earnings": [], "seats": [], "late": []}
    orders_rows, earn_rows, earn_cols = [], [], []
    per_wallet = {}

    def safe_get(path):
        nonlocal pending
        try:
            return rd.get(path)
        except TimeoutError:
            pending = True
            return None, None, None, "not read: stopped at --max-seconds, rerun to resume"

    # 1. orders
    for i, w in enumerate(mine):
        st, body, fa, err = safe_get("/requests/paid-by/" + w)
        n = None
        if err or not isinstance(body, dict):
            e = {"wallet": w, "read": "/requests/paid-by/" + w, "error": err or "no body"}
            werr[w].append(e)
            file_errors["orders"].append(e)
        else:
            orders = body.get("orders") or []
            n = len(orders)
            for o in orders:
                orders_rows.append(order_row(w, o))
        per_wallet[w] = {"orders": n}
        if (i + 1) % 100 == 0:
            print("orders %d/%d reads=%d cached=%d %.0fs" % (i + 1, len(mine), rd.reads, rd.cached, time.time() - t0),
                  flush=True)

    # 2. earnings, paged by next
    for i, w in enumerate(mine):
        before = None
        n = 0
        ok = True
        pages = 0
        while True:
            q = {"limit": "200"}
            if before is not None:
                q["before"] = str(before)
            p = "/wallets/%s/earnings?%s" % (w, urllib.parse.urlencode(q))
            st, body, fa, err = safe_get(p)
            pages += 1
            if err or not isinstance(body, dict):
                e = {"wallet": w, "read": p, "error": err or "no body"}
                werr[w].append(e)
                file_errors["earnings"].append(e)
                ok = False
                break
            for r in body.get("earnings") or []:
                for k in r:
                    if k not in earn_cols:
                        earn_cols.append(k)
                earn_rows.append((w, r))
                n += 1
            nx = body.get("next")
            if nx is None or pages > 500:
                break
            before = nx
        per_wallet[w]["earnings"] = n if ok else None
        if (i + 1) % 100 == 0:
            print("earnings %d/%d reads=%d cached=%d %.0fs" % (i + 1, len(mine), rd.reads, rd.cached,
                                                               time.time() - t0), flush=True)

    # 3. seats
    mineset = set(mine)
    seat_rows = []
    owned = {w: [] for w in mine}
    st, owners_body, owners_at, err1 = safe_get("/seats/owners")
    st, records_body, records_at, err2 = safe_get("/seats/records")
    if err1 or err2 or not isinstance(owners_body, dict) or not isinstance(records_body, dict):
        file_errors["seats"].append({"read": "/seats/owners + /seats/records", "error": err1 or err2 or "no body"})
        seats_known = False
    else:
        seats_known = True
        owners = owners_body.get("owners") or []
        for s in records_body.get("seats") or []:
            tid = str(s.get("tokenId"))
            try:
                ow = owners[int(tid)]
            except (ValueError, IndexError):
                ow = None
            ow = ow.lower() if isinstance(ow, str) else None
            if ow not in mineset:
                continue
            owned[ow].append(tid)
            work_before = None
            seen = set()
            n_before = acc_before = 0
            jobs_before = set()
            detail = None
            serr = None
            read_at = None
            for _ in range(200):
                q = {"work": "1000", "reviews": "0", "collaborators": "0"}
                if work_before:
                    q["workBefore"] = work_before
                p = "/seats/%s?%s" % (tid, urllib.parse.urlencode(q))
                st, body, fa, err = safe_get(p)
                if err or not isinstance(body, dict):
                    serr = {"tokenId": tid, "wallet": ow, "read": p, "error": err or "no body"}
                    break
                if detail is None:
                    detail = body
                    read_at = fa
                work = body.get("work") or []
                new = 0
                for r in work:
                    key = r.get("submissionHash") or compact(r)
                    if key in seen:
                        continue
                    seen.add(key)
                    new += 1
                    sa = parse_ts(r.get("submittedAt"))
                    if sa is not None and sa < T:
                        n_before += 1
                        if r.get("status") == "accepted":
                            acc_before += 1
                        if r.get("jobId"):
                            jobs_before.add(r["jobId"])
                if len(work) < 1000 or new == 0:
                    break
                work_before = work[-1].get("submittedAt")
                if not work_before:
                    break
            if serr:
                werr[ow].append(serr)
                file_errors["seats"].append(serr)
                seat_rows.append([tid, ow, None, None, None, None, None, None, None])
            else:
                seat_rows.append([tid, ow, detail.get("attempts"), detail.get("accepted"), detail.get("rejected"),
                                  n_before, acc_before, len(jobs_before), read_at])

    # 4. late jobs: orders created in [T-24h, T) that name a job
    late_rows = []
    for r in orders_rows:
        ct = parse_ts(r[4])
        if ct is None or not (LATE_FROM <= ct < T):
            continue
        jids = r[9] if isinstance(r[9], list) else [r[9]]
        for jid in jids:
            if not jid:
                continue
            p = "/jobs/%s" % jid
            st, body, fa, err = safe_get(p)
            if err or not isinstance(body, dict):
                e = {"wallet": r[0], "orderId": r[1], "read": p, "error": err or "no body"}
                werr[r[0]].append(e)
                file_errors["late"].append(e)
                late_rows.append([r[0], r[1], r[4], jid, None, None, None, None, None, fa])
                continue
            jc = body.get("createdAt")
            jct = parse_ts(jc) if isinstance(jc, str) else None
            after = None if jct is None else jct >= T
            detail = result = None
            rnote = None
            if after:
                detail = body
                rp = "/jobs/%s/result" % jid
                st2, rb, fa2, err2 = safe_get(rp)
                if err2:
                    rnote = err2
                    if st2 is None or st2 >= 500 or st2 == 429:
                        e = {"wallet": r[0], "orderId": r[1], "read": rp, "error": err2}
                        werr[r[0]].append(e)
                        file_errors["late"].append(e)
                result = rb
            elif after is None:
                rnote = "job detail names no createdAt"
            late_rows.append([r[0], r[1], r[4], jid, jc, after, detail, result, rnote, fa])

    if pending and not a.finalize:
        print("STOPPED at --max-seconds with reads left: reads=%d cached=%d; rerun to resume (no output written)"
              % (rd.reads, rd.cached), flush=True)
        return 3

    hsrc = {"holdersFrom": "sources/chain-ethereum/holders.json and sources/chain-robinhood/holders.json (landed files, not rebuilt)",
            "holderLists": hinfo, "unionWallets": union,
            "shard": {"index": SHARD, "of": SHARDS, "rule": "int(sha256(w).hexdigest()[:8],16) % 6 == 4, w lowercase with 0x",
                      "wallets": len(mine)},
            "snapshotTime": T_ISO}

    # outputs
    files = {}
    files["orders.json"] = write_source(
        "orders.json", API + "/requests/paid-by/<wallet> (GET, public)",
        dict(hsrc, note="Every order the route returns, newest first, at most 100 per wallet (wallets.json truncated=true "
                        "when 100). beforeT = createdAt strictly before %s. For resultKind oracle-group resultJobId and "
                        "resultId are lists in the order given. resultId: oracle requestId, workflow workflowId, schedule "
                        "scheduleId, else null. resultChildren: a goal's children object as given, else null." % T_ISO,
             rows=len(orders_rows), wallets=len(mine)),
        file_errors["orders"], ORDER_COLS, orders_rows)
    ecols = ["wallet"] + earn_cols
    erows = [[w] + [r.get(k) for k in earn_cols] for w, r in earn_rows]
    srows, lrows = split_earnings(ecols, erows)
    files["earnings.json"] = write_source(
        "earnings.json", API + "/wallets/<wallet>/earnings?limit=200 (GET, public), paged by before=<next>",
        dict(hsrc, note="Each earnings row the API returned (all pages), one row each, not filtered by time: 'at' "
                        "says when. Amounts are the API's raw integer strings. Launch-level fields (launchId, status, "
                        "chainId, kind, token, launch without reward), identical in every row of a launch, are in "
                        "earnings-launches.json keyed by launchNumber. launch.reward per row: rewardWork, "
                        "rewardVersion, and second = amount - rewardWork (true for every row of that shape); any "
                        "other reward is kept whole in rewardOther. join_earnings() in collect.py rebuilds the rows "
                        "exactly as given.", apiColumns=ecols,
             rows=len(srows), launchesFile="earnings-launches.json"),
        file_errors["earnings"], EARN_COLS, srows)
    files["earnings-launches.json"] = write_source(
        "earnings-launches.json", API + "/wallets/<wallet>/earnings?limit=200 (GET, public): launch fields of the earnings rows",
        dict(hsrc, note="One row per launch named by earnings.json, with the fields as given that every earnings row of "
                        "that launch repeats; 'launch' is the launch object as given minus its per-wallet 'reward'.",
             rows=len(lrows)),
        [], LAUNCH_COLS, lrows)
    files["seats.json"] = write_source(
        "seats.json", API + "/seats/owners, /seats/records (once each) and /seats/<tokenId>?work=1000&reviews=0&collaborators=0 paged by workBefore",
        dict(hsrc, ownersReadAt=owners_at, ownersAsOf=(owners_body or {}).get("at") if isinstance(owners_body, dict) else None,
             recordsReadAt=records_at, seatsKnown=seats_known,
             note="Seats in /seats/records whose owner in /seats/owners (indexed by token id) is a wallet of this shard. "
                  "Ownership is as of the read, not of T. attempts/accepted/rejected are the whole record at readAt. "
                  "workBeforeT, acceptedBeforeT, jobsBeforeT count work rows (deduplicated by submissionHash) with "
                  "submittedAt strictly before T; jobsBeforeT counts distinct jobIds.",
             rows=len(seat_rows)),
        file_errors["seats"],
        ["tokenId", "owner", "attempts", "accepted", "rejected", "workBeforeT", "acceptedBeforeT", "jobsBeforeT", "readAt"],
        seat_rows)
    files["late-jobs.json"] = write_source(
        "late-jobs.json", API + "/jobs/<id> and /jobs/<id>/result (GET, public)",
        dict(hsrc, window=[LATE_FROM.strftime("%Y-%m-%dT%H:%M:%SZ"), T_ISO],
             note="One row per job named by an order of this shard created in the 24 hours before T. detail and result "
                  "are kept only when the job was created at or after T (else null). resultNote holds the API's "
                  "answer when the result was not served (e.g. a job still running).",
             rows=len(late_rows)),
        file_errors["late"],
        ["wallet", "orderId", "orderCreatedAt", "jobId", "jobCreatedAt", "jobCreatedAtOrAfterT", "detail", "result",
         "resultNote", "readAt"],
        late_rows)
    wrows = []
    for w in mine:
        pw = per_wallet.get(w, {})
        n = pw.get("orders")
        wrows.append([w, n, None if n is None else n >= 100, pw.get("earnings"),
                      len(owned[w]) if seats_known else None, sorted(owned[w], key=int) if seats_known else None,
                      werr[w]])
    files["wallets.json"] = write_source(
        "wallets.json", "summary of orders.json, earnings.json and seats.json per wallet of shard 4",
        dict(hsrc, note="orders: rows /requests/paid-by returned (null if the read failed); truncated: true at 100 "
                        "(the list stops at the newest 100); earnings: rows over all pages; seatsOwned: seats with a "
                        "record owned at the read; errors: reads that still failed.",
             rows=len(wrows)),
        [e for k in file_errors for e in file_errors[k]],
        ["wallet", "orders", "truncated", "earnings", "seatsOwned", "seatTokenIds", "errors"], wrows)
    for name, p in files.items():
        print("wrote %s sha256=%s" % (p, hashlib.sha256(open(p, "rb").read()).hexdigest()))
    print("done: reads=%d cached=%d errors=%d %.0fs" % (rd.reads, rd.cached,
                                                         sum(len(v) for v in file_errors.values()), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
