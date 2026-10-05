#!/usr/bin/env python3
"""Export a PostgreSQL snapshot, keep its transaction alive, and publish verified backups."""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import threading
import uuid

from ops_common import Ops, OpsError, atomic_bytes, atomic_json, strict_object, utc_now


class BackupError(OpsError):
    pass


ID = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{8}')
SNAPSHOT = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[1-9][0-9]*')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
FIELDS = {'format_version', 'backup_id', 'archive', 'archive_sha256', 'archive_bytes',
          'snapshot_id', 'snapshot_at', 'completed_at', 'schema_version',
          'postgres_version', 'context_count', 'source_count', 'application_images'}


def checksum(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def timestamp(value):
    if not isinstance(value, str): raise BackupError('Invalid backup time')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed
    except ValueError: raise BackupError('Invalid backup time') from None


def validate_manifest(document):
    try:
        if not isinstance(document, dict) or set(document) != FIELDS: raise ValueError()
        if type(document['format_version']) is not int or document['format_version'] != 1: raise ValueError()
        if not ID.fullmatch(document['backup_id']) or document['archive'] != 'database.dump': raise ValueError()
        if not re.fullmatch(r'[0-9a-f]{64}', document['archive_sha256']): raise ValueError()
        if type(document['archive_bytes']) is not int or document['archive_bytes'] < 6: raise ValueError()
        if not SNAPSHOT.fullmatch(document['snapshot_id']): raise ValueError()
        for name in ('context_count', 'source_count'):
            if type(document[name]) is not int or document[name] < 0: raise ValueError()
        if not re.fullmatch(r'[1-9][0-9]*', document['schema_version']): raise ValueError()
        if not re.fullmatch(r'[0-9.]+', document['postgres_version']): raise ValueError()
        images = document['application_images']
        if set(images) != {'api-a', 'api-b'} or any(not DIGEST.fullmatch(image) for image in images.values()): raise ValueError()
        if timestamp(document['completed_at']) < timestamp(document['snapshot_at']): raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise BackupError('Invalid backup manifest') from None
    return document


def validate_backup(directory):
    directory = pathlib.Path(directory)
    try:
        if directory.is_symlink() or not directory.is_dir(): raise BackupError('Backup directory is unavailable')
        manifest = directory / 'manifest.json'
        if manifest.is_symlink() or manifest.stat().st_size > 32768: raise BackupError('Invalid backup manifest')
        document = validate_manifest(json.loads(manifest.read_text(encoding='utf-8'), object_pairs_hook=strict_object))
        if directory.name != document['backup_id']: raise BackupError('Backup identity mismatch')
        archive = directory / 'database.dump'
        if archive.is_symlink() or archive.stat().st_size != document['archive_bytes']: raise BackupError('Backup archive size mismatch')
        with archive.open('rb') as source:
            if source.read(5) != b'PGDMP': raise BackupError('Invalid custom-format archive')
        if checksum(archive) != document['archive_sha256']: raise BackupError('Backup archive checksum mismatch')
        return document
    except (OSError, UnicodeError, ValueError, TypeError):
        raise BackupError('Backup is unavailable or invalid') from None


class BackupLock:
    """Fail on overlap; stale locks require explicit operator inspection."""
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.path = self.root / '.job.lock'

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise BackupError('Another backup is running or its lock needs inspection') from None
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(str(os.getpid()) + '\n')
        return self

    def __exit__(self, *args):
        self.path.unlink()


class BackupStore:
    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def prepare(self, backup_id):
        if not ID.fullmatch(backup_id): raise BackupError('Invalid backup identifier')
        candidate = self.root / (backup_id + '.partial')
        candidate.mkdir(mode=0o700)
        return candidate

    def publish(self, candidate, metadata):
        candidate = pathlib.Path(candidate)
        if candidate.is_symlink() or candidate.resolve().parent != self.root or not candidate.name.endswith('.partial'):
            raise BackupError('Invalid backup staging directory')
        archive = candidate / 'database.dump'
        try:
            if archive.is_symlink(): raise BackupError('Invalid backup archive')
            with archive.open('rb') as stream:
                if stream.read(5) != b'PGDMP': raise BackupError('Invalid custom-format archive')
            document = {'format_version': 1, 'backup_id': candidate.name.removesuffix('.partial'),
                        'archive': 'database.dump', 'archive_sha256': checksum(archive),
                        'archive_bytes': archive.stat().st_size, 'completed_at': utc_now(), **metadata}
            validate_manifest(document)
            atomic_json(candidate / 'manifest.json', document)
            destination = self.root / document['backup_id']
            if destination.exists(): raise BackupError('Backup destination already exists')
            os.rename(candidate, destination)
            validate_backup(destination)
            atomic_json(self.root / 'latest.json', {'format_version': 1, 'backup_id': document['backup_id'],
                                                   'manifest_sha256': checksum(destination / 'manifest.json')})
            return destination
        except OSError:
            raise BackupError('Backup publication failed; latest backup was preserved') from None

    def latest(self):
        try:
            reference = json.loads((self.root / 'latest.json').read_text(), object_pairs_hook=strict_object)
            if set(reference) != {'format_version', 'backup_id', 'manifest_sha256'} or type(reference['format_version']) is not int or reference['format_version'] != 1:
                raise ValueError()
            if not ID.fullmatch(reference['backup_id']): raise ValueError()
            directory = self.root / reference['backup_id']
            validate_backup(directory)
            if checksum(directory / 'manifest.json') != reference['manifest_sha256']: raise ValueError()
            return directory
        except (OSError, ValueError, TypeError, KeyError):
            raise BackupError('Latest backup is unavailable or invalid') from None

    def retain(self, hourly=24, daily=7):
        valid = []
        for directory in self.root.iterdir():
            if not ID.fullmatch(directory.name) or directory.is_symlink(): continue
            try:
                record = validate_backup(directory)
                valid.append((timestamp(record['snapshot_at']), directory))
            except BackupError:
                continue  # Preserve damaged evidence for operator diagnosis.
        valid.sort(reverse=True)
        protected = {directory for _, directory in valid[:hourly]}
        dates = set()
        for stamp, directory in valid:
            day = stamp.astimezone(dt.timezone.utc).date()
            if day not in dates and len(dates) < daily:
                protected.add(directory); dates.add(day)
        protected.add(self.latest())
        for _, directory in valid:
            if directory not in protected:
                if directory.is_symlink() or directory.resolve().parent != self.root: raise BackupError('Unsafe retention path')
                shutil.rmtree(directory)


def write_metrics(path, latest, success):
    stamp = 0
    if latest is not None:
        stamp = int(timestamp(validate_backup(latest)['snapshot_at']).timestamp())
    atomic_bytes(path, ('# TYPE contextfence_backup_last_attempt_success gauge\n'
                       'contextfence_backup_last_attempt_success ' + str(int(success)) + '\n'
                       '# TYPE contextfence_backup_snapshot_timestamp_seconds gauge\n'
                       'contextfence_backup_snapshot_timestamp_seconds ' + str(stamp) + '\n').encode())


class ExportedSnapshot:
    def __init__(self, ops):
        self.ops = ops
        self.process = None

    def __enter__(self):
        try:
            self.process = subprocess.Popen(self.ops.database_args('backup'), cwd=self.ops.root,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, bufsize=1)
            self.process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                "SELECT json_build_object('snapshot_id',pg_export_snapshot(),'snapshot_at',clock_timestamp(),"
                "'schema_version',(SELECT max(version) FROM flyway_schema_history WHERE success),"
                "'postgres_version',split_part(current_setting('server_version'),' ',1),'context_count',(SELECT count(*) FROM context_items),"
                "'source_count',(SELECT count(*) FROM source_state));\n")
            self.process.stdin.flush()
            response = queue.Queue()
            def receive():
                try: response.put(self.process.stdout.readline())
                except (OSError, ValueError): response.put('')
            threading.Thread(target=receive, daemon=True).start()
            line = response.get(timeout=30)
            document = json.loads(line, object_pairs_hook=strict_object)
            if not SNAPSHOT.fullmatch(document['snapshot_id']): raise ValueError()
            self.document = document
            return document
        except (OSError, ValueError, TypeError, KeyError, queue.Empty):
            self._finish(False)
            raise BackupError('Could not export a database snapshot') from None

    def _finish(self, commit):
        if self.process is None: return
        try:
            if self.process.poll() is None:
                self.process.stdin.write('COMMIT;\n' if commit else 'ROLLBACK;\n')
                self.process.stdin.close()
                self.process.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.process.kill(); self.process.wait(timeout=10)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None: stream.close()

    def __exit__(self, kind, value, trace):
        self._finish(kind is None)
        if kind is None and self.process.returncode != 0:
            raise BackupError('Snapshot transaction failed; backup was not published')


def run_backup(ops):
    store = BackupStore(ops.root / '.local' / 'backups')
    latest = None
    try:
        try: latest = store.latest()
        except BackupError: pass
        with BackupLock(store.root):
            backup_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            candidate = store.prepare(backup_id)
            images = ops.image_manifest()['services']
            remote = '/tmp/contextfence-' + uuid.uuid4().hex + '.dump'
            host = ops.database_args('backup')
            pg_host = next((argument[7:] for argument in host if argument.startswith('PGHOST=')), None)
            if pg_host is None: raise BackupError('Backup database host is unavailable')
            service = ops.database_service()
            try:
                with ExportedSnapshot(ops) as metadata:
                    ops.compose('exec', '-T', '-e', 'PGHOST=' + pg_host, service, 'sh', '/ops/backup.sh',
                                metadata['snapshot_id'], remote, timeout=900)
                    ops.compose('exec', '-T', service, 'pg_restore', '--list', remote, timeout=120)
                    ops.compose('cp', service + ':' + remote, str(candidate / 'database.dump'), timeout=900)
                    # The exporting transaction remains alive through successful dump and copy.
                metadata['application_images'] = {instance: image['image_digest'] for instance, image in images.items()}
                latest = store.publish(candidate, metadata)
                store.retain()
            finally:
                ops.compose('exec', '-T', service, 'rm', '-f', remote, check=False, timeout=10)
        write_metrics(ops.root / '.local' / 'metrics' / 'backup.prom', latest, success=True)
        return validate_backup(latest)
    except (OpsError, OSError, ValueError, TypeError, KeyError):
        write_metrics(ops.root / '.local' / 'metrics' / 'backup.prom', latest, success=False)
        raise BackupError('Backup failed; last verified backup remains identifiable') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument('--verify', type=pathlib.Path)
    args = parser.parse_args()
    try:
        result = validate_backup(args.verify) if args.verify else run_backup(Ops(args.root))
        print(json.dumps({'status': 'PASS', 'backup_id': result['backup_id'], 'snapshot_at': result['snapshot_at'],
                          'archive_sha256': result['archive_sha256']}, sort_keys=True))
        return 0
    except (OpsError, OSError, ValueError):
        print('FAIL: backup validation or execution failed; check private evidence and metrics')
        return 1


if __name__ == '__main__': raise SystemExit(main())
