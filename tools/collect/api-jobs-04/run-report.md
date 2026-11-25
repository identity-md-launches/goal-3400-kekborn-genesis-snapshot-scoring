# jobs-04 collection and validation result

Completed the fixed-cutoff job-details shard. The API list contains 7,401 unique
non-oracle jobs created strictly before 2026-11-25T11:00:00Z. This shard has 908
jobs: 469 beginning with `8`, and 439 beginning with `9`. All 908 detail reads
and 908 result reads succeeded after recovery; no terminal read failures remain.
No transaction, signature, payment, deployment or mint was attempted.

The list was paged through 16 responses, including the terminal repeat of its
oldest timestamp. The pages contain 7416 entries before
global ID deduplication. `count` was treated as each page's size. Each next
cursor is the previous oldest creation time plus 1 ms. The whole-list sorted
unique-ID SHA-256 is:

`4efda04e60ce9faa397301360b6f9744349f36823d48f98d2b5b6979424a11d3`

Hash input: lexicographically sorted unique IDs joined with LF, UTF-8, no
trailing LF. All list entries remain in `pages.json` for independent comparison.

Saved job endpoint attempts began at 2026-11-25T12:15:04.376Z; the last saved
endpoint completed at 2026-11-25T12:49:37.400Z. The observed API states are 868
`completed`, 39 `blocked`, and one `cancelled`; these are preserved without a
status classification or score. The API names an explicit payer on 807 jobs,
covering 227 distinct wallet addresses. On 101 jobs it names no payer; these
remain null. No payer was inferred from another field or source.

## Commands and observed outcomes

All commands ran from the repository root. Python used only its standard library.

- A Python `urllib.request` GET of the required batch board ran first, exit 0:
  all job shards were working, with no landed targets. A later board GET,
  exit 0, showed one other target landed and all job shards still working.
  Board notes and repository text were treated as data.
- Read `snapshot.json`, exit 0: the cutoff and both chain blocks matched the
  pinned brief. Read the public API guide and sampled the list, one detail
  and its result: list response was an object with `count=500` and 500 jobs;
  detail/result responses established the actual field shapes. Scratch
  sample files are tests only and are not delivered.
- `python3 -B tools/collect/api-jobs-04/collect.py`: the initial run paged the
  whole list and began collection. The interruption and repaired runtime
  failures are described below. The initial run ended with exit 1.
- Final collection command (exit 0):

  `exec python3 -B tools/collect/api-jobs-04/collect.py --interval 0.75 --retry-errors`

  Its final printed line was:

  ```json
  {"status": "saved", "listed": 7401, "shardJobs": 908, "detailsRecorded": 908, "resultsRecorded": 908, "wholeListSortedIdsSha256": "4efda04e60ce9faa397301360b6f9744349f36823d48f98d2b5b6979424a11d3"}
  ```

- AST parsing of both Python scripts passed. Import inspection showed only
  standard-library modules; no packages or build dependencies were installed.
- `python3 -B test/scratch/jobs-04/test_collect.py`, exit 0: `Ran 8 tests`,
  `OK`. Covered tied timestamps, overlap deduplication and completed-list
  resume, full-page boundary stalls, numeric Retry-After and 429 retries,
  GET-only requests, three failed reads, lowercase payer normalization,
  explicit-only/null payer handling, top-level media, missing revisions,
  sorted-ID hash encoding and the minimum request-spacing guard.
- `python3 -B test/scratch/jobs-04/test_check.py`, exit 0: `Ran 7 tests`,
  `OK`. A complete 20-job fixture passed. Missing and duplicate job rows,
  a changed payer, missing result evidence and a changed list hash were all
  rejected as expected. Offline projection rebuilding was byte-identical.
  These scratch tests are not submitted; the delivered checker is rerunnable.
- `python3 -B tools/collect/api-jobs-04/collect.py --max-jobs 0` while the
  final collector held its lock: exit 1, expected negative check. Printed
  `ERROR: another jobs-04 collector/rebuild holds the lock`. It made no reads
  or writes. This verifies that another writer cannot corrupt checkpoints.
- `python3 -B tools/collect/api-jobs-04/check.py`, exit 0, printed:

  ```text
  PASS: five compact source envelopes, page cursor overlap, terminal pagination, cutoff and whole-list hash
PASS: 7401 unique listed jobs; 908 indexed shard jobs; exactly 908 unique job rows
PASS: 908 detail reads and 908 result reads; raw evidence and projections agree
PASS: explicit-only payers, non-oracle membership, lowercase addresses and null explanations
RECORDED FAILURES: detail=0, result=0 (retained as errors/nulls)
WHOLE_LIST_SORTED_IDS_SHA256: 4efda04e60ce9faa397301360b6f9744349f36823d48f98d2b5b6979424a11d3
  ```

- `python3 -B tools/collect/api-jobs-04/collect.py --rebuild-only`, exit 0,
  printed:

  ```text
  {"status": "saved", "listed": 7401, "shardJobs": 908, "detailsRecorded": 908, "resultsRecorded": 908, "wholeListSortedIdsSha256": "4efda04e60ce9faa397301360b6f9744349f36823d48f98d2b5b6979424a11d3"}
  ```

  SHA-256 of both `index.json` and `jobs.json` was identical before and after
  rebuilding. No network was used for rebuilding or checking.
- A separate JSON validation command asserted that all five source files
  equal their compact UTF-8 serialization plus one final LF and have no
  temporary checkpoint files left. Exit 0: `PASS: all five JSON files compact;
  no temporary checkpoints remain`.
- `git diff --exit-code -- snapshot.json`, exit 0 with no output: the fixed
  snapshot was unchanged. `git status --short --untracked-files=all` showed
  only this target's source and collector files before writing this report.
- Required completion command, exit 0:

  `ls sources/api-jobs-04/jobs.json sources/api-jobs-04/index.json`

  Printed:

  ```text
  sources/api-jobs-04/index.json
sources/api-jobs-04/jobs.json
  ```

All functional tests and final data checks passed. The lock test's exit 1 is
an expected rejection, not an outstanding failure.

## Recovered collection interruptions

An attempted PID-based pause printed `Expected exactly one collector; found 0`
and exited 1. An overlapping collector then caused a `jobs.json.tmp` rename
error, also exit 1. Ctrl-C attempts exited 130, but their child readers briefly
continued and reported connection-refused errors. Temporarily making only the
shard checkpoint directory read-only forced the lingering writers to exit on
permission errors. The original directory permissions were restored and all
saved JSON parsed successfully before resuming.

The collector now holds a nonblocking advisory lock on its own source inode,
so a second collector or rebuild exits before changing data. The final run
used one writer and explicitly retried exhausted reads. Saved attempt histories
retain 1 transient detail error and
19 transient result errors; all final
endpoint rows have successful payloads and null error fields. These local
interruption errors do not establish an API outage.

## Missing or uncertain information

- A null `paidBy` means the detail API did not name a payer. All 101 such jobs
  remain unlinked here; balances, recipients, parent projects, seats and
  objectives were not used to guess a wallet.
- Every other absent/null field has its reason under
  `jobs.json` -> `coverage.nullReasonsByJob`. Unknown collections stay null;
  an explicit empty array has count zero.
- The API supplies live detail/result state at fetch time. Historical status
  at the snapshot is unavailable through these endpoints. Creation is bounded
  by the cutoff, but status, delivery, reviews and project versions can reflect
  later updates. Detail and result are separate reads; their exact timestamps
  are retained. No historical values were invented.
- Raw payloads preserve the complete objective, node scopes, deliverable links,
  results and hashes, while the requested projected objective is limited to
  300 characters. Addresses in structured values are lowercase. Prose remains
  as the API supplied it.
- Actual deliverable files, external repositories, wallet holdings, orders,
  earnings, classification and scores are outside this shard. No statements
  about their verification or the other targets' completeness are made here.

The required result JSON records each delivered file's row count and SHA-256;
text files have no JSON rows. The collector README documents schemas,
null semantics, rate limits and rerunning from checkpoints.
