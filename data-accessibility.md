# Data accessibility

Every figure below was counted from the files in this checkout (Python 3 standard
library, `json`/`csv` modules — no network calls, no field guessed). Addresses are
lowercase in all source files; token amounts are raw integer strings there. This
file is data+documentation only; it sends no transaction and reads no live network
state itself. `manifest.json` is a separate, not-yet-landed target (`manifest`,
scope `manifest.json`, `README.md`, `tools/manifest/**`) and is not produced here.

## What is available

### Snapshot (fixed input, `snapshot.json` + `sources/snapshot-blocks.json`)
- T = `2026-11-25T11:00:00Z`. Ethereum block **26491756** (timestamp
  `2026-11-25T10:59:59Z`), Robinhood Chain (id 4663) block **122989778**
  (timestamp `2026-11-25T11:00:00Z`), each the last block at or before T.
- Cross-checked by two independent RPC endpoints per chain (`blockNumberMatches`,
  `blockHashMatches`, `nextBlockAfterSnapshot`, etc. all true for Ethereum via
  `gateway.tenderly.co/public/mainnet` and `eth.drpc.org`, and for Robinhood via
  `rpc.mainnet.chain.robinhood.com` and `robinhood-rpc.publicnode.com`).
- `sources/snapshot-blocks.json` does not use the `{source,fetchedAt,coverage,
  errors,columns,rows}` shape (it is not a tabular collection, so it is not
  counted in the row inventory below); it is a nested log of per-endpoint block
  reads with `readTimeUtc`/`httpStatus`/block fields.

### Chain holder/payment data (`sources/chain-ethereum/**`, `sources/chain-robinhood/**`)
Collected by `tools/collect/chain-ethereum` and `tools/collect/chain-robinhood`
via `eth_getLogs` (ERC-20 `Transfer`, topic
`0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`) replayed to
the snapshot block, `eth_call` (`balanceOf`/`totalSupply`, cross-checked with a
Multicall3 `aggregate3` batch on Ethereum), and `eth_getCode` for contract
detection.

| File | Endpoint/chain | Fetched (UTC) | Rows | Errors |
|---|---|---|---:|---:|
| `sources/chain-ethereum/holders.json` | Ethereum (chain id 1) Transfer-log replay, token `0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7`, via `ethereum-rpc.publicnode.com`/`gateway.tenderly.co`/`eth.drpc.org` | 2026-11-25T12:16:56Z | 4840 | 0 |
| `sources/chain-ethereum/payments.json` | same, plus watch-list from `https://api.imd.fun/requests/capabilities` (read 2026-11-25T12:20:34Z) | 2026-11-25T12:16:56Z | 7399 | 0 |
| `sources/chain-ethereum/scan.json` | same | 2026-11-25T12:16:56Z | 1496 | 0 |
| `sources/chain-robinhood/holders.json` | Robinhood Chain (id 4663) Transfer-log replay, token `0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127`, via `rpc.mainnet.chain.robinhood.com` | 2026-11-25T12:19:09Z | 2131 | **1** |
| `sources/chain-robinhood/payments.json` | same, plus capabilities watch-list | 2026-11-25T12:19:09Z | 206 | 0 |
| `sources/chain-robinhood/scan.json` | `eth_getCode` bisection + `eth_getLogs` scan | 2026-11-25T12:19:09Z | 38 | **2** |

Ethereum totals reconcile exactly: `holdersDigest`/`logsDigest` checked, 349,832
logs over blocks 23501863–26491756, 15,959 addresses seen, 4,840 holding a
positive balance, `sum(balance) == totalSupply() == 4210819525771000000000000`
raw. Robinhood: 117,301 logs, `sum == supply == 71170146452000000000000` raw, 0
negative balances.

### IMD API catalog (`sources/api-catalog/**`)

| File | Endpoint | Fetched (UTC) | Rows | Errors |
|---|---|---|---:|---:|
| `capabilities.json` | `GET /requests/capabilities` | 2026-11-25T12:23:02.430Z | 1 | 0 |
| `goals.json` | `GET /goals/{jobId}` (+ children) | 2026-11-25T12:27:56.023Z | 78 | 0 |
| `job-index.json` | `GET /jobs` (paged, `limit=500`) | 2026-11-25T12:18:59.851Z | 17021 | 0 |
| `oracle-requests.json` | `GET /oracle/requests` (paged) | 2026-11-25T12:19:30.411Z | 9504 | 0 |
| `seats-owners.json` | `GET /seats/owners` | 2026-11-25T12:22:59.772Z | 2000 | 0 |
| `seats-records.json` | `GET /seats/records` | 2026-11-25T12:23:01.186Z | 1160 | 0 |
| `workflows.json` | `GET /workflows` (paged) | 2026-11-25T12:19:02.520Z | 219 | 0 |

### IMD API job shards (`sources/api-jobs-00` .. `api-jobs-07`, 19 files)
Each shard collected its slice of `GET /jobs?exclude=oracle&limit=500&before=<T
or cursor>` plus `GET /jobs/{id}` and `GET /jobs/{id}/result` per listed job.
`api-jobs-04` additionally kept `details.json`, `results.json` and a per-page
`pages.json` log. Per-shard row counts (index == jobs rows per shard, all
`errors: 0`): jobs-00 947, jobs-01 931, jobs-02 926, jobs-03 918, jobs-04 908,
jobs-05 926, jobs-06 912, jobs-07 933 — summing to the **7,401** non-oracle jobs
(`allJobsCount` in `api-jobs-00/index.json`), matching the catalog's job-index
minus its 9,620 oracle-template rows.

### IMD API wallet shards (`sources/api-wallets-00` .. `api-wallets-05`, 32 files)
Each shard collected, per wallet of its slice: `GET /requests/paid-by/{wallet}`
(orders, capped at 100 newest, before-T and after-T both returned),
`GET /wallets/{wallet}/earnings?limit=200` (paged), `GET /seats/owners`,
`/seats/records`, `/seats/{tokenId}`, and `GET /jobs/{id}` + `/jobs/{id}/result`
for any job only reachable via a late order reference. All 32 files have
`errors: 0`. Orders rows per shard: 00=535, 01=744, 02=452, 03=827, 04=520,
05=276 (total **3354**). Earnings rows: 00=38882 (+711 launch rows), 01=24444,
02=40790, 03=39824, 04=38500 (+712 launch rows), 05=47552. Seats rows: 00=160,
01=112, 02=226, 03=167, 04=98, 05=238. Wallets-rollup rows: 00=1086, 01=1107,
02=1092, 03=1134, 04=1099, 05=1092 (**6610** total, matching `holders.csv`).
Late-jobs rows: 00=0, 01=20, 02=0, 03=11, 04=2, 05=0 (**33** total, all already
inside the 17,021-row job catalog — confirmed by `tools/jobs/build_jobs.py`).

### Derived deliverables
`holders.csv` 6610 rows; `jobs.csv` 17021 rows; `scores.csv` 6610 rows;
`methodology.md`; all rebuildable offline by `tools/holders/build_holders.py`,
`tools/jobs/build_jobs.py`, `tools/scores/score.py` (each has a `--check` mode,
see "Other ways to verify" below).

## What is missing

- **Wallets whose order list stopped at the newest 100** (API cap,
  `truncated=true` in the shard `wallets.json`, carried to `scores.csv` as
  `orders_truncated=true`): **7 wallets** total — 1 in shard 00, 3 in shard 01, 1
  in shard 02, 2 in shard 03, 0 in shards 04 and 05. Their order history before
  the cap may omit older orders; nothing before the oldest kept order is implied
  to not exist.
- **Jobs with no payer, by `unlinked_reason` in `jobs.csv`** (9918 of 17021 rows,
  58.3%):
  - `oracle_no_matching_order` — **9167** rows. Oracle-panel jobs require order
    evidence (a `paidBy` on a panel is ignored); these have none.
  - `paidBy_missing_no_matching_order` — **751** rows. No `paidBy` on the job and
    no unique matching order was found either.
  - No other `unlinked_reason` value occurs (the two values sum exactly to the
    9918 rows with empty `link_evidence`).
- **Oracle panel jobs, linked only through orders**: `oracle-requests.json` has
  no order field at all (columns `id,status,jobId,answerType,chainId,createdAt,
  attestedAt`); an oracle job can only be attributed via a saved order whose
  result names the job/request, never via `paidBy` on the panel directly. Of
  9,626 `jobs.csv` rows categorized `oracle/indexer`, only 459 are counted roots
  (linked, eligible, deduplicated); the other 9,167 are the
  `oracle_no_matching_order` rows above. Separately, the catalog records 9,620
  oracle-template jobs in `job-index.json` against 9,504 rows in
  `oracle-requests.json`; 116 of those jobs have no corresponding request in this
  list, cause unrecorded — not evidence of being unpaid.
- **Orders that name no job**: of 3,354 saved orders, **37** have both
  `resultJobId` and `resultId` null (no job or oracle request named at all).
- **Seat owners and records are known only as of the read time, not at T**:
  `seats-owners.json`/`seats-records.json` state they reflect the API as read
  (`fetchedAt` 2026-11-25T12:22:59Z/12:23:01Z, both after T), and the API
  documents up to a 10-minute seat cache; `seats_owned`/`seat_accepted_before_t`
  in `scores.csv` are shown but excluded from the score for this reason.
- **Chain ranges/state an endpoint would not serve**: Robinhood's
  `eth_getCode`-at-snapshot-block reads failed on `rpc.mainnet.chain.robinhood.com`
  with `{"code":-32000,"message":"historical state ... is not available"}` (1
  error in `chain-robinhood/holders.json`, 2 in `chain-robinhood/scan.json`);
  contract detection fell back to a `latest`-block read where this happened.
  `robinhoodchain.blockscout.com`'s REST API returned HTTP 403 with an HTML
  challenge page for every block-lookup cross-check in `snapshot.json`
  (`"verified".robinhood.checksByEndpoint`), so that third endpoint could not
  corroborate the Robinhood snapshot block. On Ethereum, `publicnode.com`
  returned HTTP 403 ("Archive requests require a personal token") and
  `gateway.tenderly.co` returned HTTP 429 on every cross-check call; only
  `eth.drpc.org` fully served the archive range used for cross-verification
  (see `tools/collect/chain-ethereum/REPORT.md`).
- **Shards absent or with errors**: no wallet or job shard is absent (6/6 wallet
  shards, 8/8 job shards present, all required by `tools/jobs/build_jobs.py` and
  `tools/holders/build_holders.py`). The only non-empty `errors` arrays in any
  `sources/**/*.json` file are the 3 Robinhood historical-state RPC errors above;
  all 62 other source files have `errors: []`.
- **740 confirmed, before-T orders with no matching on-chain payment transfer**
  (methodology.md): `imd_spent` in `scores.csv` only counts a payment it can match
  by `txHash` to a saved `chain-*/payments.json` row, so it is a lower bound on
  actual IMD spent, never an upper bound or exact figure.
- **`is_goal=true` jobs**: 79 of 17,021 rows; **182** rows are explicit grouped
  children (`counted_under != job_id`); 6,949 rows meet the counted-root rule
  (`counts=true`), of which 5,197 link to a snapshot holder.
- **51 index/detail disagreements** on `updatedAt` between `job-index.json` and
  the per-shard `jobs.json` detail records (detail wins; both values kept where
  relevant via `data_gaps`).

## Other ways to verify

- **A wallet's balance or the token supply at the snapshot block**: call
  `eth_call` with data `0x70a08231` + the 32-byte-padded address (`balanceOf`)
  against token `0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7` at block
  `0x1944D9C` (26491756) on any Ethereum archive RPC, or
  `0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127` at block `0x754A832`
  (122989778) on a Robinhood Chain (id 4663) archive RPC; `eth_call` with data
  `0x18160ddd` (`totalSupply`) at the same blocks for supply. To rebuild the
  holder set independently, run `eth_getLogs` for topic0
  `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` on the
  token address from its first block through the snapshot block and net the
  `from`/`to` transfers.
- **A job, an order list, or a goal**: `GET https://api.imd.fun/jobs/{id}` and
  `GET https://api.imd.fun/jobs/{id}/result` for job detail/result,
  `GET https://api.imd.fun/requests/paid-by/{wallet}` for a wallet's orders,
  `GET https://api.imd.fun/goals/{jobId}` for a goal and its children.
- **An on-chain order's payer**: `GET https://api.imd.fun/intake/{chainId}/{transactionHash}`.
- **A payment transaction**: look up the transaction hash from
  `sources/chain-ethereum/payments.json` or `sources/chain-robinhood/payments.json`
  on any block explorer for that chain.
- **The scripts' own `--check` commands** (all Python 3, standard library only,
  offline, run from the repository root):
  - `python3 -B tools/collect/chain-ethereum/check.py`
  - `python3 -B tools/collect/chain-robinhood/check.py`
  - `python3 -B tools/collect/api-catalog/check.py`
  - `python3 -B tools/collect/api-jobs-NN/check.py` for `NN` in `00`..`07`
  - `python3 -B tools/collect/api-wallets-NN/check.py` for `NN` in `00`..`05`
  - `python3 -B tools/holders/build_holders.py --check`
  - `python3 -B tools/jobs/build_jobs.py --check`
  - `python3 -B tools/scores/score.py --check`

All figures in this file were obtained by parsing the JSON/CSV files above with
Python's standard library (`json`, `csv`, `glob`) in this checkout; none are
remembered or assumed. Where a figure rests on a single source with no
independent corroboration in this checkout (the Robinhood snapshot block, the
740 unmatched pre-T orders, the 9,620-vs-9,504 oracle count, and every API-shard
row count above), that is noted next to the figure rather than presented as
cross-checked.
