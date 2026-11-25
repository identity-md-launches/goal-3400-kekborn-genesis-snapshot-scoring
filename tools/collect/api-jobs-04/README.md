# IMD job details, shard 04

This collector uses public GET routes only. It collects jobs whose IDs start
with `8` or `9`, excluding oracle panel jobs, created strictly before
`2026-11-25T11:00:00Z`. It never changes `snapshot.json`.

From the repository root, with Python 3 and no third party packages:

```sh
python3 -B tools/collect/api-jobs-04/collect.py
python3 -B tools/collect/api-jobs-04/check.py
ls sources/api-jobs-04/jobs.json sources/api-jobs-04/index.json
```

The collector checkpoints every completed endpoint read using atomic file
replacement. Rerunning reuses the completed list and saved reads. A stopped
run continues at its saved cursor or first missing detail/result. Exhausted
reads are retained after three attempts; explicitly use `--retry-errors` to
retry them and retain the earlier attempt history. `--max-jobs N` limits the
number of jobs newly processed in a run. `--rebuild-only` rebuilds the two
derived tables entirely offline from saved source evidence. An offline rebuild
preserves original fetch times.

On Unix, an advisory lock on `collect.py` itself prevents overlapping collectors
or rebuilds from writing the same checkpoints; a second invocation exits with
an explicit error before reading the network or changing data. No lock file
is created. The scripts use only the Python standard library, including Unix
`fcntl` for this lock.

Requests are sequential, spaced by at least 1.1 seconds by default (under 55
reads/minute). `--interval SECONDS` may increase spacing; values below 0.7
seconds are refused to keep the collector below 90 reads/minute. HTTP 429
increases spacing and honors both numeric and HTTP-date `Retry-After`. Each
endpoint is tried at most three times per run. Attempt timestamps, statuses,
errors and Retry-After delays are retained in its raw row. An exhausted list
read stops collection with incomplete coverage recorded; the collector never
skips an unreadable page. Separate collectors sharing an IP must coordinate
their aggregate traffic outside this script.

## Saved sources

All five JSON files are compact objects with exactly `source`, `fetchedAt`,
`coverage`, `errors`, `columns` and `rows`. Every row is an array in column
order. Standalone wallet address values are lowercase; original prose is
preserved. These tables do not introduce token amount fields.

| File | One row represents |
| --- | --- |
| `sources/api-jobs-04/pages.json` | A list-page response, including its cursor, full jobs array and read attempts |
| `sources/api-jobs-04/index.json` | One selected job: ID, creation time, template |
| `sources/api-jobs-04/details.json` | One detail endpoint: ID, time, HTTP status, complete JSON payload, error, attempts |
| `sources/api-jobs-04/results.json` | One result endpoint with the same raw columns |
| `sources/api-jobs-04/jobs.json` | One selected job with the requested detail/result projection |

Pagination starts at the fixed cutoff with `exclude=oracle&limit=500`. The
next cursor is the previous page's minimum `createdAt` plus one millisecond.
Repeated IDs are dropped globally. `count` is checked against that page's
array length, never treated as a total. A terminal empty page, or a short
page that repeats the final timestamp without moving the cursor, terminates
the scan. A full page stalled at one timestamp is reported as incomplete:
this interface cannot safely skip such a boundary.

Both derived tables carry `wholeListCount` and
`wholeListSortedIdsSha256` in `coverage`. The hash is SHA-256 over unique IDs
sorted lexicographically, joined by one LF byte, encoded as UTF-8, **without
a trailing LF**. The full list can be recovered and its hash independently
checked from `pages.json`. Index rows include only this shard's IDs. Detail
and result rows are sorted by ID and saved separately so either endpoint
can resume without re-reading its successful counterpart.

## Field interpretation and uncertainty

`paidBy` comes exclusively from the detail response's `paidBy`. No parent,
project, workflow, delivery, seat, recipient, transaction or objective is used
to infer a payer. A null or absent payer remains null. This collector does
not try to join wallets or classify jobs; those are other batch targets.

Project versions are their job IDs sorted by API-provided creation time,
oldest first. `nodeRoles` is a sorted array of `[role,count]` pairs.
`nodesAccepted` counts nodes in state `accepted`. Revisions and judge
revisions are summed only when every node names an integer value; an absent
value does not become zero. `seats` lists distinct named
`nodes[].seat.tokenId` strings; an explicitly null seat has no named
assignment. `reviewsSent` counts review batches in status `sent`;
`reviewValues` flattens **all** their entries' `value` fields, preserving
order and nulls. Repeated review values are retained as separate entries.

`mediaFiles` counts the top-level `media.files`, falling back to
`delivery.media.files` when top-level media is not an object. `resultFiles`
counts `result.files`; names retain their API order. `resultSourceNodes`
counts entries in `result.source`. An absent/null collection is unknown
and remains null; an explicit empty array has count zero. Per-job reasons
for absent/null fields are stored under `coverage.nullReasonsByJob`.

If a read fails three times, its raw row retains the error and attempts. The
projected row retains list-provided ID, creation time and template, sets
unavailable detail/result fields to null and names the failed endpoint in
`error`. The successful counterpart's fields remain usable. The offline
check accepts documented exhausted reads while reporting their count; it
rejects missing, duplicate or extra rows and unattempted endpoints.

The fixed cutoff limits **creation**, not the historical state of every
field. Detail/result routes expose their state at fetch time; no historical
read parameter is documented in the [API guide](https://imd.fun/docs/llms.txt).
Thus status, reviews, delivery, result files and project versions may reflect
updates after the snapshot. A project may include later versions. Raw source
times and `updatedAt` are retained for consumers to assess this limitation.
The projected row's `fetchedAt` is the latest of its endpoint read completion
times; individual read and attempt times remain in the raw files. No
deliverable files, repositories or result URLs are executed or downloaded.

`check.py` verifies all five envelopes, pagination, unique whole-list count
and hash, cutoff and shard membership, exactly one row for each indexed job,
complete endpoint attempt coverage, explicit-only payer values, lowercase
addresses, recorded failures, null reasons, and exact offline reconstruction
of the projected table. It uses no network and writes no files.
