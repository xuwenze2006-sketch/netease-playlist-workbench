from pathlib import Path
import tempfile
import unittest

from netease_organizer.web_build import LaunchError, load_build


class WebBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assets = self.root / 'frontend/dist'
        self.assets.mkdir(parents=True)
        (self.assets / 'index.html').write_bytes(b'<main>modern</main>')
        (self.assets / 'assets').mkdir()
        (self.assets / 'assets/main.js').write_bytes(b'window.modern = true;')
        (self.root / 'netease_organizer').mkdir()
        self.source = self.root / 'netease_organizer/web_server.py'
        self.source.write_bytes(b'API_VERSION = 1')

    def test_content_identity_changes_for_source_and_assets_but_snapshot_is_fixed(self):
        first = load_build(self.root)
        self.assertRegex(first.revision, r'^[a-f0-9]{64}$')
        self.assertEqual(load_build(self.root).revision, first.revision)
        self.source.write_bytes(b'API_VERSION = 2')
        self.assertNotEqual(load_build(self.root).revision, first.revision)
        (self.assets / 'assets/main.js').write_bytes(b'window.modern = false;')
        self.assertEqual(first.assets['assets/main.js'], b'window.modern = true;')
        self.assertNotEqual(load_build(self.root).revision, first.revision)

    def test_credentials_cache_logs_tests_and_docs_do_not_change_version(self):
        first = load_build(self.root)
        for name in ('.organizer/tokens.enc.json', 'artifacts/歌手精选执行结果.json',
                     '.tools/private.pem', 'tests/test_random.py', 'README.md'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'SECRET')
        self.assertEqual(load_build(self.root).revision, first.revision)
        self.assertNotIn(b'SECRET', b''.join(first.assets.values()))

    def test_missing_index_is_typed_before_any_runtime_or_account_use(self):
        (self.assets / 'index.html').unlink()
        with self.assertRaises(LaunchError) as error:
            load_build(self.root)
        self.assertEqual(error.exception.code, 'frontend_missing')

    def test_unserved_files_are_not_part_of_asset_snapshot(self):
        (self.assets / 'private.pem').write_bytes(b'SECRET')
        (self.assets / 'assets/main.js.map').write_bytes(b'SECRET')
        (self.assets / '.private.js').write_bytes(b'SECRET')
        snapshot = load_build(self.root)
        self.assertEqual(set(snapshot.assets), {'index.html', 'assets/main.js'})

    def test_oversized_or_changing_build_cannot_be_announced_as_loaded(self):
        from unittest.mock import patch
        with patch('netease_organizer.web_build.MAX_ASSET_BYTES', 3):
            with self.assertRaises(LaunchError) as error:
                load_build(self.root)
        self.assertEqual(error.exception.code, 'build_changed')

    def test_a_source_edit_during_read_is_rejected(self):
        from unittest.mock import patch
        from netease_organizer.web_build import _signature
        reads = 0

        def change_during_read(path):
            nonlocal reads
            if path == self.source:
                reads += 1
                if reads == 2:
                    self.source.write_bytes(b'API_VERSION = 2')
            return _signature(path)

        with patch('netease_organizer.web_build._signature', side_effect=change_during_read):
            with self.assertRaises(LaunchError) as error:
                load_build(self.root)
        self.assertGreaterEqual(reads, 2)
        self.assertEqual(error.exception.code, 'build_changed')

    def test_asset_snapshot_cannot_be_mutated_after_identity_was_calculated(self):
        snapshot = load_build(self.root)
        with self.assertRaises(TypeError):
            snapshot.assets['index.html'] = b'newer page'

    def test_incomplete_stable_build_is_rejected_before_opening_a_blank_window(self):
        for tag in ('<script type="module" src="/assets/missing.js"></script>',
                    '<link rel="stylesheet" href="/assets/missing.css">',
                    '<link rel="modulepreload" href="/assets/missing.js">'):
            with self.subTest(tag=tag):
                (self.assets / 'index.html').write_text(tag, encoding='utf-8')
                with self.assertRaises(LaunchError) as error:
                    load_build(self.root)
                self.assertEqual(error.exception.code, 'build_changed')

    def test_valid_local_entry_resources_are_verified_without_executing_them(self):
        (self.assets / 'assets/style.css').write_bytes(b'body {color:red}')
        (self.assets / 'index.html').write_text(
            '<script type="module" src="/assets/main.js"></script>'
            '<link rel="stylesheet" href="./assets/style.css">', encoding='utf-8')
        snapshot = load_build(self.root)
        self.assertEqual(snapshot.assets['assets/main.js'], b'window.modern = true;')

    def test_entry_cannot_depend_on_external_or_private_resources(self):
        for reference in ('https://external.invalid/app.js', '//external.invalid/app.js',
                          '/.organizer/tokens.enc.json', '/../assets/main.js'):
            with self.subTest(reference=reference):
                (self.assets / 'index.html').write_text(f'<script src="{reference}"></script>', encoding='utf-8')
                with self.assertRaises(LaunchError) as error:
                    load_build(self.root)
                self.assertEqual(error.exception.code, 'build_changed')

    def test_unknown_diagnostic_is_fixed_and_does_not_expose_arbitrary_input(self):
        error = LaunchError(code='SECRET')
        self.assertEqual(error.code, 'startup_failed')
        self.assertNotIn('SECRET', str(error))


if __name__ == '__main__':
    unittest.main()
