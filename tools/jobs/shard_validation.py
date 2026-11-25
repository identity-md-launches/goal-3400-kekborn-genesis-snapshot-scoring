"""Check saved shard boundaries, selection, and digest evidence."""
from hashlib import sha256


def digest_ids(ids):
    return sha256('\n'.join(sorted(ids)).encode()).hexdigest()


def unique(rows, key, path):
    ids = {row[key] for row in rows}
    if len(ids) != len(rows):
        raise ValueError(f'{path}: duplicate {key}')
    return ids


def validate_shards(root, docs, tables, holders, cutoff, timestamp):
    nonoracle = set()
    claims = []
    wallet_union = set()
    for i in range(8):
        base = f'sources/api-jobs-{i:02d}'
        path = base + '/index.json'
        ids = unique(tables[path], 'id', path) if tables[path] else set()
        details = unique(tables[base + '/jobs.json'], 'id', base + '/jobs.json')
        if ids != details or any(j[0] not in '0123456789abcdef'[2*i:2*i+2] for j in ids):
            raise ValueError(f'{base}: detail/index coverage or prefix mismatch')
        if nonoracle & ids or any(timestamp(r['createdAt']) is None or
                                 timestamp(r['createdAt']) >= cutoff for r in tables[path]):
            raise ValueError(f'{path}: duplicate or out-of-cutoff job')
        nonoracle.update(ids)
        doc = docs[path]
        for metadata in (doc, doc['coverage']):
            if not isinstance(metadata, dict):
                continue
            for key in ('allJobsCount', 'wholeListCount', 'listedCount', 'listCount'):
                if key in metadata:
                    claims.append((path, 'count', metadata[key]))
            for key in ('allJobsIdsSha256', 'wholeListIdsSha256', 'listedIdsSha256',
                        'listIdsSha256', 'wholeListSortedIdsSha256', 'sortedIdsSha256', 'wholeListSha256'):
                if key in metadata:
                    claims.append((path, 'digest', metadata[key]))
    for path, kind, expected in claims:
        if expected != (len(nonoracle) if kind == 'count' else digest_ids(nonoracle)):
            raise ValueError(f'{path}: original job union {kind} mismatch')
    for i in range(6):
        base = f'sources/api-wallets-{i:02d}'
        path = base + '/wallets.json'
        ids = unique(tables[path], 'wallet', path)
        if wallet_union & ids or any(int(sha256(w.encode()).hexdigest()[:8], 16) % 6 != i for w in ids):
            raise ValueError(f'{path}: wallet shard mismatch')
        if any(row['wallet'] not in ids for row in tables[base + '/orders.json']):
            raise ValueError(f'{base}: order outside wallet shard')
        wallet_union.update(ids)
    if wallet_union != holders:
        raise ValueError('Wallet shard union differs from holders.csv')
    for i in range(2):
        base = f'sources/e04-jobs-{i:02d}'
        index, records = base + '/index.json', base + '/records.json'
        ids = unique(tables[index], 'jobId', index)
        if ids != unique(tables[records], 'jobId', records) or any(
                int(sha256(j.encode()).hexdigest()[:8], 16) % 2 != i for j in ids):
            raise ValueError(f'{base}: evidence selection coverage or shard mismatch')
        coverage = docs[index]['coverage']
        if len(ids) != coverage.get('expected', coverage.get('expectedCount')):
            raise ValueError(f'{index}: expected count differs')
        if 'expectedIdsSha256' in coverage and digest_ids(ids) != coverage['expectedIdsSha256']:
            raise ValueError(f'{index}: selection digest differs')
        if 'records' in docs[index]:
            if sha256((root / records).read_bytes()).hexdigest() != docs[index]['records']['sha256']:
                raise ValueError(f'{records}: file digest differs')
    for kind in ('details', 'results'):
        base = 'sources/api-jobs-04'
        path = f'{base}/{kind}.json'
        if unique(tables[path], 'id', path) != unique(tables[base + '/index.json'], 'id', base):
            raise ValueError(f'{path}: raw response coverage differs from shard index')
