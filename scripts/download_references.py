#!/usr/bin/env python3
"""Download public reference documents/source archives without installing them.

The catalog is tracked; downloads and local manifests are ignored by Git.
HTTPS only, bounded reads, no retries, no archive extraction or execution.
"""
import argparse
import datetime
import hashlib
import http.client
import json
from pathlib import Path
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {'raw.githubusercontent.com', 'codeload.github.com', 'docs.kernel.org',
         'docs.nvidia.com', 'tier4.github.io'}
MAX_BYTES = 32 * 1024 * 1024


def checked_url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in HOSTS or
            parts.username is not None or parts.password is not None or parts.port not in (None, 443)):
        raise ValueError('reference URL must be credential-free HTTPS on an allowed public host')
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_catalog(value):
    if not isinstance(value, dict) or value.get('format_version') != 1 or not isinstance(value.get('sources'), list):
        raise ValueError('unsupported reference catalog')
    files = set()
    for row in value['sources']:
        if not isinstance(row, dict):
            raise ValueError('invalid reference entry')
        filename = row.get('file')
        if (not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}', filename)
                or filename in files or filename in ('manifest.json', 'INDEX.md', 'catalog.json')):
            raise ValueError('invalid or duplicate reference filename')
        files.add(filename)
        checked_url(row['url'])
        expected = row.get('sha256')
        if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('invalid expected sha256')
        if row.get('kind') not in ('text', 'html', 'source-archive') or not isinstance(row.get('title'), str):
            raise ValueError('invalid reference kind/title')
    if any(name + '.part' in files for name in files):
        raise ValueError('reference filenames collide with partial downloads')
    return value['sources']


def download(row, folder, opener):
    part = folder / (row['file'] + '.part')
    record = dict(row, status='failed', size_bytes=0, actual_sha256=None, error=None)
    try:
        deadline = time.monotonic() + 60
        with opener.open(row['url'], timeout=20) as response, part.open('xb') as output:
            checked_url(response.geturl())
            digest = hashlib.sha256()
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError('reference exceeded per-file time budget')
                block = response.read(65536)
                if not block:
                    break
                record['size_bytes'] += len(block)
                if record['size_bytes'] > MAX_BYTES:
                    raise ValueError('reference exceeds 32 MiB limit')
                digest.update(block)
                output.write(block)
        record['actual_sha256'] = digest.hexdigest()
        if not record['size_bytes']:
            raise ValueError('empty reference download')
        if row.get('sha256') is not None and row['sha256'] != record['actual_sha256']:
            raise ValueError('reference SHA256 mismatch; retained as .part')
        if row['kind'] in ('text', 'html'):
            content = part.read_bytes().decode('utf-8')
            if row['kind'] == 'html' and '<html' not in content.lower():
                raise ValueError('reference is not an HTML document')
            if row['kind'] == 'text' and '<html' in content[:1024].lower():
                raise ValueError('expected text but received HTML')
        elif not tarfile.is_tarfile(part):
            raise ValueError('reference is not a readable tar archive')
        part.rename(folder / row['file'])
        record['status'] = 'downloaded'
    except (OSError, ValueError, http.client.HTTPException) as error:
        record['error'] = type(error).__name__ + ': ' + str(error)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'configs/reference-sources.json')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    records = []
    owned = False
    status = 'running'
    try:
        catalog_raw = args.catalog.read_bytes()
        sources = validate_catalog(json.loads(catalog_raw))
        # Existing output is never reused, including after failed downloads.
        args.output.mkdir(parents=True, exist_ok=False)
        owned = True
        opener = urllib.request.build_opener(PublicRedirect())
        for row in sources:
            record = download(row, args.output, opener)
            records.append(record)
            print(record['status'] + ': ' + row['file'], flush=True)
        status = 'complete' if all(r['status'] == 'downloaded' for r in records) else 'failed'
        return 0 if status == 'complete' else 1
    except KeyboardInterrupt:
        status = 'interrupted'
        return 130
    except (OSError, ValueError, KeyError, TypeError) as error:
        status = 'failed'
        print('Reference download failed: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if owned:
            manifest = {'format_version': 1, 'status': status,
                        'retrieved_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'catalog_sha256': hashlib.sha256(catalog_raw).hexdigest(), 'records': records,
                        'limitations': ['Local SHA256 records bytes; not upstream signature verification.',
                                        'HTML is a page snapshot, not a complete offline website.',
                                        'Downloads are references; no installation or target validation.']}
            (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            (args.output / 'catalog.json').write_bytes(catalog_raw)
            lines = ['# 离线参考资料', '', '状态：' + status, '',
                     '文件未安装、未执行；HTML 可能依赖在线资源。源码归档保留其许可证。', '']
            for row in records:
                if row['status'] == 'downloaded':
                    lines.append('- [' + row['title'] + '](' + row['file'] + ')')
                else:
                    lines.append('- 下载失败：' + row['file'] + '；见 manifest.json')
            (args.output / 'INDEX.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
