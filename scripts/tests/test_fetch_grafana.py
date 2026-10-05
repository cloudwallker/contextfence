"""Grafana archive integrity is tested against a real local HTTP Range server."""
import hashlib
import http.server
import importlib
import pathlib
import re
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


class RangeServer:
    def __init__(self, data, behavior='valid'):
        self.data = data
        self.behavior = behavior
        self.requests = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                header = self.headers.get('Range', '')
                fixture.requests.append(header)
                match = re.fullmatch(r'bytes=(\d+)-(\d+)', header)
                if not match:
                    self.send_error(400)
                    return
                first, last = map(int, match.groups())
                chunk = fixture.data[first:last + 1]
                self.send_response(206 if fixture.behavior != 'no-range' else 200)
                range_first = first + 1 if fixture.behavior == 'wrong-range' else first
                self.send_header('Content-Range', f'bytes {range_first}-{last}/{len(fixture.data)}')
                self.send_header('Content-Length', str(len(chunk)))
                self.end_headers()
                if fixture.behavior == 'partial' or (fixture.behavior == 'partial-first' and len(fixture.requests) == 1):
                    chunk = chunk[:-1]
                self.wfile.write(chunk)

            def log_message(self, *args):
                pass

        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.http.server_port}/archive.tar.gz'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class FetchGrafanaTest(unittest.TestCase):
    DATA = bytes(range(251)) * 9 + b'final-range'

    def module(self):
        self.assertTrue((ROOT / 'scripts/fetch_grafana.py').exists(), 'Missing verified Grafana archive downloader')
        return importlib.import_module('fetch_grafana')

    def fetch(self, module, root, server, **kwargs):
        return module.ensure_archive(root, url=server.url,
                                     expected_sha256=hashlib.sha256(self.DATA).hexdigest(),
                                     expected_size=len(self.DATA), chunk_size=512,
                                     workers=3, attempts=1, timeout=2, **kwargs)

    def test_range_download_registers_only_complete_verified_archive(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA) as server:
            root = pathlib.Path(directory)
            archive = self.fetch(module, root, server)
            self.assertEqual(root / '.tools/grafana/grafana-12.2.0.verified.tar.gz', archive)
            self.assertEqual(self.DATA, archive.read_bytes())
            self.assertEqual({'bytes=0-511', 'bytes=512-1023', 'bytes=1024-1535',
                              'bytes=1536-2047', 'bytes=2048-2269'}, set(server.requests))
            self.assertEqual([archive], list(archive.parent.iterdir()))

    def test_valid_cache_is_reverified_without_any_network_request(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA) as server:
            root = pathlib.Path(directory)
            archive = root / '.tools/grafana/grafana-12.2.0.verified.tar.gz'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(self.DATA)
            self.assertEqual(archive, self.fetch(module, root, server))
            self.assertEqual([], server.requests)

    def test_corrupt_cache_is_replaced_only_after_new_download_passes_checksum(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA) as server:
            root = pathlib.Path(directory)
            archive = root / '.tools/grafana/grafana-12.2.0.verified.tar.gz'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'corrupt cache')
            self.fetch(module, root, server)
            self.assertEqual(self.DATA, archive.read_bytes())
            self.assertTrue(server.requests)

    def test_partial_wrong_range_or_ignored_range_never_becomes_verified_archive(self):
        module = self.module()
        from ops_common import OpsError
        for behavior in ('partial', 'wrong-range', 'no-range'):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA, behavior) as server:
                root = pathlib.Path(directory)
                with self.assertRaises(OpsError):
                    self.fetch(module, root, server)
                folder = root / '.tools/grafana'
                self.assertEqual([], list(folder.iterdir()))

    def test_checksum_failure_never_registers_archive_or_discloses_url_credentials(self):
        module = self.module()
        from ops_common import OpsError
        with tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA) as server:
            root = pathlib.Path(directory)
            with self.assertRaises(OpsError) as error:
                module.ensure_archive(root, url=server.url, expected_sha256='0' * 64,
                                      expected_size=len(self.DATA), chunk_size=512,
                                      workers=3, attempts=1, timeout=2)
            self.assertEqual([], list((root / '.tools/grafana').iterdir()))
            self.assertNotIn(server.url, str(error.exception))

    def test_import_does_not_trigger_archive_download(self):
        module = self.module()
        from unittest.mock import patch
        with patch('urllib.request.urlopen', side_effect=AssertionError('Import attempted network access')):
            importlib.reload(module)

    def test_transient_range_failure_is_retried_before_registering_verified_archive(self):
        module = self.module()
        # A first failed response must not cause the final verified registration
        # to include truncated bytes; subsequent real requests provide the range.
        with tempfile.TemporaryDirectory() as directory, RangeServer(self.DATA, 'partial-first') as server:
            archive = module.ensure_archive(directory, url=server.url,
                                            expected_sha256=hashlib.sha256(self.DATA).hexdigest(),
                                            expected_size=len(self.DATA), chunk_size=512,
                                            workers=1, attempts=2, timeout=2)
            self.assertEqual(self.DATA, archive.read_bytes())
            self.assertEqual(2, server.requests.count('bytes=0-511'))


if __name__ == '__main__':
    unittest.main()
