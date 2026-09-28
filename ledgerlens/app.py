"""Loopback-only UI, durable review queue, and on-demand native ledger."""
import base64
import json
import mimetypes
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from evalkit.common import canonical, digest, load, save_new
from evalkit.sources import parse
from evalkit.ai import make_request, send
from evalkit.api_config import load_api_config
from .store import Store, suppressed
from .ez import Ez, local_open

PROJECT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).with_name('static')


class App:
    def __init__(self, root, runtime, port=18083, ez_port=18084):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.secret = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.store = Store(self.root / 'review.db')
        self.store.recover_interrupted()
        self.ez = Ez(runtime, self.root / 'ez', ez_port)
        self.lock = threading.RLock()
        self.job = {'state': 'idle', 'message': ''}
        self.server = None
        self.window = None

    def start(self):
        self.ez.start()
        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', self.port), handler(self))
        except Exception:
            self.ez.stop()
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.port}/#key={self.secret}'

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.ez.stop()

    def upload(self, files):
        if not isinstance(files, list) or not 1 <= len(files) <= 2:
            raise ValueError('每次请选择 1–2 个账单文件。')
        folder = self.root / 'originals' / secrets.token_hex(12)
        folder.mkdir(parents=True)
        records, sources, platforms = [], [], set()
        for f in files:
            platform = f['platform']
            if platform not in ('wechat', 'alipay') or platform in platforms:
                raise ValueError('每个平台一次上传一个文件。')
            platforms.add(platform)
            suffix = '.xlsx' if platform == 'wechat' else '.csv'
            if not f['name'].lower().endswith(suffix):
                raise ValueError('微信请选择 XLSX；支付宝请选择 CSV。')
            raw = base64.b64decode(f['content'], validate=True)
            if len(raw) > 15 * 1024 * 1024:
                raise ValueError('单个文件不能超过 15 MB。')
            dest = folder / (platform + suffix)
            # Application persistence of user-uploaded evidence, never a source rewrite.
            with dest.open('xb') as stream:
                stream.write(raw)
            rows, source = parse(dest, platform, experiment_scope=False)
            records.extend(rows)
            source['original_name'] = Path(f['name']).name
            sources.append(source)
        dates = [r['datetime'][:10] for r in records]
        payload = {'schema_version': 1, 'parser_version': 'sources-v1-open-dates', 'sources': sources,
                   'records': records, 'scope': {'start': min(dates) if dates else None,
                   'end': max(dates) if dates else None, 'bank_cross_source': 'not_tested'}}
        data = {'dataset_id': digest(payload), **payload}
        save_new(folder / 'dataset.json', data)
        result = self.store.ingest(data)
        return result

    def run_ai(self, batch):
        with self.lock:
            if self.job['state'] == 'running':
                raise ValueError('已有检查正在进行，请稍候。')
            with self.store.db() as db:
                row = db.execute('SELECT data FROM batches WHERE id=?', (batch,)).fetchone()
            if not row:
                raise ValueError('未知账单批次')
            data = json.loads(row['data'])
            self.job = {'state': 'running', 'message': '独立 AI 检查中，可继续查看记录；暂不能确认或入账。'}

        def worker():
            try:
                items = {i['id']: i for i in self.store.snapshot(include_suppressed=True)['items']}
                candidate = {'schema_version': 1, 'dataset_id': data['dataset_id'], 'candidate_id': 'LedgerLens-review',
                    'phase': 'initial', 'provenance': {'product': 'LedgerLens review draft', 'version': '1',
                    'adapter': 'local-draft-v1', 'mode': 'combined', 'evidence': ['local sources, not native ezBookkeeping output']},
                    'entries': [{'entry_id': r['source_id'], 'source_ids': [r['source_id']],
                        'kind': 'excluded' if suppressed(r) else 'pending',
                        'native_kind': 'excluded' if suppressed(r) or items[r['source_id']]['kind'] == 'duplicate' else items[r['source_id']]['kind'],
                        'amount_cents': r['amount_cents'],
                        'reason': 'User policy: amount below CNY 1 is excluded from review and statistics (including income, expenses and refunds); source retained.' if suppressed(r) else 'Human review required',
                        'refund_allocations': []}
                        for r in data['records']]}
                config = load_api_config(PROJECT / 'api.local.json')
                prepared = make_request(data, candidate, None, config)
                folder = self.root / 'ai' / secrets.token_hex(12)
                result = send(prepared, config, folder, authorized=True)
                self.store.apply_ai(batch, result)
                self.job = {'state': 'done' if result['status'].startswith('completed') else 'error',
                            'message': 'AI 检查已返回；提示不是裁决，请人工确认。' if result['status'].startswith('completed') else 'AI 检查失败，未视为通过；可重试或人工检查全部记录。'}
            except Exception:
                self.job = {'state': 'error', 'message': 'AI 检查未完成，请检查 API 配置；没有自动入账。'}
        threading.Thread(target=worker, daemon=True).start()

    def snapshot(self):
        result = self.store.snapshot()
        result['job'] = self.job
        # The full API evidence stays on disk, not copied into every UI response.
        for b in result['batches']:
            ai = b['ai']
            b['ai'] = {'status': ai.get('status'), 'historical': ai.get('historical', False),
                       'gaps': ai.get('review', {}).get('coverage_gaps', []),
                       'findings': len(ai.get('review', {}).get('findings', []))}
        return result


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log bill contents, session URLs or tokens.

        def respond(self, value, code=200, content_type='application/json; charset=utf-8', cookie=False):
            body = canonical(value).encode('utf-8') if not isinstance(value, bytes) else value
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            if cookie:
                self.send_header('Set-Cookie', f'll_session={app.cookie}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
                return secrets.compare_digest(cookies['ll_session'].value, app.cookie)
            except (KeyError, ValueError):
                return False

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{app.port}'

        def do_GET(self):
            if not self.valid_host():
                return self.respond({'error': 'Invalid host'}, 403)
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/ll/ui.js': ('ui.js', 'text/javascript; charset=utf-8'),
                      '/ll/ui.css': ('ui.css', 'text/css; charset=utf-8')}
            if path in assets:
                file, mime = assets[path]
                return self.respond((STATIC / file).read_bytes(), content_type=mime)
            if not self.authenticated():
                return self.respond({'error': '请通过桌面入口打开 LedgerLens。'}, 401)
            if path == '/ll/state':
                return self.respond(app.snapshot())
            if path == '/ll/ez-session':
                return self.respond(app.ez.frontend_session())
            # Only read-only native UI/API paths, fixed loopback destination.
            if path.startswith(('/desktop', '/mobile', '/api/', '/js/', '/css/', '/img/', '/fonts/', '/assets/')) or path in ('/favicon.ico', '/manifest.json', '/service-worker.js'):
                try:
                    headers = {k: self.headers[k] for k in ('Authorization', 'X-Timezone-Offset', 'X-Timezone-Name') if self.headers.get(k)}
                    headers['Accept-Encoding'] = 'identity'
                    with local_open(urllib.request.Request(app.ez.base + self.path, headers=headers), timeout=30) as r:
                        return self.respond(r.read(), content_type=r.headers.get('Content-Type', 'application/octet-stream'))
                except urllib.error.HTTPError as exc:
                    return self.respond(exc.read(), exc.code, exc.headers.get('Content-Type', 'application/json'))
                except Exception:
                    return self.respond({'error': '账本服务暂不可用'}, 502)
            self.respond({'error': 'Not found'}, 404)

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 42 * 1024 * 1024:
                    return self.respond({'error': '请求大小无效'}, 400)
                self.connection.settimeout(15)
                raw_body = self.rfile.read(size)
            except (ValueError, TimeoutError):
                return self.respond({'error': '请求读取失败'}, 400)
            if not self.valid_host() or self.headers.get('Origin') not in (None, f'http://127.0.0.1:{app.port}'):
                return self.respond({'error': 'Forbidden origin'}, 403)
            # The native UI refreshes its token on every startup. Allow token
            # maintenance, but keep every native ledger write blocked.
            if self.path in ('/api/v1/tokens/refresh.json', '/api/v1/tokens/revoke.json'):
                if not self.authenticated() or not self.headers.get('Authorization', '').startswith('Bearer '):
                    return self.respond({'error': 'Unauthorized'}, 401)
                try:
                    if not 0 < size <= 4096:
                        return self.respond({'error': 'Invalid size'}, 400)
                    request = urllib.request.Request(app.ez.base + self.path,
                        b'' if self.path.endswith('/refresh.json') else raw_body,
                        {'Content-Type': 'application/json', 'Authorization': self.headers['Authorization'],
                         'X-Timezone-Offset': '480', 'X-Timezone-Name': 'Asia/Shanghai'})
                    with local_open(request, timeout=15) as response:
                        return self.respond(response.read())
                except urllib.error.HTTPError as exc:
                    return self.respond(exc.read(), exc.code)
                except Exception as exc:
                    return self.respond({'error': '令牌维护失败，请重新打开看板。', 'error_type': type(exc).__name__}, 502)
            if self.headers.get('X-LedgerLens') != '1':
                return self.respond({'error': '请在审核入口操作；看板仅供查看。'}, 403)
            try:
                payload = json.loads(raw_body)
                if self.path == '/ll/session':
                    if not secrets.compare_digest(str(payload.get('key', '')), app.secret):
                        return self.respond({'error': '请从桌面入口重新打开。'}, 403)
                    return self.respond({'ok': True}, cookie=True)
                if not self.authenticated():
                    return self.respond({'error': '请通过桌面入口打开。'}, 401)
                with app.lock:
                    if app.job['state'] == 'running' and self.path in ('/ll/decision', '/ll/publish', '/ll/upload'):
                        raise ValueError('AI 检查正在进行，完成后再确认或入账。')
                    if self.path == '/ll/upload':
                        result = app.upload(payload['files'])
                        if payload.get('ai') and result['added']:
                            app.run_ai(result['batch_id'])
                    elif self.path == '/ll/decision':
                        app.store.decide(payload['id'], payload)
                        result = {'saved': True}
                    elif self.path == '/ll/publish':
                        result = app.ez.publish(app.store)
                    elif self.path == '/ll/ai':
                        app.run_ai(payload['batch_id'])
                        result = {'started': True}
                    else:
                        return self.respond({'error': 'Not found'}, 404)
                self.respond(result)
            except ValueError as exc:
                self.respond({'error': str(exc)}, 400)
            except Exception:
                self.respond({'error': '操作未完成。进度已保留；如写入结果待核对，请勿重复提交。'}, 500)
    return Handler
