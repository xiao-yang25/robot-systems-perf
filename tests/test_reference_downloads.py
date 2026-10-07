"""Public references are bounded, recorded, and never executable dependencies."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import download_references as tool


ROW = {'file': 'readme.md', 'url': 'https://raw.githubusercontent.com/example/repo/abc/README.md',
       'kind': 'text', 'title': 'Example', 'sha256': None}


class Response(io.BytesIO):
    def geturl(self):
        return ROW['url']


class ReferenceTests(unittest.TestCase):
    def test_catalog_rejects_paths_credentials_and_collisions(self):
        for field, value in [('file', '../x'), ('file', 'manifest.json'),
                             ('url', 'http://raw.githubusercontent.com/x'),
                             ('url', 'https://user:secret@raw.githubusercontent.com/x'),
                             ('url', 'https://example.com/x'), ('sha256', 'bad')]:
            row = dict(ROW, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                tool.validate_catalog({'format_version': 1, 'sources': [row]})
        for rows in ([ROW, ROW], [ROW, dict(ROW, file='readme.md.part')]):
            with self.assertRaises(ValueError):
                tool.validate_catalog({'format_version': 1, 'sources': rows})

    def test_hash_verified_text_saved_without_install(self):
        data = b'# example\n'
        row = dict(ROW, sha256=hashlib.sha256(data).hexdigest())
        opener = type('Opener', (), {'open': lambda _, *a, **kw: Response(data)})()
        with tempfile.TemporaryDirectory() as tmp:
            record = tool.download(row, Path(tmp), opener)
            self.assertEqual(record['status'], 'downloaded')
            self.assertEqual((Path(tmp) / row['file']).read_bytes(), data)
            self.assertEqual(record['actual_sha256'], row['sha256'])

    def test_hash_mismatch_empty_html_and_bad_archive_stay_failed(self):
        for row, data in [(dict(ROW, sha256='0' * 64), b'wrong'), (ROW, b''),
                          (ROW, b'<html>access denied</html>'),
                          (dict(ROW, kind='source-archive'), b'<html>not a tar</html>')]:
            opener = type('Opener', (), {'open': lambda _, *a, **kw: Response(data)})()
            with self.subTest(row=row, data=data), tempfile.TemporaryDirectory() as tmp:
                record = tool.download(row, Path(tmp), opener)
                self.assertEqual(record['status'], 'failed')
                self.assertFalse((Path(tmp) / row['file']).exists())

    def test_size_limit_preserves_partial_and_failure(self):
        opener = type('Opener', (), {'open': lambda _, *a, **kw: Response(b'12345')})()
        with tempfile.TemporaryDirectory() as tmp, patch.object(tool, 'MAX_BYTES', 4):
            result = tool.download(ROW, Path(tmp), opener)
            self.assertEqual(result['status'], 'failed')
            self.assertTrue((Path(tmp) / 'readme.md.part').exists())

    def test_redirect_to_unapproved_host_rejected(self):
        with self.assertRaises(ValueError):
            tool.PublicRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com/x')

    def test_existing_output_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            catalog = folder / 'catalog.json'
            catalog.write_text(json.dumps({'format_version': 1, 'sources': [ROW]}))
            output = folder / 'existing'
            output.mkdir()
            keep = output / 'keep'
            keep.write_bytes(b'original')
            self.assertEqual(tool.main(['--catalog', str(catalog), '--output', str(output)]), 1)
            self.assertEqual(list(output.iterdir()), [keep])
            self.assertEqual(keep.read_bytes(), b'original')

    def test_failed_request_manifest_nonzero_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            catalog = folder / 'catalog.json'
            catalog.write_text(json.dumps({'format_version': 1, 'sources': [ROW]}))
            output = folder / 'run'
            with patch.object(tool.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = OSError('offline')
                self.assertEqual(tool.main(['--catalog', str(catalog), '--output', str(output)]), 1)
                self.assertEqual(build.return_value.open.call_count, 1)
            manifest = json.loads((output / 'manifest.json').read_bytes())
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(manifest['records'][0]['status'], 'failed')
            self.assertIn('offline', manifest['records'][0]['error'])
            self.assertEqual((output / 'catalog.json').read_bytes(), catalog.read_bytes())

    def test_truncated_http_retains_partial_and_failed_manifest(self):
        # Exercise the standard library's actual chunk parser without a server.
        # One complete 64KiB read precedes a genuinely truncated next chunk.
        prefix = b'x' * 65536
        raw = (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10000\r\n' +
               prefix + b'\r\na\r\nabc')
        class Socket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(raw)
        response = http.client.HTTPResponse(Socket())
        response.begin()
        response.geturl = lambda: ROW['url']
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            catalog = folder / 'catalog.json'
            catalog.write_text(json.dumps({'format_version': 1, 'sources': [ROW]}))
            output = folder / 'run'
            with patch.object(tool.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = response
                self.assertEqual(tool.main(['--catalog', str(catalog), '--output', str(output)]), 1)
            manifest = json.loads((output / 'manifest.json').read_bytes())
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(manifest['records'][0]['status'], 'failed')
            self.assertIn('IncompleteRead', manifest['records'][0]['error'])
            self.assertEqual((output / 'readme.md.part').read_bytes(), prefix)
            self.assertFalse((output / 'readme.md').exists())


if __name__ == '__main__':
    unittest.main()
