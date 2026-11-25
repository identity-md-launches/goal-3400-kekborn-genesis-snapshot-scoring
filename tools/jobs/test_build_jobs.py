#!/usr/bin/env python3
"""Offline fixture and regression tests; temporary outputs stay in test/scratch."""
from contextlib import redirect_stdout, redirect_stderr
import csv
import io
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import build_jobs as b

# Synthetic test addresses are confined to fixtures, never used in the deliverable.
A = '0x' + 'ab' * 20
B = '0x' + 'cd' * 20
BEFORE = '2026-11-25T10:59:59.999Z'
AFTER = '2026-11-25T11:00:00.001Z'


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = b.ROOT / 'test/scratch/jobs-fixtures'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        snapshot = {'snapshotTime': b.CUTOFF, 'chains': {
            'ethereum': {'block': 26491756}, 'robinhood': {'block': 122989778}}}
        (self.root / 'snapshot.json').write_text(json.dumps(snapshot))
        (self.root / 'holders.csv').write_text('wallet\n' + A + '\n')
        for path in b.required_paths()[2:]:
            self.source(path, [])
        self.index, self.details, self.orders, self.late = [], [], [], []
        self.goals, self.workflows, self.requests = [], [], []

    def source(self, path, rows):
        columns = sorted(set().union(*(r.keys() for r in rows))) if rows else ['id']
        doc = {'source': 'fixture', 'fetchedAt': AFTER, 'coverage': {}, 'errors': [],
               'columns': columns, 'rows': [[r.get(k) for k in columns] for r in rows]}
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def job(self, ident, **overrides):
        row = {'id': ident, 'createdAt': BEFORE, 'state': 'completed',
               'template': 'research', 'paidBy': A, 'nodes': 2,
               'nodeRoles': {'implement': 1, 'review': 1}, 'nodesAccepted': 2,
               'projectVersions': [ident], 'revisions': 0, 'judgeRevisions': 0,
               'reviewValues': [1], 'reviewsSent': 1, 'resultComplete': True,
               'resultFiles': 1, 'resultFileNames': ['report']}
        row.update(overrides)
        self.details.append(row)
        self.index.append({k: row.get(k) for k in ('id', 'createdAt', 'state', 'template')})
        return row

    def order(self, ident, job=None, **overrides):
        row = {'orderId': ident, 'wallet': A, 'createdAt': BEFORE,
               'resultKind': 'job', 'resultJobId': job, 'paymentStatus': 'confirmed'}
        row.update(overrides)
        self.orders.append(row)
        return row

    def run_build(self):
        self.source('sources/api-catalog/job-index.json', self.index)
        self.source('sources/api-jobs-00/jobs.json', self.details)
        self.source('sources/api-wallets-00/orders.json', self.orders)
        self.source('sources/api-wallets-00/late-jobs.json', self.late)
        self.source('sources/api-catalog/goals.json', self.goals)
        self.source('sources/api-catalog/workflows.json', self.workflows)
        self.source('sources/api-catalog/oracle-requests.json', self.requests)
        return {r['job_id']: r for r in b.build(self.root, verify_shards=False)}

    def test_strict_cutoff_and_order_precedence(self):
        self.job('early-order-late-job', createdAt=AFTER)
        self.order('o1', 'early-order-late-job')
        self.job('late-order-early-job')
        self.order('o2', 'late-order-early-job', createdAt=b.CUTOFF)
        self.job('at-cutoff', createdAt=b.CUTOFF)
        self.job('after-cutoff', createdAt=AFTER)
        self.job('unknown-job-time', createdAt=None)
        self.job('unknown-order-time')
        self.order('o3', 'unknown-order-time', createdAt=None)
        self.job('offset-time', createdAt='2026-11-25T12:59:59.999+02:00')
        self.order('o4', 'offset-time', createdAt='2026-11-25T12:59:59.999+02:00')
        rows = self.run_build()
        self.assertTrue(rows['early-order-late-job']['counts'])
        self.assertEqual(rows['late-order-early-job']['eligible'], 'false')
        self.assertEqual(rows['at-cutoff']['eligible'], 'unknown')
        self.assertEqual(rows['after-cutoff']['eligible'], 'unknown')
        self.assertEqual(rows['unknown-job-time']['eligible'], 'unknown')
        self.assertEqual(rows['unknown-order-time']['eligible'], 'unknown')
        self.assertEqual(rows['offset-time']['eligible'], 'true')

    def test_payer_disagreement_does_not_attribute(self):
        self.job('paid', paidBy=A.upper().replace('0X', '0x'))
        self.order('o1', 'paid', wallet=B)
        self.job('order-linked', paidBy=None)
        self.order('o2', 'order-linked', wallet=B)
        rows = self.run_build()
        self.assertIsNone(rows['paid']['wallet'])
        self.assertEqual(rows['paid']['unlinked_reason'], 'conflicting_payers')
        self.assertFalse(rows['paid']['counts'])
        self.assertIn('payer_conflict', rows['paid']['data_gaps'])
        self.assertEqual(rows['order-linked']['wallet'], B)
        self.assertEqual(rows['order-linked']['link_evidence'], 'order:o2')
        self.assertFalse(rows['order-linked']['is_holder'])
        self.assertTrue(rows['order-linked']['counts'])  # counts is not holder filtering

    def test_oracle_order_only_and_request_join(self):
        self.job('panel', template='skill:oracle-assess', paidBy=None)
        self.job('unlinked-panel', template='skill:oracle-assess', paidBy=A)
        self.requests = [{'id': 'request', 'jobId': 'panel', 'chainId': 4663}]
        self.order('o1', resultKind='oracle', resultId='request')
        self.goals = [{'jobId': 'parent', 'children': [['x', 'oracle.request', 'panel', 'opened']]}]
        rows = self.run_build()
        self.assertEqual(rows['panel']['link_evidence'], 'order:o1')
        self.assertEqual(rows['panel']['counted_under'], 'parent')
        self.assertEqual(rows['panel']['scope']['chain'], 4663)
        self.assertFalse(rows['panel']['counts'])
        self.assertIsNone(rows['unlinked-panel']['wallet'])
        self.assertEqual(rows['unlinked-panel']['unlinked_reason'], 'oracle_no_matching_order')

    def test_raw_order_result_and_detail_request_id(self):
        self.job('panel', template='skill:oracle-assess', paidBy=None, oracleRequestId='q')
        self.order('o1', result={'kind': 'oracle', 'requestId': 'q'})
        rows = self.run_build()
        self.assertEqual(rows['panel']['link_evidence'], 'order:o1')

    def test_grouping_and_paid_continuation(self):
        self.job('goal', template='goal')
        self.job('contracts', template='impl_tests')
        self.job('frontend', template='skill:build-website')
        self.job('continuation', parentJobId='contracts', template='impl_tests')
        self.order('goal-order', 'goal')
        self.order('continue-order', 'continuation', continues='contracts')
        self.goals = [{'jobId': 'goal', 'children': [['child', 'workflow.open', 'contracts', 'opened'], ['continuation', 'job.continue', 'continuation', 'opened']]}]
        self.workflows = [{'id': 'w', 'contractsJobId': 'contracts', 'frontendJobId': 'frontend'}]
        rows = self.run_build()
        self.assertEqual(rows['frontend']['counted_under'], 'goal')
        self.assertFalse(rows['frontend']['counts'])
        self.assertFalse(rows['contracts']['counts'])
        self.assertTrue(rows['goal']['counts'])
        self.assertTrue(rows['continuation']['counts'])
        self.assertEqual(rows['continuation']['counted_under'], 'continuation')
        self.assertTrue(rows['goal']['is_goal'])

    def test_paid_late_continuation_stays_distinct_and_unscored(self):
        self.job('goal', template='goal')
        self.job('late-continuation')
        self.order('goal-order', 'goal')
        self.order('late-order', 'late-continuation', action='job.continue', createdAt=AFTER)
        self.goals = [{'jobId': 'goal', 'children': [{'jobId': 'late-continuation'}]}]
        rows = self.run_build()
        self.assertTrue(rows['goal']['counts'])
        self.assertNotIn('children', rows['goal']['scope'])
        late = rows['late-continuation']
        self.assertEqual(late['counted_under'], 'late-continuation')
        self.assertEqual(late['eligible'], 'false')
        self.assertFalse(late['counts'])

    def test_workflow_order_links_explicit_contract_stage(self):
        self.job('first', paidBy=None)
        self.job('second', paidBy=None)
        self.workflows = [{'id': 'w', 'contractsJobId': 'first', 'frontendJobId': 'second'}]
        self.order('o1', resultKind='workflow', resultId='w')
        rows = self.run_build()
        self.assertEqual(rows['first']['wallet'], A)
        self.assertTrue(rows['first']['counts'])
        self.assertIsNone(rows['second']['wallet'])
        self.assertEqual(rows['second']['counted_under'], 'first')

    def test_unknowns_are_not_zero_or_success(self):
        self.index = [{'id': 'index-only', 'createdAt': BEFORE, 'state': 'blocked', 'template': 'skill:oracle-assess'}]
        row = self.run_build()['index-only']
        self.assertIsNone(row['scope']['nodes'])
        self.assertIsNone(row['quality']['accepted_share'])
        self.assertIsNone(row['deliverable_ok'])
        self.assertIsNone(row['is_holder'])
        self.assertFalse(row['counts'])
        self.assertEqual(row['status'], 'failed')
        self.assertIn('detail', row['data_gaps'])
        self.assertEqual(row['complexity'], 0)

    def test_late_union_dedup_and_raw_detail(self):
        self.job('indexed')
        self.late = [{'jobId': 'indexed', 'jobCreatedAt': BEFORE},
                     {'jobId': 'late-only', 'jobCreatedAt': AFTER, 'detail': {
                         'id': 'late-only', 'createdAt': AFTER, 'state': 'completed',
                         'template': 'research', 'paidBy': None,
                         'nodes': [{'role': 'implement', 'state': 'accepted', 'revisions': 0, 'judgeRevisions': 0}],
                         'reviews': [], 'project': {'versions': [{'jobId': 'late-only'}]}},
                      'resultOrError': {'complete': True, 'files': [{'name': 'research'}]}}]
        self.order('o1', 'late-only')
        rows = self.run_build()
        self.assertEqual(set(rows), {'indexed', 'late-only'})
        self.assertEqual(rows['late-only']['scope']['roles'], {'implement': 1})
        self.assertEqual(rows['late-only']['quality']['accepted_share'], 1)
        self.assertTrue(rows['late-only']['deliverable_ok'])
        self.assertTrue(rows['late-only']['counts'])

    def test_ambiguous_order_is_not_guessed(self):
        self.job('ambiguous', paidBy=None)
        self.order('o1', 'ambiguous')
        self.order('o2', 'ambiguous', wallet=B)
        row = self.run_build()['ambiguous']
        self.assertIsNone(row['wallet'])
        self.assertIsNone(row['order_id'])
        self.assertEqual(row['eligible'], 'unknown')
        self.assertFalse(row['counts'])
        self.assertEqual(row['unlinked_reason'], 'conflicting_payers')

    def test_invalid_address_does_not_link(self):
        self.job('bad-address', paidBy='not-an-address')
        row = self.run_build()['bad-address']
        self.assertIsNone(row['wallet'])
        self.assertIn('paidBy', row['data_gaps'])

    def test_conflicting_order_results_stay_visible_and_unscored(self):
        self.order('o1', 'job')
        self.order('o1', 'other')
        rows = self.run_build()
        for row in rows.values():
            self.assertEqual(row['wallet'], A)
            self.assertFalse(row['counts'])
            self.assertIn('result_targets', row['data_gaps']['order_conflicts']['o1'])

    def raw_order_source(self, name, payload, **fields):
        raw = json.dumps(payload)
        self.source(f'sources/e04-orders/{name}.json', [{
            'wallet': A, 'httpStatus': 200, 'bodyText': raw,
            'bodySha256': sha256(raw.encode()).hexdigest(), **fields}])

    def test_order_admission_upper_bound_and_late_admission(self):
        for ident in ('early', 'equal', 'late', 'bad-time'):
            self.job(ident)
        entries = []
        for ident, at in [('early', BEFORE), ('equal', b.CUTOFF), ('late', AFTER), ('bad-time', BEFORE)]:
            order = {'id': ident, 'status': 'paid'}
            if ident == 'bad-time':
                order['createdAt'] = 'unreadable'
            entries.append({'kind': 'request', 'payer': A, 'state': 'completed', 'order': order,
                            'admission': {'createdAt': at, 'result': {'jobId': ident}}})
        self.raw_order_source('intake', {'entries': entries}, transactionHash='shared-payment')
        rows = self.run_build()
        self.assertTrue(rows['early']['counts'])
        self.assertIsNone(rows['early']['order_created_at'])
        self.assertEqual(rows['early']['order_created_upper_bound'], BEFORE)
        for ident in ('equal', 'late', 'bad-time'):
            self.assertEqual(rows[ident]['eligible'], 'unknown')
            self.assertFalse(rows[ident]['counts'])

    def test_failed_payment_and_unconfirmed_payment(self):
        self.job('failed')
        self.order('of', 'failed', paymentStatus='failed')
        self.job('unconfirmed')
        self.order('ou', 'unconfirmed', paymentStatus=None)
        rows = self.run_build()
        self.assertEqual(rows['failed']['eligible'], 'false')
        self.assertEqual(rows['unconfirmed']['eligible'], 'unknown')
        self.assertFalse(any(row['counts'] for row in rows.values()))

    def test_oracle_group_shared_payment_duplicate_panel_and_attestation(self):
        self.job('panel-a', paidBy=None, oracleRequestId='a')
        self.job('panel-a-copy', paidBy=None, oracleRequestId='a', nodes=20)
        self.job('panel-b', paidBy=None, oracleRequestId='b')
        self.requests = [{'id': 'a', 'jobId': 'panel-a', 'status': 'attested'},
                         {'id': 'b', 'jobId': 'panel-b', 'status': 'disagreed'}]
        self.order('shared', resultKind='oracle-group', resultJobId=['panel-a', 'panel-b'],
                   resultId=['a', 'b'], paymentTx='shared-tx',
                   result={'kind': 'oracle-group', 'jobId': 'group-container', 'requests': [
                       {'requestId': 'a', 'jobId': 'panel-a'},
                       {'requestId': 'a', 'jobId': 'panel-a-copy'},
                       {'requestId': 'b', 'jobId': 'panel-b'}]})
        rows = self.run_build()
        self.assertEqual({key for key, row in rows.items() if row['counts']}, {'panel-a', 'panel-b'})
        self.assertEqual(rows['panel-a-copy']['counted_under'], 'panel-a')
        self.assertIsNone(rows['panel-b']['deliverable_ok'])
        self.assertTrue(rows['panel-a']['deliverable_ok'])
        self.assertEqual(rows['panel-a']['complexity'], 4)
        self.assertEqual({rows[key]['payment_tx'] for key in ('panel-a', 'panel-b')}, {'shared-tx'})

    def test_nested_children_aggregate_once_without_url_success(self):
        self.job('outer', template='goal', resultComplete=None, siteUrl='https://example.invalid')
        self.job('inner', template='goal', resultComplete=None, resultFiles=1)
        self.job('start', nodesAccepted=1, resultComplete=None, deliveryCommit='abc')
        self.job('finish', resultComplete=True)
        self.order('outer-order', 'outer')
        self.goals = [{'jobId': 'outer', 'children': [{'jobId': 'inner'}]},
                      {'jobId': 'inner', 'children': [{'jobId': 'start'}, {'jobId': 'finish'}]}]
        self.workflows = [{'id': 'w', 'contractsJobId': 'start', 'frontendJobId': 'finish'}]
        rows = self.run_build()
        self.assertEqual({key for key, row in rows.items() if row['counts']}, {'outer'})
        self.assertEqual({v['job_id'] for v in rows['outer']['scope']['children']}, {'inner', 'start', 'finish'})
        self.assertEqual(rows['outer']['quality']['accepted_share'], 0.875)
        self.assertEqual(rows['outer']['complexity'], 16)
        self.assertIsNone(rows['start']['deliverable_ok'])
        self.assertTrue(rows['outer']['deliverable_ok'])

    def test_raw_detail_payer_conflict_keeps_both_observations(self):
        self.job('conflicted')
        self.order('o', 'conflicted')
        raw = json.dumps({'id': 'conflicted', 'paidBy': B})
        self.source('sources/e04-jobs-01/records.json', [{
            'jobId': 'conflicted', 'detailHttpStatus': 200, 'detailBodyText': raw,
            'detailSha256': sha256(raw.encode()).hexdigest()}])
        row = self.run_build()['conflicted']
        self.assertEqual(row['unlinked_reason'], 'conflicting_payers')
        self.assertEqual(row['data_gaps']['source_disagreement.paidBy'][0]['observed'], B)
        self.assertFalse(row['counts'])

    def test_group_cycle_fails(self):
        self.job('a')
        self.job('b')
        self.workflows = [{'id': 'w1', 'contractsJobId': 'a', 'frontendJobId': 'b'},
                          {'id': 'w2', 'contractsJobId': 'b', 'frontendJobId': 'a'}]
        with self.assertRaisesRegex(ValueError, 'Cycle'):
            self.run_build()

    def test_conflicting_roots_fail(self):
        self.job('a')
        self.job('b')
        self.job('c')
        self.goals = [{'jobId': 'a', 'children': [['x', 'job.open', 'c', 'opened']]},
                      {'jobId': 'b', 'children': [['x', 'job.open', 'c', 'opened']]}]
        with self.assertRaisesRegex(ValueError, 'Conflicting goal/workflow roots'):
            self.run_build()

    def test_missing_root_does_not_promote_child(self):
        self.job('child')
        self.goals = [{'jobId': 'missing', 'children': [['x', 'job.open', 'child', 'opened']]}]
        row = self.run_build()['child']
        self.assertFalse(row['counts'])
        self.assertEqual(row['counted_under'], 'missing')
        self.assertIn('counted_under', row['data_gaps'])

    def test_byte_check_and_missing_input_preserve_output(self):
        self.job('j')
        self.run_build()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(b.main(['--root', str(self.root)], verify_shards=False), 0)
            self.assertEqual(b.main(['--root', str(self.root), '--check'], verify_shards=False), 0)
            output = self.root / 'jobs.csv'
            output.write_bytes(output.read_bytes() + b'\n')
            changed = output.read_bytes()
            self.assertEqual(b.main(['--root', str(self.root), '--check'], verify_shards=False), 1)
            self.assertEqual(output.read_bytes(), changed)
            (self.root / 'sources/api-wallets-05/late-jobs.json').unlink()
            self.assertEqual(b.main(['--root', str(self.root)], verify_shards=False), 2)
            self.assertEqual(output.read_bytes(), changed)
        self.assertIn('PASS: jobs.csv matches', stdout.getvalue())
        self.assertIn('FAIL: jobs.csv differs', stderr.getvalue())
        self.assertIn('Missing required inputs', stderr.getvalue())

    def test_pinned_snapshot_fails_closed(self):
        (self.root / 'snapshot.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'fixed batch snapshot'):
            self.run_build()

    def test_reject_bad_source_width(self):
        self.run_build()
        path = self.root / 'sources/api-wallets-05/orders.json'
        data = json.loads(path.read_text())
        data['rows'] = [['extra', 'cell']]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'width differs'):
            b.build(self.root, verify_shards=False)

    def test_csv_json_round_trip_and_determinism(self):
        self.order('o', 'quoted')
        self.job('quoted', resultFileNames=['comma,quote"', 'new\nline'])
        rows = self.run_build()
        first = b.encode(list(rows.values()))
        self.assertEqual(first, b.encode(list(self.run_build().values())))
        parsed = list(csv.DictReader(io.StringIO(first.decode())))[0]
        self.assertEqual(json.loads(parsed['deliverables'])['result_file_names'], ['comma,quote"', 'new\nline'])
        self.assertEqual(parsed['counts'], 'true')

    def test_complexity_uses_only_available_components(self):
        self.job('j', launchKind='evm_project', launchChainId=1, projectVersions=['j', 'k', 'k'])
        row = self.run_build()['j']
        self.assertEqual(row['complexity'], 7)  # 2 nodes + 2 roles + launch + chain + 1 extra version


class Rules(unittest.TestCase):
    def test_role_encodings(self):
        expected = {'implement': 1, 'review': 2}
        for value in [expected, 'implement:1;review:2', ['implement:1', 'review:2'],
                      [['implement', 1], ['review', 2]]]:
            self.assertEqual(b.roles(value), expected)
        self.assertIsNone(b.roles(None))
        self.assertEqual(b.roles(''), {})
        with self.assertRaises(ValueError):
            b.roles([['review', 1], ['review', 1]])

    def test_status(self):
        cases = [('completed', None, 'completed'), ('cancelled', None, 'abandoned'),
                 ('blocked', 'node: needs_input: select owner', 'abandoned'),
                 ('blocked', 'waiting for the requester', 'abandoned'),
                 ('blocked', 'runtime_error', 'failed'), ('blocked', None, 'failed'),
                 ('executing', None, 'ongoing'), (None, None, None),
                 ('unrecognized', None, None)]
        for state, reason, expected in cases:
            self.assertEqual(b.classify_status({'state': state, 'blockedReason': reason})[0], expected)

    def test_category_map_and_no_objective_guessing(self):
        for template, category in b.TEMPLATE_CATEGORIES.items():
            self.assertEqual(b.classify_category({'template': template}, False)[0], category)
        for template in b.MEDIA_TEMPLATES:
            category, reason = b.classify_category({'template': template}, False)
            self.assertEqual(category, 'website/product')
            self.assertIn('product content', reason)
        self.assertEqual(b.classify_category({'template': 'shape:dag', 'objective': 'write a contract'}, False)[0], 'unknown')
        self.assertEqual(b.classify_category({'template': 'shape:chain', 'launchKind': 'evm_project'}, False)[0], 'code/contracts')
        self.assertEqual(b.classify_category({'template': 'shape:dag', 'siteUrl': 'https://example.invalid'}, False)[0], 'website/product')


if __name__ == '__main__':
    unittest.main(verbosity=2)
