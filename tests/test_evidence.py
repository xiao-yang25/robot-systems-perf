import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit.evidence import (DDS_ENV_KEYS, _hash_file, _libraries,
                              declared_dds_config, summarize_runtime_evidence)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.library = self.root / 'lib own.so.1'
        self.library.write_bytes(b'public synthetic library')
        self.prefix = self.root / 'publisher'

    def mapping(self, path=None, *, device=None, inode=None, deleted=False):
        path = path or self.library
        info = self.library.stat()
        major, minor = device or (os.major(info.st_dev), os.minor(info.st_dev))
        inode = info.st_ino if inode is None else inode
        return ('1000-2000 r-xp 00000000 %x:%x %d %s%s\n' %
                (major, minor, inode, path, ' (deleted)' if deleted else ''))

    def snapshots(self, maps=None, **fields):
        for phase in ('start', 'end'):
            metadata = {'schema_version': 1, 'pid': 123, 'role': 'publisher',
                        'phase': phase, 'clock': {'name': 'CLOCK_MONOTONIC', 'observed_ns': 0},
                        'actual_qos': {'available': True, 'depth': 0, 'history': 0,
                                       'durability': 0, 'reliability': 0},
                        'maps': {'available': True}}
            metadata.update(fields)
            Path(str(self.prefix) + '-' + phase + '.json').write_text(json.dumps(metadata))
            Path(str(self.prefix) + '-' + phase + '.maps').write_text(
                self.mapping() if maps is None else maps)

    def test_mapping_identity_hash_and_zero_metadata_preserved(self):
        self.snapshots(self.mapping() + self.mapping() +
                       '3000-4000 rw-p 00000000 00:00 0 [heap]\n')
        result = summarize_runtime_evidence(self.prefix)
        self.assertTrue(result['identity_matches'])
        self.assertFalse(result['transport_verified'])
        for snapshot in result['snapshots'].values():
            self.assertTrue(snapshot['available'])
            self.assertEqual(snapshot['metadata']['actual_qos']['depth'], 0)
            self.assertEqual(snapshot['metadata']['clock']['observed_ns'], 0)
            self.assertEqual(len(snapshot['libraries']), 1)
            library = snapshot['libraries'][0]
            self.assertEqual(len(library['mappings']), 2)
            self.assertEqual(library['sha256'], hashlib.sha256(self.library.read_bytes()).hexdigest())
            self.assertEqual(library['mapped_inode'], self.library.stat().st_ino)
            self.assertEqual(library['current_stat']['inode'], library['mapped_inode'])

    def test_replaced_path_device_and_inode_mismatch_never_hash(self):
        recorded = self.mapping()
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'replacement file must not count as loaded')
        replacement.replace(self.library)
        cases = [recorded, self.mapping(device=(0, 0)), self.mapping(inode=0)]
        for line in cases:
            with self.subTest(line=line), patch('perfkit.evidence.os.open') as opened:
                libraries, malformed = _libraries(line)
                self.assertFalse(malformed)
                self.assertFalse(libraries[0]['available'])
                self.assertIsNone(libraries[0]['sha256'])
                self.assertTrue(libraries[0]['reason'])
                opened.assert_not_called()

    def test_missing_deleted_and_escaped_files_never_claim_loaded_hash(self):
        cases = [(self.mapping(path=self.root / 'missing.so'), 'missing'),
                 (self.mapping(deleted=True), 'deleted'),
                 (self.mapping(path='/synthetic/lib\\012name.so'), 'unescaped')]
        for line, reason in cases:
            with self.subTest(reason=reason):
                libraries, malformed = _libraries(line)
                self.assertFalse(malformed)
                self.assertIsNone(libraries[0]['sha256'])
                self.assertIn(reason, libraries[0]['reason'])

    def test_malformed_snapshot_records_and_identity_are_explicit(self):
        self.snapshots('bad map\n' + self.mapping().replace('1000-2000', '2000-1000'))
        result = summarize_runtime_evidence(self.prefix)
        self.assertFalse(result['snapshots']['start']['available'])
        self.assertEqual(len(result['snapshots']['start']['malformed_maps']), 2)
        end = Path(str(self.prefix) + '-end.json')
        data = json.loads(end.read_text()); data['pid'] = 456
        end.write_text(json.dumps(data))
        self.assertFalse(summarize_runtime_evidence(self.prefix)['identity_matches'])
        data['pid'] = 0; end.write_text(json.dumps(data))
        self.assertIn('invalid', summarize_runtime_evidence(self.prefix)['snapshots']['end']['reason'])
        end.write_text('not json')
        self.assertIn('parse', summarize_runtime_evidence(self.prefix)['snapshots']['end']['reason'])
        end.unlink()
        self.assertIn('missing', summarize_runtime_evidence(self.prefix)['snapshots']['end']['reason'])

    def test_capture_failure_is_not_available(self):
        self.snapshots(maps='')
        path = Path(str(self.prefix) + '-start.json')
        data = json.loads(path.read_text()); data['maps'] = {'available': False, 'reason': 'denied'}
        path.write_text(json.dumps(data))
        self.assertFalse(summarize_runtime_evidence(self.prefix)['snapshots']['start']['available'])

    def test_phase_mapping_change_is_visible(self):
        self.snapshots()
        Path(str(self.prefix) + '-end.maps').write_text(self.mapping(inode=0))
        result = summarize_runtime_evidence(self.prefix)
        self.assertEqual(result['changes'][0]['reason'], 'mapping identity changed between phases')

    def test_hash_reads_chunks_and_invalidates_when_file_changes(self):
        self.library.write_bytes(b'x' * (2 * 1024 * 1024 + 17))
        original = hashlib.sha256
        reads = []
        library = self.library

        class Digest:
            def __init__(self):
                self.digest = original()

            def update(self, data):
                reads.append(len(data))
                self.digest.update(data)
                if len(reads) == 1:
                    with library.open('ab') as changed:
                        changed.write(b'changed during hashing')

            def hexdigest(self):
                return self.digest.hexdigest()

        with patch('perfkit.evidence.hashlib.sha256', Digest):
            result = _hash_file(self.library)
        self.assertFalse(result['available'])
        self.assertIsNone(result['sha256'])
        self.assertIn('during', result['reason'])
        self.assertGreater(len(reads), 2)
        self.assertLessEqual(max(reads), 1024 * 1024)

    def test_replacement_after_stat_or_hash_invalidates_digest(self):
        opened = os.open
        replacement = self.root / 'new-backing-file'
        replacement.write_bytes(b'new backing file')

        def replace_before_open(path, flags):
            replacement.replace(self.library)
            return opened(path, flags)

        with patch('perfkit.evidence.os.open', side_effect=replace_before_open):
            result = _hash_file(self.library)
        self.assertIsNone(result['sha256'])
        self.assertIn('before', result['reason'])

        real_stat = Path.stat
        calls = []
        replacement.write_bytes(b'next backing file')

        def replace_before_final_stat(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                replacement.replace(self.library)
            return real_stat(path, *args, **kwargs)

        with patch('perfkit.evidence.Path.stat', replace_before_final_stat):
            result = _hash_file(self.library)
        self.assertIsNone(result['sha256'])
        self.assertIn('during', result['reason'])

    def test_hash_rejects_directory_and_open_failure(self):
        self.assertIn('regular', _hash_file(self.root)['reason'])
        with patch('perfkit.evidence.os.open', side_effect=PermissionError('denied')):
            result = _hash_file(self.library)
        self.assertFalse(result['available'])
        self.assertIsNone(result['sha256'])
        self.assertIn('denied', result['reason'])

    def test_only_whitelisted_environment_keys_are_requested(self):
        class Environment:
            def get(self, key):
                if key not in DDS_ENV_KEYS:
                    raise AssertionError('unapproved environment access')
                return '0' if key == 'RMW_FASTRTPS_USE_QOS_FROM_XML' else None
        result = declared_dds_config(Environment())
        self.assertEqual(set(result['declarations']), set(DDS_ENV_KEYS))
        self.assertEqual(result['declarations']['RMW_FASTRTPS_USE_QOS_FROM_XML']['value'], '0')
        self.assertFalse(result['transport_verified'])
        self.assertTrue(result['declarations']['RMW_IMPLEMENTATION']['reason'])

    def test_local_declared_config_path_and_file_uri_are_hashed(self):
        config = self.root / 'config with space.xml'; config.write_bytes(b'<DDS/>')
        for value in (str(config), config.as_uri()):
            with self.subTest(value=value):
                entry = declared_dds_config({'CYCLONEDDS_URI': value})['declarations']['CYCLONEDDS_URI']
                self.assertTrue(entry['source']['available'])
                self.assertEqual(entry['source']['sha256'], hashlib.sha256(b'<DDS/>').hexdigest())
        result = declared_dds_config({'FASTDDS_DEFAULT_PROFILES_FILE': str(config)})
        self.assertTrue(result['declarations']['FASTDDS_DEFAULT_PROFILES_FILE']['source']['available'])
        missing = declared_dds_config({'FASTRTPS_DEFAULT_PROFILES_FILE': str(self.root / 'missing.xml')})
        self.assertIn('missing', missing['declarations']['FASTRTPS_DEFAULT_PROFILES_FILE']['source']['reason'])

    def test_remote_authority_query_and_list_are_not_read(self):
        for value in ('https://example.invalid/dds.xml', 'file://localhost/tmp/dds.xml',
                      'file:///tmp/dds.xml?query', 'file:///tmp/dds.xml#fragment',
                      'file:relative.xml', '//host/config.xml',
                      'file:///tmp/one.xml,file:///tmp/two.xml', '/tmp/a.xml,/tmp/b.xml',
                      'file:///tmp/%00.xml'):
            with self.subTest(value=value), patch('perfkit.evidence._hash_file') as hashed:
                source = declared_dds_config({'CYCLONEDDS_URI': value})['declarations']['CYCLONEDDS_URI']['source']
                self.assertFalse(source['available'])
                self.assertIsNone(source['sha256'])
                self.assertTrue(source['reason'])
                hashed.assert_not_called()

    def test_inline_xml_is_hash_only_and_empty_is_distinct(self):
        xml = '  <CycloneDDS><Domain id="public-fixture"/></CycloneDDS>'
        result = declared_dds_config({'CYCLONEDDS_URI': xml, 'RMW_IMPLEMENTATION': ''})
        for key in ('CYCLONEDDS_URI', 'FASTDDS_DEFAULT_PROFILES_FILE'):
            declaration = declared_dds_config({key: xml})['declarations'][key]
            self.assertIsNone(declaration['value'])
            self.assertNotIn('public-fixture', json.dumps(declaration))
        entry = result['declarations']['CYCLONEDDS_URI']
        self.assertIsNone(entry['value'])
        self.assertEqual(entry['source']['sha256'], hashlib.sha256(xml.encode()).hexdigest())
        self.assertNotIn(xml, json.dumps(result))
        self.assertNotIn('public-fixture', json.dumps(result))
        self.assertIn('empty', result['declarations']['RMW_IMPLEMENTATION']['reason'])


if __name__ == '__main__':
    unittest.main()
