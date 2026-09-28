import argparse
import json
from pathlib import Path
from .common import load, load_dataset, save_new, digest, file_hash
from .sources import prepare
from .rules import check, replay, truth_entries
from .review import build_page
from .ai import make_request, send
from .api_config import DEFAULT_CONFIG, load_api_config, check_setup


def main():
    parser = argparse.ArgumentParser(description='LedgerLens: local bookkeeping evaluation; no API sends without api-send --send')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--alipay', required=True); p.add_argument('--wechat', required=True)
    p.add_argument('--output', default='.local/datasets')
    for name in ('check', 'review-page', 'truth-check', 'api-prepare', 'replay'):
        p = commands.add_parser(name)
        p.add_argument('--dataset', required=True)
        p.add_argument('--output', required=True)
        if name in ('check', 'api-prepare'):
            p.add_argument('--candidate', required=True)
            p.add_argument('--truth')
        if name in ('review-page', 'truth-check'):
            p.add_argument('--truth', required=True)
        if name == 'api-prepare':
            p.add_argument('--config', default=str(DEFAULT_CONFIG))
        if name == 'replay':
            p.add_argument('--before', required=True); p.add_argument('--after', required=True)
            p.add_argument('--evidence', required=True)
    p = commands.add_parser('api-send')
    p.add_argument('--request', required=True); p.add_argument('--config', default=str(DEFAULT_CONFIG))
    p.add_argument('--output', required=True); p.add_argument('--send', action='store_true')
    p = commands.add_parser('api-check', help='Check local setup only; no network request or key output')
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    p = commands.add_parser('compare')
    p.add_argument('--reports', nargs='+', required=True); p.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'api-check':
        result = check_setup(args.config)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result['status'] == 'ready_for_connection_test' else 2)
    if args.command == 'prepare':
        folder, data, truth = prepare(args.alipay, args.wechat, args.output)
        build_page(data, truth, folder / 'review.html')
        print(json.dumps({'folder': str(folder.resolve()), 'dataset_id': data['dataset_id'],
                          'records': len(data['records']), 'human_confirmed': 0, 'api': 'not_called'}, ensure_ascii=False))
        return
    if args.command == 'api-send':
        result = send(load(args.request), load_api_config(args.config), args.output, authorized=args.send)
    elif args.command == 'compare':
        reports = [load(path) for path in args.reports]
        if len({r['dataset_id'] for r in reports}) != 1 or len({r.get('truth_hash') for r in reports}) != 1:
            raise ValueError('Cannot compare different datasets or different human truth versions')
        result = {'schema_version': 1, 'note': 'No weighted score or automatic winner; AI/replay and experience remain separate evidence.',
                  'reports': reports, 'selected_candidate': None}
        save_new(args.output, result)
    else:
        data = load_dataset(args.dataset)
        # Verify archived originals as well as the canonical JSON fingerprint.
        for source in data['sources']:
            original = Path(args.dataset).parent / 'originals' / source['filename']
            if file_hash(original) != source['sha256']:
                raise ValueError('Frozen original file changed')
        truth = load(args.truth) if getattr(args, 'truth', None) else None
        if args.command == 'review-page':
            truth_entries(data, truth)
            build_page(data, truth, args.output)
            print('Created local review page; no network request made')
            return
        if args.command == 'check':
            result = check(data, load(args.candidate), truth)
        elif args.command == 'truth-check':
            entries, confirmed = truth_entries(data, truth)
            result = {'status': 'complete' if entries is not None else 'pending', 'confirmed': confirmed,
                      'total': len(data['records']), 'truth_hash': digest(truth)}
        elif args.command == 'replay':
            result = replay(data, load(args.before), load(args.after), load(args.evidence))
        else:
            result = make_request(data, load(args.candidate), truth, load_api_config(args.config))
        save_new(args.output, result)
    print(json.dumps({'output': str(Path(args.output).resolve()), 'status': result.get('status', 'saved')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
