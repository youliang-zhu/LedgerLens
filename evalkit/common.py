import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_new(path, obj):
    """Never overwrite an earlier observation or a user's review."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def assert_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}: expected non-negative integer cents')


def load_dataset(path):
    data = load(path)
    payload = {k: v for k, v in data.items() if k != 'dataset_id'}
    if data.get('dataset_id') != digest(payload):
        raise ValueError('Dataset fingerprint mismatch; do not edit frozen data')
    return data
