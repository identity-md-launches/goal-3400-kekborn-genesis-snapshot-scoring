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

from shard_validation import unique, validate_shards
from order_evidence import aggregate_orders, body, payment_facts, references, result_references

ROOT = Path(__file__).resolve().parents[2]
CUTOFF = '2026-11-25T11:00:00Z'
FIELDS = '''job_id created_at template state wallet link_evidence unlinked_reason
is_holder order_id order_created_at eligible counted_under counts status category
scope complexity deliverables deliverable_ok quality is_goal category_reason
status_reason eligibility_reason grouping_reason data_gaps sources
oracle_request_id payment_tx order_created_upper_bound'''.split()
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
    except (ValueError, OverflowError):
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
        if out[target] is None and parent == 'launch':
            out[target] = obj(r.get(parent)).get(key)
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
    return 'unknown', 'no mapped template, recognized launch kind, or explicit site evidence'


def required_paths():
    return (['snapshot.json', 'holders.csv'] +
            [f'sources/api-jobs-{i:02d}/{name}.json' for i in range(8) for name in ('index', 'jobs')] +
            [f'sources/api-jobs-04/{name}.json' for name in ('details', 'results')] +
            [f'sources/api-wallets-{i:02d}/{name}.json' for i in range(6) for name in ('wallets', 'orders', 'late-jobs')] +
            [f'sources/e04-jobs-{i:02d}/{name}.json' for i in range(2) for name in ('index', 'records')] +
            [f'sources/e04-orders/{name}.json' for name in ('paid-by', 'intake')] +
            [f'sources/api-catalog/{name}.json' for name in ('job-index', 'goals', 'workflows', 'oracle-requests')])


def build(root, *, verify_shards=True):
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
    if verify_shards:
        validate_shards(root, docs, tables, holders, timestamp(CUTOFF), timestamp)
    catalog = lambda name: tables[f'sources/api-catalog/{name}.json']
    jobs, provenance, gaps = {}, defaultdict(set), defaultdict(dict)

    def merge(ident, row, source, overwrite=True):
        if not isinstance(ident, str) or not ident:
            raise ValueError(f'{source}: missing job ID')
        current = jobs.setdefault(ident, {'id': ident})
        for key, val in row.items():
            old = current.get(key)
            if key == 'nodeRoles':
                val, old = roles(val), roles(old)
            if key == 'paidBy' and val:
                val, old = address(val) or val, address(old) or old
            if old is not None and val is not None and old != val:
                conflict = gaps[ident].setdefault('source_disagreement.' + key, [])
                conflict.append({'previous': old, 'observed': val, 'source': source,
                                 'selected': 'observed' if overwrite else 'previous'})
            if val is not None and (overwrite or old is None):
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
        if '/api-jobs-' in p and p.endswith('/jobs.json'):
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

    def merge_raw(ident, detail, result, detail_path, result_path):
        jobs.setdefault(ident, {'id': ident})
        if detail is not None:
            if detail.get('id') != ident:
                raise ValueError(f'{detail_path}: mismatched detail ID for {ident}')
            merge(ident, reduce_detail(detail, None), detail_path, overwrite=False)
            detailed.add(ident)
        if result is not None:
            if result.get('jobId') != ident:
                raise ValueError(f'{result_path}: mismatched result ID for {ident}')
            row = reduce_detail({}, result)
            row['state'] = result.get('state')
            merge(ident, row, result_path, overwrite=False)

    detail_path, result_path = ('sources/api-jobs-04/details.json', 'sources/api-jobs-04/results.json')
    raw_details = {r['id']: r for r in tables[detail_path]}
    for r in tables[result_path]:
        ident = r['id']
        merge_raw(ident, body(raw_details[ident], 'httpStatus', 'payload'),
                  body(r, 'httpStatus', 'payload'), detail_path, result_path)
        for kind, path, observation in (('detail', detail_path, raw_details[ident]),
                                         ('result', result_path, r)):
            provenance[ident].add(path)
            if observation.get('error'):
                gaps[ident]['raw_' + kind] = observation['error']
    for i in range(2):
        path = f'sources/e04-jobs-{i:02d}/records.json'
        for r in tables[path]:
            ident = r['jobId']
            if i == 0:
                detail = body(r, 'detailHttpStatus', 'detail')
                result = body(r, 'resultHttpStatus', 'result')
            else:
                detail = body(r, 'detailHttpStatus', 'detailBodyText', 'detailSha256')
                result = body(r, 'resultHttpStatus', 'resultBodyText', 'resultSha256')
            merge_raw(ident, detail, result, path, path)
            provenance[ident].add(path)
            for kind in ('detail', 'result'):
                if r.get(kind + 'Error'):
                    gaps[ident]['e04_' + kind] = r[kind + 'Error']

    def keyed_catalog(name, key):
        rows = catalog(name)
        unique(rows, key, f'sources/api-catalog/{name}.json')
        return {row[key]: row for row in rows}

    goals = keyed_catalog('goals', 'jobId')
    workflows = keyed_catalog('workflows', 'id')
    requests = keyed_catalog('oracle-requests', 'id')
    request_jobs, job_questions = defaultdict(set), defaultdict(set)
    catalog_primary = {}
    oracle_source = 'sources/api-catalog/oracle-requests.json'

    def question_job(question, ident):
        request_jobs[question].add(ident)
        job_questions[ident].add(question)

    for question, request in requests.items():
        if request.get('jobId'):
            ident = request['jobId']
            catalog_primary[question] = ident
            question_job(question, ident)
            provenance[ident].add(oracle_source)
    for ident, job in jobs.items():
        if job.get('oracleRequestId'):
            question_job(job['oracleRequestId'], ident)

    parents = defaultdict(set)
    graph_sources = defaultdict(set)

    def edge(child, parent, source):
        if child and parent:
            parents[child].add(parent)
            graph_sources[child].add(source)

    for workflow in workflows.values():
        edge(workflow.get('frontendJobId'), workflow.get('contractsJobId'),
             'sources/api-catalog/workflows.json')
    for goal in goals.values():
        for child in goal.get('children') or []:
            child_id = child[2] if isinstance(child, list) and len(child) >= 3 else obj(child).get('jobId')
            edge(child_id, goal['jobId'], 'sources/api-catalog/goals.json')

    orders = aggregate_orders(tables)
    order_primary = defaultdict(set)
    linked_orders = defaultdict(set)
    continuations = defaultdict(set)
    order_goals = set()
    observed_refs = {}
    for order_id, observations in orders.items():
        for index, (path, row) in enumerate(observations):
            raw, flat = result_references(row)
            observed_refs[order_id, index] = raw, flat
            result = obj(row.get('result'))
            direct = result.get('jobId') or row.get('resultJobId')
            if isinstance(direct, str) and (result.get('kind') or row.get('resultKind')) == 'goal':
                order_goals.add(direct)
                for child in result.get('children') or row.get('resultChildren') or []:
                    child_id = child[2] if isinstance(child, list) and len(child) >= 3 else obj(child).get('jobId')
                    edge(child_id, direct, path)
            for ident, question, _ in raw + flat:
                if ident and question:
                    question_job(question, ident)
                    order_primary[question].add(ident)

    representative = {}
    ambiguous_questions = set()
    for question, members in request_jobs.items():
        candidates = order_primary[question]
        if question in catalog_primary:
            representative[question] = catalog_primary[question]
        elif len(candidates) == 1:
            representative[question] = next(iter(candidates))
        elif len(members) == 1:
            representative[question] = next(iter(members))
        else:
            ambiguous_questions.add(question)
            # Keep one stable audit root while withholding both attribution and credit.
            representative[question] = min(members)
        for ident in members:
            jobs.setdefault(ident, {'id': ident})
            if ident != representative[question]:
                edge(ident, representative[question], oracle_source)

    def resolve(refs):
        targets = set()
        for ident, question, workflow in refs:
            if not question and len(job_questions[ident]) == 1:
                question = next(iter(job_questions[ident]))
            target = representative.get(question, ident) or obj(workflows.get(workflow)).get('contractsJobId')
            if target:
                targets.add(target)
        return targets

    order_conflicts = defaultdict(dict)
    for order_id, observations in orders.items():
        observed_targets = set()
        flat_projections = set()
        raw_projections = set()
        for index, (path, row) in enumerate(observations):
            raw, flat = observed_refs[order_id, index]
            raw_targets, flat_targets = resolve(raw), resolve(flat)
            if raw_targets:
                raw_projections.add(tuple(sorted(raw_targets)))
            if flat_targets:
                flat_projections.add(tuple(sorted(flat_targets)))
            observed_targets.update(raw_targets | flat_targets)
            for target in raw_targets | flat_targets:
                if row.get('continues') or obj(row.get('result')).get('continues') or row.get('action') == 'job.continue':
                    continuations[target].add(order_id)
                jobs.setdefault(target, {'id': target})
                provenance[target].add(path)
            for field, original, observed in row.get('e04Conflicts') or []:
                order_conflicts[order_id][field] = [original, observed]
        if len(raw_projections | flat_projections) > 1:
            order_conflicts[order_id]['result_targets'] = sorted(raw_projections | flat_projections)
        for target in observed_targets:
            linked_orders[target].add(order_id)

    oracle_jobs = {ident for ident, questions in job_questions.items() if questions} | {ident for ident, job in jobs.items()
                                         if job.get('template') == 'skill:oracle-assess'}
    cutoff = timestamp(CUTOFF)
    order_meta = {}
    for order_id, observations in orders.items():
        wallets, created, bounds, txs = set(), set(), set(), set()
        failed = confirmed = False
        for path, row in observations:
            wallets.add(address(row.get('wallet')))
            payment_payer = obj(row.get('payment')).get('payer')
            if payment_payer is not None:
                wallets.add(address(payment_payer))
            if row.get('lookupWallet') and address(row.get('lookupWallet')) != address(row.get('wallet')):
                wallets.add(address(row['lookupWallet']))
            if row.get('createdAtSource') == 'intake.admission.createdAt':
                if row.get('createdAt') is not None:
                    bounds.add(row['createdAt'])
            elif row.get('createdAt') is not None:
                created.add(row['createdAt'])
            if row.get('bound') is not None:
                bounds.add(row['bound'])
            fail, confirm, hashes = payment_facts(row)
            failed |= fail
            confirmed |= confirm
            txs.update(hashes)
        conflicts = order_conflicts[order_id]
        if len(wallets) > 1:
            conflicts['wallet'] = sorted(wallets, key=str)
        created_times = {timestamp(value) for value in created}
        if len(created_times) > 1:
            conflicts['createdAt'] = sorted(created)
        if len(txs) > 1:
            conflicts['paymentTx'] = sorted(txs)
        if failed and confirmed:
            conflicts['payment_status'] = ['failed', 'confirmed']
        bound_times = {timestamp(value) for value in bounds}
        date, eligible = None, 'unknown'
        if failed:
            eligible, reason = 'false', 'explicit payment failure or refusal'
        elif conflicts:
            reason = 'conflicting order observations'
        elif not confirmed:
            reason = 'payment success unavailable'
        elif created:
            date = min(created)
            dt = timestamp(date)
            if dt is None:
                reason = 'unreadable order createdAt'
            elif any(bound is not None and bound < dt for bound in bound_times):
                reason = 'admission bound precedes observed order creation'
            else:
                eligible, reason = ('true' if dt < cutoff else 'false'), 'observed order createdAt'
        elif bounds and None not in bound_times and max(bound_times) < cutoff:
            eligible, reason = 'true', 'specific order was admitted before T (intake admission.createdAt upper bound)'
        else:
            reason = 'no readable pre-T order creation evidence; late admission is not a creation timestamp'
        order_meta[order_id] = {
            'wallets': wallets, 'paid': confirmed and not failed and not conflicts,
            'upper_bound': min(bounds, key=timestamp) if bounds and None not in bound_times else None,
            'date': min(created) if len(created_times) == 1 else None, 'bounds': sorted(bounds),
            'eligible': eligible, 'reason': reason, 'tx': next(iter(txs)) if len(txs) == 1 else None,
            'conflicts': conflicts, 'sources': {path for path, _ in observations},
        }

    # Validate the complete observed graph, including edges bypassed by paid continuations.
    def check_cycle(ident, active, seen):
        if ident in active:
            raise ValueError(f'Cycle in goal/workflow/oracle membership at {ident}')
        if ident not in seen:
            for parent in parents[ident]:
                check_cycle(parent, active | {ident}, seen)
            seen.add(ident)

    seen = set()
    for ident in set(jobs) | set(parents):
        check_cycle(ident, set(), seen)

    separate = set()
    for ident, ids in continuations.items():
        if len(linked_orders[ident]) != 1 or ident in oracle_jobs:
            continue
        oid = next(iter(linked_orders[ident]))
        meta = order_meta[oid]
        payer = jobs[ident].get('paidBy')
        if (oid in ids and meta['paid'] and len(meta['wallets']) == 1
                and None not in meta['wallets'] and (not payer or address(payer) in meta['wallets'])
                and 'source_disagreement.paidBy' not in gaps[ident]):
            separate.add(ident)
    roots = {}

    def root_of(ident):
        if ident not in roots:
            candidates = {root_of(p) for p in parents[ident]} if ident not in separate else set()
            if len(candidates) > 1:
                raise ValueError(f'Conflicting goal/workflow roots for {ident}: {sorted(candidates)}')
            roots[ident] = next(iter(candidates), ident)
        return roots[ident]

    result_rows = []
    for ident in sorted(jobs):
        job = jobs[ident]
        g = gaps[ident]
        if ident not in detailed:
            g['detail'] = 'no detailed job record in saved job shards, late jobs or e04 evidence'
        if job.get('error'):
            g['source_error'] = job['error']
        ids = sorted(linked_orders[ident])
        order_id = ids[0] if len(ids) == 1 else None
        order = order_meta.get(order_id)
        job_payer = address(job.get('paidBy')) if ident not in oracle_jobs else None
        if job.get('paidBy') and not address(job['paidBy']):
            g['paidBy'] = 'invalid address supplied by source'
        ambiguous_oracle = (len(job_questions[ident]) > 1 or bool(job_questions[ident] & ambiguous_questions)
                            or 'source_disagreement.oracleRequestId' in g)
        payers = {job_payer} if job_payer else set()
        for candidate in ids:
            meta = order_meta[candidate]
            payers.update(meta['wallets'] - {None})
            provenance[ident].update(meta['sources'])
            if meta['conflicts']:
                g.setdefault('order_conflicts', {})[candidate] = meta['conflicts']
        if 'source_disagreement.paidBy' in g and ident not in oracle_jobs:
            payers.update(address(c['observed']) for c in g['source_disagreement.paidBy'])
        wallet = next(iter(payers)) if len(payers) == 1 else None
        evidence, unlinked = 'none', None
        eligible, eligibility_reason = 'unknown', 'no matching order; job time cannot establish paid order creation'
        if len(payers) > 1 or 'source_disagreement.paidBy' in g and ident not in oracle_jobs:
            wallet = None
            g['payer_conflict'] = sorted(payers, key=str)
            unlinked, eligibility_reason = 'conflicting_payers', 'conflicting payer observations'
        elif ambiguous_oracle:
            wallet = None
            unlinked, eligibility_reason = 'ambiguous_oracle_identity', 'conflicting oracle request/job identity'
        elif len(ids) > 1:
            g['order'] = ids
            unlinked = 'ambiguous_orders' if not wallet else None
            eligibility_reason = 'multiple matching orders; no order selected'
        elif order:
            eligible, eligibility_reason = order['eligible'], order['reason']
            if wallet not in order['wallets'] or None in order['wallets']:
                eligible, eligibility_reason = 'unknown', 'order payer unavailable or unresolved'
            if order['bounds']:
                g['order_admission_bounds'] = order['bounds']
        if wallet:
            evidence = 'order:' + order_id if order and order['wallets'] == {wallet} else (
                'paidBy' if job_payer == wallet else 'ambiguous_orders_same_wallet')
        elif not unlinked:
            unlinked = ('order_payer_missing_or_invalid' if order else
                        'oracle_no_matching_order' if ident in oracle_jobs else
                        'detail_unavailable_no_matching_order' if ident not in detailed else
                        'paidBy_missing_no_matching_order')
        root_id = root_of(ident)
        status, status_reason = classify_status(job)
        is_goal = ident in goals or ident in order_goals or job.get('template') in ('goal', 'skill:goal-orchestrator')
        if ident in oracle_jobs and not is_goal:
            category, category_reason = 'oracle/indexer', 'explicit oracle template or request membership'
        else:
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
        request_chains = {r['chainId'] for r in (requests[q] for q in job_questions[ident] if q in requests) if r.get('chainId') is not None}
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
        oracle_statuses = {requests[q]['status'] for q in job_questions[ident]
                           if q in requests and requests[q].get('status') is not None}
        attested = oracle_statuses == {'attested'} and not ambiguous_oracle
        positive = deliverables['launch_status'] == 'live' or attested or (
            deliverables['result_complete'] is True and ident not in oracle_jobs)
        deliverables['oracle_attested'] = True if attested else None
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
            grouping_reason = 'explicit goal/workflow membership or duplicate panel; transitive canonical root'
            provenance[ident].update(graph_sources[ident])
            if root_id not in jobs:
                g['counted_under'] = 'root referenced by catalog but absent from job evidence; child still excluded'
        else:
            grouping_reason = 'separately paid continuation' if ident in separate else 'canonical root'
        result_rows.append(dict(zip(FIELDS, [
            ident, job.get('createdAt'), job.get('template'), job.get('state'), wallet,
            evidence, unlinked, wallet in holders if wallet else None,
            order_id, order['date'] if order else None,
            eligible, root_id, eligible == 'true' and wallet is not None and root_id == ident,
            status, category, scope, complexity, deliverables, deliverable_ok, quality, is_goal,
            category_reason, status_reason, eligibility_reason, grouping_reason, g, sorted(provenance[ident]),
            next(iter(job_questions[ident])) if len(job_questions[ident]) == 1 else None,
            order['tx'] if order else None, order['upper_bound'] if order else None
        ])))
    aggregate_children(result_rows, job_questions, representative)
    validate_rows(result_rows)
    return result_rows


def aggregate_children(rows, job_questions, representative):
    by_id = {r['job_id']: r for r in rows}
    members = defaultdict(list)
    for row in rows:
        ident, parent = row['job_id'], row['counted_under']
        if parent == ident or parent not in by_id:
            continue
        root = by_id[parent]
        root['sources'] = sorted(set(root['sources']) | set(row['sources']))
        questions = job_questions[ident]
        if questions and any(representative[q] != ident for q in questions):
            continue  # Panel copies do not inflate the evidence for a question.
        if row['data_gaps'].get('payer_conflict') or (
                row['wallet'] and root['wallet'] and row['wallet'] != root['wallet']):
            root['data_gaps'].setdefault('child_payer_conflicts', []).append(ident)
            root['counts'] = False
            root['eligible'] = 'unknown'
            root['eligibility_reason'] = 'canonical membership has conflicting payer evidence'
            continue
        members[parent].append(row)
    for ident, children in members.items():
        root = by_id[ident]
        work = [root, *children]
        root['scope']['children'] = [{'job_id': row['job_id'], **row['scope']} for row in children]
        root['deliverables']['children'] = [
            {'job_id': row['job_id'], 'deliverable_ok': row['deliverable_ok'], **row['deliverables']}
            for row in children]
        root['quality']['children'] = [{'job_id': row['job_id'], **row['quality']} for row in children]
        root['complexity'] = sum(row['complexity'] for row in work)
        if any(row['deliverable_ok'] is True for row in work):
            root['deliverable_ok'] = True
            root['data_gaps'].pop('deliverable_ok', None)
        # Weight only rows with both observed accepted and total nodes.
        measured = [(row['quality']['nodes_accepted'], row['scope']['nodes']) for row in work
                    if row['quality']['accepted_share'] is not None]
        if measured:
            root['quality']['accepted_share'] = round(
                sum(a for a, _ in measured) / sum(n for _, n in measured), 8)
            gap = root['data_gaps'].get('quality.null_fields')
            if gap and 'accepted_share' in gap['fields']:
                gap['fields'].remove('accepted_share')


def validate_rows(rows):
    by_id = {row['job_id']: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('Duplicate normalized job IDs')
    questions = set()
    for row in rows:
        root = by_id.get(row['counted_under'])
        if root and root['counted_under'] != root['job_id']:
            raise ValueError(f"{row['job_id']}: nonterminal canonical root")
        if row['counts'] and (root is not row or row['eligible'] != 'true'
                              or not row['wallet'] or not row['order_id']):
            raise ValueError(f"{row['job_id']}: counted work lacks a canonical eligible paid order")
        if row['counts'] and row['oracle_request_id']:
            question = row['oracle_request_id']
            if question in questions:
                raise ValueError(f'Duplicate counted oracle question: {question}')
            questions.add(question)
        if not row['wallet'] and not row['unlinked_reason']:
            raise ValueError(f"{row['job_id']}: missing unlinked reason")


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


def main(argv=None, *, verify_shards=True):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='input/output checkout (also supports fixtures)')
    parser.add_argument('--check', action='store_true', help='compare exact bytes; do not write')
    args = parser.parse_args(argv)
    try:
        rows = build(args.root, verify_shards=verify_shards)
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
