# Genesis e04 normalization report

The normalization target rebuilt `holders.csv` and `jobs.csv` from landed
records. The fixed snapshot remains 2026-11-25T11:00:00Z, Ethereum block
26491756 (10:59:59Z) and Robinhood block 122989778 (11:00:00Z).
The two snapshot files match the original Git bytes. No new chain or job
records were fetched for this target. The batch board was the only network read.

## Holders

| Measure | Ethereum | Robinhood | Merged |
| --- | ---: | ---: | ---: |
| Positive wallets | 4,840 | 2,131 | 6,610 |
| Raw balances summed | 4210819525771000000000000 | 71170146452000000000000 | 4281989672223000000000000 |
| Transfer logs | 349,832 | 117,301 | |
| Wallets on both chains | | | 361 |

All original holder CSV fields and values match the prior CSV. Ethereum
archival code reads classify 2,945 empty, 1,514 EIP-7702 delegated, and 381
ordinary contract-code accounts. The recorded read period is 14:57:10Z
through 15:15:04Z on November 25. Ethereum balances agree with 15,959
snapshot `balanceOf` reads, including zero-balance addresses, and the
independent Transfer replay agrees with the saved scan.

Robinhood's original code flags came from `latest` around the original
source collection at 12:19:09Z on November 25. They show 1,314 empty and
817 nonempty observations. The historical state read failed with RPC
`-32000`, historical state unavailable. The concrete block, per-wallet
read time and EIP-7702 classification are missing. The chain check found
27 matching replay responses and 11 unavailable. Transfer replay remains
the balance evidence.

## Jobs

| Status | All rows | Counted roots |
| --- | ---: | ---: |
| Completed | 16,462 | 3,566 |
| Abandoned | 349 | 131 |
| Failed | 209 | 91 |
| Ongoing | 1 | 1 |
| Unknown | 7 | 0 |
| Total | 17,028 | 3,789 |

| Category | All rows | Counted roots |
| --- | ---: | ---: |
| Code/contracts | 1,489 | 908 |
| Research | 1,817 | 457 |
| Audit | 493 | 390 |
| Website/product | 3,052 | 979 |
| Oracle/indexer | 9,630 | 929 |
| Multi-agent goal | 81 | 13 |
| Unknown | 466 | 113 |

- Retained all 17,021 prior job IDs and added 7 IDs with order references
  or evidence but without a saved job detail or state. 7,662 rows are linked,
  including 5,822 holder-linked rows. The 9,366 unlinked rows have reasons.
  8,615 are oracle rows with no matching order and 751 have neither an
  observed payer nor a matching order.
- 3,789 eligible rows are canonical roots linked to holders. This is 1,408
  fewer counted holder roots than before. All 1,881 prior holder roots
  counted using the job-time fallback lack a matching order and now have
  unknown eligibility. There are 13,153 unknown and 86 false eligibility
  rows, including 79 explicit failures and 7 orders at or after the cutoff.
  3,317 roots qualify using observed creation timestamps and 472 using
  pre-cutoff intake admission bounds. No counted job with an observed
  creation timestamp has a job creation at or after the cutoff in this
  dataset. The late-admission regression confirms a pre-cutoff order can
  qualify even when its job starts later.
- Reconciled 3,910 corrected order IDs with 3,357 raw paid-by observations
  from 462 saved responses and 551 explicitly linked intake observations from
  470 saved responses. The intake records also contain 3 refusals without
  order IDs. They do not establish paid work. No landed result or payer
  conflicts required withholding eligibility beyond explicit failures and
  missing records. Conflicting payer, order, or oracle evidence is covered
  by regression tests.
- The 3,789 counted roots reference 3,787 unique orders and 3,695 distinct
  nonempty payment hashes. Each of the two oracle-group orders contributed
  two distinct questions sharing its order and payment. There are 926 counted
  oracle request IDs and 3 other counted oracle/indexer jobs based on the
  indexer template.
- There are 185 folded rows. 181 canonical roots carry child scope,
  deliverable, and quality observations, including 63 counted roots.
  There are 527 independently paid continuations, including one after the
  cutoff. The independent audit verified canonical endpoints, unique counted
  request IDs, payer/holder membership, timestamp evidence, and an unlinked
  reason on every unattributed row.
- Across counted roots, 3,441 deliverables have an explicit success signal,
  202 have an explicit incomplete result, and 146 have no usable success
  signal. Across all rows, the corresponding totals are 15,970, 386, and 672.

## Coverage and remaining gaps

- The original eight non-oracle job shards cover 7,401 unique IDs. Their
  index/detail sets, prefix assignments, declared counts, and whole-list
  digests agree. The union SHA-256 over sorted IDs joined with LF is
  `4efda04e60ce9faa397301360b6f9744349f36823d48f98d2b5b6979424a11d3`.
  The six wallet shards cover the entire merged holder list. Their counts
  are 1,086, 1,107, 1,092, 1,134, 1,099, and 1,092.
- The e04 evidence index and records cover the same 516 shard-00 IDs and
  488 shard-01 IDs. Shard 00's two reads per ID failed with a remote
  connection closure. Shard 01 has 488 successful details and results,
  with result and detail body hashes verified. Original shard 04 has 908
  successful raw details and 908 successful raw results. Body hashing and
  recorded source conflicts protect the evidence used in the rebuild.
- 9,128 rows lack details. Scope and quality gaps are explicit, and 64 rows
  show conflicting `updatedAt` observations retained in `data_gaps`.
  Status and quality are read-time observations. The newest-100 wallet
  history cap and targeted intake lookup leave order coverage incomplete.
  No missing values were fabricated.
- Scores, tier cut points, genetics, and the overall manifest are owned by
  downstream targets. `eligible`, `counts`, `order_created_upper_bound`,
  `oracle_request_id`, and `payment_tx` are available to those consumers.

## Commands and results

| Command | Result |
| --- | --- |
| `curl -sS -A 'Mozilla/5.0' 'https://api.imd.fun/goals/c4da2fbc-8de8-490f-9aa5-32ad67dda3e3/board?batch=genesis-repair-e04'` | Exit 0. Read the board before repository inspection. |
| `python3 -B tools/holders/build_holders.py` | Exit 0. Wrote 6,610 wallet rows. |
| `python3 -B tools/jobs/build_jobs.py` | Exit 0. Wrote 17,028 job rows. |
| `python3 -B tools/holders/build_holders.py --check && python3 -B tools/jobs/build_jobs.py --check && python3 -B tools/jobs/test_build_jobs.py` | Exit 0. Both byte comparisons passed. All 28 job tests passed. |
| `python3 -B tools/holders/test_build_holders.py` | Exit 0. All four tests passed. |
| `python3 -B test/scratch/normalize/audit.py` | Exit 0. Independently checked exact raw/display balance arithmetic and previous holder fields, job retention, paid cutoff, grouping, question uniqueness, and unlinked reasons. |
| `python3 -B tools/e04-chain-ethereum/check.py` | 40 passed, zero failed, three unavailable. |
| `python3 -B tools/e04-chain-robinhood/check.py` | Exit 0. Snapshot, source hashes, balances, and replay coverage checks passed. |
| `python3 -B tools/e04-orders/check.py` | Exit 0. 51 checks passed. |
| `git diff --check` | Exit 0. No whitespace errors. |
| `git diff --exit-code -- snapshot.json sources/snapshot-blocks.json` | Exit 0. Both snapshot files match the original bytes. |

During development one payer-conflict fixture expected a generic ambiguity
reason after the builder began emitting the more specific conflict reason.
The assertion at `tools/jobs/test_build_jobs.py:197` in that run was updated and the final suite passed. The scratch audit
initially tried to read an order ID from a null intake order. Its rerun
handles refusal rows and passed.

## SHA-256 inventory

This table records decisive inputs, chain and order corroboration,
and generated CSVs for the checked rebuild.

| File | SHA-256 |
| --- | --- |
| `holders.csv` | `9438fa7c8f0a64f317f8f715944615d5c438e420dbe7368b847063c461783a93` |
| `jobs.csv` | `1249989a8b8ab31f6c538ab8faf231dc3fe57e948e2e1bcc00d3280ec7a8884c` |
| `snapshot.json` | `b94dc67d27bc6aaa49811431ee68a3fd25d6c90f09de8fde0f1920792d7dd425` |
| `sources/api-catalog/goals.json` | `625864a29b3a76df18d8af715a2d83e909063e32cad35d336023a399edf94e23` |
| `sources/api-catalog/job-index.json` | `16c7c39733f62902ea0a63307f541a2b9327021687955f2b967a09e6ae143582` |
| `sources/api-catalog/oracle-requests.json` | `2784c64ac14a502a594e68a5719b5985e64858b0dcb4d366d5841ecff8bb70ff` |
| `sources/api-catalog/workflows.json` | `cc54ddefe546dd508ed6d1bb41937e0009666a3b8bb8287105fb305a6dd26577` |
| `sources/api-jobs-00/index.json` | `b2759bd71782f59357631ac9219a6b4f1048fa98c42a65e6b9b164fc34612331` |
| `sources/api-jobs-00/jobs.json` | `e6b4b12dbfd69aa27151445341e6022a4eb9ee829fe72d07f484fa1440a5c4aa` |
| `sources/api-jobs-01/index.json` | `f1a4f8b7568f546c589db113353ae85841da3721663efe2235821d4d6c7552d1` |
| `sources/api-jobs-01/jobs.json` | `c2bf7fb2b633fae57108c8f28cc7d816d674d3efe62a0879ff0a8340f1519076` |
| `sources/api-jobs-02/index.json` | `83dc409d42fb044dfe0040649134be24deaf146983e2e068f343be364dd6106a` |
| `sources/api-jobs-02/jobs.json` | `6bbff0cced4937785e629247a7f766436aa754995759129b54ad063c4ff9293f` |
| `sources/api-jobs-03/index.json` | `13b2fa49127fc6db9ebbc7455ba886b29239fa61acb6579024634024ed6866e5` |
| `sources/api-jobs-03/jobs.json` | `22ae7fc59810d2e6ec05ae9c386fb87c4bb7dc853e77117f79052ba666958454` |
| `sources/api-jobs-04/details.json` | `311182a6e29ca2c8ad9bd0f44f8afcc30153778634be5b4688be38ee86d070b7` |
| `sources/api-jobs-04/index.json` | `0533c0484b9c21d1f86f2a767afe29cc3624573bf4d53c44e638d193b88cc396` |
| `sources/api-jobs-04/jobs.json` | `670b94dd9b23bacb4fa8880b50f0af52d0dc09455cebc315b0f03325a587e3f0` |
| `sources/api-jobs-04/results.json` | `884fd5315e0e1ca8a3fab3df5d5fee9b708b3f1ea500238034f1ce33a560ce6b` |
| `sources/api-jobs-05/index.json` | `fda79ed25a3ddaff429a9b812741a1a990f0a0a5951a00ae58d6578cc3afecac` |
| `sources/api-jobs-05/jobs.json` | `73297aee70dd9f3cd8825c8e285554bca90f1629ac73a091d3ae689639340b45` |
| `sources/api-jobs-06/index.json` | `df5ce7518393d5ae010413c9909e604d93b2acff0bbb944d374df4c93fb9e43e` |
| `sources/api-jobs-06/jobs.json` | `dfdfd78d4be42dbd658ea6f4c701809ad01e48de2f4c6fccfb145a9cacd0cae9` |
| `sources/api-jobs-07/index.json` | `71f3f1bbfa67c99e0c5c357155adf8287c75b431c6d04649f89dd82b223fdcd6` |
| `sources/api-jobs-07/jobs.json` | `880aa8c674f5f5cd415104e919a9a477e620e20c7387cac40e961118cbc9fad1` |
| `sources/api-wallets-00/late-jobs.json` | `6ba3da57c49e8f7f44b3c0524d6657de2d99c5dddc645b7fd9d2eb7722f89619` |
| `sources/api-wallets-00/orders.json` | `29c5196bb51a3bf89b346789234210f6ac324eb7250afd28b080e90d074c3d8c` |
| `sources/api-wallets-00/wallets.json` | `d2cf73b1fe189b1a2444e9dcb9bc701f48eff581910ad1e9eac71c9aaffa067d` |
| `sources/api-wallets-01/late-jobs.json` | `12084afdde04f49b701f67de4b48319538a69e2539e9d8fbedf18fd64a726e30` |
| `sources/api-wallets-01/orders.json` | `b0542c928a84777927a16e4fd628084fa5e9d7374baa040a6a2aae0465485417` |
| `sources/api-wallets-01/wallets.json` | `efbd291a7f81792b19066f15732d2028f97d1c5a8a3aec21318a20409bd5e881` |
| `sources/api-wallets-02/late-jobs.json` | `658298899c8f53b7ee001b895021264e19d179a45b6aabe1a6ebff0e79ea9551` |
| `sources/api-wallets-02/orders.json` | `f6c49211fd9f1fe42583b22cc6aa5e405f35c485dfa244bb3f2ba66de1ac00be` |
| `sources/api-wallets-02/wallets.json` | `b56ce580ff06aa4e970552545774fa2808ca430ec5246e6988cb337fb7a5867f` |
| `sources/api-wallets-03/late-jobs.json` | `46f46ecec2238f8e7b7d96884328ac8976328b12b173d69fba22650a70738bff` |
| `sources/api-wallets-03/orders.json` | `002903a838bd9f610f4a2110f5bd72626995702dad96bdacedbc9726c02ace1f` |
| `sources/api-wallets-03/wallets.json` | `773cb6b3a3b612c9f488af1ed0a01f0d5999a6ac929491e5d7d63584c9b8c3aa` |
| `sources/api-wallets-04/late-jobs.json` | `d6e4b3720b31881b99d0859611517ef7722a1b01a40b9c16fba560b498e1383e` |
| `sources/api-wallets-04/orders.json` | `233c6859e28d3a8633bfa52ee1938336726eae3a9726480060d17071e0d9a1dc` |
| `sources/api-wallets-04/wallets.json` | `3fe59d586dbf7008ad156123ff68f0056cf778533dcd75a2508100919db78345` |
| `sources/api-wallets-05/late-jobs.json` | `a30cbecf04b67855f93723c8e6ef5b0f426fbb1662647a5499c8dab94d19a216` |
| `sources/api-wallets-05/orders.json` | `140e2247e4cf1d1e54f88cdba8ec416392482087cd64162103056040bd9a398a` |
| `sources/api-wallets-05/wallets.json` | `4f0a48e48f75a0320cf83531e3978701ab6fa6edd8bc2b2b8cf75f24af364110` |
| `sources/chain-ethereum/holders.json` | `af6eb561b70b77028e651fd4f598a947f2bdafac40893f09446454b28cfcebb3` |
| `sources/chain-ethereum/payments.json` | `7742994a3199b2ab9258a0e89a6a64ff2ebbbd0f8e6da2c4005dc77f757c2a2e` |
| `sources/chain-ethereum/scan.json` | `158bb3a458b639eae9b99e5402fab47f89e49bfeefecbfa925ad71a0fe57af40` |
| `sources/chain-robinhood/holders.json` | `326df5b986fa21301e76002f1cacb9a9221d48f18c9e1a210e181bdbe1eb4024` |
| `sources/chain-robinhood/payments.json` | `38c1c5ea0d2aa37517500fd9a01a6ada58b034d80b6cdc2f7e1abdedb5c623e3` |
| `sources/chain-robinhood/scan.json` | `138ec40a791eccb666b209c63c95ba4ae6985734cc5f71afa80e34f9ae0ce08e` |
| `sources/e04-chain-ethereum/balances.json` | `9e3cd1841019f6fa0bca62fd449bff84eb574f9289bec722461287fb6c9916f8` |
| `sources/e04-chain-ethereum/code.json` | `29bd66e57897e2b449f34e65e331cfb0406a7ec7c7f3de174a9588e3ff40f0dc` |
| `sources/e04-chain-ethereum/imd-capabilities.json` | `9e74ea029e000638ecad9fe63f7e72a7adf0c974ad6a640ad57871d8e61e2d56` |
| `sources/e04-chain-ethereum/replay-mevblocker.json` | `b74ffd2834f9170f8561118c1558bdc936813fe541c40ffb09d4d04d8fbaf500` |
| `sources/e04-chain-ethereum/replay-tenderly.json` | `83407195cd45fc59f05fe1ce2ff4b24a5ccfd562b36173f879d90862af9c11d8` |
| `sources/e04-chain-ethereum/state-calls.json` | `aa3c0ae651ef9efa67d8e3f2a82afd8d2f6336019ee35146dbd20cd03206467c` |
| `sources/e04-chain-ethereum/windows.json` | `ff61c5b316d359d065c087c4605b4e50c710f63353843695b1dbb928158be177` |
| `sources/e04-chain-robinhood/rpc-observations.json` | `409ed43f28e2d3914860591fa5a68a9110dbe13f7b0d81172b4a568cfdc9d301` |
| `sources/e04-jobs-00/index.json` | `1ab3f247447bc90c9265a2e748337cef59887d2f1f0ff0b0d4c9b96633d27902` |
| `sources/e04-jobs-00/records.json` | `e403a049eeb3fe0912d3b7caf138ad3834fafe5c22711ea3fadc6a9277f1384d` |
| `sources/e04-jobs-01/index.json` | `60be584392d5089c40c507fd34f4b0478faf6c29a4e03167f9711aed83368dc3` |
| `sources/e04-jobs-01/records.json` | `a24c36dc2458f87239737fb8dab19c1c75784e50631da5e7068405eed44fa42f` |
| `sources/e04-orders/baseline-orders.json` | `f44ecb0d3d0a723ce8f5821a1fbbfe63a61137822747bec4fa83d0176f70485a` |
| `sources/e04-orders/candidates.json` | `d5c4c7dd563ad3c06fa3ef411b04430011cb4cf0ae3980b9f9fe645b49ea9fba` |
| `sources/e04-orders/intake.json` | `05c74ecc2face82c7bc4d6b706b5cb98931377790c7ad71057900f15e2dfee7c` |
| `sources/e04-orders/ledger.json` | `83791ed3fd345913590c3ed7a4c2ff926a516228a4a0ff6c2bc4c85349636a60` |
| `sources/e04-orders/paid-by.json` | `76c772578edae9e48f338bfd2527647f8d0c4e6ad3b71ef8438809e83dda9838` |
| `sources/e04-orders/rpc-receipts.json` | `163e1d7a23c8b2b6400f2cb6b0923e85bfa466a66ec0604e27f18edae8010232` |
| `sources/snapshot-blocks.json` | `102297f76a6b5755fde2c91d13386221a272da3583bf7ac5986accdfdf1b61b0` |
