import base64
import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from evalkit.common import save_new, file_hash
from ledgerlens.app import App, handler
from ledgerlens.archive import Archive
from ledgerlens.ez import local_open


def bill(amount='12.34'):
    text = io.StringIO(newline='')
    writer = csv.writer(text)
    writer.writerow(['共1笔'])
    writer.writerow(['交易时间','交易对方','收/支','交易分类','商品说明','金额','收/付款方式','交易状态','交易订单号','商家订单号'])
    writer.writerow(['2026-10-01 12:00:00','合成商户','支出','餐饮美食','测试',amount,'余额','交易成功','TEST-ORDER-1','TEST-MERCHANT-1'])
    return text.getvalue().encode('utf-8')


def upload_file(raw=None, name='支付宝原始账单.csv', platform='alipay'):
    return {'platform': platform, 'name': name,
            'content': base64.b64encode(bill() if raw is None else raw).decode()}


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(self.root, self.root / 'unused')

    def tearDown(self):
        self.temp.cleanup()

    def test_upload_original_bytes_name_dates_and_reopen(self):
        self.app.upload([upload_file()])
        a = self.app.archive.catalog()['archives'][0]
        self.assertEqual(a['status'], 'imported')
        self.assertEqual(a['added'], 1)
        f = a['files'][0]
        self.assertEqual(f['name'], '支付宝原始账单.csv')
        self.assertEqual(f['integrity'], 'ok')
        self.assertEqual(f['count'], 1)
        self.assertEqual((f['start'], f['end']), ('2026-10-01', '2026-10-01'))
        self.assertEqual(Path(f['path']).read_bytes(), bill())
        self.assertEqual(Archive(self.root, self.app.store).catalog(), self.app.archive.catalog())

    def test_repeat_is_separate_archive_not_duplicate_transaction(self):
        self.app.upload([upload_file()])
        self.app.upload([upload_file()])
        archives = self.app.archive.catalog()['archives']
        self.assertEqual(len(archives), 2)
        self.assertNotEqual(archives[0]['path'], archives[1]['path'])
        self.assertEqual(sorted(a['added'] for a in archives), [0, 1])
        self.assertEqual(len(self.app.store.snapshot()['items']), 1)

    def test_both_originals_saved_before_first_parse_failure(self):
        files = [upload_file(b'bad-csv'), upload_file(b'bad-xlsx', '微信原件.xlsx', 'wechat')]
        with self.assertRaises(ValueError):
            self.app.upload(files)
        a = self.app.archive.catalog()['archives'][0]
        self.assertEqual(a['status'], 'failed')
        self.assertEqual([Path(f['path']).read_bytes() for f in a['files']], [b'bad-csv', b'bad-xlsx'])
        self.assertTrue(all(f['integrity'] == 'ok' for f in a['files']))
        self.assertEqual(self.app.store.snapshot()['items'], [])

    def test_conflicting_reimport_keeps_both_originals(self):
        self.app.upload([upload_file()])
        with self.assertRaises(ValueError):
            self.app.upload([upload_file(bill('99.99'))])
        archives = self.app.archive.catalog()['archives']
        self.assertEqual({a['status'] for a in archives}, {'imported', 'failed'})
        self.assertTrue(all(a['files'][0]['integrity'] == 'ok' for a in archives))

    def test_legacy_frozen_layout_is_visible_without_migration(self):
        self.app.upload([upload_file()])
        with self.app.store.db() as db:
            data = json.loads(db.execute('SELECT data FROM batches').fetchone()[0])
        folder = self.root / 'originals' / data['dataset_id'] / 'originals'
        folder.mkdir(parents=True)
        (folder / 'alipay.csv').write_bytes(bill())
        save_new(folder.parent / 'dataset.json', data)
        archives = self.app.archive.catalog()['archives']
        legacy = next(a for a in archives if a['id'] == data['dataset_id'])
        self.assertEqual(legacy['status'], 'imported')
        self.assertTrue(legacy['time_estimated'])
        self.assertEqual(legacy['files'][0]['integrity'], 'ok')

    def test_missing_and_modified_originals_reported(self):
        self.app.upload([upload_file()])
        a = self.app.archive.catalog()['archives'][0]
        original = Path(a['files'][0]['path'])
        original.write_bytes(b'changed')
        self.assertEqual(self.app.archive.catalog()['archives'][0]['files'][0]['integrity'], 'changed')
        original.unlink()
        self.assertEqual(self.app.archive.catalog()['archives'][0]['files'][0]['integrity'], 'missing')

    def test_small_flows_and_completed_review_do_not_remove_evidence(self):
        self.app.upload([upload_file(bill('0.99'))])
        self.assertEqual(self.app.store.snapshot()['items'], [])
        a = self.app.archive.catalog()['archives'][0]
        self.assertEqual(a['files'][0]['count'], 1)
        self.assertEqual(a['files'][0]['integrity'], 'ok')
        self.assertEqual(Path(a['files'][0]['path']).read_bytes(), bill('0.99'))

    def test_invalid_id_and_manifest_paths_rejected(self):
        for invalid in ('../', '.', 'C:/Users', '', None):
            with self.assertRaises(ValueError):
                self.app.archive.folder(invalid)
        folder = self.root / 'originals' / ('a' * 24)
        folder.mkdir(parents=True)
        save_new(folder / 'upload.json', {'files': [{'filename': '../review.db'}]})
        self.assertEqual(self.app.archive.catalog()['archives'][0]['status'], 'damaged')

    def test_bad_manifest_does_not_hide_healthy_archives(self):
        self.app.upload([upload_file()])
        folder = self.root / 'originals' / ('b' * 24)
        folder.mkdir()
        (folder / 'dataset.json').write_text('{bad', encoding='utf-8')
        self.assertEqual({a['status'] for a in self.app.archive.catalog()['archives']}, {'damaged', 'imported'})

    def test_http_auth_and_open_folder_boundary(self):
        self.app.upload([upload_file()])
        a = self.app.archive.catalog()['archives'][0]
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.app.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        headers = {'Cookie': 'll_session=' + self.app.cookie, 'X-LedgerLens': '1', 'Content-Type': 'application/json'}
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                local_open(base + '/ll/archives')
            self.assertEqual(error.exception.code, 401)
            with local_open(urllib.request.Request(base + '/ll/archives', headers=headers)) as r:
                self.assertEqual(json.load(r)['archives'][0]['files'][0]['integrity'], 'ok')
            with patch('ledgerlens.app.os.startfile', create=True) as opener:
                with local_open(urllib.request.Request(base + '/ll/archive/open', json.dumps({'id': a['id']}).encode(), headers)) as r:
                    self.assertTrue(json.load(r)['opened'])
                opener.assert_called_once_with(a['path'])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    local_open(urllib.request.Request(base + '/ll/archive/open', b'{"id":"../"}', headers))
                self.assertEqual(error.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    local_open(urllib.request.Request(base + '/ll/archive/open', json.dumps({'id': a['id']}).encode(), {**headers, 'Origin': 'https://evil.invalid'}))
                self.assertEqual(error.exception.code, 403)
                opener.assert_called_once()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
