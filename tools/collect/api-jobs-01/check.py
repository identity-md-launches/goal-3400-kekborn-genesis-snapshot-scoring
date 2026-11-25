"""Verify the delivered shard offline."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / 'sources/api-jobs-01'
CUTOFF = datetime.fromisoformat('2026-11-25T11:00:00+00:00')


def load(name, required):
    data = json.loads((SOURCE / name).read_text(encoding='utf-8'))
    columns = data['columns']
    if len(set(columns)) != len(columns) or not set(required).issubset(columns):
        raise ValueError(f'{name}: invalid columns')
    rows = []
    for number, row in enumerate(data['rows'], 1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f'{name}: row {number} has the wrong width')
        rows.append(dict(zip(columns, row)))
    counts = Counter(row['id'] for row in rows)
    for row in rows:
        job_id = row['id']
        if not isinstance(job_id, str) or not job_id.startswith(('2', '3')):
            raise ValueError(f'{name}: out-of-shard ID {job_id!r}')
        if counts[job_id] != 1:
            raise ValueError(f'{name}: duplicate ID {job_id}')
        if datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00')) >= CUTOFF:
            raise ValueError(f'{name}: job {job_id} is outside the cutoff')
    return data, {row['id']: row for row in rows}


def main():
    index, indexed = load('index.json', ('id', 'createdAt', 'template'))
    jobs, collected = load('jobs.json', ('id', 'createdAt', 'template', 'error'))
    if indexed.keys() != collected.keys():
        missing = sorted(indexed.keys() - collected.keys())
        extra = sorted(collected.keys() - indexed.keys())
        raise ValueError(f'ID mismatch: {len(missing)} missing {missing[:5]}, '
                         f'{len(extra)} extra {extra[:5]}')
    for job_id, row in indexed.items():
        for key in ('createdAt', 'template'):
            if row[key] != collected[job_id][key]:
                raise ValueError(f'{job_id}: inconsistent {key}')
    for key in ('createdBefore', 'wholeListCount', 'wholeListIdsSha256'):
        if index['coverage'][key] != jobs['coverage'][key]:
            raise ValueError(f'Coverage mismatch: {key}')
    errors = [error for row in collected.values() for error in (row['error'] or [])]
    if errors != jobs['errors']:
        raise ValueError('Endpoint errors do not match the row errors')
    print(f'PASS: {len(indexed)} indexed jobs each have exactly one matching row; '
          f'{len(errors)} endpoint failures recorded')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        sys.exit(f'FAIL: {exc}')
