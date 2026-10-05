#!/usr/bin/env python3
"""Cache the pinned official Grafana archive only after full integrity verification."""
import argparse
import concurrent.futures
import hashlib
import http.client
import os
import pathlib
import re
import sys
import tempfile
import urllib.error
import urllib.request

from ops_common import OpsError

ARCHIVE_URL = 'https://dl.grafana.com/grafana/release/12.2.0/grafana_12.2.0_17949786146_linux_amd64.tar.gz'
ARCHIVE_SHA256 = 'c4f53551ed4887c792caeb9d02fa0c1a36e3db9ee8bdda32b1ced810cb135a93'
ARCHIVE_SIZE = 189052083
ARCHIVE_NAME = 'grafana-12.2.0.verified.tar.gz'


def _archive_matches(path, expected_sha256, expected_size):
    try:
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open('rb') as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False


def _fetch_range(url, first, last, total_size, timeout, attempts):
    for _ in range(attempts):
        try:
            request = urllib.request.Request(url, headers={
                'Range': f'bytes={first}-{last}', 'Accept-Encoding': 'identity',
                'User-Agent': 'ContextFence-Grafana-Archive/1',
            })
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 206 or response.headers.get('Content-Range') != f'bytes {first}-{last}/{total_size}':
                    raise OpsError('Grafana archive range verification failed')
                expected_length = last - first + 1
                declared = response.headers.get('Content-Length')
                if declared is not None and declared != str(expected_length):
                    raise OpsError('Grafana archive range length verification failed')
                chunk = response.read(expected_length + 1)
                if len(chunk) != expected_length:
                    raise OpsError('Grafana archive range is incomplete')
                return first, chunk
        except (OpsError, OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
            pass
    raise OpsError('Grafana archive download or range verification failed') from None


def ensure_archive(root, *, url=ARCHIVE_URL, expected_sha256=ARCHIVE_SHA256,
                   expected_size=ARCHIVE_SIZE, chunk_size=1024 * 1024,
                   workers=32, attempts=3, timeout=60):
    """Verify cached bytes or download validated ranges and atomically register the archive.

    Keyword overrides support controlled local HTTP fixtures; the CLI always uses
    the pinned official URL, byte count and checksum above.
    """
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or any(type(value) is not int or value < 1 for value in (expected_size, chunk_size, workers, attempts))
            or not isinstance(timeout, (int, float)) or timeout <= 0):
        raise OpsError('Grafana archive download configuration is invalid')
    folder = pathlib.Path(root).resolve() / '.tools' / 'grafana'
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / ARCHIVE_NAME
    if _archive_matches(archive, expected_sha256, expected_size):
        return archive
    # A failed verification must never leave corrupt bytes under the verified name.
    if archive.exists():
        archive.unlink()
    descriptor, temporary_name = tempfile.mkstemp(prefix='.grafana-download-', dir=str(folder))
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w+b') as file:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_fetch_range, url, first, min(first + chunk_size - 1, expected_size - 1),
                                       expected_size, timeout, attempts)
                           for first in range(0, expected_size, chunk_size)]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        first, chunk = future.result()
                        file.seek(first)
                        file.write(chunk)
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
            file.flush()
            os.fsync(file.fileno())
        if not _archive_matches(temporary, expected_sha256, expected_size):
            raise OpsError('Grafana archive checksum verification failed')
        os.replace(temporary, archive)
        return archive
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    try:
        ensure_archive(args.root)
        print('PASS: official Grafana archive size and SHA-256 verified; cache is ready.')
        return 0
    except Exception:
        print('FAIL: Grafana archive could not be verified; incomplete downloads were not registered.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
