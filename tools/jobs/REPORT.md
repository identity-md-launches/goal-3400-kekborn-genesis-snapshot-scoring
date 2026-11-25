# Alljobs validation report

The deliverable is `jobs.csv` plus the offline builder, fixture tests and schema
notes in this directory. Only assigned delivery paths were changed. The fixed
snapshot and all collected sources were read without modification. File row
counts and SHA-256 values are recorded in the required `alljobs/result.json`
output; non-tabular files have a null row count.

## Commands and outcomes

| Command | Exit / result |
| --- | --- |
| `curl -sS --fail 'https://api.imd.fun/goals/c4da2fbc-8de8-490f-9aa5-32ad67dda3e3/board?batch=genesis-pipeline-e03'` | 0; retrieved the board first. It reported 18 landed targets, alljobs working, and three later targets waiting. This was the only network read. No retry or failed read occurred. |
| `rg --files` with dependency/artifact exclusions; targeted `rg -n` and `sed -n` over source collectors | 0; found all named inputs and identified four node-role encodings and several late-record schemas. Repository text was used as format evidence only. |
| Python standard-library schema/count inspections over named source JSON/CSV | 0; 17,021 indexed jobs, 7,401 detail-shard rows, 33 late references (none outside the index), 11 raw late details, 3,354 orders, 6,610 holders. Some verbose inspection output was display-truncated; subsequent inspections selected the relevant fields. |
| `python3 -B tools/jobs/build_jobs.py` | 0; `Wrote jobs.csv (17021 rows)` and the JSON counts below. An initial successful build was followed by an improvement to preserve oracle-request chain evidence, then the final successful rebuild. |
| `python3 -B tools/jobs/test_build_jobs.py` | 0; `Ran 22 tests` / `OK`. All fixture checks passed. |
| `python3 -B tools/jobs/build_jobs.py --check` | 0; printed the counts below and `PASS: jobs.csv matches offline rebuild`. |
| `python3 -B test/scratch/jobs-audit/audit.py` | 0; independent full-dataset assertions passed, as quoted below. Scratch audit scaffolding is not submitted. |
| `git status --short --untracked-files=all` | 0; listed only jobs.csv and tools/jobs delivery files before the required result output was written. |
| `git diff --check` | 0; no diagnostics. New files are untracked, so their executable validation is provided by the builder/tests above. |
| `git diff --exit-code -- snapshot.json` | 0; no output; fixed snapshot unchanged. |
| Python standard-library SHA-256 / CSV-row-count / zlib inspection | 0; hashes recorded in the result inventory; CSV 29,403,088 bytes, 17,021 rows; zlib level-6 size 1,986,023 bytes (not a remote upload certification). |
| `ls jobs.csv tools/jobs/build_jobs.py` | 0; printed `jobs.csv` and `tools/jobs/build_jobs.py`. This is the target's required check. |

There were no unexpected failing checks. Fixtures deliberately exercised the
following rejection paths and verified the expected result: changed CSV
(`--check` exit 1), missing required input (exit 2, existing CSV preserved),
changed snapshot, invalid source row width, conflicting order observations,
ambiguous group roots, grouping cycles, and repeated role names. Ambiguous payer
orders and unknown timestamps stay unresolved and uncounted instead of failing
or selecting a guessed value. These are passing negative tests, not unresolved
build failures.

The 22 tests also cover timestamp offsets, exactly-T exclusion, order-time
precedence, late-only jobs, duplicate late references, raw detail projection,
`paidBy` precedence and case normalization, oracle order-only linkage, request-ID
joins, transitive goal/workflow grouping, separately paid continuations, absent
roots, holder vs nonholder distinction, null propagation, all role encodings,
status/category maps, media category reasons, no free-text category guessing,
complexity components, and CSV/JSON escaping and deterministic bytes.

Independent audit output:

```
PASS exact sorted index/late union: 17021 unique rows; late records: 33
PASS wallet evidence, holder membership, root-count rule, strict timestamp cutoff, JSON fields for every row: 7103 linked
PASS workflow root equality and oracle order-only independent roots
Observed details: 7412 orders: 3354 holders: 6610
CSV bytes: 29403088 sha256: 9c859d80d8b9cdc863cf3b1f64f79707e2b5f1d5b89b9d0639ad5844a62a6206
```

## Counts

| Status | All rows | Counted roots |
| --- | ---: | ---: |
| completed | 16,462 | 6,597 |
| abandoned | 335 | 205 |
| failed | 223 | 146 |
| ongoing | 1 | 1 |
| Total | 17,021 | 6,949 |

| Category | All rows | Counted roots |
| --- | ---: | ---: |
| code/contracts | 1,489 | 1,375 |
| research | 1,817 | 1,796 |
| audit | 493 | 493 |
| website/product | 3,052 | 2,397 |
| oracle/indexer | 9,626 | 459 |
| multi-agent goal | 79 | 13 |
| other | 465 | 416 |

| Unlinked reason | Rows |
| --- | ---: |
| oracle_no_matching_order | 9,167 |
| paidBy_missing_no_matching_order | 751 |
| Total unlinked | 9,918 |

7,103 rows have wallet evidence; 5,263 of them link to snapshot holders. There are
182 explicitly grouped children. 6,949 rows meet the eligible/linked/root rule;
5,197 of those roots link to holders. All 17,021 rows pass the creation cutoff
under the specified order-first, job-time-fallback rule. This does not assert
that missing orders were observed or paid.

## Missing or uncertain

All required input files are present. The saved datasets do not provide full job
details for 9,609 rows, an attributable wallet for 9,918 jobs, or historical
mutable API values at the fixed snapshot time. Scope/deliverable/quality nulls
remain in the CSV with reasons. The 117 source error cells in detailed rows are
structured absent-payer notes with empty failed-read lists, not 117 failed HTTP
reads. The 51 index/detail disagreements concern `updatedAt`; details take
precedence and the discrepancies are recorded per row.

Orders cover the collected holder histories and inherit their source caps;
oracle-group child details are not retained in the two relevant flattened order
records. No wallet is inferred from those groups, a parent, or a workflow ID.
Generic job shapes remain `other` absent category evidence. A positive
`deliverable_ok` is a saved evidence flag, not an independently executed test.
The full schema, formula, classification policy, and downstream scoring cautions
are in `README.md`. No payment amounts or Genesis scores were invented; those
are other targets' work.
