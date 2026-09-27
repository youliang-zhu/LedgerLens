"""Deterministic checks. No guessed labels and no automatic transaction repair."""
from collections import Counter, defaultdict
from .common import assert_int, canonical, digest

KINDS = {'expense', 'income', 'refund', 'transfer', 'excluded', 'pending'}
FIELDS = ('datetime', 'amount_cents', 'direction', 'status', 'transaction_id')


def totals(entries):
    result = {k: 0 for k in KINDS}
    for e in entries:
        result[e['kind']] += e['amount_cents']
    result['net_expense'] = result['expense'] - result['refund']
    return result


def validate_entries(entries, sources):
    if not isinstance(entries, list):
        raise ValueError('entries must be a list')
    ids = [e['entry_id'] for e in entries]
    if any(not isinstance(v, str) or not v for v in ids) or len(ids) != len(set(ids)):
        raise ValueError('Missing or duplicate entry_id')
    used = []
    by_id = {e['entry_id']: e for e in entries}
    allocated = Counter()
    for e in entries:
        if e['kind'] not in KINDS:
            raise ValueError('Unknown entry kind')
        assert_int(e['amount_cents'], 'amount_cents')
        refs = e['source_ids']
        if not isinstance(refs, list) or not refs or any(s not in sources for s in refs):
            raise ValueError('Empty or unknown source reference')
        used.extend(refs)
        if e['amount_cents'] not in {sources[s]['amount_cents'] for s in refs}:
            raise ValueError('Entry amount differs from all linked source amounts; gross amounts required')
        if e['kind'] in {'excluded', 'pending'} and not e.get('reason', '').strip():
            raise ValueError('Excluded/pending entry requires an explanation')
        allocations = e.get('refund_allocations', [])
        if not isinstance(allocations, list):
            raise ValueError('refund_allocations must be a list')
        if e['kind'] != 'refund' and allocations:
            raise ValueError('Only refund entries may allocate a refund')
        target_ids = []
        for a in allocations:
            target = a['entry_id']
            target_ids.append(target)
            assert_int(a['amount_cents'], 'refund allocation')
            if a['amount_cents'] == 0 or target not in by_id or by_id[target]['kind'] != 'expense':
                raise ValueError('Refund target must be a known expense with positive allocation')
            allocated[target] += a['amount_cents']
        if len(target_ids) != len(set(target_ids)):
            raise ValueError('Repeated refund target')
        if e['kind'] == 'refund' and sum(a['amount_cents'] for a in allocations) != e['amount_cents']:
            raise ValueError('Refund allocation must equal refund amount; unresolved refund must be pending')
    if Counter(used) != Counter(sources.keys()):
        raise ValueError('Source coverage must be exactly once: missing, duplicate or unknown records')
    for target, amount in allocated.items():
        if amount > by_id[target]['amount_cents']:
            raise ValueError('Refunds exceed original gross expense')


def semantic(entries):
    """Compare economic records, ignoring arbitrary result IDs and ordering."""
    keys = {e['entry_id']: tuple(sorted(e['source_ids'])) for e in entries}
    return sorted((tuple(sorted(e['source_ids'])), e['kind'], e['amount_cents'],
                   tuple(sorted((keys[a['entry_id']], a['amount_cents'])
                                for a in e.get('refund_allocations', [])))) for e in entries)


def truth_entries(data, truth):
    sources = {r['source_id']: r for r in data['records']}
    if truth.get('dataset_id') != data['dataset_id']:
        raise ValueError('Truth belongs to another dataset')
    decisions = truth.get('decisions', [])
    if Counter(d['source_id'] for d in decisions) != Counter(sources.keys()):
        raise ValueError('Truth must contain every source exactly once')
    confirmed = [d for d in decisions if d.get('confirmed') is True]
    if len(confirmed) != len(sources):
        return None, len(confirmed)
    if not truth.get('reviewer', '').strip():
        raise ValueError('Completed truth requires a reviewer name')
    groups = defaultdict(list)
    for d in decisions:
        if not d.get('group_id') or d.get('kind') not in KINDS - {'pending'}:
            raise ValueError('Confirmed truth needs a group and a resolved kind')
        groups[d['group_id']].append(d)
    entries = []
    for group, ds in groups.items():
        signatures = {(d['kind'], sources[d['source_id']]['amount_cents'],
                       canonical(d.get('refund_allocations', []))) for d in ds}
        if len(signatures) != 1:
            raise ValueError('Members of one economic event must agree on amount, kind and refund targets')
        d = ds[0]
        allocations = [{'entry_id': a['group_id'], 'amount_cents': a['amount_cents']}
                       for a in d.get('refund_allocations', [])]
        entries.append({'entry_id': group, 'source_ids': [x['source_id'] for x in ds],
                        'kind': d['kind'], 'amount_cents': sources[d['source_id']]['amount_cents'],
                        'refund_allocations': allocations, 'reason': d.get('note', '')})
    validate_entries(entries, sources)
    return entries, len(confirmed)


def check(data, candidate, truth=None):
    rows = []
    sources = {r['source_id']: r for r in data['records']}

    def add(rule, status, detail):
        rows.append({'rule': rule, 'status': status, 'detail': detail})

    envelope_ok = candidate.get('dataset_id') == data['dataset_id'] and candidate.get('schema_version') == 1
    add('dataset', 'pass' if envelope_ok else 'fail', 'Candidate must reference the frozen dataset and schema v1')
    provenance = candidate.get('provenance', {})
    has_provenance = (candidate.get('phase') in {'initial', 'corrected'}
                      and bool(candidate.get('candidate_id'))
                      and all(provenance.get(k) for k in ('product', 'version', 'adapter', 'mode', 'evidence'))
                      and provenance.get('mode') in {'native', 'configured', 'combined'})
    add('provenance', 'pass' if has_provenance else 'not_tested',
        'Version, adapter, implementation mode and actual export evidence are required; evidence is manually audited')
    valid = False
    try:
        validate_entries(candidate.get('entries'), sources)
        valid = envelope_ok
        add('structure', 'pass', 'Full source coverage, integer cents, gross amounts and refund conservation')
    except (ValueError, KeyError, TypeError) as exc:
        add('structure', 'fail', str(exc))
    observations = candidate.get('observations', [])
    try:
        if Counter(o['source_id'] for o in observations) != Counter(sources.keys()):
            raise ValueError('No complete independently exported source observations')
        missing, differences = [], []
        for o in observations:
            source = sources[o['source_id']]
            for f in FIELDS:
                if f not in o or o[f] is None:
                    missing.append([o['source_id'], f])
                elif type(o[f]) is not type(source[f]) or o[f] != source[f]:
                    differences.append([o['source_id'], f])
        add('parsed_fields', 'fail' if differences else 'not_tested' if missing else 'pass',
            {'mismatches': differences, 'unavailable_fields': missing})
    except (ValueError, KeyError, TypeError) as exc:
        add('parsed_fields', 'not_tested', str(exc))
    actual = totals(candidate['entries']) if valid else None
    if valid:
        pending = [e['entry_id'] for e in candidate['entries'] if e['kind'] == 'pending']
        add('uncertainty', 'pending' if pending else 'pass', {'pending_entries': pending,
             'note': 'No pending labels alone does not prove purposes or associations are correct'})
        reported = candidate.get('reported_totals')
        keys = ('expense', 'income', 'refund', 'net_expense')
        if not isinstance(reported, dict) or any(k not in reported for k in keys):
            add('product_totals', 'not_tested', 'Product-exported gross expense, income, refund and net expense required')
        else:
            add('product_totals', 'pass' if all(type(reported[k]) is int and reported[k] == actual[k] for k in keys) else 'fail',
                'Compare product totals against independently recalculated entries; adapters must not fabricate totals')
    confirmed = 0
    try:
        expected, confirmed = truth_entries(data, truth) if truth is not None else (None, 0)
        if expected is None:
            add('human_truth', 'pending', {'confirmed': confirmed, 'total': len(sources)})
        elif valid:
            add('human_truth', 'pass' if semantic(expected) == semantic(candidate['entries']) else 'fail',
                {'expected_totals': totals(expected), 'actual_totals': actual,
                 'note': 'Per-event groups, classifications, amounts AND refund links must match; totals alone are insufficient'})
        else:
            add('human_truth', 'fail', 'Cannot compare malformed candidate')
    except (ValueError, KeyError, TypeError) as exc:
        add('human_truth', 'fail', 'Invalid human truth: ' + str(exc))
    add('bank_cross_source_dedup', 'not_tested', 'No bank statement in this experiment')
    add('repeat_import', 'not_tested', 'Use replay command with real before/after product exports')
    add('overlap_import', 'not_tested', 'Use replay command with real before/after product exports')
    status = 'fail' if any(r['status'] == 'fail' for r in rows) else 'incomplete'
    # This report alone can never certify the complete three-stage acceptance.
    return {'schema_version': 1, 'rules_version': 'rules-v1', 'dataset_id': data['dataset_id'],
            'candidate_id': candidate.get('candidate_id'), 'phase': candidate.get('phase'),
            'candidate_hash': digest(candidate), 'truth_hash': digest(truth) if truth is not None else None,
            'status': status, 'rules': rows, 'computed_totals': actual,
            'ai': 'not_tested', 'human_experience': 'not_tested'}


def replay(data, before, after, evidence):
    sources = {r['source_id']: r for r in data['records']}
    required = set(sources) if evidence.get('case') == 'repeat' else {
        s for s, r in sources.items() if r['datetime'] >= '2026-09-15'}
    try:
        if evidence.get('case') not in {'repeat', 'overlap'} or evidence.get('executed') is not True:
            raise ValueError('Real execution and valid case required')
        if (evidence.get('before_hash') != digest(before) or evidence.get('after_hash') != digest(after)
                or not evidence.get('input_file_hashes') or not evidence.get('execution_log')):
            raise ValueError('Missing/mismatched immutable execution evidence')
        if Counter(evidence.get('imported_source_ids', [])) != Counter(required):
            raise ValueError('Actual imported rows differ from frozen scenario')
        if before.get('candidate_id') != after.get('candidate_id'):
            raise ValueError('Different candidates')
        for c in (before, after):
            if c.get('dataset_id') != data['dataset_id']:
                raise ValueError('Dataset mismatch')
            validate_entries(c['entries'], sources)
        unchanged = semantic(before['entries']) == semantic(after['entries'])
        return {'status': 'pass' if unchanged else 'fail', 'case': evidence['case'],
                'before_hash': digest(before), 'after_hash': digest(after),
                'note': 'Idempotence only; does not establish accuracy of initial data. Execution evidence needs audit.'}
    except (ValueError, KeyError, TypeError) as exc:
        return {'status': 'fail', 'detail': str(exc)}
