"""Offline evidence from benchmark-owned snapshots; never inspect another process.

Hashes identify current backing files with the recorded mapping identity, not the
memory bytes loaded at snapshot time. DDS configuration is a declaration only.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit


DDS_ENV_KEYS = (
    'RMW_IMPLEMENTATION', 'CYCLONEDDS_URI',
    'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE',
    'RMW_FASTRTPS_USE_QOS_FROM_XML', 'RMW_FASTRTPS_PUBLICATION_MODE',
)
_MAP = re.compile(r'^([0-9a-fA-F]+)-([0-9a-fA-F]+)\s+([r-][w-][x-][ps])\s+'
                  r'([0-9a-fA-F]+)\s+([0-9a-fA-F]+):([0-9a-fA-F]+)\s+(\d+)'
                  r'(?:\s+(.*))?$')
_SHARED = re.compile(r'\.so(?:\.[^/]+)?$')
_CHUNK = 1024 * 1024


def _identity(info):
    return {'device_major': os.major(info.st_dev), 'device_minor': os.minor(info.st_dev),
            'inode': info.st_ino, 'size_bytes': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def _stable_fields(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_file(path, mapped=None):
    """Hash only a regular file, checking open-file and pathname identities."""
    result = {'available': False, 'sha256': None, 'current_stat': None, 'reason': None}
    try:
        before = Path(path).stat()
        result['current_stat'] = _identity(before)
        if not stat.S_ISREG(before.st_mode):
            result['reason'] = 'current path is not a regular file'
            return result
        if mapped is not None and (
                os.major(before.st_dev), os.minor(before.st_dev), before.st_ino) != mapped:
            result['reason'] = 'current file device/inode differs from recorded mapping'
            return result
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if _stable_fields(opened) != _stable_fields(before):
                result['reason'] = 'file changed before hashing'
                return result
            digest = hashlib.sha256()
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(source.fileno())
        current = Path(path).stat()
        if (_stable_fields(after) != _stable_fields(before) or
                _stable_fields(current) != _stable_fields(before)):
            result['reason'] = 'file changed during hashing'
            return result
        result.update(available=True, sha256=digest.hexdigest())
    except FileNotFoundError:
        result['reason'] = 'current file missing'
    except (OSError, ValueError) as error:
        result['reason'] = 'file read/stat failed: ' + str(error)
    return result


def _libraries(text):
    libraries, malformed = {}, []
    for number, line in enumerate(text.splitlines(), 1):
        match = _MAP.fullmatch(line)
        if not match:
            malformed.append({'line': number, 'reason': 'malformed maps record'})
            continue
        start, end, permissions, offset, major, minor, inode, path = match.groups()
        if int(end, 16) <= int(start, 16):
            malformed.append({'line': number, 'reason': 'invalid mapping address range'})
            continue
        path = path or ''
        deleted = path.endswith(' (deleted)')
        if deleted:
            path = path[:-10]
        if not _SHARED.search(path):
            continue
        device = (int(major, 16), int(minor, 16), int(inode))
        key = (path, device, deleted)
        if key not in libraries:
            record = {'path': path, 'mapped_device_major': device[0],
                      'mapped_device_minor': device[1], 'mapped_inode': device[2],
                      'deleted': deleted, 'mappings': []}
            if deleted:
                record.update(available=False, sha256=None, current_stat=None,
                              reason='mapping marked deleted; current pathname cannot identify loaded file')
            elif not path.startswith('/') or '\\' in path:
                record.update(available=False, sha256=None, current_stat=None,
                              reason='mapping path is not an unescaped absolute local path')
            elif not device[2]:
                record.update(available=False, sha256=None, current_stat=None,
                              reason='mapping has zero inode; file identity unavailable')
            else:
                record.update(_hash_file(path, device))
            libraries[key] = record
        libraries[key]['mappings'].append({'start': start, 'end': end,
                                          'permissions': permissions, 'offset': offset})
    return list(libraries.values()), malformed


def _snapshot(prefix, phase):
    metadata_path, maps_path = str(prefix) + '-' + phase + '.json', str(prefix) + '-' + phase + '.maps'
    result = {'available': False, 'reason': None, 'metadata_path': metadata_path,
              'maps_path': maps_path, 'metadata': None, 'libraries': [], 'malformed_maps': []}
    try:
        metadata = json.loads(Path(metadata_path).read_text())
        if (not isinstance(metadata, dict) or metadata.get('schema_version') != 1 or
                metadata.get('phase') != phase or metadata.get('role') not in ('publisher', 'subscriber') or
                type(metadata.get('pid')) is not int or metadata['pid'] <= 0):
            result['reason'] = 'invalid snapshot metadata identity/schema'
            return result
        result['metadata'] = metadata
        maps_status = metadata.get('maps')
        if not isinstance(maps_status, dict) or maps_status.get('available') is not True:
            result['reason'] = 'snapshot did not confirm maps capture'
            return result
        maps_text = Path(maps_path).read_text()
        if not maps_text.strip():
            result['reason'] = 'snapshot maps file empty'
            return result
        libraries, malformed = _libraries(maps_text)
        result.update(libraries=libraries, malformed_maps=malformed,
                      available=not malformed,
                      reason='malformed maps records' if malformed else None)
    except FileNotFoundError:
        result['reason'] = 'snapshot file missing'
    except (OSError, UnicodeError, ValueError) as error:
        result['reason'] = 'snapshot read/parse failed: ' + str(error)
    return result


def summarize_runtime_evidence(prefix):
    """Return start/end metadata and identity-checked library hashes for PREFIX.

    Call in the same filesystem namespace as the benchmark after it stops. This
    reads only PREFIX-{start,end}.{json,maps} and library paths in those maps.
    """
    snapshots = {phase: _snapshot(prefix, phase) for phase in ('start', 'end')}
    start, end = snapshots['start']['metadata'], snapshots['end']['metadata']
    identity_matches = bool(start and end and
                            (start['pid'], start['role']) == (end['pid'], end['role']))
    changes = []
    initial = {item['path']: item for item in snapshots['start']['libraries']}
    for item in snapshots['end']['libraries']:
        previous = initial.get(item['path'])
        if previous:
            fields = ('mapped_device_major', 'mapped_device_minor', 'mapped_inode', 'deleted')
            if any(previous[key] != item[key] for key in fields):
                changes.append({'path': item['path'], 'reason': 'mapping identity changed between phases'})
            elif previous['sha256'] and item['sha256'] and previous['sha256'] != item['sha256']:
                changes.append({'path': item['path'], 'reason': 'backing file hashes differ between summaries'})
    return {'schema_version': 1, 'snapshots': snapshots,
            'identity_matches': identity_matches,
            'identity_reason': None if identity_matches else 'start/end process identity missing or differs',
            'changes': changes, 'transport_verified': False,
            'limitation': 'Maps prove file mappings, not transport selection or loaded memory bytes. '
                          'Hashes describe unchanged current backing files matching recorded device/inode; '
                          'in-place changes before summarization cannot be ruled out.'}


def _config_source(value, cyclone=False):
    if value.lstrip().startswith('<'):
        return {'kind': 'inline_xml', 'available': True,
                'sha256': hashlib.sha256(value.encode('utf-8')).hexdigest(), 'reason': None}
    if cyclone and ',' in value:
        return {'kind': 'unresolved_uri_list', 'available': False, 'sha256': None,
                'reason': 'multiple configuration sources are not resolved'}
    try:
        uri = urlsplit(value)
    except ValueError:
        return {'kind': 'unsupported_uri', 'available': False, 'sha256': None,
                'reason': 'malformed URI; not read'}
    if uri.netloc and not uri.scheme:
        return {'kind': 'unsupported_uri', 'available': False, 'sha256': None,
                'reason': 'network-style authority is not a local file path'}
    if uri.scheme:
        if (uri.scheme != 'file' or uri.netloc or uri.query or uri.fragment or
                not uri.path.startswith('/')):
            return {'kind': 'unsupported_uri', 'available': False, 'sha256': None,
                    'reason': 'only local absolute file URI without authority/query/fragment is read'}
        path = unquote(uri.path)
    else:
        path = value
    if not path or '\x00' in path:
        return {'kind': 'local_file', 'path': path, 'available': False, 'sha256': None,
                'reason': 'empty or invalid local path'}
    return {'kind': 'local_file', 'path': path, **_hash_file(path)}


def declared_dds_config(env):
    """Read only whitelisted entries from the supplied environment mapping.

    Return declarations plus hashes of explicitly named local configuration
    files; never resolve network URIs or preserve inline XML content. The caller
    saves this result locally. No process environment or business args are read.
    """
    declarations = {}
    for key in DDS_ENV_KEYS:
        value = env.get(key)
        entry = {'present': value is not None, 'value': None, 'reason': None}
        if value is None:
            entry['reason'] = 'environment variable not set'
        elif not isinstance(value, str):
            entry['reason'] = 'environment value is not a string'
        elif value.lstrip().startswith('<'):
            entry['source'] = _config_source(value, cyclone=True)
            entry['reason'] = 'inline XML retained only as SHA256'
        else:
            entry['value'] = value
            if not value:
                entry['reason'] = 'environment variable is empty'
            elif key == 'CYCLONEDDS_URI' or key.endswith('_PROFILES_FILE'):
                entry['source'] = _config_source(value, cyclone=key == 'CYCLONEDDS_URI')
        declarations[key] = entry
    return {'schema_version': 1, 'declarations': declarations, 'transport_verified': False,
            'reason': 'Declared environment/configuration does not establish the transport actually used.'}
