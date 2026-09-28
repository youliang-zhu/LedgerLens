"""Parse source evidence only. No transaction classification, netting or merging."""
import csv
import io
import re
import shutil
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from openpyxl import load_workbook
from .common import digest, file_hash, save_new

PARSER_VERSION = 'sources-v1'
FIELDS = {
    'alipay': {'type': '交易分类', 'description': '商品说明', 'amount': '金额',
               'method': '收/付款方式', 'status': '交易状态', 'transaction_id': '交易订单号',
               'merchant_id': '商家订单号'},
    'wechat': {'type': '交易类型', 'description': '商品', 'amount': '金额(元)',
               'method': '支付方式', 'status': '当前状态', 'transaction_id': '交易单号',
               'merchant_id': '商户单号'},
}


def clean(value):
    return '' if value is None else str(value).strip().lstrip('\ufeff')


def cents(value):
    value = re.sub(r'[¥￥,\s]', '', clean(value))
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Invalid amount') from exc
    if not number.is_finite() or number < 0 or number * 100 != (number * 100).to_integral_value():
        raise ValueError('Amount must be non-negative with exact cents')
    return int(number * 100)


def read_rows(path, platform):
    if platform == 'alipay':
        raw = Path(path).read_bytes()
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                text = raw.decode(encoding)
                return list(csv.reader(io.StringIO(text))), {'encoding': encoding, 'sheet': None}
            except UnicodeDecodeError:
                continue
        raise ValueError('Unsupported CSV encoding')
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(wb.worksheets) != 1:
            raise ValueError('Unexpected multiple sheets; inspect rather than silently skip')
        ws = wb.worksheets[0]
        rows = []
        for row in ws.iter_rows():
            if any(cell.data_type == 'f' for cell in row):
                raise ValueError('Unexpected formula in bank statement')
            rows.append([cell.value for cell in row])
        return rows, {'encoding': 'xlsx', 'sheet': ws.title}
    finally:
        wb.close()


def parse(path, platform, *, experiment_scope=True):
    rows, fmt = read_rows(path, platform)
    indexes = [i for i, row in enumerate(rows) if '交易时间' in [clean(v) for v in row]]
    if len(indexes) != 1:
        raise ValueError('Expected exactly one transaction header')
    h = indexes[0]
    headers = [clean(v) for v in rows[h]]
    named = [v for v in headers if v]
    if len(set(named)) != len(named):
        raise ValueError('Duplicate column names')
    mapping = FIELDS[platform]
    required = {'交易时间', '交易对方', '收/支', *mapping.values()}
    if not required.issubset(headers):
        raise ValueError(f'{platform}: missing columns {sorted(required - set(headers))}')
    preamble = [' '.join(clean(v) for v in row if clean(v)) for row in rows[:h]]
    metadata = [v for v in preamble if re.match(r'^(起始时间|导出时间|导出交易类型|导出类型|共\d+笔|收入：|支出：|不计收支：|中性交易：)', v)]
    declared = [int(m[1]) for v in preamble if (m := re.search(r'共\s*(\d+)\s*笔', v))]
    if len(declared) != 1:
        raise ValueError('Missing or ambiguous declared transaction count')
    records = []
    for row_number, values in enumerate(rows[h + 1:], h + 2):
        if not any(clean(v) for v in values):
            continue
        if len(values) > len(headers) and any(clean(v) for v in values[len(headers):]):
            raise ValueError(f'Unexpected extra fields at row {row_number}')
        raw = dict(zip(headers, [clean(v) for v in values]))
        if any(not key and value for key, value in raw.items()):
            raise ValueError(f'Unnamed field contains data at row {row_number}')
        date = raw.get('交易时间', '')
        try:
            dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        except ValueError as exc:
            raise ValueError(f'Unparsed nonempty row {row_number}; refusing silent skip') from exc
        if experiment_scope and not datetime(2026, 8, 27) <= dt < datetime(2026, 9, 28):
            raise ValueError('Source date outside current experiment scope')
        tid = raw.get(mapping['transaction_id'], '')
        if not tid or re.search(r'\d[Ee][+-]?\d', tid):
            raise ValueError(f'Missing/damaged transaction ID at row {row_number}')
        if platform == 'wechat' and not isinstance(values[headers.index(mapping['transaction_id'])], str):
            raise ValueError('XLSX transaction IDs must be stored as text')
        record = {
            'source_id': ('A-' if platform == 'alipay' else 'W-') + digest([platform, tid])[:16],
            'platform': platform, 'source_row': row_number, 'sheet': fmt['sheet'],
            'datetime': date, 'amount_cents': cents(raw[mapping['amount']]),
            'direction': raw['收/支'], 'counterparty': raw['交易对方'],
            **{key: raw.get(value, '') for key, value in mapping.items() if key != 'amount'},
            'raw': {key: value for key, value in raw.items() if key},
        }
        records.append(record)
    if len(records) != declared[0]:
        raise ValueError('Parsed count differs from source declaration')
    ids = [r['source_id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Repeated source identity: must inspect before freezing')
    return records, {'platform': platform, 'filename': Path(path).name, 'sha256': file_hash(path),
                     'header_row': h + 1, 'count': len(records), 'metadata': metadata, **fmt}


def prepare(alipay, wechat, output_root):
    records, manifests = [], []
    paths = {'alipay': Path(alipay), 'wechat': Path(wechat)}
    for platform, path in paths.items():
        parsed, manifest = parse(path, platform)
        records.extend(parsed)
        manifests.append(manifest)
    payload = {'schema_version': 1, 'parser_version': PARSER_VERSION,
               'scope': {'start': '2026-08-27', 'end': '2026-09-27', 'bank_cross_source': 'not_tested'},
               'sources': manifests, 'records': records}
    data = {'dataset_id': digest(payload), **payload}
    folder = Path(output_root) / data['dataset_id'][:12]
    folder.mkdir(parents=True, exist_ok=False)
    raw_folder = folder / 'originals'
    raw_folder.mkdir()
    for manifest in manifests:
        src = paths[manifest['platform']]
        dest = raw_folder / manifest['filename']
        shutil.copy2(src, dest)
        if file_hash(dest) != manifest['sha256'] or file_hash(src) != manifest['sha256']:
            raise ValueError('Source changed while freezing; discard incomplete snapshot manually')
    save_new(folder / 'dataset.json', data)
    truth = {'schema_version': 1, 'dataset_id': data['dataset_id'], 'reviewer': '',
             'decisions': [{'source_id': r['source_id'], 'confirmed': False, 'kind': '',
                            'group_id': r['source_id'], 'refund_allocations': [], 'note': ''} for r in records]}
    save_new(folder / 'truth.blank.json', truth)
    # These are source-ID scenarios, NOT claims that a product has been tested.
    overlap = [r['source_id'] for r in records if r['datetime'] >= '2026-09-15']
    save_new(folder / 'scenarios.json', {'dataset_id': data['dataset_id'],
             'initial_ids': [r['source_id'] for r in records],
             'repeat_ids': [r['source_id'] for r in records], 'overlap_ids': overlap,
             'status': 'planned_not_executed',
             'note': 'Each candidate must actually reimport originals and the specified overlapping rows, then export evidence.'})
    return folder, data, truth
