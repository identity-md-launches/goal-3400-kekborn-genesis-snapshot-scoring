# Genesis API catalog

This target collects the public network lists needed to join job, goal, workflow,
oracle and seat data. It does not collect orders, reconstruct historical seat
ownership, attribute payments, classify jobs, or calculate contribution scores.
Those are separate targets in the Genesis batch.

The input is the repository's immutable `snapshot.json`: the exclusive creation
cutoff is **2026-11-25T11:00:00Z**, Ethereum block **26491756**, Robinhood Chain block
**122989778**. Both scripts verify those values and never write the snapshot.

## Commands

Run from the repository root, with Python 3.9 or later and no dependencies:

```sh
python3 -B tools/collect/api-catalog/collect.py --reads-per-minute 45
python3 -B tools/collect/api-catalog/check.py
python3 -B tools/collect/api-catalog/test_catalog.py
ls sources/api-catalog/job-index.json sources/api-catalog/goals.json sources/api-catalog/workflows.json sources/api-catalog/oracle-requests.json
```

`collect.py` only sends HTTPS GETs to `https://api.imd.fun`. Its default is 60 reads
per minute; the submitted collection used 45. Values at or above 90 are refused.
The pace is per process, so lower it further when other collectors share an IP.
HTTP 429 honors `Retry-After` (seconds or HTTP date); transient HTTP, network and
JSON failures are retried, with five attempts by default. A missing or malformed
`Retry-After` uses exponential backoff. Every attempt, including a recovered
failure, is retained in `coverage.reads`; a read that still fails is also in
`errors`. Long retry waits print progress in intervals of at most 30 seconds.

A rerun replaces these seven files with a fresh observation of the same creation
cohort; mutable status, counters and capabilities can change. Each individual
file is replaced atomically. The set of files is not a transactional API snapshot,
and interrupting a rerun can leave files from different runs. Run `check.py` after
collection. `--output-dir` and `--input-dir` allow comparison in another directory.
The collector and checker return nonzero on collection or validation failure.

## Saved format

Every source is one compact UTF-8 JSON object with exactly these top-level keys:
`source`, `fetchedAt`, `coverage`, `errors`, `columns`, `rows`. Each row is an array
aligned with `columns`. Addresses in address-valued strings are lowercase;
token amounts remain raw decimal integer strings, without float conversion.
`fetchedAt` is the last read's completion time, not the cutoff time.

| File in `sources/api-catalog/` | Content |
| --- | --- |
| `job-index.json` | Every visible pre-cutoff job, including oracle panels: id, state, template, createdAt, updatedAt, launchKind, projectId, projectVersion. Project columns are copied from `project.id` and `project.version`. |
| `goals.json` | One row per index entry with template exactly `goal`, using both `/goals/<id>` and `/goals/<id>/children`. Item totals come from `items.total`, `items.accepted`, `items.failed`. |
| `workflows.json` | Pre-cutoff workflow id, status, contractsJobId, frontendJobId. Source creation times are retained in `coverage.rowCreatedAt` for independent cutoff checks. |
| `oracle-requests.json` | Pre-cutoff request id, status, jobId, answerType, chainId, createdAt, attestedAt. API attester metadata is retained with each page. |
| `seats-owners.json` | tokenId and owner from the full `owners` array; tokenId is its zero-based array index, as documented by the API. The API's `at`, chain, collection, count and holder count are retained in coverage. |
| `seats-records.json` | Every source row in source order, preserving all supplied fields. Any additional fields join the column list. The API count is retained. |
| `capabilities.json` | One row, one `response` cell containing the entire returned JSON object, including pricing, launch options and limits. No fields are dropped. |

Goal `children` cells are arrays of arrays, with columns
`[key, action, jobId, state]` declared in `coverage.childrenColumns`. Extra source
child fields, including workflow information where supplied, are retained in
`coverage.additionalChildFields` keyed by parent jobId and child key. An empty
children array is a successful response with no children; null denotes an
unavailable response and is accompanied by an error. The parent row's jobId
always comes from the index, even when the goal endpoint fails.

Child `jobId` is copied from `child.jobId` when non-null, otherwise from the
explicit `child.job.id` field if supplied. Conflicting direct and nested IDs are
an error. Child `state` keeps the child's own state (for example `opened`), while
the nested job's state and workflow status remain in the extra fields. This
preserves the actual job/workflow links without equating different state fields.

`coverage.nullPolicy` and `coverage.nulls` explain and count source nulls, omitted
fields and unavailable responses by column. No omitted value is filled with a
guess. A null `project` supplies neither a project ID nor a version.

## Pagination and completeness

The three list endpoints request `limit=500` and start with `before=T`, without
`exclude=oracle`, status, search or template filters. Each next cursor is the
oldest returned `createdAt` plus **one millisecond**. Overlapping records are
deduplicated by ID and the first observation is retained. If a repeated record
changes during the crawl, the ID and page are recorded. Each page's `count` must
equal its actual size; it is never used as an overall count.

Collection continues to an empty page or an exhausted short overlapping page.
A full repeated page or stalled cursor is an explicit error, not evidence of
completion: a timestamp bucket containing 500 or more rows can exceed what a
time-only cursor exposes. No such saturation occurred in the saved collection.
Page order, cursor continuity, overlap counts, returned creation times and the
termination reason are saved and checked offline.

`coverage.total` is the number of unique selected IDs. `sortedIdsSha256` is:

```python
hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()
```

There is **no trailing newline** in that digest input. This differs from each
file's SHA-256, which hashes the actual saved bytes including the final newline.
Read ledger digests hash the original HTTP response body before projection and
normalization. They are provenance metadata; the full HTTP bodies are not saved,
so these body digests cannot be independently recomputed from projected rows.

Workflow and oracle references missing from the index are explicitly enumerated
in `coverage.absentJobIds`, with referring row, field, jobId and reason. Goal
child absences use `coverage.absentChildJobIds`. An absent reference establishes
only that the ID was not returned by this pre-cutoff index. It does not establish
whether the job was created later, removed, private, or omitted by the API.

## What these records cannot establish

Completeness means exhaustion of the API-visible lists, not proof that the API
contains every job ever created. The API can change during a multi-request crawl,
and inaccessible or deleted records cannot be discovered by these list routes.
The current API guide is at <https://imd.fun/docs/llms.txt>.

Job, goal, workflow and oracle **creation** times are filtered before T. Status,
updatedAt, attestedAt, item counts, writeup and judge are observed when read, and
may reflect events after T. These catalogs cannot reconstruct their historical
values. A later attestation remains the returned value, not a fabricated null or
evidence that it existed at T. Order eligibility must still be checked against
the actual order creation time by the order/job targets.

Both seat files explicitly say they are what the API reported when read, **not
ownership or work totals at T**, and include that read time. Owners also preserve
the API's own `at`; seat records may come from the documented cache of up to ten
minutes. The capabilities response similarly describes current availability and
prices, not historical spending. Historical seat ownership needs chain transfer
history; historical work needs dated submissions/reviews; spending needs actual
orders and payment evidence. None of those values is inferred here.

In this collection, the index contains 9,620 `skill:oracle-assess` jobs while the
9,504 listed oracle requests refer to 9,504 distinct indexed jobs of that
template. The other 116 jobs have no request in this list. Their cause is unknown;
this does not mean they were unpaid or unsuccessful. Job detail records collected
by the job targets may provide an `oracleRequestId` for further verification.
The sets can be reproduced by subtracting request `jobId` values from index IDs
whose template is `skill:oracle-assess`.

## Offline checks

`check.py` checks the fixed snapshot values; the compact six-key format; row
widths, unique IDs, required fields, null accounting, lowercase addresses and raw
amount strings; page evidence and exclusive cutoff; total and sorted-ID digests;
one row plus both reads for every goal; every workflow/oracle job reference being
indexed or explicitly absent; child-reference absences; and seat read-time
caveats. It checks internal consistency, not the truth of an API response.

`test_catalog.py` uses synthetic in-memory API responses and a fake clock. It
tests equal timestamps across page boundaries, saturated timestamps, the strict
cutoff, missing timestamps, per-page counts, read pacing, 429 Retry-After,
HTTP-date delays, exhausted retries, permanent 404s, and deliberate corruption of
IDs, goal coverage, reference ledgers, digests and seat time semantics. Temporary
fixtures are created only in `test/scratch/`; they are not delivered as sources.

The [validation report](REPORT.md) contains the file inventory with row counts
and SHA-256 values, executed validation commands and outcomes, and observed limits.
The required result artifact contains only `status`, `check`, and `notes`, as
specified by the task. Its notes link to the report and include the report's hash.
The result artifact's own hash is reported separately to avoid self-reference.
