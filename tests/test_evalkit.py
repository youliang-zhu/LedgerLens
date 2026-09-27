import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from evalkit.common import digest, save_new, load_dataset
from evalkit.sources import cents
from evalkit.rules import check, replay, truth_entries, totals, FIELDS
from evalkit.ai import make_request, validate_response, send
from evalkit.review import build_page


def fixture():
    sources = []
    for sid, amount, direction in [('s1', 10000, '支出'), ('s2', 2500, '收入'), ('s3', 2000, '支出')]:
        sources.append({'source_id': sid, 'platform': 'wechat', 'source_row': len(sources) + 19,
            'datetime': '2026-09-16 12:00:00', 'amount_cents': amount, 'direction': direction,
            'status': '支付成功', 'type': '商户消费', 'transaction_id': 'PRIVATE-ORDER-' + sid,
            'counterparty': 'PRIVATE-NAME', 'merchant_id': '', 'method': 'PRIVATE-CARD',
            'description': 'PRIVATE-DESCRIPTION', 'raw': {'备注': 'PRIVATE-REMARK'}})
    payload = {'schema_version': 1, 'records': sources, 'sources': []}
    data = {'dataset_id': digest(payload), **payload}
    entries = [
        {'entry_id': 'e1', 'source_ids': ['s1'], 'kind': 'expense', 'amount_cents': 10000},
        {'entry_id': 'e2', 'source_ids': ['s2'], 'kind': 'refund', 'amount_cents': 2500,
         'refund_allocations': [{'entry_id': 'e1', 'amount_cents': 2500}]},
        {'entry_id': 'e3', 'source_ids': ['s3'], 'kind': 'transfer', 'amount_cents': 2000}]
    candidate = {'schema_version': 1, 'dataset_id': data['dataset_id'], 'candidate_id': 'C01', 'phase': 'initial',
        'provenance': {'product': 'SYNTHETIC ONLY', 'version': 'test', 'adapter': 'fixture', 'mode': 'native',
                       'evidence': ['synthetic-unit-test-not-a-real-product']},
        'entries': entries, 'observations': [{k: r[k] for k in ('source_id', *FIELDS)} for r in sources],
        'reported_totals': {'expense': 10000, 'income': 0, 'refund': 2500, 'net_expense': 7500}}
    truth = {'schema_version': 1, 'dataset_id': data['dataset_id'], 'reviewer': 'synthetic-test',
             'decisions': [{'source_id': e['source_ids'][0], 'confirmed': True, 'kind': e['kind'],
                            'group_id': e['entry_id'], 'note': '', 'refund_allocations': [
                                {'group_id': a['entry_id'], 'amount_cents': a['amount_cents']}
                                for a in e.get('refund_allocations', [])]} for e in entries]}
    return data, candidate, truth


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.data, self.candidate, self.truth = fixture()

    def rules(self):
        return {r['rule']: r['status'] for r in check(self.data, self.candidate, self.truth)['rules']}

    def test_valid_accounting_not_full_acceptance(self):
        result = check(self.data, self.candidate, self.truth)
        self.assertEqual(self.rules()['human_truth'], 'pass')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['computed_totals']['net_expense'], 7500)
        self.assertEqual(result['ai'], 'not_tested')

    def test_missing_source(self):
        self.candidate['entries'].pop()
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_duplicate_source(self):
        extra = copy.deepcopy(self.candidate['entries'][2]); extra['entry_id'] = 'e4'
        self.candidate['entries'].append(extra)
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_unknown_source(self):
        self.candidate['entries'][0]['source_ids'] = ['NOT-REAL']
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_duplicate_entry_id(self):
        self.candidate['entries'][2]['entry_id'] = 'e1'
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_rounding_or_wrong_amount(self):
        for value in (10000.0, True, 10001, -10):
            self.candidate['entries'][0]['amount_cents'] = value
            self.assertEqual(self.rules()['structure'], 'fail')

    def test_partial_refund_is_not_netted_twice(self):
        self.candidate['entries'][0]['amount_cents'] = 7500
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_over_refund(self):
        self.candidate['entries'][1]['refund_allocations'][0]['amount_cents'] = 11000
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_refund_target_not_expense(self):
        self.candidate['entries'][1]['refund_allocations'][0]['entry_id'] = 'e3'
        self.assertEqual(self.rules()['structure'], 'fail')

    def test_refund_as_income(self):
        self.candidate['entries'][1]['kind'] = 'income'
        self.candidate['entries'][1]['refund_allocations'] = []
        self.assertEqual(self.rules()['human_truth'], 'fail')

    def test_transfer_as_expense(self):
        self.candidate['entries'][2]['kind'] = 'expense'
        self.assertEqual(self.rules()['human_truth'], 'fail')

    def test_wrong_parsed_status(self):
        self.candidate['observations'][0]['status'] = 'something-else'
        self.assertEqual(self.rules()['parsed_fields'], 'fail')

    def test_unavailable_parsed_field_is_not_pass(self):
        del self.candidate['observations'][0]['transaction_id']
        self.assertEqual(self.rules()['parsed_fields'], 'not_tested')

    def test_no_product_totals_is_not_pass(self):
        self.candidate.pop('reported_totals')
        self.assertEqual(self.rules()['product_totals'], 'not_tested')

    def test_all_pending_cannot_pass(self):
        for e in self.candidate['entries']:
            e.update(kind='pending', reason='Need human', refund_allocations=[])
        for d in self.truth['decisions']:
            d['confirmed'] = False
        self.assertEqual(self.rules()['human_truth'], 'pending')
        self.assertEqual(self.rules()['uncertainty'], 'pending')

    def test_truth_other_dataset(self):
        self.truth['dataset_id'] = 'other'
        self.assertEqual(self.rules()['human_truth'], 'fail')

    def test_truth_without_reviewer(self):
        self.truth['reviewer'] = ''
        self.assertEqual(self.rules()['human_truth'], 'fail')

    def test_same_amount_false_merge(self):
        self.data['records'][2]['amount_cents'] = 10000
        self.candidate['entries'][0]['source_ids'].append('s3')
        self.candidate['entries'].pop()
        self.assertEqual(self.rules()['structure'], 'pass')
        self.assertEqual(self.rules()['human_truth'], 'fail')

    def test_replay_requires_actual_evidence(self):
        self.assertEqual(replay(self.data, self.candidate, self.candidate, {'case': 'repeat'})['status'], 'fail')

    def evidence(self, after):
        return {'case': 'repeat', 'executed': True, 'before_hash': digest(self.candidate), 'after_hash': digest(after),
                'input_file_hashes': ['synthetic'], 'execution_log': 'synthetic only',
                'imported_source_ids': ['s1', 's2', 's3']}

    def test_replay_stable_and_changed(self):
        after = copy.deepcopy(self.candidate)
        self.assertEqual(replay(self.data, self.candidate, after, self.evidence(after))['status'], 'pass')
        after['entries'][2]['kind'] = 'expense'
        self.assertEqual(replay(self.data, self.candidate, after, self.evidence(after))['status'], 'fail')

    def test_cents(self):
        self.assertEqual(cents('¥2,604.00'), 260400)
        for value in ('NaN', '0.001', '-1', 'abc', 'Infinity'):
            with self.assertRaises(ValueError):
                cents(value)

    def test_no_overwrite_and_dataset_integrity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.json'; save_new(path, self.data)
            self.assertEqual(load_dataset(path)['dataset_id'], self.data['dataset_id'])
            with self.assertRaises(FileExistsError):
                save_new(path, {})
            bad = copy.deepcopy(self.data); bad['records'][0]['amount_cents'] = 1
            save_new(Path(folder) / 'bad.json', bad)
            with self.assertRaises(ValueError):
                load_dataset(Path(folder) / 'bad.json')


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.data, self.candidate, self.truth = fixture()
        self.config = {'protocol': 'responses', 'model': 'gpt-6-astra', 'endpoint': 'https://example.invalid/v1/responses',
                       'key_env': 'EVALKIT_TEST_KEY'}
        self.prepared = make_request(self.data, self.candidate, self.truth, self.config)
        self.review = {'checked_source_ids': ['s1', 's2', 's3'], 'findings': [], 'coverage_gaps': []}

    def test_allowlist_privacy(self):
        body = json.dumps(self.prepared['body'])
        for value in ('PRIVATE-NAME', 'PRIVATE-CARD', 'PRIVATE-ORDER', 'PRIVATE-DESCRIPTION', 'PRIVATE-REMARK', 'SYNTHETIC ONLY'):
            self.assertNotIn(value, body)

    def test_no_findings_is_not_correctness_pass(self):
        self.assertEqual(validate_response(self.review, self.prepared), 'completed_not_a_pass')

    def test_incomplete_coverage(self):
        self.review['checked_source_ids'].pop()
        self.assertEqual(validate_response(self.review, self.prepared), 'completed_with_gaps')

    def test_invented_ids_rejected(self):
        self.review['checked_source_ids'].append('made-up')
        with self.assertRaises(ValueError):
            validate_response(self.review, self.prepared)

    def test_bad_finding_rejected(self):
        self.review['findings'] = [{'type': 'refund', 'source_ids': ['made-up'], 'entry_ids': [],
                                   'evidence': 'x', 'suggestion': 'x', 'needs_human': True}]
        with self.assertRaises(ValueError):
            validate_response(self.review, self.prepared)

    def test_no_implicit_send(self):
        with self.assertRaises(ValueError):
            send(self.prepared, self.config, 'must-not-be-created')

    def test_endpoint_change_rejected(self):
        self.config['endpoint'] = 'https://another.invalid/v1/responses'
        with self.assertRaises(ValueError):
            send(self.prepared, self.config, 'must-not-be-created', authorized=True)

    def test_http_rejected(self):
        self.config['endpoint'] = 'http://example.invalid/'
        with self.assertRaises(ValueError):
            send(self.prepared, self.config, 'must-not-be-created', authorized=True)

    def test_mock_transport_not_real_api(self):
        response = {'status': 'completed', 'model': 'gpt-6-astra', 'usage': {'total_tokens': 1},
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(self.review)}]}]}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'EVALKIT_TEST_KEY': 'synthetic-secret'}):
            result = send(self.prepared, self.config, Path(folder) / 'run', True,
                          transport=lambda request: json.dumps(response).encode())
            self.assertEqual(result['status'], 'completed_not_a_pass')
            for path in (Path(folder) / 'run').glob('*.json'):
                self.assertNotIn('synthetic-secret', path.read_text(encoding='utf-8'))

    def test_malformed_api_response_not_pass(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'EVALKIT_TEST_KEY': 'synthetic-secret'}):
            result = send(self.prepared, self.config, Path(folder) / 'run', True, transport=lambda r: b'not-json')
            self.assertEqual(result['status'], 'incomplete')

    def test_html_escape(self):
        self.data['records'][0]['counterparty'] = '</script><script>EVIL</script>'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.html'; build_page(self.data, self.truth, path)
            html = path.read_text(encoding='utf-8')
            self.assertNotIn('</script><script>EVIL', html)
            self.assertIn("connect-src 'none'", html)


if __name__ == '__main__':
    unittest.main()
