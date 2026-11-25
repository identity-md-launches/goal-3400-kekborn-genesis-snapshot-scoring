#!/usr/bin/env python3
"""Rebuild jobs.csv offline from the fixed Genesis snapshot's saved sources.

No network, dependencies, balances-as-scores, or objective-text classification.
See README.md for the schema, decision rules, and limits of these observations.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
CUTOFF = '2026-11-25T11:00:00Z'
FIELDS = '''job_id created_at template state wallet link_evidence unlinked_reason
is_holder order_id order_created_at eligible counted_under counts status category
scope complexity deliverables deliverable_ok quality is_goal category_reason
status_reason eligibility_reason grouping_reason data_gaps sources'''.split()
TEMPLATE_CATEGORIES = {
    'goal': 'multi-agent goal', 'skill:goal-orchestrator': 'multi-agent goal',
    'research': 'research', 'skill:research-report': 'research',
    'skill:quick-answer': 'research', 'skill:write-readme-and-docs': 'research',
    'audit': 'audit', 'skill:audit-imported-code': 'audit',
    'skill:adversarial-review': 'audit', 'fuzz': 'audit',
    'skill:build-website': 'website/product',
    'skill:frontend-for-contract': 'website/product',
    'skill:site-content-check': 'website/product',
    'skill:build-contract-project': 'code/contracts',
    'skill:scaffold-project': 'code/contracts',
    'skill:refine-project': 'code/contracts',
    'skill:implement-and-test': 'code/contracts',
    'impl_tests': 'code/contracts', 'impl_tests_review': 'code/contracts',
    'multi_contract': 'code/contracts',
    'skill:oracle-assess': 'oracle/indexer',
    'skill:build-ponder-indexer': 'oracle/indexer',
}
MEDIA_TEMPLATES = {'skill:create-image', 'skill:token-logo',
                   'skill:create-audio', 'skill:create-video'}
CODE_LAUNCHES = {'custom_token', 'evm_contracts', 'univ4_hook', 'evm_project'}
RUNNING_STATES = {'executing', 'running', 'pending', 'queued', 'planning',
                  'created', 'ready', 'waiting', 'reviewing'}
INPUT_MARKER = re.compile(r'\bneeds_input\b|\b(?:waiting|awaiting) (?:on |for )?(?:the )?requester\b', re.I)
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def obj(value):
    return value if isinstance(value, dict) else {}


def address(value):
    return value.lower() if isinstance(value, str) and ADDRESS.fullmatch(value) else None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None
    except ValueError:
        return None


def nonnegative(value):
    return value if type(value) is int and value >= 0 else None


def roles(value):
    """Normalize all four landed shard encodings without deriving missing roles."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(';') if value else []
    if isinstance(value, dict):
        value = list(value.items())
    if not isinstance(value, list):
        raise ValueError('Unsupported nodeRoles encoding')
    result = {}
    for pair in value:
        if isinstance(pair, str):
            name, count = pair.rsplit(':', 1)
            pair = (name, int(count))
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError('Malformed nodeRoles pair')
        name, count = pair
        if not isinstance(name, str) or nonnegative(count) is None or name in result:
            raise ValueError('Invalid or repeated node role')
        result[name] = count
    return dict(sorted(result.items()))


def read_source(path):
    doc = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(doc, dict) or not {'source', 'fetchedAt', 'coverage', 'errors', 'columns', 'rows'} <= doc.keys():
        raise ValueError(f'{path}: missing source envelope fields')
    columns = doc['columns']
    if not isinstance(columns, list) or len(columns) != len(set(columns)):
        raise ValueError(f'{path}: invalid columns')
    rows = []
    for i, row in enumerate(doc['rows']):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f'{path}: row {i} width differs from columns')
        rows.append(dict(zip(columns, row)))
    return doc, rows


def reduce_detail(detail, result):
    """Project a late job's raw API detail/result into the shard field schema."""
    d, r = obj(detail), obj(result)
    out = {k: d.get(k) for k in ('id', 'state', 'template', 'createdAt', 'updatedAt',
           'paidBy', 'parentJobId', 'oracleRequestId', 'blockedReason')}
    for target, parent, key in (
        ('workflowId', 'workflow', 'id'), ('projectId', 'project', 'id'),
        ('launchKind', 'launch', 'kind'), ('launchStatus', 'launch', 'status'),
        ('launchChainId', 'launch', 'chainId'), ('launchId', 'launch', 'id'),
        ('siteUrl', 'site', 'url')):
        out[target] = obj(d.get(parent)).get(key)
    versions = obj(d.get('project')).get('versions')
    out['projectVersions'] = [obj(v).get('jobId') for v in versions] if isinstance(versions, list) else None
    delivery = {**obj(r.get('delivery')), **{k: v for k, v in obj(d.get('delivery')).items() if v is not None}}
    for target, key in [('deliveryRepoUrl', 'repoUrl'), ('deliveryCommit', 'commit'), ('deliveredAt', 'deliveredAt')]:
        out[target] = delivery.get(key)
    media = obj(delivery.get('media') or d.get('media')).get('files')
    out['mediaFiles'] = len(media) if isinstance(media, list) else None
    nodes = d.get('nodes')
    if isinstance(nodes, list):
        out['nodes'] = len(nodes)
        rs = [obj(n).get('role') for n in nodes]
        out['nodeRoles'] = dict(Counter(rs)) if all(isinstance(v, str) for v in rs) else None
        states = [obj(n).get('state') for n in nodes]
        out['nodesAccepted'] = states.count('accepted') if None not in states else None
        for key in ('revisions', 'judgeRevisions'):
            vals = [obj(n).get(key) for n in nodes]
            out[key] = sum(vals) if all(nonnegative(v) is not None for v in vals) else None
    reviews = d.get('reviews')
    if isinstance(reviews, list):
        states = [obj(v).get('status') for v in reviews]
        out['reviewsSent'] = states.count('sent') if None not in states else None
        entries = [obj(v).get('entries') for v in reviews]
        out['reviewValues'] = [obj(e).get('value') for es in entries for e in es] if all(isinstance(es, list) for es in entries) else None
    out['resultComplete'] = r.get('complete')
    files = r.get('files')
    out['resultFiles'] = len(files) if isinstance(files, list) else None
    out['resultFileNames'] = [obj(f).get('name') for f in files] if isinstance(files, list) else None
    return out


def classify_status(job):
    state = job.get('state')
    if state == 'completed':
        return 'completed', 'state=completed'
    if state in ('cancelled', 'canceled'):
        return 'abandoned', 'state=cancelled'
    if state == 'blocked':
        if INPUT_MARKER.search(job.get('blockedReason') or ''):
            return 'abandoned', 'blocked with explicit requester-input marker'
        return 'failed', 'blocked without explicit requester-input marker; reason may be unavailable'
    if state == 'failed':
        return 'failed', 'state=failed'
    if state in RUNNING_STATES:
        return 'ongoing', 'state=' + state
    return None, 'state absent or not recognized; not assumed to be running'


def classify_category(job, is_goal):
    template = job.get('template')
    if is_goal:
        return 'multi-agent goal', 'goal catalog membership or explicit goal template'
    # A specific template is stronger evidence than incidental launch/site metadata.
    if template in TEMPLATE_CATEGORIES:
        return TEMPLATE_CATEGORIES[template], 'template=' + template
    if template in MEDIA_TEMPLATES:
        return 'website/product', 'template=' + template + '; digital visual/audio/video asset production counted as product content, not code'
    if job.get('launchKind') in CODE_LAUNCHES:
        return 'code/contracts', 'launchKind=' + job['launchKind']
    if job.get('siteUrl'):
        return 'website/product', 'explicit siteUrl on otherwise generic template'
    return 'other', 'no specific mapped template, recognized launch kind, or site; generic execution shapes do not establish subject matter'


def required_paths():
    return (['snapshot.json', 'holders.csv'] +
            [f'sources/api-jobs-{i:02d}/jobs.json' for i in range(8)] +
            [f'sources/api-wallets-{i:02d}/{name}.json' for i in range(6) for name in ('orders', 'late-jobs')] +
            [f'sources/api-catalog/{name}.json' for name in ('job-index', 'goals', 'workflows', 'oracle-requests')])


def build(root):
    missing = [p for p in required_paths() if not (root / p).is_file()]
    if missing:
        raise ValueError('Missing required inputs (no output written): ' + ', '.join(missing))
    snapshot = json.loads((root / 'snapshot.json').read_text())
    if (snapshot.get('snapshotTime') != CUTOFF or
        obj(obj(snapshot.get('chains')).get('ethereum')).get('block') != 26491756 or
        obj(obj(snapshot.get('chains')).get('robinhood')).get('block') != 122989778):
        raise ValueError('snapshot.json does not match the fixed batch snapshot')
    with (root / 'holders.csv').open(newline='', encoding='utf-8') as f:
        holder_rows = list(csv.DictReader(f))
    holders = {address(r.get('wallet')) for r in holder_rows}
    if None in holders or len(holders) != len(holder_rows):
        raise ValueError('holders.csv has invalid or duplicate wallet addresses')
    docs, tables = {}, {}
    for p in required_paths()[2:]:
        docs[p], tables[p] = read_source(root / p)
    catalog = lambda name: tables[f'sources/api-catalog/{name}.json']
    jobs, provenance, gaps = {}, defaultdict(set), defaultdict(dict)

    def merge(ident, row, source, overwrite=True):
        if not isinstance(ident, str) or not ident:
            raise ValueError(f'{source}: missing job ID')
        current = jobs.setdefault(ident, {'id': ident})
        for key, val in row.items():
            if val is not None and (overwrite or current.get(key) is None):
                if key in current and current[key] is not None and current[key] != val:
                    gaps[ident]['source_disagreement.' + key] = 'different observed values; detail preferred over index; first detail preferred over late detail'
                current[key] = val
        provenance[ident].add(source)

    for row in catalog('job-index'):
        if row['id'] in jobs:
            raise ValueError('Duplicate catalog job: ' + row['id'])
        merge(row['id'], row, 'sources/api-catalog/job-index.json')
    late_rows = []
    for p in sorted(tables):
        if p.endswith('/late-jobs.json'):
            for row in tables[p]:
                late_rows.append((p, row))
                merge(row['jobId'], {'createdAt': row.get('jobCreatedAt') or row.get('createdAt'),
                                    'state': row.get('jobState')}, p, overwrite=False)
    detailed = set()
    for p in sorted(tables):
        if '/api-jobs-' in p:
            for row in tables[p]:
                ident = row['id']
                if ident not in jobs:
                    raise ValueError(f'{p}: detail job outside index and late-job union: {ident}')
                if ident in detailed:
                    raise ValueError('Duplicate detail job: ' + ident)
                merge(ident, row, p)
                detailed.add(ident)
    for p, row in late_rows:
        if isinstance(row.get('detail'), dict):
            ident = row['jobId']
            if row['detail'].get('id') != ident:
                raise ValueError(f'{p}: late detail ID mismatch')
            merge(ident, reduce_detail(row['detail'], row.get('result', row.get('resultOrError'))), p, overwrite=False)
            detailed.add(ident)

    goals = {r['jobId']: r for r in catalog('goals')}
    workflows = {r['id']: r for r in catalog('workflows')}
    requests = {r['id']: r for r in catalog('oracle-requests')}
    request_jobs = defaultdict(set)
    job_requests = defaultdict(list)
    for request in requests.values():
        if request.get('jobId'):
            request_jobs[request['id']].add(request['jobId'])
            job_requests[request['jobId']].append(request)
    for ident, job in jobs.items():
        if job.get('oracleRequestId'):
            request_jobs[job['oracleRequestId']].add(ident)
    oracle_jobs = {r['jobId'] for r in requests.values() if r.get('jobId')}
    oracle_jobs.update(j for j, r in jobs.items() if r.get('template') == 'skill:oracle-assess')
    # Explicit membership edges only: parentJobId is deliberately never consulted.
    parents = defaultdict(set)
    for workflow in workflows.values():
        first, second = workflow.get('contractsJobId'), workflow.get('frontendJobId')
        if first and second and first != second:
            parents[second].add(first)
    for goal in goals.values():
        for child in goal.get('children') or []:
            child_id = child[2] if isinstance(child, list) and len(child) >= 3 else obj(child).get('jobId')
            if child_id and child_id != goal['jobId']:
                parents[child_id].add(goal['jobId'])
    # Goal membership is stronger than a workflow's internal edge; resolve both
    # paths and require agreement, rather than choosing an arbitrary parent.
    def root_of(ident, trail=()):
        if ident in trail:
            raise ValueError('Cycle in goal/workflow membership: ' + ' -> '.join(trail + (ident,)))
        if ident in oracle_jobs or not parents[ident]:
            return ident
        roots = {root_of(p, trail + (ident,)) for p in parents[ident]}
        if len(roots) != 1:
            raise ValueError(f'Conflicting goal/workflow roots for {ident}: {sorted(roots)}')
        return roots.pop()

    orders = {}
    for p in sorted(tables):
        if not p.endswith('/orders.json'):
            continue
        for row in tables[p]:
            ident = row.get('orderId')
            if not ident:
                raise ValueError(f'{p}: missing orderId')
            previous = orders.setdefault(ident, {})
            for key, val in row.items():
                if val is not None:
                    if previous.get(key) is not None and previous[key] != val:
                        raise ValueError(f'Conflicting observations of order {ident} field {key}')
                    previous[key] = val
            previous.setdefault('_sources', set()).add(p)
    linked_orders = defaultdict(set)

    def link_result(result, order_id):
        """Only result IDs, never parent/continues/objective/URL guesses."""
        if not isinstance(result, dict):
            return
        if result.get('jobId'):
            linked_orders[result['jobId']].add(order_id)
        request_id = result.get('requestId')
        for job_id in request_jobs.get(request_id, ()):
            linked_orders[job_id].add(order_id)
            provenance[job_id].add('sources/api-catalog/oracle-requests.json')
        # Explicit child results (when retained by the source).
        for child in result.get('children') or []:
            link_result(child, order_id)

    for order_id, order in orders.items():
        link_result(order.get('result'), order_id)
        link_result({'jobId': order.get('resultJobId'),
                     'requestId': order.get('resultId') if order.get('resultKind') == 'oracle' else None,
                     'children': order.get('resultChildren')}, order_id)
        # An order naming a workflow but no job is not direct job-payer evidence.
        # Catalog membership groups jobs, but never manufactures wallet links.

    result_rows = []
    for ident in sorted(jobs):
        job = jobs[ident]
        g = gaps[ident]
        if ident not in detailed:
            g['detail'] = 'no detailed job record in saved job shards or late-jobs; index-only fields retained'
        if job.get('error'):
            g['source_error'] = job['error']
        candidates = [orders[k] for k in sorted(linked_orders[ident])]
        payer = address(job.get('paidBy'))
        if job.get('paidBy') and payer is None:
            g['paidBy'] = 'invalid address supplied by source'
        if ident in oracle_jobs and payer:
            g['paidBy'] = 'oracle panel linkage requires order evidence; paidBy is not used'
            payer = None
        order = None
        if len(candidates) == 1:
            order = candidates[0]
        elif len(candidates) > 1:
            g['order'] = 'multiple orders name job: ' + ','.join(o['orderId'] for o in candidates)
        wallet, evidence, unlinked = payer, 'paidBy' if payer else 'none', None
        if not payer:
            if order and address(order.get('wallet')):
                wallet, evidence = address(order['wallet']), 'order:' + order['orderId']
            elif len(candidates) > 1:
                unlinked = 'ambiguous_orders'
            elif order:
                unlinked = 'order_payer_missing_or_invalid'
            elif ident in oracle_jobs:
                unlinked = 'oracle_no_matching_order'
            elif ident not in detailed:
                unlinked = 'detail_unavailable_no_matching_order'
            else:
                unlinked = 'paidBy_missing_no_matching_order'
        for candidate in candidates:
            provenance[ident].update(candidate['_sources'])
        if order and payer and address(order.get('wallet')) != payer:
            g['payer_conflict'] = 'paidBy takes precedence over disagreeing order wallet; order still controls cutoff'
        if candidates:
            basis = order.get('createdAt') if order else None
            eligibility_reason = 'order createdAt' if order else 'multiple matching orders; no order selected'
        else:
            basis = job.get('createdAt')
            eligibility_reason = 'no order seen; job createdAt fallback (order coverage is incomplete)'
        dt = timestamp(basis)
        eligible = 'unknown' if dt is None else ('true' if dt < timestamp(CUTOFF) else 'false')
        if dt is None:
            eligibility_reason += '; timestamp missing or invalid'
        root_id = root_of(ident)
        status, status_reason = classify_status(job)
        is_goal = ident in goals or job.get('template') in ('goal', 'skill:goal-orchestrator')
        category, category_reason = classify_category(job, is_goal)
        node_roles = roles(job.get('nodeRoles'))
        versions = job.get('projectVersions')
        if versions is not None and (not isinstance(versions, list) or any(not isinstance(v, str) for v in versions)):
            g['versions'] = 'source versions missing IDs or not an array'
            versions = None
        node_count = nonnegative(job.get('nodes'))
        scope = {'nodes': node_count, 'roles': node_roles, 'launch_kind': job.get('launchKind'),
                 'chain': job.get('launchChainId'), 'versions': versions,
                 'version_count': len(set(versions)) if versions is not None else None}
        request_chains = {r['chainId'] for r in job_requests[ident] if r.get('chainId') is not None}
        if scope['chain'] is None and len(request_chains) == 1:
            scope['chain'] = request_chains.pop()
            provenance[ident].add('sources/api-catalog/oracle-requests.json')
        elif scope['chain'] is None and len(request_chains) > 1:
            g['chain'] = 'oracle requests name multiple chains; no single chain selected'
        # A reproducible evidence-only proxy, not an assertion of effort. Unknown
        # components add zero but remain null in scope and are listed in data_gaps.
        complexity = ((node_count or 0) + (len(node_roles) if node_roles is not None else 0)
                      + int(bool(scope['launch_kind'])) + int(scope['chain'] is not None)
                      + (max(0, scope['version_count'] - 1) if scope['version_count'] is not None else 0))
        deliverables = {'commit': job.get('deliveryCommit'), 'repo': job.get('deliveryRepoUrl'),
                        'delivered_at': job.get('deliveredAt'), 'launch_id': job.get('launchId'),
                        'launch_status': job.get('launchStatus'), 'site': job.get('siteUrl'),
                        'media_files': job.get('mediaFiles'), 'result_files': job.get('resultFiles'),
                        'result_file_names': job.get('resultFileNames'), 'result_complete': job.get('resultComplete')}
        # True means a saved explicit success signal, not external verification.
        positive = bool(deliverables['commit'] or deliverables['site'] or
                        deliverables['launch_status'] == 'live' or
                        (nonnegative(deliverables['media_files']) or 0) > 0 or
                        (nonnegative(deliverables['result_files']) or 0) > 0 or
                        deliverables['result_complete'] is True)
        deliverable_ok = True if positive else False if deliverables['result_complete'] is False else None
        accepted = nonnegative(job.get('nodesAccepted'))
        if accepted is not None and node_count is not None and accepted > node_count:
            raise ValueError(f'{ident}: accepted nodes exceed total')
        quality = {'nodes_accepted': accepted,
                   'accepted_share': round(accepted / node_count, 8) if accepted is not None and node_count else None,
                   'revisions': job.get('revisions'), 'judge_revisions': job.get('judgeRevisions'),
                   'reviews_sent': job.get('reviewsSent'), 'review_values': job.get('reviewValues')}
        if ident in goals:
            quality['goal'] = {k: goals[ident].get(k) for k in ('workers', 'itemsTotal', 'itemsAccepted', 'itemsFailed', 'writeup', 'judge', 'orchestrated')}
            provenance[ident].add('sources/api-catalog/goals.json')
        for group_name, values in [('scope', scope), ('deliverables', deliverables), ('quality', quality)]:
            absent = [k for k, v in values.items() if v is None]
            if absent:
                g[group_name + '.null_fields'] = {'fields': absent, 'reason': 'not supplied by saved source, explicitly null, or unavailable; absence is not zero or failure'}
        if deliverable_ok is None:
            g['deliverable_ok'] = 'no explicit deliverable or result completion signal'
        if status is None:
            g['status'] = status_reason
        if root_id != ident:
            grouping_reason = 'explicit goal child or workflow frontend; transitive root'
            provenance[ident].update(['sources/api-catalog/goals.json', 'sources/api-catalog/workflows.json'])
            if root_id not in jobs:
                g['counted_under'] = 'root referenced by catalog but absent from index/late union; child still excluded'
        else:
            grouping_reason = 'oracle panel is its own root' if ident in oracle_jobs else 'no explicit goal/workflow parent; parentJobId never deduplicates continuations'
        result_rows.append(dict(zip(FIELDS, [
            ident, job.get('createdAt'), job.get('template'), job.get('state'), wallet,
            evidence, unlinked, wallet in holders if wallet else None,
            order.get('orderId') if order else None, order.get('createdAt') if order else None,
            eligible, root_id, eligible == 'true' and wallet is not None and root_id == ident,
            status, category, scope, complexity, deliverables, deliverable_ok, quality, is_goal,
            category_reason, status_reason, eligibility_reason, grouping_reason, g, sorted(provenance[ident])
        ])))
    return result_rows


def encode(rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: compact(v) if isinstance(v, (dict, list, bool)) else v for k, v in row.items()})
    return stream.getvalue().encode('utf-8')


def summary(rows):
    counts = lambda field, items: dict(sorted(Counter(r[field] or 'unknown' for r in items).items()))
    counted = [r for r in rows if r['counts']]
    return {'rows': len(rows), 'linked': sum(r['wallet'] is not None for r in rows),
            'holder_linked': sum(r['is_holder'] is True for r in rows), 'counted_roots': len(counted),
            'counted_holder_roots': sum(r['is_holder'] is True for r in counted),
            'grouped_children': sum(r['job_id'] != r['counted_under'] for r in rows),
            'by_status': counts('status', rows), 'by_category': counts('category', rows),
            'by_unlinked_reason': counts('unlinked_reason', [r for r in rows if not r['wallet']]),
            'by_eligibility': counts('eligible', rows), 'counted_by_status': counts('status', counted),
            'counted_by_category': counts('category', counted)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='input/output checkout (also supports fixtures)')
    parser.add_argument('--check', action='store_true', help='compare exact bytes; do not write')
    args = parser.parse_args(argv)
    try:
        rows = build(args.root)
        expected = encode(rows)
        path = args.root / 'jobs.csv'
        print(compact(summary(rows)))
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                print('FAIL: jobs.csv differs from offline rebuild (or is missing)', file=sys.stderr)
                return 1
            print('PASS: jobs.csv matches offline rebuild')
        else:
            path.write_bytes(expected)
            print(f'Wrote jobs.csv ({len(rows)} rows)')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
