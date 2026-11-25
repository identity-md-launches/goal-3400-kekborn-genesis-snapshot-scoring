"""Normalize order observations while retaining conflicting evidence."""
from collections import defaultdict
from hashlib import sha256
import json


def body(row, status, text, digest=None):
    if row.get(status) != 200:
        return None
    raw = row.get(text)
    if digest:
        if not isinstance(raw, str) or sha256(raw.encode()).hexdigest() != row.get(digest):
            raise ValueError(f'Body digest mismatch in {text}')
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError(f'Expected JSON object in {text}')
    return raw


def order_observations(tables):
    for path, rows in sorted(tables.items()):
        if path.startswith('sources/api-wallets-') and path.endswith('/orders.json'):
            for row in rows:
                yield path, row
        elif path == 'sources/e04-orders/paid-by.json':
            for row in rows:
                payload = body(row, 'httpStatus', 'bodyText', 'bodySha256')
                if payload is not None:
                    for order in payload['orders']:
                        yield path, {**order, 'wallet': row['wallet']}
        elif path == 'sources/e04-orders/intake.json':
            for row in rows:
                payload = body(row, 'httpStatus', 'bodyText', 'bodySha256')
                if payload is None:
                    continue
                for entry in payload['entries']:
                    order = entry.get('order') or {}
                    admission = entry.get('admission') or {}
                    if entry.get('kind') == 'request' and order.get('id'):
                        yield path, {
                            'orderId': order['id'], 'wallet': entry.get('payer'),
                            'lookupWallet': row.get('wallet'), 'action': order.get('action'),
                            'status': order.get('status'), 'payment': entry,
                            'paymentTx': row['transactionHash'],
                            'createdAt': order.get('createdAt'), 'bound': admission.get('createdAt'),
                            'result': admission.get('result'),
                        }


def aggregate_orders(tables):
    orders = defaultdict(list)
    for path, row in order_observations(tables):
        ident = row.get('orderId')
        if not isinstance(ident, str) or not ident:
            raise ValueError(f'{path}: missing orderId')
        orders[ident].append((path, row))
    return orders


def references(result):
    if not isinstance(result, dict):
        return
    if result.get('kind') == 'oracle-group' and result.get('requests'):
        for request in result['requests']:
            yield from references(request)
        return
    jobs = result.get('jobId')
    questions = result.get('requestId')
    if isinstance(jobs, list) or isinstance(questions, list):
        # The flattened group arrays are independent projections. Raw request
        # objects establish pairings when both IDs are needed.
        for value in jobs if isinstance(jobs, list) else [jobs]:
            if isinstance(value, str) and value:
                yield value, None, None
        for value in questions if isinstance(questions, list) else [questions]:
            if isinstance(value, str) and value:
                yield None, value, None
    elif jobs or questions or result.get('workflowId'):
        yield jobs, questions, result.get('workflowId')
    for request in result.get('requests') or []:
        yield from references(request)


def result_references(row):
    raw = list(references(row.get('result')))
    kind = row.get('resultKind')
    flat = list(references({
        'jobId': row.get('resultJobId'),
        'requestId': row.get('resultId') if kind in ('oracle', 'oracle-group') else None,
        'workflowId': row.get('resultId') if kind == 'workflow' else None,
    }))
    # Compare flattened references through the order resolver when necessary.
    # Keep raw pairings even when the two projections use different shapes.
    return raw, flat


def payment_facts(row):
    payment = row.get('payment') or {}
    states = {str(value).lower() for value in
              (row.get('paymentStatus'), payment.get('status'), payment.get('state'))
              if value is not None}
    order_state = str(row.get('status') or '').lower()
    failed = bool(states & {'failed', 'refused', 'reverted', 'rejected', 'payment_failed',
                            'canceled', 'cancelled', 'expired', 'unpaid'}) or order_state == 'payment_failed'
    confirmed = bool(states & {'confirmed', 'completed', 'succeeded', 'success'})
    txs = {value.lower() for value in (row.get('paymentTx'), payment.get('transactionHash'),
                                      payment.get('txHash'), payment.get('paymentTx'), payment.get('tx'))
           if isinstance(value, str) and value}
    return failed, confirmed, txs
