#!/usr/bin/env python
"""Build the allowlisted Action Center distribution; never infer release acceptance.

No dependencies. Archive contents are deterministic for identical source bytes.
Tests, local receipts, credentials, generated harness copies and Git metadata
cannot enter the package through a recursive directory walk.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

SHIPPING_FILES = (
    'README.md',
    'LICENSE',
    'dashboard/manifest.json',
    'dashboard/plugin_api.py',
    'desktop/plugin.js',
)


def build_release(root: Path, output: Path) -> Path:
    root = Path(root).resolve()
    snapshot = {}
    for name in SHIPPING_FILES:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Shipping path escapes source root: {name}')
        if not path.is_file():
            raise FileNotFoundError(f'Missing shipping file: {name}')
        snapshot[name] = path.read_bytes()
    manifest = json.loads(snapshot['dashboard/manifest.json'])
    version = manifest.get('version', '')
    if (manifest.get('name') != 'action-center'
            or manifest.get('api') != 'plugin_api.py'
            or not isinstance(version, str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.]+)?', version)):
        raise ValueError('Invalid Action Center manifest identity, API path, or version')
    inventory = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name}\n'
        for name, data in sorted(snapshot.items())
    ).encode('utf-8')
    entries = {**snapshot, 'CONTENTS.sha256': inventory}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo('action-center/' + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)
    payload = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as verified:
        if verified.testzip() is not None:
            raise RuntimeError('Archive CRC verification failed')
        for name, data in entries.items():
            if verified.read('action-center/' + name) != data:
                raise RuntimeError(f'Archive source-byte mismatch: {name}')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'action-center-{version}.zip'
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()
    target = build_release(args.root, args.output or args.root / 'dist')
    print(json.dumps({'archive': str(target.resolve()),
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                      'shipping_files': len(SHIPPING_FILES),
                      'release_acceptance': 'not implied by packaging'}))


if __name__ == '__main__':
    main()
