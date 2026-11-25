# Shard 05 collection

The checkout already held both landed holder sources. The two files contained 6,610 distinct addresses after merging, including 361 addresses found on both chains. Applying the shard hash selected 1,092 wallets, including 69 found on both chains. The collector checked the digest and supply recorded by the chain collectors. The fixed order cutoff was 2026-11-25T11:00:00Z.

| Chain | Snapshot block | Positive-balance holders | Holders digest | Raw supply |
| --- | ---: | ---: | --- | ---: |
| Ethereum | 26,491,756 | 4,840 | `b7821f527799170ea380616debdc2a18ced45554acfd8a920ef458f08224a321` | 4210819525771000000000000 |
| Robinhood | 122,989,778 | 2,131 | `e8fe3d1218d599209d81d7c29af5f3bce7a13f58368e661693cd524ed358f5f5` | 71170146452000000000000 |

Ethereum's recorded supply came from a historical `totalSupply()` call. Robinhood's source used mints less burns because historical state was unavailable. Its latest supply matched the reconstructed balance sum, and its scan reported no intervening mint or burn logs as of that check.

## Files

| File | Rows | Recorded errors | SHA-256 |
| --- | ---: | ---: | --- |
| `sources/api-wallets-05/orders.json` | 276 | 0 | `e763a052d909b039d189bcc9cc7e8c807ee3d1f6a58dd319986709262d279e19` |
| `sources/api-wallets-05/earnings.json` | 47,552 | 0 | `2ff23530c9ff7e39f522c24e836ab71f849f28bad189006732fd57f493e86734` |
| `sources/api-wallets-05/seats.json` | 238 | 0 | `053e2ed28f868a8b6ea69305a29c8e4b11e67ae814caf69b5179084c79fd2ea4` |
| `sources/api-wallets-05/wallets.json` | 1,092 | 0 | `4f0a48e48f75a0320cf83531e3978701ab6fa6edd8bc2b2b8cf75f24af364110` |
| `sources/api-wallets-05/late-jobs.json` | 0 | 0 | `a30cbecf04b67855f93723c8e6ef5b0f426fbb1662647a5499c8dab94d19a216` |
| `tools/collect/api-wallets-05/check.py` | — | — | `43143d78ef6b27834c755c74318449c78992e741dcdef5b544de1e7f38895194` |
| `tools/collect/api-wallets-05/collect.py` | — | — | `efe736303ddfaf9058a53a43bb2fa12002c2a0e20287070aefa74ea99ff1f06a` |
| `tools/collect/api-wallets-05/rebuild.py` | — | — | `f7c6df77983d180d869e52f5f7dfdab3964fa85edab59bb73eacd18133aa8d08` |

| `tools/collect/api-wallets-05/source_format.py` | — | — | `023a672f154b14540bc90ff43744b0d8ae87d745d80e786fd0636d4b9f1141b8` |

## Earnings export repair

The previous bundle check exited 1 at 13,887,712 bytes against an 8,388,608-byte limit. The revised collector writes all 47,552 earnings rows to the same JSON path. The `launch` and `token` cells store zero-based references to their arrays in `coverage.columnDictionaries`. Null cells remain null. Other cells, row order and source metadata are preserved. The offline checker reads the decoded rows.

Expand to literal-row JSON using:

```sh
python3 -B tools/collect/api-wallets-05/source_format.py sources/api-wallets-05/earnings.json
```

Piping that command to `cmp test/scratch/earnings-original.json -` exited 0 with no output. The expanded file's SHA-256 is `ac839d65f4e7a9bdbbb8f6b50e03696ae3f0abe54a866862804740987f2f9ef0`. The offline checker and required `ls` both exited 0 again with the output below. The repair used the saved export and made no new earnings reads. A local Git pack of all shard source and tool files, measured with `git pack-objects --stdout | wc -c` in a scratch repository, is a size estimate for the upload check.

## Coverage

- Orders include all rows visible from the API, including attempted payments. 276 rows have `beforeT=true`, 0 have `beforeT=false`, and 0 have no usable creation time. 0 wallets reached the API's newest-100 limit. Chain payment records can corroborate transactions. Earlier API-only order details remain unavailable for capped histories.
- Earnings preserve all returned fields and follow the `next` cursor to the end. The API's earnings records describe token allocations and related launch metadata. Claim status is outside the response fields. 94 wallets had earnings.
- Seat ownership was read at 2026-11-25T12:28:27.109471Z. The shard owned 260 seats at that read and 238 had a work record. 0 seat rows have at least one indeterminate cutoff metric. Historical ownership at T remains unverified. The cutoff metrics use recorded work submission and acceptance times. Seat 637 had a tied timestamp at a page boundary. The final replay verified strictly older timestamps on the next page and all 3,248 lifetime attempts across four pages.
- Late-job collection considered 0 distinct job IDs from orders in the last 24 hours before T. It found 0 matching order/job rows with job creation at or after T. 0 of these rows have an unavailable result.
- HTTP attempts in the primary collection totalled 2703. The collector handled 5 rate-limit responses and recorded 0 reads that still failed. 0 wallet rows have an error entry. Source envelopes carry read errors and null reasons. Job input and signatures are outside the public paid-by response fields.
- Successful responses are cached by request URL. Run the same collector command to resume. Supply `--cache` with a new directory for a fresh observation of the live API.

## Commands and checks

The batch-board GET returned the expected batch with both holder targets landed. The guide GET documented newest-100 orders, seat cursor semantics and earnings pagination. Direct Robinhood historical RPC probes returned HTTP 403 in this run. The existing holder sources supplied the shard inputs, and the fallback was checked with mocked RPC responses. [IMD API guide](https://imd.fun/docs/llms.txt)

Before the earnings format repair, the live and replay commands were both `python3 -B tools/collect/api-wallets-05/collect.py`. The live command exited 0 and reported these final lines.

```text
Wallets 1092/1092, live reads 2703, cache hits 0, final failures 0, 429s 5
Late-job candidates 0, matching order rows 0
orders.json 276 rows 0 errors 7d4258575cbf946a5600a117efc5398020d96aeb5c3998d1e34963251da2e160
earnings.json 47552 rows 0 errors ac839d65f4e7a9bdbbb8f6b50e03696ae3f0abe54a866862804740987f2f9ef0
seats.json 238 rows 2 errors 3d2cac7df980efffa017c88b7cd69ae6a82b28cbf655f48ba31028ab2512fb26
wallets.json 1092 rows 0 errors 6f62fbdd30b43b6e15f292e1aa2e0d0a027b19b8e256403646bea6cc702edfbd
late-jobs.json 0 rows 0 errors a30cbecf04b67855f93723c8e6ef5b0f426fbb1662647a5499c8dab94d19a216
API live reads 2703, cache hits 0, final failures 0, 429s 5
```

`python3 -B tools/collect/api-wallets-05/collect.py` exited 0 with the output below.

```text
Holders {"ethereum": {"mode": "checkout", "count": 4840, "holdersDigest": "b7821f527799170ea380616debdc2a18ced45554acfd8a920ef458f08224a321", "block": 26491756, "supplyRaw": "4210819525771000000000000", "supplySource": "eth_call totalSupply() at block 26491756"}, "robinhood": {"mode": "checkout", "count": 2131, "holdersDigest": "e8fe3d1218d599209d81d7c29af5f3bce7a13f58368e661693cd524ed358f5f5", "block": 122989778, "supplyRaw": "71170146452000000000000", "supplySource": "mints less burns (state not served: rpc error {\"code\": -32000, \"message\": \"historical state 3edd84ec3633ef2bf2f75b85b3060a7f4dd0888d3a16df6f1dce40159ad14cd5 is not available\"})"}}; shard 1092
Wallets 100/1092, live reads 0, cache hits 532, final failures 0, 429s 0
Wallets 200/1092, live reads 0, cache hits 739, final failures 0, 429s 0
Wallets 300/1092, live reads 0, cache hits 956, final failures 0, 429s 0
Wallets 400/1092, live reads 0, cache hits 1172, final failures 0, 429s 0
Wallets 500/1092, live reads 0, cache hits 1394, final failures 0, 429s 0
Wallets 600/1092, live reads 0, cache hits 1613, final failures 0, 429s 0
Wallets 700/1092, live reads 0, cache hits 1835, final failures 0, 429s 0
Wallets 800/1092, live reads 0, cache hits 2055, final failures 0, 429s 0
Wallets 900/1092, live reads 0, cache hits 2280, final failures 0, 429s 0
Wallets 1000/1092, live reads 0, cache hits 2499, final failures 0, 429s 0
Wallets 1092/1092, live reads 0, cache hits 2698, final failures 0, 429s 0
Late-job candidates 0, matching order rows 0
orders.json 276 rows 0 errors e763a052d909b039d189bcc9cc7e8c807ee3d1f6a58dd319986709262d279e19
earnings.json 47552 rows 0 errors ac839d65f4e7a9bdbbb8f6b50e03696ae3f0abe54a866862804740987f2f9ef0
seats.json 238 rows 0 errors 053e2ed28f868a8b6ea69305a29c8e4b11e67ae814caf69b5179084c79fd2ea4
wallets.json 1092 rows 0 errors 4f0a48e48f75a0320cf83531e3978701ab6fa6edd8bc2b2b8cf75f24af364110
late-jobs.json 0 rows 0 errors a30cbecf04b67855f93723c8e6ef5b0f426fbb1662647a5499c8dab94d19a216
API live reads 0, cache hits 2698, final failures 0, 429s 0
```


`python3 -B tools/collect/api-wallets-05/check.py` exited 0 with the output below.

```text
PASS orders.json schema and shard size (276 rows, 0 recorded errors)
PASS wallets.json schema and shard size (1092 rows, 0 recorded errors)
PASS seats.json schema and shard size (238 rows, 0 recorded errors)
PASS earnings.json schema and shard size (47552 rows, 0 recorded errors)
PASS late-jobs.json schema and shard size (0 rows, 0 recorded errors)
PASS exact shard membership, one row per wallet (1092)
PASS shard ownership, unique IDs, wallet counts, truncation, raw earnings and cutoff joins
```


`ls sources/api-wallets-05/orders.json sources/api-wallets-05/wallets.json sources/api-wallets-05/earnings.json` exited 0 with the output below.

```text
sources/api-wallets-05/earnings.json
sources/api-wallets-05/orders.json
sources/api-wallets-05/wallets.json
```


The Git staging command exited 128 because the Git index was read-only. The source files are present in the working tree.

The following commands also exited 0.

| Command | Output |
| --- | --- |
| `python3 -B tools/collect/chain-ethereum/check.py` | `all checks passed` |
| `python3 -B tools/collect/chain-robinhood/check.py` | `holders=2131 payments=206 logs=117301 sum=71170146452000000000000` |
| `python3 -B test/scratch/w05-test.py` | `PASS holder provenance, order cutoff, oracle groups, late-job joins, earnings cursors, seat cutoff, HTTP retry/resume and Transfer reconstruction` |
| `python3 -B test/scratch/w05-check-test.py` | `PASS offline checker accepts exact membership and rejects missing or duplicate shard rows` |
| Python `compile()` on all three shard scripts | `PASS compile check.py`, `PASS compile collect.py`, `PASS compile rebuild.py` |
