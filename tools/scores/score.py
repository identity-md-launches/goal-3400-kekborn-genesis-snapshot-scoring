#!/usr/bin/env python3
"""Genesis Contribution Score, tiers and genetics. Offline, standard library only.
Reads holders.csv, jobs.csv, sources/; writes scores.csv. --check: exit 1 if scores.csv differs.
Usage: score.py [--root DIR] [--check]"""
import csv, glob, io, json, math, os, sys

CATS = ["code/contracts", "research", "audit", "website/product", "oracle/indexer", "multi-agent goal", "other"]
CATCOL = {c: "cat_" + c.replace("/", "_").replace(" ", "_").replace("-", "_") for c in CATS}
TRAITS = {"code/contracts": "problem-solving, technical aptitude", "research": "memory, perception, analysis",
          "audit": "defence, resilience", "website/product": "creativity, adaptability",
          "oracle/indexer": "perception, information processing"}
# name, weight, cap: term = weight * min(1, log(1+x)/log(1+cap)); diversity uses sqrt
TERMS = [("completed", 25, 50), ("imd_spent", 15, 100), ("diversity", 10, 6),
         ("complexity", 15, 500), ("deliverables", 15, 50), ("quality", 10, 50), ("goals", 10, 5)]
TIERS = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]
HEAD = ["wallet", "balance", "jobs_completed", "jobs_abandoned", "jobs_failed", "jobs_ongoing"] + \
       [CATCOL[c] for c in CATS] + ["categories_distinct", "complexity_sum", "deliverables_ok", "quality_points",
       "goals_paid", "imd_spent", "imd_spent_raw", "payments_matched", "orders_unmatched", "seats_owned",
       "seat_accepted_before_t", "score", "tier", "genetics", "orders_truncated"]


def load(path):
    d = json.load(open(path))
    return [dict(zip(d["columns"], r)) for r in d["rows"]]


def logterm(x, w, cap):
    return w * min(1.0, math.log1p(max(x, 0)) / math.log1p(cap))


def score_of(m):
    parts = {}
    for name, w, cap in TERMS:
        if name == "diversity":
            parts[name] = w * min(1.0, math.sqrt(m[name] / cap))
        else:
            parts[name] = logterm(m[name], w, cap)
    return round(sum(parts.values()), 4), parts


def jenks(values_counts, k):
    """Exact Fisher-Jenks over sorted distinct values with weights. Returns list of class upper bounds."""
    v = [x for x, _ in values_counts]; w = [c for _, c in values_counts]; n = len(v)
    if n <= k:
        return v[:]
    W = [0] * (n + 1); S = [0.0] * (n + 1); Q = [0.0] * (n + 1)
    for i in range(n):
        W[i+1] = W[i] + w[i]; S[i+1] = S[i] + w[i]*v[i]; Q[i+1] = Q[i] + w[i]*v[i]*v[i]
    def ssd(i, j):  # values i..j-1
        ww = W[j]-W[i]; s = S[j]-S[i]
        return (Q[j]-Q[i]) - s*s/ww
    INF = float("inf")
    D = [[INF]*(n+1) for _ in range(k+1)]; B = [[0]*(n+1) for _ in range(k+1)]
    D[0][0] = 0.0
    for c in range(1, k+1):
        for j in range(c, n+1):
            best, arg = INF, 0
            for i in range(c-1, j):
                if D[c-1][i] < INF:
                    t = D[c-1][i] + ssd(i, j)
                    if t < best - 1e-12:
                        best, arg = t, i
            D[c][j] = best; B[c][j] = arg
    ups, j = [], n
    for c in range(k, 0, -1):
        ups.append(v[j-1]); j = B[c][j]
    return ups[::-1]


def tier_cuts(scores):
    pos = sorted(s for s in scores if s > 0)
    vc = []
    for s in pos:
        if vc and vc[-1][0] == s: vc[-1][1] += 1
        else: vc.append([s, 1])
    if not vc:
        return []
    return jenks([tuple(x) for x in vc], 5)


def tier_of(s, ups):
    if s <= 0 or not ups:
        return TIERS[0]
    off = 5 - len(ups)  # fewer than 5 distinct positive scores: classes take the top tiers
    for i, u in enumerate(ups):
        if s <= u:
            return TIERS[i + off]
    return TIERS[-1]


def genetics(cc):
    tot = sum(cc.values())
    if not tot:
        return ""
    out, others = [], []
    for c in sorted(CATS, key=lambda c: (-cc[c], CATS.index(c))):
        if cc[c] == 0 or c in ("multi-agent goal",):
            continue
        sh = cc[c] / tot
        if c == "other":
            out.append("other %d%%: no trait" % round(sh*100)); continue
        out.append("%s %d%%: %s" % (c, round(sh*100), TRAITS[c])); others.append(c)
    if cc["multi-agent goal"]:
        sh = cc["multi-agent goal"] / tot
        name = " x ".join(TRAITS[c].split(",")[0] for c in others[:2]) or "pure coordination"
        out.append("multi-agent goal %d%%: rare special combination [%s]" % (round(sh*100), name))
    return " | ".join(out)


def build(root):
    P = lambda *a: os.path.join(root, *a)
    holders = list(csv.DictReader(open(P("holders.csv"))))
    jobs = [r for r in csv.DictReader(open(P("jobs.csv"))) if r["counts"] == "true" and r["wallet"]]
    pays = {}
    for f in glob.glob(P("sources", "chain-*", "payments.json")):
        d = json.load(open(f)); cov = d.get("coverage") or {}
        dest = set()
        if isinstance(cov, dict):
            dest = set(a.lower() for a in (cov.get("payTo") or []) + (cov.get("intakes") or []))
        for r in d["rows"]:
            r = dict(zip(d["columns"], r))
            if dest and r["to"].lower() not in dest:
                continue
            pays.setdefault(r["txHash"].lower(), []).append(r)
    orders, trunc, seats = {}, {}, {}
    for f in sorted(glob.glob(P("sources", "api-wallets-*", "orders.json"))):
        for o in load(f):
            orders.setdefault(o["wallet"].lower(), {})[o["orderId"]] = o
    for f in sorted(glob.glob(P("sources", "api-wallets-*", "wallets.json"))):
        for w in load(f):
            trunc[w["wallet"].lower()] = bool(w.get("truncated"))
    for f in sorted(glob.glob(P("sources", "api-wallets-*", "seats.json"))):
        for s in load(f):
            seats.setdefault(s["owner"].lower(), {})[s["tokenId"]] = s
    byw = {}
    for j in jobs:
        byw.setdefault(j["wallet"].lower(), []).append(j)
    rows = []
    for h in holders:
        w = h["wallet"].lower(); js = byw.get(w, [])
        st = {s: sum(1 for j in js if j["status"] == s) for s in ("completed", "abandoned", "failed", "ongoing")}
        cc = {c: sum(1 for j in js if j["category"] == c) for c in CATS}
        spent, matched, unmatched, used = 0, 0, 0, set()
        for o in orders.get(w, {}).values():
            if o.get("beforeT") is not True or o.get("paymentStatus") != "confirmed":
                continue
            tx = (o.get("paymentTx") or "").lower()
            hit = [p for p in pays.get(tx, []) if p["from"].lower() == w]
            if not hit:
                unmatched += 1; continue
            for p in hit:
                key = (tx, p["logIndex"])
                if key not in used:
                    used.add(key); spent += int(p["valueRaw"]); matched += 1
        m = {"completed": st["completed"],
             "imd_spent": spent / 1e18,
             "diversity": sum(1 for c in CATS if c != "other" and cc[c]),
             "complexity": sum(int(j["complexity"] or 0) for j in js if j["status"] == "completed"),
             "deliverables": sum(1 for j in js if j["deliverable_ok"] == "true"),
             "quality": 0.0,
             "goals": sum(1 for j in js if j["is_goal"] == "true")}
        for j in js:
            try:
                q = json.loads(j["quality"] or "{}").get("accepted_share")
            except ValueError:
                q = None
            if isinstance(q, (int, float)) and j["status"] == "completed":
                m["quality"] += q
        sc, _ = score_of(m) if js else (0.0, None)
        if js and sc <= 0:
            sc = 0.0001  # a linked job is never 0
        so = seats.get(w, {})
        rows.append({"wallet": w, "balance": h["total_balance"],
            "jobs_completed": st["completed"], "jobs_abandoned": st["abandoned"], "jobs_failed": st["failed"],
            "jobs_ongoing": st["ongoing"], **{CATCOL[c]: cc[c] for c in CATS},
            "categories_distinct": m["diversity"], "complexity_sum": m["complexity"],
            "deliverables_ok": m["deliverables"], "quality_points": round(m["quality"], 4), "goals_paid": m["goals"],
            "imd_spent": "%d.%018d" % divmod(spent, 10**18), "imd_spent_raw": str(spent),
            "payments_matched": matched, "orders_unmatched": unmatched, "seats_owned": len(so),
            "seat_accepted_before_t": sum(int(s.get("acceptedBeforeT") or 0) for s in so.values()),
            "score": sc, "genetics": genetics(cc),
            "orders_truncated": "" if w not in trunc else str(trunc[w]).lower()})
    ups = tier_cuts([r["score"] for r in rows])
    for r in rows:
        r["tier"] = tier_of(r["score"], ups)
        r["score"] = "%.4f" % r["score"]
    buf = io.StringIO(); wr = csv.DictWriter(buf, HEAD, lineterminator="\n"); wr.writeheader(); wr.writerows(rows)
    return buf.getvalue(), ups, rows


def main(a):
    root = a[a.index("--root")+1] if "--root" in a else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    out, ups, rows = build(root)
    dst = os.path.join(root, "scores.csv")
    from collections import Counter
    info = "rows=%d cuts=%s tiers=%s" % (len(rows), ups, dict(Counter(r["tier"] for r in rows)))
    if "--check" in a:
        same = os.path.exists(dst) and open(dst).read() == out
        print(("OK " if same else "DIFFERS ") + info); return 0 if same else 1
    open(dst, "w").write(out); print(info); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
