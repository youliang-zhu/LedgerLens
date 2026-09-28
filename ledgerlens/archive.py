"""Read-only catalog of local source evidence, including pre-catalog imports."""
import re
from datetime import datetime, timezone
from pathlib import Path

from evalkit.common import load, file_hash


class Archive:
    def __init__(self, root, store):
        self.root = Path(root).resolve() / 'originals'
        self.store = store

    def folder(self, archive_id):
        if not isinstance(archive_id, str) or not re.fullmatch(r'[a-f0-9]{24}|[a-f0-9]{64}', archive_id):
            raise ValueError('未知账单存档')
        folder = (self.root / archive_id).resolve()
        if folder.parent != self.root or not folder.is_dir():
            raise ValueError('账单存档目录不存在或不安全')
        return folder

    def _json(self, path):
        if not path.exists():
            return {}
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('存档路径越界')
        value = load(path)
        if not isinstance(value, dict):
            raise ValueError('存档清单格式无效')
        return value

    def catalog(self):
        with self.store.db() as db:
            imported = {r['id'] for r in db.execute('SELECT id FROM batches')}
        result = []
        if not self.root.exists():
            return {'root': str(self.root), 'archives': result}
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or not re.fullmatch(r'[a-f0-9]{24}|[a-f0-9]{64}', candidate.name):
                continue
            try:
                folder = self.folder(candidate.name)
                upload = self._json(folder / 'upload.json')
                data = self._json(folder / 'dataset.json')
                outcome = self._json(folder / 'outcome.json')
                batch_id = data.get('dataset_id')
                status = 'imported' if batch_id in imported else 'failed' if outcome.get('status') == 'failed' else 'unfinished'
                files = []
                manifests = upload.get('files') or data.get('sources', [])
                if not manifests:
                    # An older interrupted upload may have no manifest at all.
                    manifests = [{'filename': p.name, 'platform': p.stem} for p in folder.iterdir()
                                 if p.suffix.lower() in ('.csv', '.xlsx')]
                for manifest in manifests:
                    name = manifest['filename']
                    if Path(name).name != name or '/' in name or '\\' in name:
                        raise ValueError('存档文件名无效')
                    path = folder / name
                    if not path.exists() and not upload:
                        path = folder / 'originals' / name  # Frozen experiment migration.
                    path = path.resolve()
                    if not path.is_relative_to(folder):
                        raise ValueError('原件路径越界')
                    exists = path.is_file()
                    expected = manifest.get('sha256')
                    integrity = ('ok' if file_hash(path) == expected else 'changed') if exists and expected else 'unverified' if exists else 'missing'
                    platform = manifest.get('platform')
                    dates = [r['datetime'][:10] for r in data.get('records', []) if r['platform'] == platform]
                    files.append({'name': manifest.get('original_name', name), 'platform': platform,
                                  'path': str(path), 'bytes': path.stat().st_size if exists else None,
                                  'integrity': integrity, 'sha256': expected,
                                  'count': len(dates) if data else None,
                                  'start': min(dates) if dates else None, 'end': max(dates) if dates else None})
                result.append({'id': folder.name, 'path': str(folder), 'status': status,
                               'saved_at': upload.get('saved_at') or datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc).isoformat(),
                               'time_estimated': not bool(upload.get('saved_at')), 'files': files,
                               'added': outcome.get('added'), 'skipped': outcome.get('skipped')})
            except (OSError, ValueError, KeyError, TypeError):
                # A damaged archive must not hide all the other statements.
                result.append({'id': candidate.name, 'status': 'damaged', 'files': [], 'saved_at': '',
                               'path': str(candidate), 'time_estimated': True})
        result.sort(key=lambda a: a['saved_at'], reverse=True)
        return {'root': str(self.root), 'archives': result}
