"""Copy a verified existing experiment into the review queue, never approve it."""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evalkit.common import load, load_dataset, file_hash, save_new
from ledgerlens.store import Store


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dataset', required=True)
    p.add_argument('--root', required=True)
    p.add_argument('--historical-ai')
    args = p.parse_args()
    source = Path(args.dataset)
    data = load_dataset(source)
    review = load(args.historical_ai) if args.historical_ai else None
    if review and review.get('dataset_id') != data['dataset_id']:
        raise ValueError('Historical AI report does not match dataset')
    destination = Path(args.root) / 'originals' / data['dataset_id']
    originals = destination / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    for manifest in data['sources']:
        name = manifest['filename']
        if Path(name).name != name:
            raise ValueError('Invalid original filename')
        original = source.parent / 'originals' / name
        if file_hash(original) != manifest['sha256']:
            raise ValueError('Frozen original changed')
        target = originals / name
        if target.exists():
            if file_hash(target) != manifest['sha256']:
                raise ValueError('Destination original changed')
        else:
            shutil.copy2(original, target)
            if file_hash(target) != manifest['sha256']:
                raise ValueError('Copy verification failed')
    if not (destination / 'dataset.json').exists():
        save_new(destination / 'dataset.json', data)
    result = Store(Path(args.root) / 'review.db').ingest(data, review)
    print('Queue imported:', result['added'], 'new;', result['skipped'], 'already present; none approved')


if __name__ == '__main__':
    main()
