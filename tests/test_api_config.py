import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from evalkit.api_config import load_api_config, resolve_key, check_setup
from evalkit.ai import make_request, send
from test_evalkit import fixture


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'api.local.json'
        self.env = self.root / '.env'
        self.config = {'protocol': 'responses', 'endpoint': 'https://example.invalid/v1/responses',
                       'model': 'gpt-6-astra', 'key_env': 'LEDGERLENS_TEST_KEY', 'key_file': '.env'}
        self.write_config()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start(); self.addCleanup(environment.stop)

    def write_config(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def test_relative_key_file_uses_config_directory(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=synthetic-file-secret\n', encoding='utf-8')
        cfg = load_api_config(self.path)
        self.assertEqual(resolve_key(cfg), ('synthetic-file-secret', 'local_file'))

    def test_environment_precedence(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=synthetic-file-secret', encoding='utf-8')
        with patch.dict(os.environ, {'LEDGERLENS_TEST_KEY': 'synthetic-env-secret'}):
            self.assertEqual(resolve_key(load_api_config(self.path)), ('synthetic-env-secret', 'environment'))

    def test_blank_key_not_ready(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=', encoding='utf-8')
        self.assertEqual(check_setup(self.path)['status'], 'not_ready')

    def test_missing_file_not_ready(self):
        self.assertEqual(check_setup(self.path)['status'], 'not_ready')

    def test_bom_comments_and_quotes(self):
        self.env.write_text('# comment\nexport LEDGERLENS_TEST_KEY="synthetic-secret"\n', encoding='utf-8-sig')
        self.assertEqual(resolve_key(load_api_config(self.path))[0], 'synthetic-secret')

    def test_duplicate_key_rejected_without_echo(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=synthetic-secret\nLEDGERLENS_TEST_KEY=another-secret', encoding='utf-8')
        report = check_setup(self.path)
        self.assertEqual(report['status'], 'not_ready')
        self.assertNotIn('synthetic-secret', json.dumps(report))

    def test_wrong_key_env_never_echoes_literal_key(self):
        self.config['key_env'] = 'sk-synthetic-not-a-real-key'
        self.write_config()
        report = check_setup(self.path)
        self.assertEqual(report['status'], 'not_ready')
        self.assertNotIn(self.config['key_env'], json.dumps(report))

    def test_placeholder_and_invalid_characters_rejected(self):
        for value in ('your_api_key_here', 'Bearer synthetic-secret', '请填写密钥', '"synthetic secret"'):
            self.env.write_text('LEDGERLENS_TEST_KEY=' + value, encoding='utf-8')
            self.assertEqual(check_setup(self.path)['status'], 'not_ready')

    def test_invalid_endpoint_and_limit(self):
        for endpoint in ('', 'http://example.invalid/responses', 'https://example.invalid/v1',
                         'https://user:secret@example.invalid/responses', 'https://example.invalid/responses?key=secret'):
            self.config['endpoint'] = endpoint; self.write_config()
            report = check_setup(self.path)
            self.assertEqual(report['status'], 'not_ready')
            self.assertNotIn('user:secret', json.dumps(report))
        self.config['endpoint'] = 'https://example.invalid/v1/responses'
        self.config['max_output_tokens'] = True; self.write_config()
        self.assertEqual(check_setup(self.path)['status'], 'not_ready')

    def test_ready_means_offline_only(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=synthetic-secret', encoding='utf-8')
        report = check_setup(self.path)
        self.assertEqual(report['status'], 'ready_for_connection_test')
        self.assertFalse(report['network_tested'])
        self.assertNotIn('synthetic-secret', json.dumps(report))

    def test_file_key_reaches_transport_but_not_logs(self):
        self.env.write_text('LEDGERLENS_TEST_KEY=synthetic-file-secret', encoding='utf-8')
        cfg = load_api_config(self.path)
        data, candidate, truth = fixture()
        prepared = make_request(data, candidate, truth, cfg)
        review = {'checked_source_ids': ['s1', 's2', 's3'], 'findings': [], 'coverage_gaps': []}

        def transport(request):
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-file-secret')
            return json.dumps({'status': 'completed', 'model': cfg['model'],
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(review)}]}]}).encode()

        result = send(prepared, cfg, self.root / 'run', True, transport=transport)
        self.assertEqual(result['status'], 'completed_not_a_pass')
        for path in (self.root / 'run').glob('*.json'):
            self.assertNotIn('synthetic-file-secret', path.read_text(encoding='utf-8'))
            self.assertNotIn(str(self.env), path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
