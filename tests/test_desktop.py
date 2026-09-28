import copy
import base64
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from evalkit.common import digest
from ledgerlens.store import Store
from ledgerlens.ez import Ez
from ledgerlens.app import App


def row(sid, amount=10000, kind='商户消费', date='2026-10-01 12:00:00'):
    return {'source_id': sid, 'platform': 'wechat', 'source_row': 19, 'datetime': date,
            'amount_cents': amount, 'direction': '支出', 'counterparty': '合成测试商户',
            'status': '支付成功', 'type': kind, 'transaction_id': sid, 'method': '零钱',
            'description': '合成测试', 'raw': {}}


def dataset(rows):
    data = {'schema_version': 1, 'records': rows, 'sources': []}
    return {'dataset_id': digest(data), **data}


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'review.db')
        self.data = dataset([row('expense'), row('refund', 2500, '商户退款', '2026-10-02 12:00:00'), row('other', 5000)])
        self.store.ingest(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def confirm(self, sid, kind='expense', target='', note=''):
        self.store.decide(sid, {'action': 'confirm', 'kind': kind, 'category': '测试', 'target': target, 'note': note})

    def test_initial_all_pending(self):
        self.assertEqual(self.store.snapshot()['pending'], 3)
        self.assertEqual(self.store.snapshot()['posted'], 0)

    def test_sub_yuan_boundary_and_evidence_retained(self):
        small = dataset([row('small-' + str(n), n) for n in (0, 1, 99, 100, 101)])
        self.store.ingest(small)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['suppressed'], 3)
        self.assertEqual(snapshot['pending'], 5)
        self.assertTrue(all(i['raw']['amount_cents'] >= 100 for i in snapshot['items']))
        self.assertEqual(len(self.store.snapshot(include_suppressed=True)['items']), 8)
        self.assertEqual(self.store.ingest(small)['added'], 0)
        self.assertEqual(Store(self.store.path).snapshot()['suppressed'], 3)

    def test_sub_yuan_cannot_confirm_edit_or_defer(self):
        self.store.ingest(dataset([row('small', 99)]))
        for action in ('confirm', 'edit', 'defer'):
            with self.assertRaisesRegex(ValueError, '已屏蔽'):
                self.store.decide('small', {'action': action})

    def test_sub_yuan_existing_approved_all_kinds_not_published(self):
        kinds = ('expense', 'income', 'refund', 'transfer', 'excluded', 'duplicate')
        self.store.ingest(dataset([row('small-' + k, 99) for k in kinds]))
        # Simulate approvals saved by the previous version; retain them as evidence.
        with self.store.db() as db:
            for k in kinds:
                db.execute("UPDATE items SET state='approved',kind=? WHERE id=?", (k, 'small-' + k))
        ez = Ez(Path(self.temp.name), Path(self.temp.name) / 'ez')
        ez.backup = Mock(); ez.request = Mock()
        self.assertEqual(ez.publish(self.store)['posted_now'], 0)
        ez.request.assert_not_called()
        self.assertEqual(self.store.snapshot()['unsynced'], 0)
        self.assertEqual(self.store.snapshot()['suppressed'], 6)

    def test_ai_hint_does_not_restore_sub_yuan_record(self):
        data = dataset([row('small', 99)])
        self.store.ingest(data)
        self.store.apply_ai(data['dataset_id'], {'status': 'completed_with_gaps', 'dataset_id': data['dataset_id'],
            'review': {'findings': [{'source_ids': ['small'], 'evidence': 'test', 'suggestion': 'check'}]}})
        self.assertNotIn('small', [i['id'] for i in self.store.snapshot()['items']])

    def test_suppressed_only_batch_not_incomplete(self):
        store = Store(Path(self.temp.name) / 'only-small.db')
        store.ingest(dataset([row('small', 99)]))
        self.assertFalse(store.snapshot()['incomplete'])
        self.assertEqual(store.snapshot()['items'], [])

    def test_upload_future_month_and_repeat(self):
        content = io.StringIO(newline='')
        writer = csv.writer(content)
        writer.writerow(['共1笔'])
        writer.writerow(['交易时间','交易对方','收/支','交易分类','商品说明','金额','收/付款方式','交易状态','交易订单号','商家订单号'])
        writer.writerow(['2026-10-01 12:00:00','合成商户','支出','餐饮美食','测试','12.34','余额','交易成功','TEST-ORDER-1','TEST-MERCHANT-1'])
        files = [{'platform':'alipay','name':'synthetic.csv','content':base64.b64encode(content.getvalue().encode('utf-8')).decode()}]
        app = App(Path(self.temp.name) / 'upload', Path(self.temp.name) / 'unused-runtime')
        self.assertEqual(app.upload(files)['added'], 1)
        self.assertEqual(app.upload(files)['added'], 0)
        self.assertEqual(app.store.snapshot()['pending'], 1)
        self.assertEqual(app.store.snapshot()['items'][0]['raw']['amount_cents'], 1234)

    def test_upload_rejects_wrong_extension(self):
        app = App(Path(self.temp.name) / 'upload', Path(self.temp.name) / 'unused-runtime')
        with self.assertRaises(ValueError):
            app.upload([{'platform':'alipay','name':'wrong.xlsx','content':''}])

    def test_repeat_and_overlap(self):
        self.assertEqual(self.store.ingest(self.data)['added'], 0)
        self.assertEqual(self.store.ingest(dataset([row('other', 5000), row('new')]))['added'], 1)
        self.assertEqual(len(self.store.snapshot()['items']), 4)

    def test_conflict_atomic(self):
        with self.assertRaises(ValueError):
            self.store.ingest(dataset([row('new'), row('expense', 9900)]))
        self.assertEqual(len(self.store.snapshot()['items']), 3)

    def test_decision_persists(self):
        self.confirm('expense')
        reopened = Store(self.store.path)
        self.assertEqual(next(x for x in reopened.snapshot()['items'] if x['id'] == 'expense')['state'], 'approved')

    def test_defer_never_approved(self):
        self.store.decide('expense', {'action': 'defer'})
        self.assertEqual(self.store.snapshot()['pending'], 3)

    def test_refund_needs_confirmed_expense(self):
        with self.assertRaises(ValueError):
            self.confirm('refund', 'refund', 'expense')
        self.confirm('expense')
        self.confirm('refund', 'refund', 'expense')
        self.assertEqual(self.store.snapshot()['unsynced'], 2)

    def test_cumulative_refund_limit(self):
        self.confirm('expense')
        self.confirm('refund', 'refund', 'expense')
        self.store.ingest(dataset([row('refund2', 8000, '退款', '2026-10-03 12:00:00')]))
        with self.assertRaises(ValueError):
            self.confirm('refund2', 'refund', 'expense')

    def test_no_self_link_or_future_refund(self):
        self.confirm('expense')
        with self.assertRaises(ValueError):
            self.confirm('expense', 'duplicate', 'expense', '重复')
        with self.assertRaises(ValueError):
            self.confirm('other', 'refund', 'refund')

    def test_duplicates_require_same_amount_and_note(self):
        self.confirm('expense')
        with self.assertRaises(ValueError):
            self.confirm('other', 'duplicate', 'expense', '重复')
        with self.assertRaises(ValueError):
            self.confirm('other', 'excluded')

    def test_dependent_original_cannot_be_changed(self):
        self.confirm('expense')
        self.confirm('refund', 'refund', 'expense')
        with self.assertRaises(ValueError):
            self.confirm('expense', 'income')

    def test_posted_cannot_reconfirm(self):
        self.store.set_state('expense', 'posted', 'test-id')
        with self.assertRaises(ValueError):
            self.confirm('expense')

    def test_crash_marks_uncertain(self):
        self.store.set_state('expense', 'sending')
        self.store.recover_interrupted()
        self.assertEqual(next(x for x in self.store.snapshot()['items'] if x['id']=='expense')['state'], 'uncertain')

    def test_ai_cannot_approve(self):
        self.store.apply_ai(self.data['dataset_id'], {'status': 'completed_with_gaps', 'dataset_id': self.data['dataset_id'],
            'review': {'findings': [{'source_ids': ['expense'], 'evidence': '<script>bad</script>', 'suggestion': 'check'}]}})
        self.assertEqual(self.store.snapshot()['pending'], 3)

    def test_ai_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            self.store.apply_ai(self.data['dataset_id'], {'review': {'findings': [{'source_ids': ['unknown']}]}})

    def test_publish_only_approved_and_refund_negative(self):
        self.confirm('expense')
        self.confirm('refund', 'refund', 'expense')
        ez = Ez(Path(self.temp.name), Path(self.temp.name) / 'ez')
        ez.backup = Mock()
        ez.account = Mock(return_value='a')
        ez.category = Mock(return_value='c')
        ez.request = Mock(side_effect=[{'id': '1'}, {'id': '2'}])
        self.assertEqual(ez.publish(self.store)['posted_now'], 2)
        self.assertEqual([c.args[1]['sourceAmount'] for c in ez.request.call_args_list], [10000, -2500])
        self.assertEqual(ez.publish(self.store)['posted_now'], 0)

    def test_timeout_no_blind_retry(self):
        self.confirm('expense')
        ez = Ez(Path(self.temp.name), Path(self.temp.name) / 'ez')
        ez.backup = Mock(); ez.account = Mock(return_value='a'); ez.category = Mock(return_value='c')
        ez.request = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            ez.publish(self.store)
        self.assertEqual(ez.publish(self.store)['posted_now'], 0)
        self.assertEqual(ez.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
