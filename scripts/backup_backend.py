"""Snapshot SQLite online; restore only into a new, empty directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sqlite_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst, pages=256, sleep=0.05)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup integrity check failed')


def secure_directory(path):
    if os.name == 'nt':
        user = subprocess.check_output(['whoami'], text=True).strip()
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', user + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True)


def create_backup(destination, include_session=False):
    destination = destination.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT):
        raise ValueError('Backup destination must be outside the repository')
    destination.mkdir(parents=True, exist_ok=True)
    secure_directory(destination)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    target = destination / ('neno-' + stamp)
    target.mkdir()
    files = []

    def copy(source, relative=None, database=False):
        relative = relative or source.relative_to(ROOT)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        if database:
            sqlite_copy(source, output)
        else:
            shutil.copy2(source, output)
        files.append({'path': relative.as_posix(), 'bytes': output.stat().st_size, 'sha256': digest(output)})

    try:
        # Freeze database references before copying media that those snapshots point to.
        databases = [ROOT / 'data/bot.db', ROOT / 'data/operations.db', *sorted((ROOT / 'data/world-trials').glob('*/world.db'))]
        for source in databases:
            if source.is_file():
                copy(source, database=True)
        for folder in ['app', 'telegram_gateway', 'scripts', 'docs', 'data/media', 'data/stickers/current']:
            base = ROOT / folder
            if base.exists():
                for source in base.rglob('*'):
                    if source.is_file() and not source.is_symlink() and '__pycache__' not in source.parts:
                        copy(source)
        for name in ['.env', '.env.example', 'requirements.txt', 'pyproject.toml', 'uv.lock',
                     'telegram_sidecar/build/Release/neno-tdlib-sidecar.exe', 'runtime/tdlib/tdjson.dll']:
            source = ROOT / name
            if source.is_file():
                copy(source)
        trial_root = ROOT / 'data/world-trials'
        for source in trial_root.glob('*/trial.json'):
            copy(source)
        for source in trial_root.glob('*/final.json'):
            copy(source)
        session_state = 'not_included'
        if include_session:
            # Refuse a torn TDLib copy; auth databases must be copied while its sidecar is stopped.
            if os.name == 'nt':
                processes = subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq neno-tdlib-sidecar.exe', '/FO', 'CSV', '/NH'], text=True, errors='replace')
                if 'neno-tdlib-sidecar.exe' in processes.lower():
                    raise RuntimeError('Stop the Telegram gateway and TDLib sidecar before a session snapshot')
            base = ROOT / 'data/telegram/tdlib/database'
            for source in base.rglob('*'):
                if source.is_file():
                    copy(source)
            session_state = 'cold_snapshot'
        manifest = {'application': 'Neno', 'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'files': files, 'tdlib_session': session_state, 'sqlite_snapshot': 'per-database online snapshot'}
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        verify(target)
        status = {'status': 'ok', 'created_at': manifest['created_at'], 'directory': str(target),
                  'files': len(files), 'bytes': sum(x['bytes'] for x in files), 'tdlib_session': session_state}
        (ROOT / 'data/backup_status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        # Only remove our verified, non-session snapshots beneath this explicit destination.
        eligible = []
        for old in destination.glob('neno-*'):
            marker = old / 'manifest.json'
            if old.is_dir() and marker.is_file() and old.resolve().parent == destination:
                meta = json.loads(marker.read_text(encoding='utf-8'))
                if meta.get('application') == 'Neno' and meta.get('tdlib_session') != 'cold_snapshot':
                    eligible.append(old)
        retain = max(1, int(os.getenv('NENO_BACKUP_RETENTION', '7')))
        for old in sorted(eligible, reverse=True)[retain:]:
            shutil.rmtree(old)
        return status
    except BaseException:
        (target / 'INCOMPLETE').write_text('Backup did not finish; do not restore.', encoding='utf-8')
        raise


def verify(snapshot):
    snapshot = snapshot.resolve()
    if (snapshot / 'INCOMPLETE').exists():
        raise ValueError('Incomplete backup')
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('application') != 'Neno' or manifest.get('version') != 1:
        raise ValueError('Unknown backup format')
    for item in manifest['files']:
        path = (snapshot / item['path']).resolve()
        if not path.is_relative_to(snapshot) or not path.is_file() or digest(path) != item['sha256']:
            raise ValueError('Backup file is missing, outside the snapshot, or has changed: ' + item['path'])
    return manifest


def restore(snapshot, destination):
    manifest = verify(snapshot)
    destination = destination.resolve()
    if destination == ROOT or destination.is_relative_to(snapshot.resolve()) or destination == destination.parent or (destination.exists() and any(destination.iterdir())):
        raise ValueError('Restore destination must be a new or empty directory, never the live repository')
    destination.mkdir(parents=True, exist_ok=True)
    secure_directory(destination)
    for item in manifest['files']:
        output = (destination / item['path']).resolve()
        if not output.is_relative_to(destination):
            raise ValueError('Invalid restoration path')
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / item['path'], output)
        if digest(output) != item['sha256']:
            raise RuntimeError('Restored content failed verification')
    return {'restored_to': str(destination), 'files': len(manifest['files'])}


if __name__ == '__main__':
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path(os.getenv('NENO_BACKUP_DIRECTORY', 'E:/NenoBackups')))
    parser.add_argument('--include-session', action='store_true')
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--restore', type=Path)
    parser.add_argument('--restore-to', type=Path)
    args = parser.parse_args()
    (ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    (ROOT / 'data').mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / 'logs/backup-backend.log', level=logging.INFO, encoding='utf-8')
    try:
        if args.restore:
            if not args.restore_to:
                parser.error('--restore requires --restore-to')
            result = restore(args.restore, args.restore_to)
        elif args.verify:
            result = {'verified_files': len(verify(args.verify)['files'])}
        else:
            result = create_backup(args.destination, args.include_session)
        logging.info('%s', json.dumps(result, ensure_ascii=False))
        if sys_stdout := getattr(__import__('sys'), 'stdout', None):
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        (ROOT / 'data/backup_status.json').write_text(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'checked_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
        logging.exception('Backup operation failed')
        raise
