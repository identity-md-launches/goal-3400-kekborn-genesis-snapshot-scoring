# Genesis Contribution Score — methodology

Script: `tools/scores/score.py` (Python 3, standard library, offline). `python3 tools/scores/score.py` rewrites
`scores.csv` from `holders.csv`, `jobs.csv` and `sources/`; `--check` exits 1 if `scores.csv` differs.
Fixture test: `tools/scores/test_score.py`. Snapshot fixed: T = 2026-11-25T11:00:00Z (see snapshot.json).

## Inputs and rules
- One row per wallet of `holders.csv` (6610). `balance` = `total_balance` (Ethereum + Robinhood); shown only, **no term of the score**.
- Jobs: rows of `jobs.csv` with `counts=true` and a non-empty `wallet`. Status (completed/abandoned/failed/ongoing),
  category, complexity, deliverable_ok, quality and is_goal are taken as `jobs.csv` set them (see tools/jobs/README.md);
  `counts` already ensures each job is counted once and goal children are not counted twice. Unlinked jobs (no wallet) are not scored; jobs.csv lists their reasons.
- `imd_spent`: for each order in `sources/api-wallets-*/orders.json` of the wallet with `beforeT=true` and
  `paymentStatus=confirmed`, its `paymentTx` is matched to `sources/chain-*/payments.json` rows with the same txHash,
  `from` = wallet and `to` in the file's payTo/intakes; each (txHash, logIndex) counted once. Orders without such a
  match are counted in `orders_unmatched` and their amount is never added.
- Seat work (`seats_owned`, `seat_accepted_before_t` from `sources/api-wallets-*/seats.json`) is shown in separate columns and is not a score term: seat ownership is a current read, not a state at T.
- `orders_truncated` from wallets.json `truncated` (API returned at most 100 orders); empty if the wallet is absent there.

## Formula (max 100)
For a counted input x with weight w and cap c: `term = w * min(1, ln(1+x) / ln(1+c))`; diversity uses `w * min(1, sqrt(d/6))`.

| term | x | weight | cap |
|---|---|---|---|
| completed jobs | counted jobs with status completed | 25 | 50 |
| IMD spent | imd_spent in whole IMD | 15 | 100 |
| category diversity | distinct categories among counted jobs, `other` excluded (0–6) | 10 | 6 (sqrt) |
| scope & complexity | sum of `complexity` over completed counted jobs | 15 | 500 |
| successful deliverables | counted jobs with deliverable_ok=true | 15 | 50 |
| quality signals | sum of `quality.accepted_share` over completed counted jobs (null = 0, not guessed) | 10 | 50 |
| multi-agent goals | counted jobs with is_goal=true (goals paid for) | 10 | 5 |

Score is rounded to 4 decimals. A wallet with no linked counted job scores 0 ("nothing found", not "nothing done");
a wallet with at least one linked job is floored at 0.0001 so it never scores 0.

## Worked example: 0x047f606fd5b2baa5f5c6c4ab8958e45cb6b054b7
completed 50 → 25.0000; spent 30.5 IMD → 15·ln31.5/ln101 = 11.2131; diversity 5 → 10·sqrt(5/6) = 9.1287;
complexity 321 → 15·ln322/ln501 = 13.9334; deliverables 39 → 15·ln40/ln51 = 14.0732; quality 38.5495 → 10·ln39.5495/ln51 = 9.3533;
goals 4 → 10·ln5/ln6 = 8.9824. Total **91.6841** → Legendary.

## Tiers
Exact Fisher–Jenks natural breaks, k = 5, computed in the script over the **distinct positive scores weighted by their
count** (minimum within-class weighted sum of squares, dynamic programming). **Zeros are not in the breaks**: every
0-score wallet is Common. Equal scores are never split (classes are built on distinct values). Under five distinct
positive scores, each distinct value is its own class and they take the top tiers (highest = Legendary, downward).

Current cuts (class upper bound, inclusive; counts include zeros for Common):
| tier | score range | wallets |
|---|---|---|
| Common | 0 – 22.7485 | 6365 (6149 zero + 216 positive) |
| Uncommon | 22.7485 < s ≤ 35.9432 | 115 |
| Rare | ≤ 51.0367 | 70 |
| Epic | ≤ 66.8886 | 33 |
| Legendary | ≤ 91.6841 | 27 |

## Genetics
Share of each category among the wallet's counted jobs, ordered by share: code/contracts = problem-solving, technical aptitude;
research = memory, perception, analysis; audit = defence, resilience; website/product = creativity, adaptability;
oracle/indexer = perception, information processing; `other` = no trait. multi-agent goal = "rare special combination",
named from the first trait of the wallet's two largest other categories (e.g. `[creativity x problem-solving]`), or
"pure coordination" if it has none. Empty for wallets with no counted job.

## Limits
- 740 confirmed pre-T orders have no matching payment transfer (e.g. paid by another address, or tx outside the saved logs); their amounts are left out, so imd_spent is a lower bound.
- Orders are capped at 100 per wallet by the API (`orders_truncated=true`): history may be incomplete for those wallets.
- Job classification, linkage and dedup are inherited from jobs.csv; errors there propagate here.
- Weights and caps are a design choice, stated here so anyone can recompute or reweight.
