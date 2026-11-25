"""Collect shard 01. Keep resumable endpoint responses in test/scratch."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

API = 'https://api.imd.fun'
T = '2026-11-25T11:00:00Z'
ROOT = Path(__file__).resolve().parents[3]
DEST = ROOT / 'sources/api-jobs-01'
INDEX = 'id createdAt template'.split()
COLUMNS = (
    'id state template createdAt updatedAt objective paidBy parentJobId projectId '
    'projectVersions workflowId oracleRequestId via launchKind launchStatus '
    'launchChainId launchId siteUrl blockedReason deliveryRepoUrl deliveryCommit '
    'deliveredAt mediaFiles nodes nodeRoles nodesAccepted revisions judgeRevisions '
    'seats reviewsSent reviewValues resultComplete resultFiles resultFileNames '
    'resultSourceNodes fetchedAt error'
).split()


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def iso(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def now():
    return iso(datetime.now(timezone.utc))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n',
                   encoding='utf-8')
    tmp.replace(path)


class Reader:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.lock = threading.Lock()
        self.next_request = 0
        self.paused_until = 0

    def pace(self):
        while True:
            with self.lock:
                delay = max(self.next_request, self.paused_until) - time.monotonic()
                if delay <= 0:
                    self.next_request = time.monotonic() + 0.71
                    return
            time.sleep(delay)

    def read(self, route, identity=None, cache_failure=True):
        key = hashlib.sha256((API + route).encode()).hexdigest()
        path = self.checkpoint / (key + '.json')
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        for attempt in range(1, 4):
            self.pace()
            try:
                with urlopen(API + route, timeout=35) as response:
                    data = json.load(response)
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                if identity and data.get(identity[0]) != identity[1]:
                    raise ValueError('Response job ID does not match the requested job')
                record = {'data': data, 'fetchedAt': now(), 'error': None}
                break
            except HTTPError as exc:
                error = f'HTTP {exc.code}'
                delay = attempt
                if exc.code == 429:
                    value = exc.headers.get('Retry-After', '60')
                    try:
                        delay = float(value)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(value) -
                                     datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            delay = 60
                    with self.lock:
                        self.paused_until = max(self.paused_until,
                                                time.monotonic() + max(1, delay))
                exc.close()
            except ValueError as exc:
                error = f'invalid response: {exc}'
                delay = attempt
            except (URLError, OSError, HTTPException) as exc:
                error = type(exc).__name__
                delay = attempt
            if attempt < 3:
                time.sleep(max(0, delay))
        else:
            record = {'data': None, 'fetchedAt': now(),
                      'error': {'endpoint': route, 'attempts': 3, 'reason': error}}
        if record['error'] is None or cache_failure:
            save(path, record)
        return record


def catalog(reader):
    before = T
    jobs = {}
    pages = 0
    fetched_at = None
    while True:
        route = '/jobs?' + urlencode({'exclude': 'oracle', 'limit': 500, 'before': before})
        read = reader.read(route, cache_failure=False)
        if read['error']:
            raise RuntimeError(read['error'])
        fetched_at = max(fetched_at or read['fetchedAt'], read['fetchedAt'])
        page = read['data']['jobs']
        pages += 1
        for entry in page:
            if utc(entry['createdAt']) < utc(T):
                jobs[entry['id']] = entry
        if not page:
            break
        oldest = min(utc(entry['createdAt']) for entry in page)
        cursor = iso(oldest + timedelta(milliseconds=1))
        if utc(cursor) >= utc(before):
            if len(page) >= 500:
                raise RuntimeError('A full page shares a cursor timestamp; completeness is unknown')
            cursor = iso(oldest)
            if utc(cursor) >= utc(before):
                raise RuntimeError('The list endpoint did not advance the cursor')
        before = cursor
    return jobs, pages, fetched_at


def obj(value):
    return value if isinstance(value, dict) else {}


def count(value):
    return len(value) if isinstance(value, list) else None


def field_list(items, field):
    return [item.get(field) for item in items] if isinstance(items, list) else None


def aggregate(items, field):
    values = field_list(items, field)
    return sum(values) if values is not None and all(type(v) is int for v in values) else None


def project_job(entry, detail, result):
    d = obj(detail['data'])
    r = obj(result['data'])
    if (d and d.get('id') != entry['id']) or (r and r.get('jobId') != entry['id']):
        raise ValueError(f'Endpoint ID mismatch for {entry["id"]}')
    row = {key: d.get(key) for key in COLUMNS}
    row.update({key: entry.get(key) for key in INDEX})
    if isinstance(row['objective'], str):
        row['objective'] = row['objective'][:300]
    if isinstance(row['paidBy'], str):
        row['paidBy'] = row['paidBy'].lower()
    project = obj(d.get('project'))
    launch = obj(d.get('launch'))
    delivery = obj(d.get('delivery'))
    nodes = d.get('nodes')
    roles = field_list(nodes, 'role')
    states = field_list(nodes, 'state')
    reviews = d.get('reviews')
    review_statuses = field_list(reviews, 'status')
    review_entries = field_list(reviews, 'entries')
    files = r.get('files')
    row.update(
        projectId=project.get('id'),
        projectVersions=field_list(project.get('versions'), 'jobId'),
        workflowId=obj(d.get('workflow')).get('id'),
        launchKind=launch.get('kind'), launchStatus=launch.get('status'),
        launchChainId=launch.get('chainId'), launchId=launch.get('id'),
        siteUrl=obj(d.get('site')).get('url'),
        deliveryRepoUrl=delivery.get('repoUrl'), deliveryCommit=delivery.get('commit'),
        deliveredAt=delivery.get('deliveredAt'),
        mediaFiles=count(obj(d.get('media')).get('files')),
        nodes=count(nodes),
        nodeRoles={role: roles.count(role) for role in sorted(set(roles))}
        if roles is not None and all(isinstance(role, str) for role in roles) else None,
        nodesAccepted=states.count('accepted') if states is not None and None not in states else None,
        revisions=aggregate(nodes, 'revisions'), judgeRevisions=aggregate(nodes, 'judgeRevisions'),
        seats=sorted({seat for node in nodes if
                      (seat := obj(node.get('seat')).get('tokenId')) is not None})
        if isinstance(nodes, list) else None,
        reviewsSent=review_statuses.count('sent')
        if review_statuses is not None and None not in review_statuses else None,
        reviewValues=[entry.get('value') for entries in review_entries for entry in entries]
        if review_entries is not None and all(isinstance(e, list) for e in review_entries) else None,
        resultComplete=r.get('complete'), resultFiles=count(files),
        resultFileNames=field_list(files, 'name'), resultSourceNodes=count(r.get('source')),
        fetchedAt=max(detail['fetchedAt'], result['fetchedAt']),
        error=[read['error'] for read in (detail, result) if read['error']] or None,
    )
    return [row[key] for key in COLUMNS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, default=ROOT / 'test/scratch/api-jobs-01')
    args = parser.parse_args()
    reader = Reader(args.checkpoint)
    entries, pages, catalog_fetched_at = catalog(reader)
    all_ids = sorted(entries)
    ids = [job_id for job_id in all_ids if job_id.startswith(('2', '3'))]
    coverage = {
        'createdBefore': T, 'exclude': 'oracle', 'idPrefixes': ['2', '3'],
        'listPages': pages, 'wholeListCount': len(all_ids),
        'wholeListIdsSha256': hashlib.sha256('\n'.join(all_ids).encode()).hexdigest(),
        'digestEncoding': 'UTF-8 sorted IDs separated by LF, no trailing LF',
    }
    print(f'Catalog: {pages} pages, {len(all_ids)} unique IDs, {len(ids)} shard jobs; '
          f'digest {coverage["wholeListIdsSha256"]}', flush=True)
    save(DEST / 'index.json', dict(source=API + '/jobs', fetchedAt=catalog_fetched_at,
                                  coverage=coverage, errors=[], columns=INDEX,
                                  rows=[[entries[job_id].get(key) for key in INDEX] for job_id in ids]))

    def collect(job_id):
        detail = reader.read(f'/jobs/{job_id}', ('id', job_id))
        result = reader.read(f'/jobs/{job_id}/result', ('jobId', job_id))
        return project_job(entries[job_id], detail, result)

    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for row in pool.map(collect, ids):
            rows.append(row)
            if len(rows) % 100 == 0:
                print(f'Collected {len(rows)}/{len(ids)} shard rows', flush=True)
    errors = [error for row in rows for error in (row[-1] or [])]
    coverage['nullReason'] = ('The endpoint supplied no value, a required input to an aggregate '
                              'was absent, or the row records an endpoint failure. A null paidBy '
                              'means the detail endpoint supplied no payer or failed.')
    coverage['observation'] = ('The cutoff applies to job creation. Detail and result values '
                               'were observed at collection time.')
    save(DEST / 'jobs.json', dict(source=[API + '/jobs/{id}', API + '/jobs/{id}/result'],
                                 fetchedAt=max((row[-2] for row in rows), default=now()),
                                 coverage=coverage, errors=errors, columns=COLUMNS, rows=rows))
    print(f'Wrote {len(rows)} shard rows; {len(errors)} failed endpoint reads', flush=True)


if __name__ == '__main__':
    main()
