# API catalog collection and validation report

The catalog is complete for the API-visible lists described below. This repair addresses the rejected result artifact's shape: the six unsupported top-level keys (`files`, `inventoryNote`, `commands`, `checks`, `uncertainties`, `sourceInspections`) have been moved into this report. The output artifact contains exactly `status`, `check`, and `notes`.

The seven saved datasets and all three Python scripts were preserved byte-for-byte. The README now points to this report. No recollection was performed during this repair. The snapshot remains fixed at T = 2026-11-25T11:00:00Z, Ethereum block 26491756, Robinhood block 122989778.

Initial board GET during this repair succeeded. It reported 3 landed targets (ethereum, robinhood, holders), 15 working targets, and 4 waiting targets; catalog was working. Other targets' files were left unchanged.

## Current file inventory

SHA-256 values hash exact file bytes. Row counts count source `rows` entries. `null` means the file is documentation, Python source, or a result object rather than a row dataset.

| Path | Row count | Bytes | SHA-256 |
| --- | ---: | ---: | --- |
| `sources/api-catalog/capabilities.json` | 1 | 11903 | `505dd1c1df477d00f71c758412b903c3960cd854cc515599928fe2686f93a793` |
| `sources/api-catalog/goals.json` | 78 | 55193 | `625864a29b3a76df18d8af715a2d83e909063e32cad35d336023a399edf94e23` |
| `sources/api-catalog/job-index.json` | 17021 | 3654299 | `16c7c39733f62902ea0a63307f541a2b9327021687955f2b967a09e6ae143582` |
| `sources/api-catalog/oracle-requests.json` | 9504 | 2140935 | `2784c64ac14a502a594e68a5719b5985e64858b0dcb4d366d5841ecff8bb70ff` |
| `sources/api-catalog/seats-owners.json` | 2000 | 108275 | `df8f4d5641d7ddd64f28815e042c3f716d202b6a56881b47583978b72e439c94` |
| `sources/api-catalog/seats-records.json` | 1160 | 69503 | `4397408dcf990f44ff58318f2fc797b64165f26d0a200ee28a1512abdc355b97` |
| `sources/api-catalog/workflows.json` | 219 | 44837 | `cc54ddefe546dd508ed6d1bb41937e0009666a3b8bb8287105fb305a6dd26577` |
| `tools/collect/api-catalog/README.md` | null | 10163 | `a44c3d691e637a7bf945b4dfdba4219e4fcf205c9b2008aca9effe02ae72b9db` |
| `tools/collect/api-catalog/check.py` | null | 18337 | `58c0bb3bc8141e1aa48f429218b031327366e42c72003085d4494c8f93da43e4` |
| `tools/collect/api-catalog/collect.py` | null | 25360 | `ca59c4b81a0e66bad5af6cc62bbdc18c189339d6fb3eca07577fd739bd441246` |
| `tools/collect/api-catalog/test_catalog.py` | null | 14077 | `8b7391a7757fbcef5278af60afd6695defddcdae8f01505eb4380c59cfdfe727` |

`tools/collect/api-catalog/REPORT.md` has rowCount=null; its exact SHA-256 is in the result artifact's `notes`. The required `artifacts/goal/batches/genesis-pipeline-e03/items/catalog/result.json` also has rowCount=null; its exact SHA-256 is provided separately in the final response. This avoids embedding a file's own hash within itself.

## Commands rerun during the repair

### Validation 1

```sh
python3 -B tools/collect/api-catalog/check.py
```

Exit code: 0. Output:

```text
NOTE workflows: 0 references absent from index, all explicitly listed
NOTE oracle-requests: 0 references absent from index, all explicitly listed
NOTE goals: 78 parents; 0 absent child job references explicitly listed
PASS fixed snapshot: T=2026-11-25T11:00:00Z Ethereum=26491756 Robinhood=122989778
PASS job-index.json: rows=17021 sha256=16c7c39733f62902ea0a63307f541a2b9327021687955f2b967a09e6ae143582
PASS goals.json: rows=78 sha256=625864a29b3a76df18d8af715a2d83e909063e32cad35d336023a399edf94e23
PASS workflows.json: rows=219 sha256=cc54ddefe546dd508ed6d1bb41937e0009666a3b8bb8287105fb305a6dd26577
PASS oracle-requests.json: rows=9504 sha256=2784c64ac14a502a594e68a5719b5985e64858b0dcb4d366d5841ecff8bb70ff
PASS seats-owners.json: rows=2000 sha256=df8f4d5641d7ddd64f28815e042c3f716d202b6a56881b47583978b72e439c94
PASS seats-records.json: rows=1160 sha256=4397408dcf990f44ff58318f2fc797b64165f26d0a200ee28a1512abdc355b97
PASS capabilities.json: rows=1 sha256=505dd1c1df477d00f71c758412b903c3960cd854cc515599928fe2686f93a793
PASS unique IDs, exclusive cutoff, pagination overlap/exhaustion, ID digests, goal coverage, references, null accounting, address/amount formats, and seat observation caveats
PASS: all 7 catalogs validated offline
```

### Validation 2

```sh
python3 -B tools/collect/api-catalog/test_catalog.py
```

Exit code: 0. Output:

```text
test_corrupt_id_digest_fails (__main__.IntegrationTests.test_corrupt_id_digest_fails) ... ok
test_duplicate_ids_fail (__main__.IntegrationTests.test_duplicate_ids_fail) ... ok
test_missing_goal_fails (__main__.IntegrationTests.test_missing_goal_fails) ... ok
test_seats_cannot_claim_snapshot_state (__main__.IntegrationTests.test_seats_cannot_claim_snapshot_state) ... ok
test_unlisted_oracle_reference_fails (__main__.IntegrationTests.test_unlisted_oracle_reference_fails) ... ok
test_unlisted_workflow_reference_fails (__main__.IntegrationTests.test_unlisted_workflow_reference_fails) ... ok
test_valid_collection_and_explicit_absence_pass (__main__.IntegrationTests.test_valid_collection_and_explicit_absence_pass) ... ok
test_api_count_is_page_length_not_total (__main__.PaginationTests.test_api_count_is_page_length_not_total) ... ok
test_conflicting_direct_and_nested_child_job_ids_fail (__main__.PaginationTests.test_conflicting_direct_and_nested_child_job_ids_fail) ... ok
test_cutoff_violation_is_reported_and_row_excluded (__main__.PaginationTests.test_cutoff_violation_is_reported_and_row_excluded) ... ok
test_full_timestamp_bucket_fails_instead_of_skipping_unseen_rows (__main__.PaginationTests.test_full_timestamp_bucket_fails_instead_of_skipping_unseen_rows) ... ok
test_missing_timestamp_cannot_be_silently_skipped (__main__.PaginationTests.test_missing_timestamp_cannot_be_silently_skipped) ... ok
test_nested_child_job_id_is_linked_without_guessing (__main__.PaginationTests.test_nested_child_job_id_is_linked_without_guessing) ... ok
test_timestamp_ties_cross_page_boundary_without_losing_ids (__main__.PaginationTests.test_timestamp_ties_cross_page_boundary_without_losing_ids) ... ok
test_429_honors_retry_after_and_records_attempt (__main__.RetryTests.test_429_honors_retry_after_and_records_attempt) ... ok
test_nonretryable_404_is_recorded_once (__main__.RetryTests.test_nonretryable_404_is_recorded_once) ... ok
test_read_pace_and_limit (__main__.RetryTests.test_read_pace_and_limit) ... ok
test_retries_exhausted_are_saved_as_failure (__main__.RetryTests.test_retries_exhausted_are_saved_as_failure) ... ok
test_retry_after_http_date (__main__.RetryTests.test_retry_after_http_date) ... ok

----------------------------------------------------------------------
Ran 19 tests in 0.128s

OK
```

### Validation 3

```sh
ls sources/api-catalog/job-index.json sources/api-catalog/goals.json sources/api-catalog/workflows.json sources/api-catalog/oracle-requests.json
```

Exit code: 0. Output:

```text
sources/api-catalog/goals.json
sources/api-catalog/job-index.json
sources/api-catalog/oracle-requests.json
sources/api-catalog/workflows.json
```

### Validation 4

```sh
python3 -B - <<'PY'
import hashlib,json,subprocess
from pathlib import Path
p=Path('artifacts/goal/batches/genesis-pipeline-e03/items/catalog/result.json')
record=json.loads(p.read_bytes())
for item in record['files']:
    data=Path(item['path']).read_bytes()
    assert hashlib.sha256(data).hexdigest()==item['sha256'], item['path']
    if item['rowCount'] is not None:
        assert len(json.loads(data)['rows'])==item['rowCount'], item['path']
print('PASS: all 11 existing inventory hashes and seven row counts match saved files')
snapshot=Path('snapshot.json').read_bytes()
assert snapshot==subprocess.check_output(['git','show','HEAD:snapshot.json'])
print('PASS: snapshot.json byte-for-byte unchanged; sha256='+hashlib.sha256(snapshot).hexdigest())
assert not subprocess.check_output(['git','ls-files','--',str(p)])
print('PASS: result.json is untracked')
print('CONFIRMED rejected result has extra keys: '+', '.join(sorted(set(record)-{'status','check','notes'})))
PY
```

Exit code: 0. Output:

```text
PASS: all 11 existing inventory hashes and seven row counts match saved files
PASS: snapshot.json byte-for-byte unchanged; sha256=b94dc67d27bc6aaa49811431ee68a3fd25d6c90f09de8fde0f1920792d7dd425
PASS: result.json is untracked
CONFIRMED rejected result has extra keys: checks, commands, files, inventoryNote, sourceInspections, uncertainties
```

The test names ending in `fails` verify rejection of deliberately corrupted synthetic fixtures; every test passed. The `CONFIRMED rejected result` message describes the old artifact before this repair.

Additional inspection commands: `git status --short` printed nothing initially; `rg --files sources/api-catalog tools/collect/api-catalog artifacts/goal/batches/genesis-pipeline-e03/items/catalog test/scratch` listed the eleven source/tool files and the artifact, and exited 2 only because `test/scratch` did not yet exist. The test suite subsequently created its own scratch directory. `git ls-files -- sources/api-catalog tools/collect/api-catalog artifacts/goal/batches/genesis-pipeline-e03/items/catalog/result.json snapshot.json` listed the source/tool files and snapshot, but no result artifact. `cat` and `sed` inspected the result, README and Python source; they completed successfully. An inline Python inventory recomputed every delivered data/code/documentation file's row count, byte size and SHA-256; it exited 0 and is reflected in the table above.

## Original collection record

The following commands and outputs are preserved from the previous result artifact. They describe the earlier collection, not commands rerun during this shape repair. The current data hashes were independently checked against that artifact before the README edit and all eleven matched.

Original rows: 17,021 jobs (9,620 oracle panel jobs), 78 goals, 219 workflows, 9,504 oracle requests, 2,000 seat owners, 1,160 seat records, and one full capabilities response. Collection used 45 reads/minute; source read ledgers contain no unrecovered read failures. The goals projection was repaired during the original run to retain explicit nested `child.job.id` links and goals were reread. Three refused children retain null job IDs with reasons; one opened child links to an indexed job. Every workflow and oracle non-null job reference is indexed.

### python3 -B tools/collect/api-catalog/collect.py --reads-per-minute 45

```sh
python3 -B tools/collect/api-catalog/collect.py --reads-per-minute 45
```

Recorded exit code: 0. Recorded output:

```text
Pages: jobs 36, workflows 2, oracle requests 21. SAVED seven files; all errors=0. COLLECTION files=7 reads=218 failedChecks=0.
```

### before nested-child schema repair

```sh
python3 -B tools/collect/api-catalog/test_catalog.py
```

Recorded exit code: 0. Recorded output:

```text
Ran 17 tests in 0.095s; OK.
```

### initial data validation

```sh
python3 -B tools/collect/api-catalog/check.py
```

Recorded exit code: 0. Recorded output:

```text
PASS: all 7 catalogs validated offline. A subsequent manual source review found a nested child job ID that the initial checker did not require; repaired below.
```

### python3 -B - <<'PY'

```sh
python3 -B - <<'PY'
import sys,json
from pathlib import Path
sys.path.insert(0,'tools/collect/api-catalog')
import collect
jobs=json.loads(Path('sources/api-catalog/job-index.json').read_bytes())
client=collect.Client(reads_per_minute=45)
doc=collect.collect_goals(client,jobs)
collect.write_json(Path('sources/api-catalog/goals.json'),doc)
print('GOAL REFRESH reads=%d errors=%d' % (len(client.reads),len(doc['errors'])))
sys.exit(bool(doc['errors']))
PY
```

Recorded exit code: 0. Recorded output:

```text
Goals 1/78 through 78/78. SAVED goals.json rows=78 sha256=625864a29b3a76df18d8af715a2d83e909063e32cad35d336023a399edf94e23 errors=0. GOAL REFRESH reads=156 errors=0.
```

### after nested-child schema repair

```sh
python3 -B tools/collect/api-catalog/test_catalog.py
```

Recorded exit code: 0. Recorded output:

```text
Ran 19 tests in 0.097s; OK. Both nested-link projection and conflicting source IDs tested.
```

### final saved data

```sh
python3 -B tools/collect/api-catalog/check.py
```

Recorded exit code: 0. Recorded output:

```text
NOTE workflows: 0 references absent from index, all explicitly listed
NOTE oracle-requests: 0 references absent from index, all explicitly listed
NOTE goals: 78 parents; 0 absent child job references explicitly listed
PASS fixed snapshot: T=2026-11-25T11:00:00Z Ethereum=26491756 Robinhood=122989778
PASS each file row count and SHA-256 (see files inventory)
PASS unique IDs, exclusive cutoff, pagination overlap/exhaustion, ID digests, goal coverage, references, null accounting, address/amount formats, and seat observation caveats
PASS: all 7 catalogs validated offline
```

### ls sources/api-catalog/job-index.json sources/api-catalog/goals.json sources/api-catalog/workflows.json sources/api-catalog/oracle-requests.json

```sh
ls sources/api-catalog/job-index.json sources/api-catalog/goals.json sources/api-catalog/workflows.json sources/api-catalog/oracle-requests.json
```

Recorded exit code: 0. Recorded output:

```text
sources/api-catalog/goals.json
sources/api-catalog/job-index.json
sources/api-catalog/oracle-requests.json
sources/api-catalog/workflows.json

```

### python3 -B - (ast.parse on all catalog Python files; git ls-files --others --exclude-standard; git diff --name-only; snapshot byte comparison to git show HEAD:snapshot.json)

```sh
python3 -B - (ast.parse on all catalog Python files; git ls-files --others --exclude-standard; git diff --name-only; snapshot byte comparison to git show HEAD:snapshot.json)
```

Recorded exit code: 0. Recorded output:

```text
PASS Python syntax for check.py, collect.py, test_catalog.py; PASS scope: 11 changed/untracked paths, all allowed; PASS snapshot byte-for-byte unchanged. The required result artifact was added afterward.
```

### git status --short --untracked-files=all

```sh
git status --short --untracked-files=all
```

Recorded exit code: 0. Recorded output:

```text
Only the seven source JSON files and four collector/check/test/documentation files were untracked before writing the required result. No tracked changes.
```

## Recorded checks

- Fixed snapshot byte identity: passed.
- Compact six-key envelopes; array rows; required columns; null accounting; address and token amount formats: passed.
- Unique IDs; exclusive cutoff; full pagination evidence; page counts; exhaustion; total and sorted-ID digests: passed.
- Exactly one row for every goal, both detail and children GETs, nested child job links, child null explanations: passed.
- Every workflow job ID and oracle jobId indexed or explicitly absent: passed; missing references=0.
- Seat read time and API at retained, current-state caveats present: passed.
- All 19 offline regression tests: passed.
  - IntegrationTests.test_corrupt_id_digest_fails: passed.
  - IntegrationTests.test_duplicate_ids_fail: passed.
  - IntegrationTests.test_missing_goal_fails: passed.
  - IntegrationTests.test_seats_cannot_claim_snapshot_state: passed.
  - IntegrationTests.test_unlisted_oracle_reference_fails: passed.
  - IntegrationTests.test_unlisted_workflow_reference_fails: passed.
  - IntegrationTests.test_valid_collection_and_explicit_absence_pass: passed.
  - PaginationTests.test_api_count_is_page_length_not_total: passed.
  - PaginationTests.test_conflicting_direct_and_nested_child_job_ids_fail: passed.
  - PaginationTests.test_cutoff_violation_is_reported_and_row_excluded: passed.
  - PaginationTests.test_full_timestamp_bucket_fails_instead_of_skipping_unseen_rows: passed.
  - PaginationTests.test_missing_timestamp_cannot_be_silently_skipped: passed.
  - PaginationTests.test_nested_child_job_id_is_linked_without_guessing: passed.
  - PaginationTests.test_timestamp_ties_cross_page_boundary_without_losing_ids: passed.
  - RetryTests.test_429_honors_retry_after_and_records_attempt: passed.
  - RetryTests.test_nonretryable_404_is_recorded_once: passed.
  - RetryTests.test_read_pace_and_limit: passed.
  - RetryTests.test_retries_exhausted_are_saved_as_failure: passed.
  - RetryTests.test_retry_after_http_date: passed.
- Allowed path scope and required output existence: passed.
- Remaining actual collection/check failures: none (0).

The initial output-shape check failed because of the six extra result keys, as reported by the requester. The repair removes them; collection/behavior checks above still pass. The final artifact is validated for exact allowed keys, types, length bounds, matching required-check output, report digest, allowed changed paths, unchanged saved data/scripts/snapshot, and remaining untracked.

## Missing or uncertain data

- API-visible list exhaustion is not proof that inaccessible, deleted or omitted historical records do not exist. HTTP body digests are recorded, but full bodies are not saved; projected data cannot reproduce body hashes.
- 116 of the 9620 indexed skill:oracle-assess jobs have no request among the 9504 listed requests. The lists provide no reason. Job detail oracleRequestId fields are another verification route; no payment, failure or ownership inference was made.
- Three refused children have no source job ID; their jobId remains null. Their original refusal reasons are saved. The opened workflow child explicitly links to job 48ad7795-23de-4483-90d3-a8789ee539ee and workflow b48935c9-5fc8-4bc3-8353-c79a2372b880.
- Job/goal/workflow/oracle creation is filtered before T, but mutable fields are read-time observations, not an as-of-T state reconstruction. API reads are not transactional across endpoints.
- Seat owners at=2026-11-25T12:22:19.618Z, fetchedAt=2026-11-25T12:22:59.772Z. Seat records fetchedAt=2026-11-25T12:23:01.186Z; the API may cache them up to 10 minutes. Neither is ownership/work totals at T; chain transfers and dated submissions are needed for that.
- Capabilities are current published settings, not historical prices or evidence of actual spend. Actual pre-T orders and payment attribution are separate targets.

## Original source inspections

These are the observations recorded by the original collection, not the current board state.

- https://api.imd.fun/goals/c4da2fbc-8de8-490f-9aa5-32ad67dda3e3/board?batch=genesis-pipeline-e03 — GET before repository inspection. 0 landed, 11 working, 11 waiting; catalog scope confirmed. No prior attempt files were present.
- https://imd.fun/docs/llms.txt — GET/read API reference. Confirmed list routes, counts, goal child route, zero-based owners array and seat-record cache semantics.
- https://api.imd.fun — Python urllib.request GET probes of the seven assigned routes and two goal examples. Endpoints readable; observed project nesting, createdAt pagination fields, response shapes and current-seat timestamps. No failed probe reads.

