# e04-jobs-00 report (shard 0 of 2)

Shard rule: int(sha256(exact jobId UTF-8).hexdigest()[:8],16)%2==0.

- Expected: 516 job IDs (253 holder-linked jobs.csv oracle rows lacking detail; 263 IDs from pre-T orders in sources/e04-orders/{paid-by,intake}.json, direct jobId or result.requests[]; none already saved in api-jobs-*).
- Fetched: 0. Unavailable: 516 (detail and result both). Every failure is in index/records `errors` and in each record's per-read error fields.
- Cause: this run's network proxy refused api.imd.fun (`Proxy CONNECT aborted` / `Remote end closed connection`). No API answer was received, so nothing is guessed or inferred.
- To complete: from a host that reaches api.imd.fun, run `python3 -B tools/collect/e04-jobs-00/collect.py fetch` (~36 min at 2.1 s/read, 2 reads per job) then `check.py`. The collector expands result.requests[] to newly found shard-0 jobs.
- Status/result values are read-time observations, not historical state at T. No payer was inferred from seats, consumers or names. Oracle attestation metadata is kept verbatim in raw results when returned; a panel assessment is not treated as a successful oracle answer.
