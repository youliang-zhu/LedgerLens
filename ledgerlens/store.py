"""Durable source evidence and explicit human decisions; amounts are integer cents."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from evalkit.common import canonical, digest, now

KINDS = {'expense', 'income', 'refund', 'transfer', 'excluded', 'duplicate'}


def suppressed(record):
    """User policy: ignore sub-yuan flows, including refunds; retain evidence."""
    return abs(record['amount_cents']) < 100


def suggestion(r):
    kind = 'expense' if r['direction'] == '支出' else 'income' if r['direction'] == '收入' else 'excluded'
    hints = []
    if '退款' in r['type']:
        kind = 'refund'
        hints.append('退款：请选择原消费，不能仅凭金额相同关联。')
    elif '退款' in r['status']:
        hints.append('原消费带退款状态：保留原金额，退款应另行关联，避免抵扣两次。')
    if any(x in r['type'] + r['description'] for x in ('转账', '红包', '提现', '充值', '还款', '收款', '代付')):
        hints.append('请确认实际用途：他人转账可能是消费或收入，只有自有账户转移才选自有转账。')
    if r['direction'] not in ('收入', '支出'):
        hints.append('来源标记不计收支，不代表一定无需统计；请确认用途。')
    if r['status'] not in ('支付成功', '交易成功', '已收钱', '已到账'):
        hints.append('请核对交易状态：' + r['status'])
    return kind, hints


def identity(r):
    return digest({k: r.get(k) for k in ('platform', 'transaction_id', 'datetime', 'amount_cents',
                  'direction', 'counterparty', 'type', 'status', 'method', 'description')})


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, raw TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL, category TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                    hints TEXT NOT NULL, ez_id TEXT, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    n INTEGER PRIMARY KEY, item_id TEXT, action TEXT NOT NULL,
                    payload TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, ai TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, sid, action, payload):
        db.execute('INSERT INTO events(item_id,action,payload,created) VALUES(?,?,?,?)',
                   (sid, action, canonical(payload), now()))

    def ingest(self, data, historical_review=None):
        records = data['records']
        if len({r['source_id'] for r in records}) != len(records):
            raise ValueError('同一批次存在重复来源编号，未导入。')
        added = 0
        with self.db() as db:
            for r in records:
                old = db.execute('SELECT fingerprint FROM items WHERE id=?', (r['source_id'],)).fetchone()
                if old and old['fingerprint'] != identity(r):
                    raise ValueError('同一交易编号的内容发生变化，整批未导入。请保留两份账单核对：' + r['source_id'])
            for r in records:
                kind, hints = suggestion(r)
                if db.execute('SELECT 1 FROM items WHERE id=?', (r['source_id'],)).fetchone():
                    continue
                # Similarity only raises a question. It never merges records.
                for prior in db.execute('SELECT id,raw FROM items'):
                    other = json.loads(prior['raw'])
                    if (other['amount_cents'] == r['amount_cents'] and other['datetime'][:10] == r['datetime'][:10]
                            and other['counterparty'] == r['counterparty'] and kind != 'refund'):
                        hints.append('同日同金额同对方的另一条记录，请检查是否重复：' + prior['id'])
                hints.append('首次接入逐笔确认；系统建议不是已核实结论。')
                category = r['type'] if r['platform'] == 'alipay' and kind == 'expense' else ('其他支出' if kind in ('expense', 'refund') else '其他收入')
                db.execute('INSERT INTO items(id,raw,fingerprint,kind,category,hints,updated) VALUES(?,?,?,?,?,?,?)',
                           (r['source_id'], canonical(r), identity(r), kind, category, canonical(hints), now()))
                self.event(db, r['source_id'], 'source_added', {'dataset': data['dataset_id']})
                added += 1
            db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?)',
                       (data['dataset_id'], canonical(data), canonical({'status': 'not_run'})))
        if historical_review:
            self.apply_ai(data['dataset_id'], historical_review, historical=True)
        return {'added': added, 'skipped': len(records) - added, 'batch_id': data['dataset_id']}

    def apply_ai(self, batch_id, result, historical=False):
        with self.db() as db:
            row = db.execute('SELECT data FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                raise ValueError('未知批次')
            ids = {r['source_id'] for r in json.loads(row['data'])['records']}
            if historical and result.get('dataset_id') != batch_id:
                raise ValueError('AI 报告与账单不匹配')
            if not historical and result.get('status', '').startswith('completed'):
                if result.get('dataset_id') != batch_id:
                    raise ValueError('AI 报告与账单不匹配')
            findings = result.get('review', {}).get('findings', [])
            if any(not set(f['source_ids']) <= ids for f in findings):
                raise ValueError('AI 引用了未知来源')
            db.execute('UPDATE batches SET ai=? WHERE id=?', (canonical({**result, 'historical': historical}), batch_id))
            for f in findings:
                for sid in f['source_ids']:
                    row = db.execute('SELECT hints FROM items WHERE id=?', (sid,)).fetchone()
                    hints = json.loads(row['hints'])
                    hint = ('历史实验 AI 提醒：' if historical else 'AI 提醒：') + f['evidence'] + '\n建议：' + f['suggestion']
                    if hint not in hints:
                        hints.append(hint)
                        db.execute('UPDATE items SET hints=? WHERE id=?', (canonical(hints), sid))

    def snapshot(self, include_suppressed=False):
        with self.db() as db:
            items = [{**dict(r), 'raw': json.loads(r['raw']), 'hints': json.loads(r['hints'])}
                     for r in db.execute('SELECT * FROM items ORDER BY json_extract(raw,\'$.datetime\') DESC,id')]
            batches = [{'id': r['id'], 'ai': json.loads(r['ai'])} for r in db.execute('SELECT id,ai FROM batches')]
        visible = [r for r in items if not suppressed(r['raw'])]
        pending = sum(r['state'] in ('pending', 'deferred') for r in visible)
        unsynced = sum(r['state'] in ('approved', 'sending', 'uncertain') for r in visible)
        return {'items': items if include_suppressed else visible,
                'suppressed': len(items) - len(visible),
                'pending': pending, 'unsynced': unsynced, 'batches': batches,
                'incomplete': bool(pending or unsynced),
                'posted': sum(r['state'] == 'posted' for r in visible)}

    def decide(self, sid, payload):
        action = payload.get('action')
        if action not in ('confirm', 'defer', 'edit'):
            raise ValueError('未知操作')
        with self.db() as db:
            item = db.execute('SELECT * FROM items WHERE id=?', (sid,)).fetchone()
            if not item:
                raise ValueError('记录不存在')
            if suppressed(json.loads(item['raw'])):
                raise ValueError('小于 1 元的流水已屏蔽，不参与审核和统计。')
            if item['state'] in ('posted', 'resolved', 'sending', 'uncertain'):
                raise ValueError('已入账或写入结果待核对的记录不能直接重审。')
            kind = payload.get('kind', item['kind'])
            category = payload.get('category', item['category']).strip()
            target = payload.get('target', '').strip()
            note = payload.get('note', '').strip()
            if kind not in KINDS or len(category) > 40 or len(note) > 500:
                raise ValueError('类型、分类或说明无效')
            if kind in ('expense', 'income') and not category:
                raise ValueError('请填写分类')
            state = 'deferred' if action == 'defer' else 'pending' if action == 'edit' else 'approved'
            if state == 'approved':
                r = json.loads(item['raw'])
                if kind in ('refund', 'duplicate'):
                    other = db.execute('SELECT * FROM items WHERE id=?', (target,)).fetchone()
                    if not other or target == sid or other['state'] not in ('approved', 'posted', 'resolved'):
                        raise ValueError('请先确认关联的原消费／保留记录。')
                    original = json.loads(other['raw'])
                    if suppressed(original):
                        raise ValueError('不能关联已屏蔽的小额流水。')
                    if kind == 'refund':
                        if other['kind'] != 'expense' or original['datetime'] > r['datetime']:
                            raise ValueError('退款必须关联之前发生的支出。')
                        allocated = db.execute("SELECT COALESCE(SUM(json_extract(raw,'$.amount_cents')),0) FROM items WHERE target=? AND kind='refund' AND state IN ('approved','posted','sending','uncertain') AND id!=?", (target, sid)).fetchone()[0]
                        if allocated + r['amount_cents'] > original['amount_cents']:
                            raise ValueError('累计退款超过原消费金额。请重新核对。')
                        category = other['category']
                    elif other['kind'] not in ('expense', 'income', 'transfer') or original['amount_cents'] != r['amount_cents']:
                        raise ValueError('重复记录须关联同金额且不是退款／重复标记的保留记录。')
                elif target:
                    raise ValueError('仅退款和重复记录需要关联对象')
                if kind in ('excluded', 'transfer', 'duplicate') and not note:
                    raise ValueError('请简短注明不计收支／自有转账／重复的依据。')
            # Do not invalidate already-confirmed dependent refunds or duplicates.
            dependents = db.execute("SELECT 1 FROM items WHERE target=? AND state IN ('approved','posted','sending','uncertain') LIMIT 1", (sid,)).fetchone()
            if dependents and (state != 'approved' or kind != item['kind'] or category != item['category']):
                raise ValueError('已有已确认的关联记录，请先处理关联记录。')
            db.execute('UPDATE items SET kind=?,category=?,target=?,note=?,state=?,updated=? WHERE id=?',
                       (kind, category, target, note, state, now(), sid))
            self.event(db, sid, action, {'kind': kind, 'category': category, 'target': target, 'note': note, 'state': state})

    def set_state(self, sid, state, ez_id=None):
        with self.db() as db:
            db.execute('UPDATE items SET state=?,ez_id=COALESCE(?,ez_id),updated=? WHERE id=?', (state, ez_id, now(), sid))
            self.event(db, sid, state, {'ez_id': ez_id})

    def recover_interrupted(self):
        with self.db() as db:
            db.execute("UPDATE items SET state='uncertain' WHERE state='sending'")
