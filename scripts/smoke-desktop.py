"""Opt-in native integration test, isolated synthetic ledger only; no cloud API."""
import argparse
import json
import socket
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledgerlens.app import App
from ledgerlens.ez import local_open
from evalkit.common import digest

urllib.request.urlopen = local_open


def run(runtime, root):
    app = App(root, runtime, 18183, 18184)
    rows = []
    for sid, amount, date, typ in [('synthetic-expense', 10000, '2026-09-01 12:00:00', '商户消费'),
                                  ('synthetic-refund', 2500, '2026-09-02 12:00:00', '商户退款'),
                                  ('synthetic-pending', 30000, '2026-09-03 12:00:00', '转账'),
                                  ('synthetic-small', 99, '2026-09-04 12:00:00', '商户消费')]:
        rows.append({'source_id': sid, 'platform': 'wechat', 'source_row': 19, 'datetime': date,
             'amount_cents': amount, 'direction': '支出', 'counterparty': 'SYNTHETIC', 'type': typ,
             'status': '支付成功', 'description': 'SYNTHETIC', 'method': '零钱', 'transaction_id': sid, 'raw': {}})
    payload = {'schema_version': 1, 'records': rows, 'sources': []}
    data = {'dataset_id': digest(payload), **payload}
    try:
        app.start()
        app.store.ingest(data)
        # Previously approved sub-yuan rows must also stay out of the native ledger.
        app.store.set_state('synthetic-small', 'approved')
        assert app.store.snapshot()['suppressed'] == 1
        assert app.ez.request('/v1/transactions/count.json')['totalCount'] == 0
        app.store.decide(rows[0]['source_id'], {'action': 'confirm', 'kind': 'expense', 'category': '测试消费'})
        app.store.decide(rows[1]['source_id'], {'action': 'confirm', 'kind': 'refund', 'target': rows[0]['source_id']})
        assert app.ez.publish(app.store)['posted_now'] == 2
        assert app.store.ingest(data)['added'] == 0
        assert app.ez.publish(app.store)['posted_now'] == 0
        exported = app.ez.request('/v1/transactions/list/all.json?start_time=1788192000&end_time=1790784000')
        assert len(exported) == 2, type(exported)
        assert sum(x['sourceAmount'] for x in exported) == 7500
        assert app.store.snapshot()['pending'] == 1
        assert app.store.snapshot()['incomplete']
        base = 'http://127.0.0.1:18183'
        try:
            urllib.request.urlopen(base + '/ll/state')
            raise AssertionError('Unauthenticated access accepted')
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        request = urllib.request.Request(base + '/ll/session', json.dumps({'key': app.secret}).encode(),
                    {'Content-Type': 'application/json', 'X-LedgerLens': '1'})
        with urllib.request.urlopen(request) as response:
            cookie = response.headers['Set-Cookie'].split(';')[0]
        request = urllib.request.Request(base + '/ll/state', headers={'Cookie': cookie})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['posted'] == 2
        with urllib.request.urlopen(urllib.request.Request(base + '/ll/ez-session', headers={'Cookie': cookie})) as response:
            front = json.load(response)
        headers = {'Cookie': cookie, 'Authorization': 'Bearer ' + front['token'],
                   'Origin': base, 'Content-Type': 'application/json',
                   'X-Timezone-Offset': '480', 'X-Timezone-Name': 'Asia/Shanghai'}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + '/api/v1/tokens/refresh.json', b'{}', headers)) as response:
                refreshed = json.load(response)['result']
        except urllib.error.HTTPError as exc:
            error = json.load(exc)
            raise AssertionError({k: error.get(k) for k in ('error', 'error_type', 'errorCode', 'errorMessage')}) from None
        if refreshed.get('newToken'):
            headers['Authorization'] = 'Bearer ' + refreshed['newToken']
            with urllib.request.urlopen(urllib.request.Request(base + '/api/v1/tokens/revoke.json',
                    json.dumps({'tokenId': refreshed['oldTokenId']}).encode(), headers)) as response:
                assert json.load(response)['success']
        assert app.ez.request('/v1/transactions/count.json')['totalCount'] == 2
        with urllib.request.urlopen(urllib.request.Request(base + '/api/v1/transactions/statistics.json?use_transaction_timezone=true', headers=headers)) as response:
            assert json.load(response)['success']
        print('Dashboard auth PASS: refresh/revoke works; adapter token remains usable; statistics returns successfully')
        for path in ('/api/v1/transactions/add.json', '/ll/decision'):
            try:
                urllib.request.urlopen(urllib.request.Request(base + path, b'{}', {'Cookie': cookie, 'Origin': 'https://evil.invalid'}))
                raise AssertionError('Cross-origin write accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
        print('Native PASS: 2 confirmed rows; net expense 75.00; 1 pending excluded; approved 0.99 suppressed; repeat adds 0; auth/CSRF enforced')
    finally:
        app.stop()
    for port in (18183, 18184):
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
    print('Lifecycle PASS: owned ports released on stop')
    # Restart the same ledger; mapping reuse and persisted decisions must survive.
    app = App(root, runtime, 18183, 18184)
    try:
        app.start()
        assert app.ez.request('/v1/transactions/count.json')['totalCount'] == 2
        assert app.ez.publish(app.store)['posted_now'] == 0
        print('Restart PASS: decisions and native rows retained without duplicates')
    finally:
        app.stop()


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--runtime', required=True); args = p.parse_args()
    root = tempfile.mkdtemp(prefix='ledgerlens-native-')
    run(args.runtime, root)
    print('Synthetic evidence retained:', root)
