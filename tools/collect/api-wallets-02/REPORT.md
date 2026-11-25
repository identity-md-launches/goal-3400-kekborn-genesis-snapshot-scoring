# IMD wallet histories for shard 02

The collector used both landed holder files. It deduplicated their lowercase addresses and selected wallets whose first eight SHA-256 hex digits as an integer give remainder 2 modulo 6.

| Chain | Input | Snapshot block | Holders | Holders digest |
| --- | --- | ---: | ---: | --- |
| ethereum | `sources/chain-ethereum/holders.json` | 26491756 | 4840 | `b7821f527799170ea380616debdc2a18ced45554acfd8a920ef458f08224a321` |
| robinhood | `sources/chain-robinhood/holders.json` | 122989778 | 2131 | `e8fe3d1218d599209d81d7c29af5f3bce7a13f58368e661693cd524ed358f5f5` |

Each holder digest covers sorted `wallet:balanceRaw\n` lines for the entire chain input. The read-only Transfer replay fallback passed synthetic tests. This collection used the landed files.

| File | Data rows | SHA-256 |
| --- | ---: | --- |
| `sources/api-wallets-02/earnings.json` | 40790 | `5e32c88169d7badbb4d1a67d052d27356d809616d83cc4407bf4b57d8186e5d6` |
| `sources/api-wallets-02/late-jobs.json` | 0 | `658298899c8f53b7ee001b895021264e19d179a45b6aabe1a6ebff0e79ea9551` |
| `sources/api-wallets-02/orders.json` | 452 | `74c50de13f990df3141d4c5d123918165952fe72986113d209b6852a626e692f` |
| `sources/api-wallets-02/seats.json` | 226 | `8b901e7b59528a090f00f03101b5c56c75cf9dadd28de03358d37a53fb042c80` |
| `sources/api-wallets-02/wallets.json` | 1092 | `b56ce580ff06aa4e970552545774fa2808ca430ec5246e6988cb337fb7a5867f` |
| `tools/collect/api-wallets-02/chain.py` | — | `0c4e3907c28dcc98d64017732c1f70738a8c849bbddf64b09778b70abe648307` |
| `tools/collect/api-wallets-02/check.py` | — | `c05b6e7969bd2fce27d53c5ec544fa37ed880aacceae6c3fda966648e0c52b0e` |
| `tools/collect/api-wallets-02/collect.py` | — | `7472c7040e261045dc32016f39e00a4f87866e36f304a472677fec0adee91136` |
| `tools/collect/api-wallets-02/source_format.py` | — | `548cca6fd64ae58f99943c7d1f7b2a16dcb9aadc6adb4e7d0bc55c63cacf4c6e` |

## Reading the compact sources

All five JSON objects retain the six-key envelope and one array per logical row. Two sources use zero-based references recorded in `coverage.rowDictionaries`.

- Earnings `launch` and `token` cells index their respective dictionaries in `value` mode.
- Seat `jobsBeforeT` cells contain lists of indices into the job-ID dictionary in `items` mode.
- Null cells and empty lists retain their meanings. All other cells, including wallet addresses and raw integer amount strings, remain literal.

To produce the original literal-row JSON offline, run:

```sh
python3 -B tools/collect/api-wallets-02/source_format.py sources/api-wallets-02/earnings.json
python3 -B tools/collect/api-wallets-02/source_format.py sources/api-wallets-02/seats.json
```

Python readers can import `decode_source` from the same module and pass it the parsed JSON object. It also accepts literal-row JSON. The collector emits dictionary-encoded sources automatically, and the checker decodes them before checking data integrity.

## Coverage and limits

- The API returned 452 orders created strictly before the cutoff, 0 at or after it, and 0 with an unknown cutoff flag. It capped 1 wallets at 100 newest orders. Order coverage is unknown for 0 wallets.
- The earnings endpoint returned 40790 rows. Pagination was incomplete for 0 wallets. Earnings describe the API at collection time.
- Seat ownership comes from the owners index observed at 2026-11-25T12:22:19.618Z. Ownership was unknown for 0 wallets. Seat totals are lifetime counters. Timestamped work and acceptance are filtered strictly before the cutoff. The timestamp-only work cursor can omit rows tied at a page boundary.
- The final 24-hour order window referenced 0 distinct jobs. The late-jobs file records 0 order/job links whose job creation was at or after the cutoff. Job details and results reflect the read time.
- Public order history includes unsuccessful payment attempts. A null result field reflects the data exposed by the endpoint. Confirmed transactions can be cross-checked against the landed chain payment files.
- The API returned 2 grouped oracle orders. Their top-level job and result IDs are null. The nested requests can be recovered from the original paid-by response.
- Goals, jobs outside the late window, and scoring are covered by separate batch targets.

Recorded dataset issues

- `earnings.json` — 0
- `late-jobs.json` — 0
- `orders.json` — 0
- `seats.json` — 0
- `wallets.json` — 0

## Local checks

- The batch board GET identified both landed holder inputs. The API guide documented the order cap, earnings cursor, and work cursor. Public sample GETs confirmed the wallet and job envelopes, the 2,000-element seat owner index, and 1,160 seat records at the initial read. A warm-up pass cached first pages before end-to-end collection.

**Collection**

`python3 -B tools/collect/api-wallets-02/collect.py` exited 0.

```text
Processed 700/1092 wallets
Processed 800/1092 wallets
Processed 900/1092 wallets
Processed 1000/1092 wallets
orders.json: 452 rows, 0 recorded issues
earnings.json: 40790 rows, 0 recorded issues
seats.json: 226 rows, 0 recorded issues
late-jobs.json: 0 rows, 0 recorded issues
wallets.json: 1092 rows, 0 recorded issues
```

**Synthetic checks**

`python3 -B test/scratch/wallets-02-smoke.py` exited 0.

```text
PASS order cutoff, workflow ID, and candidate extraction
PASS earnings cursor and partial-read failure
PASS seat work cursor, strict cutoff, and unique jobs
PASS late job boundary and unavailable result
PASS synthetic RPC range splitting, mint replay, and supply reconciliation
PASS rate-limit retry and cache resumption
PASS checker rejects one missing wallet
```

**Offline dataset checks**

`python3 -B tools/collect/api-wallets-02/check.py` exited 0.

```text
PASS exact wallet coverage (1092 wallets)
PASS order cutoff, order and earnings counts (452 orders, 40790 earnings)
PASS seat ownership and observed counts (226 seats)
PASS late job order links and creation cutoff (0 links)
```

**Required completion command**

`ls sources/api-wallets-02/orders.json sources/api-wallets-02/wallets.json sources/api-wallets-02/earnings.json` exited 0.

```text
sources/api-wallets-02/earnings.json
sources/api-wallets-02/orders.json
sources/api-wallets-02/wallets.json
```

**Scope and syntax checks**

`python3 -B test/scratch/wallets-02-scope.py` exited 0.

```text
?? sources/api-wallets-02/earnings.json
?? sources/api-wallets-02/late-jobs.json
?? sources/api-wallets-02/orders.json
?? sources/api-wallets-02/seats.json
?? sources/api-wallets-02/wallets.json
?? tools/collect/api-wallets-02/chain.py
?? tools/collect/api-wallets-02/check.py
?? tools/collect/api-wallets-02/collect.py
PASS syntax and trailing whitespace for tools/collect/api-wallets-02/chain.py
PASS syntax and trailing whitespace for tools/collect/api-wallets-02/check.py
PASS syntax and trailing whitespace for tools/collect/api-wallets-02/collect.py
PASS independent membership check (6610 combined holders, 1092 shard wallets)
PASS changed-path scope
```

## Upload size repair

- The upload size check rejected the prior bundle at 17,906,199 bytes against an 8,388,608-byte limit. A local source-blob pack made with `git pack-objects --stdout` measured 17,905,154 bytes for the prior files and 4,766,893 bytes for the repaired files before this log entry. The repaired source archive also measured 5,392,559 bytes using `tar -cf - sources/api-wallets-02 tools/collect/api-wallets-02 | gzip -1 | wc -c`.
- The batch board fetched via curl returned 13,253 bytes and confirmed both chain holder inputs were landed. The repair reused the saved API records.
- The two decoder invocations above, each piped to `cmp` against the original export in scratch, exited 0 and reproduced both original files byte for byte.
- A `python3 -B` scratch fixture initially had a bracket typo at stdin line 8. The corrected collector export fixture exited 0 and printed three exports of 2, 4 and 0 rows with zero issues, followed by `PASS collector export preserves repeated values, nulls, empty lists and empty earnings`.
- The offline dataset checks and required `ls` above were rerun after encoding and produced the same output with exit code 0. `git diff --check HEAD` exited 0 without output.
