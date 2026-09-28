"""Explicit, independently logged API review. Nothing sends data during preparation."""
import json
import time
import urllib.request
import urllib.error
import re
from urllib.parse import urlsplit
from pathlib import Path
from .common import canonical, digest, now, save_new
from .rules import check
from .api_config import resolve_key, validate_config

PROMPT = Path(__file__).resolve().parent.parent / 'prompts' / 'reviewer-v1.txt'
TYPES = {'missing', 'duplicate', 'false_merge', 'refund', 'transfer', 'income', 'totals', 'uncertainty'}
SAFE_SOURCE_TYPES = {'商户消费', '微信红包', '微信红包（单发）', '微信红包（群发）',
    '扫二维码付款', '二维码收款', '群收款', '转账', '转账红包', '退款', '商户退款',
    '餐饮美食', '日用百货', '投资理财', '充值提现', '零钱充值', '零钱提现', '信用卡还款',
    '交通出行', '服饰装扮', '数码电器', '生活服务', '住房物业', '医疗健康', '文化休闲',
    '教育培训', '运动户外', '美容美发', '爱车养车', '酒店旅游', '亲友代付', '其他'}


def make_request(data, candidate, truth, config):
    validate_config(config)
    if config.get('protocol') != 'responses' or not config.get('model'):
        raise ValueError('Configure the explicit Responses-compatible protocol and selected model')
    if candidate.get('dataset_id') != data['dataset_id'] or (truth is not None and truth.get('dataset_id') != data['dataset_id']):
        raise ValueError('Dataset mismatch; do not send unrelated results')
    aliases = {}

    def alias(field, value):
        if not value or value == '/':
            return None
        key = (field, value)
        if key not in aliases:
            aliases[key] = field + '-' + str(1 + sum(k[0] == field for k in aliases))
        return aliases[key]

    records = []
    for r in data['records']:
        item = {k: r[k] for k in ('source_id', 'platform', 'source_row', 'datetime', 'amount_cents', 'direction', 'type', 'status')}
        # WeChat's type column may itself contain a merchant name, e.g.
        # "merchant-name-退款". A column allowlist alone is not anonymization.
        if item['type'] not in SAFE_SOURCE_TYPES:
            item['type'] = '商户退款' if item['type'].endswith('-退款') else alias('source_type', item['type'])
        for key in ('counterparty', 'transaction_id', 'merchant_id', 'method'):
            item[key] = alias(key, r.get(key))
        records.append(item)
    # Explicit relationship evidence survives replacement of transaction IDs.
    relationships = []
    for a in data['records']:
        for b in data['records']:
            if a['source_id'] != b['source_id'] and a['platform'] == b['platform']:
                if len(b.get('transaction_id', '')) >= 10 and b['transaction_id'] in a.get('transaction_id', ''):
                    relationships.append({'source_id': a['source_id'], 'contains_transaction_id_of': b['source_id'],
                                          'meaning': 'Text containment only; not confirmed refund linkage'})
    entry_alias = {e['entry_id']: 'E' + str(i + 1) for i, e in enumerate(candidate.get('entries', []))}
    entries = []
    for e in candidate.get('entries', []):
        entries.append({'entry_id': entry_alias[e['entry_id']], 'source_ids': e['source_ids'],
                        'kind': e['kind'], 'amount_cents': e['amount_cents'],
                        'refund_allocations': [{'entry_id': entry_alias.get(a['entry_id'], 'UNKNOWN'),
                                                'amount_cents': a['amount_cents']} for a in e.get('refund_allocations', [])]})
        # Native draft bookkeeping and review markers are different facts. Preserve
        # both without sending free-text labels, account names or product identity.
        if e.get('native_kind') in {'expense', 'income', 'refund', 'transfer', 'excluded', 'pending'}:
            entries[-1]['native_kind'] = e['native_kind']
        if e.get('native_review_state') in {'pending', 'not_marked'}:
            entries[-1]['native_review_state'] = e['native_review_state']
    # Notes, reviewer names, product brands and free-text candidate reasons stay local.
    decisions = []
    if truth:
        group_alias = {d['group_id']: 'G' + str(i + 1) for i, d in enumerate(truth['decisions'])}
        for d in truth['decisions']:
            decisions.append({'source_id': d['source_id'], 'confirmed': d.get('confirmed') is True,
                              'kind': d.get('kind'), 'group_id': group_alias[d['group_id']],
                              'refund_allocations': [{'group_id': group_alias.get(a['group_id'], 'UNKNOWN'),
                                                      'amount_cents': a['amount_cents']} for a in d.get('refund_allocations', [])]})
    report = check(data, candidate, truth)
    payload = {'schema_version': 1, 'task': 'Review this JSON evidence and return the required JSON object.',
               'dataset_id': data['dataset_id'], 'candidate': 'CANDIDATE',
               'records': records, 'identifier_relationships': relationships, 'entries': entries,
               'truth': decisions, 'rules': [{'rule': r['rule'], 'status': r['status']} for r in report['rules']],
               'computed_totals': report['computed_totals'], 'reported_totals': {
                   k: v for k, v in (candidate.get('reported_totals') or {}).items()
                   if k in {'expense', 'income', 'refund', 'net_expense'} and type(v) is int},
               'privacy_limits': ['No descriptions, remarks, names, raw account/card/order numbers or candidate brand',
                                  'Free-text reasons omitted; purpose inference therefore limited'],
               'scope_limits': ['No bank statement', 'Snapshot does not cover entire final day']}
    prompt = PROMPT.read_text(encoding='utf-8')
    body = {'model': config['model'], 'instructions': prompt,
            'input': canonical(payload), 'store': False,
            'max_output_tokens': config.get('max_output_tokens', 12000),
            'text': {'format': {'type': 'json_object'}}}
    return {'schema_version': 1, 'created_at': now(), 'dataset_id': data['dataset_id'],
            'candidate_hash': digest(candidate), 'truth_hash': digest(truth) if truth is not None else None,
            'prompt_version': 'reviewer-v1', 'prompt_hash': digest(prompt),
            'comparison_config_hash': digest({'model': body['model'], 'protocol': config['protocol'],
                'endpoint': config.get('endpoint'), 'max_output_tokens': body['max_output_tokens'], 'prompt': prompt}),
            'body': body, 'body_hash': digest(body), 'entry_aliases': entry_alias,
            'status': 'prepared_not_sent'}


def validate_response(value, prepared):
    payload = json.loads(prepared['body']['input'])
    sources = {r['source_id'] for r in payload['records']}
    entries = {e['entry_id'] for e in payload['entries']}
    if not isinstance(value, dict) or set(value) != {'checked_source_ids', 'findings', 'coverage_gaps'}:
        raise ValueError('Response keys do not match review schema')
    checked = value['checked_source_ids']
    if not isinstance(checked, list) or len(checked) != len(set(checked)) or not set(checked) <= sources:
        raise ValueError('Invalid checked_source_ids')
    if not isinstance(value['findings'], list) or not isinstance(value['coverage_gaps'], list):
        raise ValueError('findings and coverage_gaps must be lists')
    if any(not isinstance(s, str) or not s.strip() for s in value['coverage_gaps']):
        raise ValueError('Each coverage gap must have an explanation')
    for f in value['findings']:
        if set(f) != {'type', 'source_ids', 'entry_ids', 'evidence', 'suggestion', 'needs_human'}:
            raise ValueError('Finding schema mismatch')
        if f['type'] not in TYPES or f['needs_human'] is not True:
            raise ValueError('Findings must be a known type and go to human review')
        if not isinstance(f['source_ids'], list) or not f['source_ids'] or not set(f['source_ids']) <= sources:
            raise ValueError('Finding references unknown/missing source')
        if not set(f['source_ids']) <= set(checked):
            raise ValueError('Finding cites a source not listed as checked')
        if not isinstance(f['entry_ids'], list) or not set(f['entry_ids']) <= entries:
            raise ValueError('Finding references unknown result')
        if any(not isinstance(f[k], str) or not f[k].strip() for k in ('evidence', 'suggestion')):
            raise ValueError('Finding needs evidence and suggestion')
    return 'completed_with_gaps' if set(checked) != sources or value['coverage_gaps'] else 'completed_not_a_pass'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('API redirects prohibited: verify endpoint explicitly')


def send(prepared, config, output_folder, authorized=False, transport=None):
    if not authorized:
        raise ValueError('Explicit --send required; no network request made')
    endpoint = config.get('endpoint', '')
    parts = urlsplit(endpoint)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Configure an HTTPS endpoint without embedded credentials/query/fragment')
    if prepared.get('body_hash') != digest(prepared.get('body')):
        raise ValueError('Prepared body fingerprint mismatch')
    if config.get('model') != prepared['body'].get('model') or config.get('protocol') != 'responses':
        raise ValueError('Config differs from prepared request')
    expected = digest({'model': config['model'], 'protocol': config['protocol'], 'endpoint': endpoint,
                       'max_output_tokens': prepared['body']['max_output_tokens'], 'prompt': prepared['body']['instructions']})
    if expected != prepared['comparison_config_hash']:
        raise ValueError('Endpoint/config changed: prepare a new request for explicit inspection')
    secret, _ = resolve_key(config)
    folder = Path(output_folder)
    folder.mkdir(parents=True, exist_ok=False)
    save_new(folder / 'request.json', prepared)
    started = time.monotonic()
    result = {'schema_version': 1, 'status': 'incomplete', 'started_at': now(),
              'dataset_id': prepared['dataset_id'], 'candidate_hash': prepared['candidate_hash'],
              'body_hash': prepared['body_hash'], 'comparison_config_hash': prepared['comparison_config_hash'],
              'endpoint': endpoint, 'requested_model': config['model']}
    try:
        request = urllib.request.Request(endpoint, data=canonical(prepared['body']).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret}, method='POST')
        if transport is None:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=config.get('timeout_seconds', 180)) as response:
                raw = response.read(8 * 1024 * 1024)
        else:
            raw = transport(request)
        # Redact any accidental key echo before retaining provider output.
        raw_text = raw.decode('utf-8').replace(secret, '[REDACTED_KEY]')
        parsed = json.loads(raw_text)
        save_new(folder / 'response.json', parsed)
        result['returned_model'] = parsed.get('model')
        result['model_identity'] = 'matches' if parsed.get('model') == config['model'] else 'needs_provider_verification'
        result['usage'] = parsed.get('usage')
        if parsed.get('status') != 'completed':
            raise ValueError('API response not completed')
        texts = [c['text'] for o in parsed.get('output', []) if o.get('type') == 'message'
                 for c in o.get('content', []) if c.get('type') == 'output_text']
        review = json.loads(''.join(texts))
        result['status'] = validate_response(review, prepared)
        result['review'] = review
    except urllib.error.HTTPError as exc:
        result['error_type'] = type(exc).__name__
        result['http_status'] = exc.code
        result['error_note'] = 'Provider rejected request; not a passed AI check.'
        try:
            error = json.loads(exc.read(1024 * 1024)).get('error', {})
            # Never retain free-text messages: a provider may echo credentials or
            # financial inputs. Short machine identifiers suffice for diagnosis.
            for field in ('type', 'code', 'param'):
                value = error.get(field)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', value) and secret not in value:
                    result['provider_error_' + field] = value
        except Exception:
            pass
    except Exception as exc:
        # Do not log error bodies, URLs, credentials or provider-echoed input.
        result['error_type'] = type(exc).__name__
        result['error_note'] = 'Request or schema validation failed; not a passed AI check. Inspect local response if available.'
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    save_new(folder / 'result.json', result)
    return result
