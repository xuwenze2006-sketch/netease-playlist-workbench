import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_release import audit_repository


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git('init', '-q')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def codes(self, report):
        return {item['code'] for item in report['findings']}

    def test_clean_untracked_source_and_synthetic_markers_are_allowed(self):
        self.write('app.py', 'private_key = "fixture-not-a-key"\n')
        self.write('tests/test_sample.py', 'header = "-----BEGIN PRIVATE KEY-----"\n')
        report = audit_repository(self.root)
        self.assertTrue(report['ok'])
        self.assertEqual(report['candidate_count'], 2)

    def test_ignored_personal_files_are_not_read_but_forced_staging_is_blocked(self):
        self.write('.gitignore', 'artifacts/\n')
        self.write('artifacts/record.json', '{"personal": true}')
        self.assertTrue(audit_repository(self.root)['ok'])
        self.git('add', '-f', 'artifacts/record.json')
        self.assertIn('private_path', self.codes(audit_repository(self.root)))

    def test_staged_secret_is_detected_after_working_copy_was_cleaned(self):
        token = 'ghp_' + 'A' * 36
        self.write('settings.py', 'value = "' + token + '"\n')
        self.git('add', 'settings.py')
        self.write('settings.py', 'value = "example"\n')
        report = audit_repository(self.root)
        self.assertIn('credential', self.codes(report))
        self.assertTrue(any(row['source'] == 'index' for row in report['findings']))
        self.assertNotIn(token, json.dumps(report))

    def test_unstaged_secret_is_detected_beside_clean_index(self):
        self.write('settings.py', 'value = "example"\n')
        self.git('add', 'settings.py')
        self.write('settings.py', 'value = "' + 'github_pat_' + 'B' * 82 + '"\n')
        self.assertIn('credential', self.codes(audit_repository(self.root)))

    def test_git_replace_cannot_hide_the_actual_staged_secret_blob(self):
        self.write('settings.py', 'value = "' + 'ghp_' + 'C' * 36 + '"\n')
        self.git('add', 'settings.py')
        original = self.git('rev-parse', ':settings.py').decode().strip()
        self.write('settings.py', 'value = "example"\n')
        replacement = self.git('hash-object', '-w', 'settings.py').decode().strip()
        self.git('replace', original, replacement)
        self.assertIn('credential', self.codes(audit_repository(self.root)))

    def test_private_key_body_in_tests_is_not_exempted(self):
        key = '-----BEGIN PRIVATE KEY-----\n' + 'A' * 64 + '\n-----END PRIVATE KEY-----\n'
        self.write('tests/key.txt', key)
        report = audit_repository(self.root)
        self.assertIn('private_key', self.codes(report))
        self.assertNotIn('A' * 64, json.dumps(report))

    def test_complete_private_key_with_json_escaped_line_breaks_is_detected(self):
        for newline in ('\n', '\r\n'):
            key = newline.join(['-----BEGIN PRIVATE KEY-----', 'B' * 64, '-----END PRIVATE KEY-----'])
            self.write('settings.json', json.dumps({'private_key': key}))
            self.assertIn('private_key', self.codes(audit_repository(self.root)))

    def test_encrypted_cli_credentials_cannot_be_published_outside_the_private_directory(self):
        for name in ('credentials.enc.json', 'tokens.enc.json'):
            self.write(name, '{"ciphertext": "synthetic"}')
        self.assertEqual(self.codes(audit_repository(self.root)), {'private_path'})
        self.git('add', '--', 'credentials.enc.json', 'tokens.enc.json')
        report = audit_repository(self.root)
        self.assertEqual(len([row for row in report['findings'] if row['source'] == 'index']), 2)

    def test_environment_files_and_personal_history_cannot_be_force_added(self):
        for name in ('.env.production', 'docs/superpowers/private.md', 'scripts/finish_classification_03.py'):
            self.write(name, 'personal')
        self.assertEqual(self.codes(audit_repository(self.root)), {'private_path'})

    def test_hardcoded_personal_paths_are_reported_without_the_username(self):
        username = 'private-' + 'person'
        path = 'C:' + chr(92) + 'Users' + chr(92) + username + chr(92) + 'project'
        self.write('README.md', path)
        report = audit_repository(self.root)
        self.assertIn('personal_path', self.codes(report))
        self.assertNotIn(username, json.dumps(report))

    def test_oversized_and_unexpected_binary_sources_are_blocked(self):
        self.write('large.txt', 'x' * (2 * 1024 * 1024 + 1))
        self.write('download.exe', 'not a source file')
        self.assertEqual(self.codes(audit_repository(self.root)), {'oversized', 'unsupported_file'})

    def test_staged_symlink_is_not_followed(self):
        blob = subprocess.check_output(['git', '-C', str(self.root), 'hash-object', '-w', '--stdin'],
                                       input=b'../private.txt\n').decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '120000,' + blob + ',link.py')
        self.assertIn('symlink', self.codes(audit_repository(self.root)))

    def test_empty_repository_is_not_reported_as_a_release(self):
        self.assertIn('empty_repository', self.codes(audit_repository(self.root)))
