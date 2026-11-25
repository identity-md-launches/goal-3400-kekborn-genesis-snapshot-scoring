# Kekborn Genesis snapshot

This repository contains the frozen IMD holder snapshot, job history, and Genesis Contribution Scores. The order cutoff is **2026-11-25 11:00:00 UTC**. Eligible orders were created strictly before that time.

| Chain | Chain ID | Snapshot block | Block time (UTC) |
| --- | ---: | ---: | --- |
| Ethereum | 1 | 26491756 | 2026-11-25 10:59:59 |
| Robinhood Chain | 4663 | 122989778 | 2026-11-25 11:00:00 |

## Data and scripts

| Path | Contents |
| --- | --- |
| [snapshot.json](snapshot.json) | Fixed cutoff, token addresses, block identifiers, timestamps, and verification evidence. |
| [sources/](sources/) | Saved JSON records for snapshot blocks, chain balances and payments, and IMD API catalogs, jobs, wallet orders, and earnings. |
| [holders.csv](holders.csv) | One row per wallet with a positive IMD balance on either chain, with per-chain and combined raw balances. |
| [jobs.csv](jobs.csv) | Job and goal records with payer evidence, unlinked reasons, status, category, and scoring signals. Grouped child jobs remain visible with their counting flags. |
| [scores.csv](scores.csv) | Per-wallet balances, job counts, IMD spend, contribution scores, rarity tiers, and genetic tendencies. |
| [methodology.md](methodology.md) | Score formula, rarity cut points, and genetics rules. |
| [data-accessibility.md](data-accessibility.md) | Source coverage, missing evidence, and independent verification options. |
| [tools/collect/](tools/collect/) | Source collection and validation scripts. |
| [tools/holders/](tools/holders/), [tools/jobs/](tools/jobs/), [tools/scores/](tools/scores/) | Offline builders for the three CSVs. |
| [tools/manifest/manifest.py](tools/manifest/manifest.py) | Offline inventory generator and verifier. |
| [manifest.json](manifest.json) | Sorted repository paths with byte sizes and SHA-256 hashes, the starting trunk commit, and the snapshot blocks. The file explains its self-exclusion. |

## Rebuild and verify offline

Run from the repository root with Python 3 and Git. The CSV builders use the saved snapshot and source records.

```sh
python3 tools/holders/build_holders.py
python3 tools/jobs/build_jobs.py
python3 tools/scores/score.py
python3 tools/manifest/manifest.py --check
```

Pass `--check` to each CSV builder to compare the saved CSV to its rebuild. After an intentional repository change, run `python3 tools/manifest/manifest.py` to regenerate the inventory. The inventory reads working-tree bytes for Git-tracked paths and includes non-ignored additions at `README.md` and under `tools/manifest/`. For symbolic links, it hashes the link target text, matching the bytes Git stores.
