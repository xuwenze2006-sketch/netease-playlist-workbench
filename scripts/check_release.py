"""Audit Git candidates and staged blobs without printing matched secret values.

This bounded check complements human review; it is not a complete secret scanner
and does not inspect previous Git history or ignored local account records.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


PROJECT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_TEXT_SUFFIXES = {'.py', '.ps1', '.cmd', '.md', '.toml', '.json', '.ts', '.tsx', '.css',
                  '.html', '.svg', '.yml', '.yaml', '.js', '.cjs', '.mjs', '.txt'}
_PLAIN_NAMES = {'.gitignore', '.gitattributes', '.editorconfig', '.npmrc', '.env.example', 'LICENSE'}
_PRIVATE_PARTS = {'artifacts', '.organizer', '.tools', 'node_modules', '__pycache__',
                  '.playwright-cli', '.superpowers', '.venv', '.git'}
_CREDENTIAL = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{50,255}'
                         r'|sk-(?:proj-)?[A-Za-z0-9_-]{32,255}|AKIA[0-9A-Z]{16})\b')
_KEY_NEWLINE = r'(?:\r?\n|\\n|\\r\\n)'
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[ \t]*'
                          + _KEY_NEWLINE + r'(?:[A-Za-z0-9+/=]{16,}' + _KEY_NEWLINE + r')+'
                          r'-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')
_PERSONAL_PATH = re.compile(r'(?:[A-Za-z]:[\\/]+(?:Users|用户文件)[\\/]+|/(?:Users|home)/)'
                            r'(?![<$%])[^\s\\/\"\'<>]+', re.IGNORECASE)


def _git(root, *args, payload=None):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(root), *args], input=payload, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout


def _path_problem(name):
    path = PurePosixPath(name)
    parts = tuple(part.lower() for part in path.parts)
    if (path.is_absolute() or '..' in parts or any(part in _PRIVATE_PARTS for part in parts)
            or parts[:2] in {('frontend', 'dist'), ('docs', 'superpowers')}
            or name == 'scripts/finish_classification_03.py'
            or path.name.lower() in {'credentials.json', 'tokens.json', 'credentials.enc.json', 'tokens.enc.json'}
            or (path.name.startswith('.env') and path.name != '.env.example')
            or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3'}):
        return 'private_path'
    if path.suffix.lower() not in _TEXT_SUFFIXES and path.name not in _PLAIN_NAMES:
        return 'unsupported_file'
    return None


def audit_repository(root):
    root = Path(root).resolve()
    findings = []
    report = {'ok': False, 'candidate_count': 0, 'staged_count': 0, 'findings': findings,
              'scope': 'current Git candidates, working copies, and index blobs; not previous history'}

    def add(path, code, source, line=None):
        finding = {'path': path, 'code': code, 'source': source}
        if line is not None:
            finding['line'] = line
        findings.append(finding)

    def inspect(name, raw, source):
        if len(raw) > MAX_FILE_BYTES:
            add(name, 'oversized', source)
            return
        try:
            text = raw.decode('utf-8-sig')
            if '\0' in text:
                raise UnicodeError()
        except UnicodeError:
            add(name, 'unsupported_file', source)
            return
        for code, pattern in (('credential', _CREDENTIAL), ('private_key', _PRIVATE_KEY),
                              ('personal_path', _PERSONAL_PATH)):
            match = pattern.search(text)
            if match:
                add(name, code, source, text.count('\n', 0, match.start()) + 1)

    try:
        actual = _git(root, 'rev-parse', '--show-toplevel').decode('utf-8').strip()
        if Path(actual).resolve() != root:
            add('.', 'not_repository_root', 'git')
            return report
        candidates = sorted({name.decode('utf-8') for name in
                             _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0') if name})
        report['candidate_count'] = len(candidates)
        if not candidates:
            add('.', 'empty_repository', 'git')
        if len(candidates) > 4000:
            add('.', 'too_many_files', 'git')
            return report
        total = 0
        for name in candidates:
            problem = _path_problem(name)
            if problem:
                add(name, problem, 'working_tree')
                continue
            path = root / name
            # Do not follow a file symlink, junction, or redirected ancestor.
            redirected = False
            for ancestor in (path, *path.parents):
                if ancestor == root:
                    break
                try:
                    info = ancestor.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    redirected = True
                    break
            if redirected:
                add(name, 'symlink', 'working_tree')
                continue
            if not path.exists():  # Its index blob is still checked below.
                continue
            if not path.is_file():
                add(name, 'unsupported_file', 'working_tree')
                continue
            size = path.stat().st_size
            total += size
            if total > MAX_TOTAL_BYTES:
                add('.', 'total_size_limit', 'working_tree')
                return report
            if size > MAX_FILE_BYTES:
                add(name, 'oversized', 'working_tree')
                continue
            with path.open('rb') as stream:
                inspect(name, stream.read(MAX_FILE_BYTES + 1), 'working_tree')

        # Inspect the actual index as well: cleaning a working copy does not
        # remove an older secret already staged for the next commit.
        entries = []
        for entry in _git(root, 'ls-files', '--stage', '-z').split(b'\0'):
            if not entry:
                continue
            metadata, raw_name = entry.split(b'\t', 1)
            mode, oid, stage = metadata.decode('ascii').split()
            name = raw_name.decode('utf-8')
            report['staged_count'] += 1
            if stage != '0':
                add(name, 'unmerged_index', 'index')
            elif mode not in {'100644', '100755'}:
                add(name, 'symlink' if mode == '120000' else 'unsupported_file', 'index')
            elif (problem := _path_problem(name)):
                add(name, problem, 'index')
            else:
                entries.append((name, oid))
        if entries:
            request = ''.join(oid + '\n' for _, oid in entries).encode('ascii')
            metadata = _git(root, 'cat-file', '--batch-check', payload=request).splitlines()
            if len(metadata) != len(entries):
                raise ValueError('incomplete metadata')
            accepted = []
            total = 0
            for (name, oid), row in zip(entries, metadata):
                actual_oid, kind, raw_size = row.decode('ascii').split()
                size = int(raw_size)
                if actual_oid != oid or kind != 'blob' or size < 0:
                    raise ValueError('invalid blob')
                total += size
                if total > MAX_TOTAL_BYTES:
                    add('.', 'total_size_limit', 'index')
                    return report
                if size > MAX_FILE_BYTES:
                    add(name, 'oversized', 'index')
                else:
                    accepted.append((name, oid, size))
            if accepted:
                request = ''.join(oid + '\n' for _, oid, _ in accepted).encode('ascii')
                blobs = _git(root, 'cat-file', '--batch', payload=request)
                cursor = 0
                for name, oid, size in accepted:
                    end = blobs.index(b'\n', cursor)
                    if blobs[cursor:end] != f'{oid} blob {size}'.encode('ascii'):
                        raise ValueError('invalid blob header')
                    cursor = end + 1
                    raw = blobs[cursor:cursor + size]
                    if len(raw) != size or blobs[cursor + size:cursor + size + 1] != b'\n':
                        raise ValueError('incomplete blob')
                    inspect(name, raw, 'index')
                    cursor += size + 1
                if cursor != len(blobs):
                    raise ValueError('unexpected blob data')
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        add('.', 'repository_read_failed', 'git')
    report['ok'] = not findings
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROJECT, help='Git repository root to inspect')
    options = parser.parse_args()
    report = audit_repository(options.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
