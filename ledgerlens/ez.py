"""Local ezBookkeeping adapter. Never writes unconfirmed source rows."""
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

from evalkit.common import load, save_new


def local_open(request, timeout=30):
    # Loopback finance traffic must never use system or environment HTTP proxies.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)

class Ez:
    def __init__(self, runtime, root, port=18084):
        self.runtime, self.root, self.port = Path(runtime).resolve(), Path(root).resolve(), port
        self.root.mkdir(parents=True, exist_ok=True)
        self.base = f'http://127.0.0.1:{port}'
        self.token = ''
        self.process = None
        self.log = None

    def request(self, path, body=None):
        headers = {'X-Timezone-Offset': '480', 'X-Timezone-Name': 'Asia/Shanghai'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + '/api' + path,
                    data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with local_open(request, timeout=30) as response:
                result = json.load(response)
        except Exception as exc:
            raise RuntimeError('ezBookkeeping 请求失败；未自动重试写入。') from exc
        if not result.get('success'):
            raise RuntimeError('ezBookkeeping 拒绝请求；请检查本地服务日志。')
        return result['result']

    def start(self):
        executable = self.runtime / 'ezbookkeeping.exe'
        if not executable.is_file():
            raise ValueError('未找到 ezBookkeeping 运行目录，请设置 LEDGERLENS_EZ_RUNTIME。')
        credential_path = self.root / 'credentials.json'
        first = not (self.root / 'registered.json').exists()
        if not credential_path.exists():
            save_new(credential_path, {'username': 'ledgerlens', 'password': secrets.token_urlsafe(32)})
        credentials = load(credential_path)
        secret_path = self.root / 'secret.json'
        if not secret_path.exists():
            save_new(secret_path, {'secret': secrets.token_hex(32)})
        self._spawn(first, load(secret_path)['secret'])
        try:
            if first:
                try:
                    result = self.request('/authorize.json', {'loginName': credentials['username'], 'password': credentials['password']})
                except RuntimeError:
                    result = self.request('/register.json', {**credentials, 'email': 'local@example.invalid',
                        'nickname': 'LedgerLens 已确认账本', 'language': 'zh-Hans', 'defaultCurrency': 'CNY',
                        'firstDayOfWeek': 1, 'categories': []})
                save_new(self.root / 'registered.json', {'registered': True})
                self.stop()
                self._spawn(False, load(secret_path)['secret'])
            result = self.request('/authorize.json', {'loginName': credentials['username'], 'password': credentials['password']})
            self.token, self.user = result['token'], result['user']
            self._mappings()
        except Exception:
            self.stop()
            raise

    def _spawn(self, register, secret):
        # Never adopt or kill a process that this launcher did not start.
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', self.port)) == 0:
                raise ValueError('账本端口被占用，请先退出另一个 LedgerLens 窗口。')
        env = dict(os.environ)
        (self.root / 'storage').mkdir(exist_ok=True)
        env.update(EBK_SERVER_HTTP_ADDR='127.0.0.1', EBK_SERVER_HTTP_PORT=str(self.port),
                   EBK_SERVER_DOMAIN='127.0.0.1', EBK_DATABASE_DB_PATH=str(self.root / 'ledger.db'),
                   EBK_SECURITY_SECRET_KEY=secret, EBK_USER_ENABLE_REGISTER=str(register).lower(),
                   EBK_SERVER_LOG_REQUEST='false', EBK_LOG_MODE='console',
                   EBK_STORAGE_LOCAL_FILESYSTEM_PATH=str(self.root / 'storage'))
        self.log = (self.root / 'service.log').open('ab')
        self.process = subprocess.Popen([str(self.runtime / 'ezbookkeeping.exe'), '--no-boot-log', 'server', 'run'],
                    cwd=self.runtime, env=env, stdout=self.log, stderr=self.log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError('账本服务启动失败，请查看本地 service.log。')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', self.port)) == 0:
                    return
            time.sleep(.1)
        self.stop()
        raise RuntimeError('账本服务启动超时。')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()
        self.process = None

    def frontend_session(self):
        # Separate browser token: its refresh/revocation must not invalidate the
        # adapter's token used for confirmed writes.
        credentials = load(self.root / 'credentials.json')
        result = self.request('/authorize.json', {'loginName': credentials['username'],
                                                  'password': credentials['password']})
        return {'token': result['token'], 'user': result['user']}

    def _mappings(self):
        self.accounts = {a['name']: a['id'] for a in self.request('/v1/accounts/list.json?visible_only=false')}
        self.categories = {}
        self.parents = {}
        groups = self.request('/v1/transaction/categories/list.json')
        for group in groups.values():
            for parent in group:
                self.parents[parent['type']] = parent['id']
                for child in parent.get('subCategories', []):
                    self.categories[(child['type'], child['name'])] = child['id']

    def account(self, name):
        if name not in self.accounts:
            value = self.request('/v1/accounts/add.json', {'name': name, 'category': 4, 'type': 1,
                'icon': '1', 'color': '607D8B', 'currency': 'CNY', 'balance': '0', 'balanceTime': 946684800})
            self.accounts[name] = value['id']
        return self.accounts[name]

    def category(self, kind, name):
        typ = 1 if kind == 'income' else 2
        if typ not in self.parents:
            self.parents[typ] = self.request('/v1/transaction/categories/add.json', {'name': '收入' if typ == 1 else '支出',
                'type': typ, 'parentId': '0', 'icon': '1', 'color': '607D8B'})['id']
        if (typ, name) not in self.categories:
            self.categories[(typ, name)] = self.request('/v1/transaction/categories/add.json', {'name': name,
                'type': typ, 'parentId': self.parents[typ], 'icon': '1', 'color': '607D8B'})['id']
        return self.categories[(typ, name)]

    def backup(self):
        folder = self.root / 'backups'
        folder.mkdir(exist_ok=True)
        path = folder / (datetime.now().strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3) + '.db')
        with closing(sqlite3.connect(self.root / 'ledger.db')) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)

    def publish(self, store):
        self.backup()
        count = 0
        # Expenses first: a refund can be sent only after its target was sent.
        rows = sorted(store.snapshot()['items'], key=lambda x: x['kind'] == 'refund')
        for item in rows:
            if item['state'] != 'approved':
                continue
            if item['kind'] in ('excluded', 'transfer', 'duplicate'):
                store.set_state(item['id'], 'resolved')
                continue
            if item['kind'] == 'refund':
                target = next(x for x in store.snapshot()['items'] if x['id'] == item['target'])
                if target['state'] != 'posted':
                    continue
            r = item['raw']
            # Platform account is deliberately a statistical container, not a bank balance.
            account = self.account('支付宝流水' if r['platform'] == 'alipay' else '微信流水')
            category = self.category(item['kind'], item['category'])
            comment = f"[LL:{item['id']}] {r['counterparty']} {r['description']}"
            if item['target']:
                comment += ' 关联:' + item['target']
            body = {'type': 2 if item['kind'] == 'income' else 3, 'categoryId': category,
                    'time': int(datetime.strptime(r['datetime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=8))).timestamp()),
                    'utcOffset': 480, 'sourceAccountId': account, 'destinationAccountId': '0',
                    'sourceAmount': -r['amount_cents'] if item['kind'] == 'refund' else r['amount_cents'],
                    'destinationAmount': 0, 'hideAmount': False, 'tagIds': [], 'pictureIds': [],
                    'comment': comment[:255], 'clientSessionId': 'ledgerlens-' + item['id']}
            store.set_state(item['id'], 'sending')
            try:
                result = self.request('/v1/transactions/add.json', body)
                store.set_state(item['id'], 'posted', result['id'])
                count += 1
            except Exception:
                # A timeout can mean the server committed. Never blindly resubmit.
                store.set_state(item['id'], 'uncertain')
                raise
        return {'posted_now': count}
