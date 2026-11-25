# Offline jobs dataset

`python3 -B tools/jobs/build_jobs.py` rebuilds the repository's `jobs.csv` using
Python 3's standard library. It performs no network requests. Run
`python3 -B tools/jobs/build_jobs.py --check` to compare exact UTF-8 bytes without
writing. A difference or missing CSV exits 1; malformed or missing input exits 2.
`python3 -B tools/jobs/test_build_jobs.py` runs the fixture tests. `--root PATH`
selects an alternative input/output checkout for fixtures.

The builder reads, but never changes, `snapshot.json`. It checks the fixed UTC
cutoff `2026-11-25T11:00:00Z`, Ethereum block 26491756, and Robinhood block
122989778. All eight `api-jobs-NN/jobs.json` files, all six pairs of
`api-wallets-NN/orders.json` and `late-jobs.json`, the four named catalog files,
and `holders.csv` are required. Missing input prevents output generation; an
existing CSV is not overwritten on this failure.

## Row universe and serialization

One row for each distinct job ID in the union of
`sources/api-catalog/job-index.json` and all wallet `late-jobs.json` files, sorted
by ID. Detailed jobs outside this union are rejected instead of silently added.
Repeated late references do not add rows. The current 33 late references all
already occur in the 17,021-row catalog. There are 7,401 shard detail records
and 11 additional raw late-job details.

The CSV has one header row and LF line endings. Scalar nulls are empty cells;
booleans are `true` and `false`. `eligible` is `true`, `false`, or `unknown`.
`scope`, `deliverables`, `quality`, `data_gaps`, and `sources` contain compact
JSON; JSON nulls retain unknown values. A null does not mean zero or failure.
`data_gaps` names missing field groups and their reason. Wallets are lowercase.
There are no token amounts in this output: the named jobs schema has no spending
column, and these order extracts do not provide IMD amounts. The scoring worker
must join payment evidence separately; neither an order count nor a balance is
an IMD-spend estimate.

## Column semantics

| Columns | Meaning |
| --- | --- |
| `job_id`, `created_at`, `template`, `state` | Saved job values; detailed fields take precedence over catalog values. |
| `wallet`, `link_evidence`, `unlinked_reason` | Explicit attribution, as described below. |
| `is_holder` | Membership in `holders.csv`; null for an unlinked job. Nonholder jobs remain in the dataset. |
| `order_id`, `order_created_at` | The unique matching order, if one was seen. An order may be shown even when `paidBy` supplies attribution. |
| `eligible`, `eligibility_reason` | Strict order cutoff, falling back to job creation only when no matching order was seen. |
| `counted_under`, `counts`, `grouping_reason` | Explicit goal/workflow grouping, and whether this is a linked, eligible root. |
| `status`, `status_reason` | Deterministic lifecycle classification. |
| `category`, `category_reason` | Deterministic taxonomy with evidence/rationale, including for media jobs. |
| `scope`, `complexity` | Observed dimensions and a nonnegative integer proxy computed from them. |
| `deliverables`, `deliverable_ok` | Saved output signals and a nullable evidence flag. |
| `quality` | Accepted-node share, revisions, review values, and goal observations when available. |
| `is_goal` | Goal catalog membership, or template `goal` / `skill:goal-orchestrator`. |
| `data_gaps`, `sources` | Row-level caveats and the saved source paths used for its job/link/group evidence. The global holder and cutoff inputs are described above. |

Catalog values are supplemented by shard details, then by raw late details only
where fields are still missing. Index/detail disagreements are recorded.
Unknown detail fields are not synthesized from objectives. The shard collectors
encode roles in four different ways (maps, pairs, colon-separated strings, and
lists of those strings); the builder normalizes them into a role-count map.

## Attribution and eligibility

1. Use a valid `paidBy` address, except for oracle panels, which require order
   evidence. An unexpected panel `paidBy` is ignored with a recorded reason.
2. Otherwise use the payer of a unique saved order whose result explicitly names
   the job, or names an oracle request associated with the job. Supported evidence
   includes flattened `resultJobId`, `resultId` with `resultKind=oracle`, and
   retained raw results/child results. Request-to-job relationships come from
   `oracle-requests.json` or a detailed job's `oracleRequestId`.
3. Do not inherit a wallet from a parent, a workflow, a goal, a continuation
   pointer, an objective, or the wallet that happened to collect a late record.
   A workflow ID alone does not meet the explicit job/request attribution rule.
4. More than one distinct matching order leaves the order and its cutoff
   unresolved. `paidBy` can still identify the wallet, but `eligible=unknown`
   prevents counting. Duplicate identical orders are merged; conflicting
   observations of an order fail the build. If a unique order payer disagrees
   with `paidBy`, `paidBy` wins and the disagreement is recorded; the explicit
   order still controls eligibility.
5. When any matching order exists, use its creation time. A missing/invalid
   order timestamp means `unknown`, not a fallback to job creation. With no
   order seen, use job creation and explicitly label that fallback. All
   timestamps must have a timezone. Exactly T is ineligible.

Unlinked reasons distinguish missing detail, missing `paidBy` plus no order,
missing oracle order, invalid/missing order payer, and ambiguous orders.
Attribution establishes the API's payer claim, not independent proof of payment
settlement. Order coverage is limited to the collected holder histories.

## Deduplication

`counted_under` is the row's own ID for a root. Explicit goal child membership
and `workflows.frontendJobId -> contractsJobId` edges resolve transitively to a
root. A workflow's contracts job can itself be a goal child. A child whose root
is missing remains excluded; it is not promoted to a root. Cycles and conflicting
roots fail the build. Oracle question panels are always their own roots.

`parentJobId` and order `continues` are not grouping edges: separately paid
continuations remain separate jobs. No grouping by wallet, objective, project,
or template is performed. `counts = eligible == true AND wallet is linked AND
job_id == counted_under`. It intentionally does **not** require holder membership
or completed status. A scoring consumer should select `is_holder=true` as well
and decide how to treat statuses. Summing every row instead of counting roots
would double count workflow/goal work. Child evidence remains available for
review but is not copied to root rows or counted again.

## Status and category rules

`completed` maps to completed; `cancelled`/`canceled` maps to abandoned. A blocked
job with the structured `needs_input` marker or explicit “waiting/awaiting [on/for]
[the] requester” text maps to abandoned. Other blocked jobs map to failed,
including blocked index-only jobs with no saved reason; the reason column states
this limitation. Explicit failed maps to failed. Known running states
(`executing`, `running`, `pending`, `queued`, `planning`, `created`, `ready`,
`waiting`, `reviewing`) map to ongoing. Unknown states remain null, with a reason.
The script's regular expression and state set are the complete rule.

`TEMPLATE_CATEGORIES` is the complete explicit map. Goals take precedence, then
specific templates (research/report/quick answer/docs, audit/adversarial/fuzz,
website/frontend/site check, code/project/test, oracle/indexer). Image, token-logo,
audio and video templates are website/product **by a stated taxonomy decision**:
these are digital product-content deliverables. This does not claim they were
part of a hosted website. For generic templates, recognized contract launch
kinds map to code/contracts, and an explicit site URL maps to website/product.
Otherwise category is other. A generic graph shape and free-text objective do
not establish a category. There are 465 such other rows in this release.

## Scope, deliverables and quality

Scope preserves node count, normalized role counts, launch kind, chain,
project-version IDs, and unique version count. When launch chain is unavailable,
a single unambiguous oracle-request chain can supply it. Complexity is:

```
known nodes + known distinct roles + 1 if launch kind is present
+ 1 if chain is present + max(0, known unique project versions - 1)
```

An unknown component contributes zero to this evidence-only proxy but remains
null in scope and is recorded in `data_gaps`. Thus a zero complexity value means
no counted evidence, not proven triviality. Version lists are the project's
observed history; they are not exclusively effort on this particular job.
This proxy is not a Genesis score or a verified estimate of effort.

Deliverables preserve commit, repo, delivery timestamp, launch ID/status, site,
media-file count, result-file count/names and result-complete flag. True
`deliverable_ok` means at least one positive saved signal: a commit, site,
`launchStatus=live`, positive media/result-file count, or `resultComplete=true`.
With no positive signal, an explicit incomplete result gives false; otherwise
it is null. Neither job completion alone nor accepted nodes establish it.
A true flag does not mean the URL was fetched, the commit tested, or the media
viewed. File counts rather than full filenames/URLs are all some sources retain.

Quality preserves accepted nodes, accepted share (accepted / total, rounded to
8 decimals; null for absent or zero total), node and judge revisions, sent
review count, and review values without interpreting their scale. Goal catalog
workers, item counts, writeup/judge values and orchestration status are included
under `quality.goal`. Missing review values are not replaced with a favorable
score. Accepted-node signals are API acceptance observations, not independent
certification of functionality.

## Temporal and coverage limitations

The snapshot inputs are final. This does **not** make mutable API fields historical
states at T: details, status, project versions, launch state and quality describe
when their sources were read. Only job/order creation is compared to T. Read-time
changes in `updatedAt` are recorded as disagreements; the detailed record is
preferred over the catalog. There is no attempt to reconstruct an unrecorded
status at the snapshot.

9,609 rows have no saved job details (the regular detail shards excluded oracle
jobs); 11 oracle details are retained in late records. 9,167 oracle panels have
no matching saved payer order. A further 751 detailed jobs have no `paidBy` and
no matching order. These 9,918 jobs stay unlinked. Source `error` cells are
preserved as source diagnostics; in this release 117 are structured notes that
`paidBy` was absent, with an empty failed-read list, rather than transport errors.
The holder collectors' pagination/caps and missing oracle-group child results
limit order coverage. `eligible=true` via the job-time fallback is not evidence
that an unseen order was created before T or that payment was settled.

All named input files were present. Nothing was fetched to fill these gaps;
no transaction, signature, deployment, mint, or payment was performed.
