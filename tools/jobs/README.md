# Genesis normalization

From the repository root, run `python3 -B tools/holders/build_holders.py` and
`python3 -B tools/jobs/build_jobs.py` in that order to regenerate the CSVs.
Add `--check` for byte comparison. Both builders use Python's standard library
and saved sources. The normalization cutoff is 2026-11-25 11:00:00 UTC, exclusive
for order creation. See [REPORT.md](REPORT.md) for counts, gaps, and source hashes.

## Holder records

Balances are exact integers in the token's smallest unit. The displayed values
have 18 decimal places. Addresses with a positive balance on either chain are
retained, including pools, contracts, delegated accounts, and dust. Addresses
are normalized to lowercase and merged by equality across chains.

The legacy `is_contract_*` fields mean code was nonempty for the saved read.
Use `eth_code_kind` to distinguish ordinary contract code, EIP-7702 delegation,
and empty code at the Ethereum snapshot block. Its read window records when the
archival calls were made. Robinhood fields show the original `latest` block tag
and source collection time. The exact block and per-account read times were
unavailable. Its nonempty code is unclassified because only a boolean was saved.

## Job and payment records

- `wallet` is supported by a unique observed order payer or non-oracle
  `paidBy`. Payer conflicts leave the wallet blank with a reason. Order
  multiplicity with one agreed wallet can preserve the link while withholding
  eligibility. Oracle work needs an order for wallet attribution.
- `order_created_at` is the observed order timestamp. The admission time of
  an intake record can supply an upper bound when that record names the
  specific order and payer and reports a successful payment. The bound goes
  in `order_created_upper_bound`. A bound at or after the cutoff cannot establish
  that the order was created before it. An unreadable supplied order timestamp
  makes eligibility unknown. Job time and transfer time cannot substitute for
  an order timestamp.
- `eligible=true` requires a readable qualifying timestamp or bound, a
  successful payment observation, and an unambiguous order and payer.
  An explicit failure or order creation at or after the cutoff is ineligible.
  With missing or conflicting evidence, consult `eligibility_reason` and
  `data_gaps`. Linkage and eligibility are distinct.
- A job linked to a qualifying order can have a later admission, execution,
  or result. Status, deliverables, and quality describe observations at source
  read time. The collected API data does not reconstruct their historical
  values at the cutoff.
- `counted_under` points to the terminal canonical root. Explicit goal
  children and workflow stages are folded transitively. An independently
  paid continuation stays separate, including when its order misses the cutoff.
  Generic `parentJobId` alone carries no grouping implication.
- Oracle-group results are expanded by request ID. Multiple saved panel jobs
  for the same request use its catalog job or an unambiguous explicit order job
  as representative. Panel seats and duplicate panel jobs do not add credit.
  Multiple questions can share `order_id` and `payment_tx`. Downstream spending
  should deduplicate chain transfer logs by chain, transaction, and log index.
- `counts=true` is reserved for an eligible linked canonical root.
  `is_holder` identifies snapshot holders. The six mapped categories use
  template, launch, site, goal, or oracle request evidence. `unknown` marks a
  missing category.
- `scope`, `quality`, and `deliverables` retain nullable fields and include
  child observations on a canonical root. The complexity proxy sums evidenced
  node counts, distinct role counts, launch and chain indicators, and additional
  project versions across the root and its folded stages. The root's accepted share
  uses total accepted nodes divided by total nodes in members where both are
  observed. Child observations remain individually inspectable.
- `deliverable_ok=true` requires an explicit complete non-oracle result, live
  launch status, an attested oracle request, or one of these signals on a folded
  stage. A recorded URL, commit, or file by itself does not establish success.
- For job fields, detailed projections take precedence over the catalog index.
  Late and raw evidence fills nulls and records conflicting non-null values in
  `data_gaps`. A conflicting payer or oracle request identity withholds credit.
  Source failures and reasons for missing fields remain observable.
