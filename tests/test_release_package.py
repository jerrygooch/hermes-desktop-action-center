"""Release archive contract: allowlisted shipping files, not workstation artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class ReleasePackageTests(unittest.TestCase):
    def load_builder(self):
        location = ROOT / 'scripts' / 'package_release.py'
        self.assertTrue(location.exists(), 'release builder has not been implemented')
        spec = importlib.util.spec_from_file_location('action_center_release', location)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        files = {
            'README.md': '# Action Center\n', 'LICENSE': 'MIT\n',
            'dashboard/manifest.json': json.dumps({'name': 'action-center', 'version': '0.1.0', 'api': 'plugin_api.py'}),
            'dashboard/plugin_api.py': 'router = None\n',
            'desktop/plugin.js': "export default { id: 'action-center', defaultEnabled: false };\n",
            '.env': 'SECRET_CANARY_DO_NOT_SHIP',
            'tests/live/private.log': 'SECRET_CANARY_DO_NOT_SHIP',
        }
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')

    def test_archive_is_reproducible_allowlisted_and_hash_verified(self):
        builder = self.load_builder()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            self.fixture(root)
            output = root / 'dist'
            first = builder.build_release(root, output)
            original = first.read_bytes()
            second = builder.build_release(root, output)
            self.assertEqual(original, second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                expected = {'action-center/' + name for name in builder.SHIPPING_FILES}
                self.assertEqual(names, expected | {'action-center/CONTENTS.sha256'})
                inventory = archive.read('action-center/CONTENTS.sha256').decode()
                for name in builder.SHIPPING_FILES:
                    data = archive.read('action-center/' + name)
                    self.assertIn(hashlib.sha256(data).hexdigest() + '  ' + name, inventory)
                    self.assertNotIn(b'SECRET_CANARY_DO_NOT_SHIP', data)
            self.assertIn(hashlib.sha256(original).hexdigest(), first.with_suffix('.zip.sha256').read_text())

    def test_missing_shipping_file_refuses_build(self):
        builder = self.load_builder()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'desktop/plugin.js').unlink()
            with self.assertRaises(FileNotFoundError):
                builder.build_release(root, root / 'dist')
            self.assertFalse((root / 'dist').exists())

    def test_invalid_manifest_identity_or_version_refuses_build(self):
        builder = self.load_builder()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            self.fixture(root)
            manifest = root / 'dashboard/manifest.json'
            for change in [{'name': '../other'}, {'version': '../escape'}, {'api': '../outside.py'}]:
                base = {'name':'action-center', 'version':'0.1.0', 'api':'plugin_api.py'}
                manifest.write_text(json.dumps({**base, **change}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    builder.build_release(root, root / 'dist')


if __name__ == '__main__':
    unittest.main()
