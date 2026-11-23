#!/usr/bin/env python3
"""Isolated type/preclaim/immutable-runner tests; no real mux/login/model."""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path

# Repository imports remain explicit when this test is run directly.
import sys as _g2c_sys
_G2C_REPO = Path(__file__).resolve().parents[2]
if str(_G2C_REPO) not in _g2c_sys.path:
    _g2c_sys.path.insert(0, str(_G2C_REPO))
import subprocess
import tempfile
import unittest
from unittest import mock

import gpt2codex_queue_project as project
import gpt2codex_queue_screen as screen

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('queue_types', REPO / 'gpt2codex-github-queue.py')
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


class Store:
    def __init__(self, root):
        self.state_dir = root / 'state'
        self.log_dir = root / 'log'
        self.tmp_dir = root / 'tmp'
        for path in (self.state_dir, self.log_dir, self.tmp_dir):
            path.mkdir(mode=0o700)
        self.data = {'version': 1, 'jobs': {}}

    @contextlib.contextmanager
    def lock(self):
        yield

    def load(self):
        return self.data

    def save(self, value):
        self.data = value


class TypesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=REPO / '.tmp', prefix='queue32-types-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.base = self.root / 'projects'
        self.base.mkdir()
        self.cwd = self.base / 'plain'
        self.cwd.mkdir()
        self.config = self.root / 'cfg/config.toml'
        self.config.parent.mkdir(mode=0o700)
        self.project_configs = self.config.parent / 'projects'
        self.project_configs.mkdir(mode=0o700)
        self.config.write_text('[workspace]\nproject_dir=' + json.dumps(str(self.base)) + '\n')
        self.toml = self.project_configs / 'plain.toml'
        self.toml.write_text('[project]\nid="plain"\ndirectory="plain"\ntype="non-git"\n')
        self.bin = self.root / 'codex'
        self.calls = self.root / 'calls'
        self.cli = str(REPO / 'target/debug/gpt2codex')
        self.fake_codex()
        self.patch_env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_STATE_HOME': str(self.home / 'state'), 'XDG_RUNTIME_DIR': str(self.home),
            'CODEX_HOME': str(self.home / 'codex'), 'TMPDIR': str(self.root)}, clear=False)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.cfg = {'bridge_cli': self.cli, 'registry_config': str(self.config),
                    'codex_bin': str(self.bin), 'project_roots': {'plain': str(self.cwd)},
                    'worker_hostname': 'fixture', 'project_screen_canary': True}
        self.item = {'number': 1, 'id': 1001, 'digest': 'a' * 64, 'project': 'plain',
                     'mode': 'read-only', 'task': 'fixture', 'title': 'fixture'}
        self.store = Store(self.root)
        self.store.data['jobs']['1'] = {**self.item, 'status': 'approved-project-screen',
                                        'backend': 'project-screen', 'worker_host': 'fixture'}

    def fake_codex(self, supported=True):
        flag = '--skip-git-repo-check' if supported else '--json'
        self.bin.write_text('#!/usr/bin/python3\n' + '''import json, sys
from pathlib import Path
with Path(%r).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:]==['exec','--help']:
 print(%r)
elif sys.argv[1]=='exec':
 sys.stdin.read()
 for e in [{'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'}, {'type':'turn.started'}, {'type':'turn.completed'}]: print(json.dumps(e))
else: sys.exit(91)
''' % (str(self.calls), flag))
        self.bin.chmod(0o700)

    def proof(self):
        return project.preclaim(self.cfg, 'plain', str(self.cwd), str(self.bin))

    def test_real_rust_metadata_no_git_and_unknown_types(self):
        # A fake Git would fail if the non-git registry invoked it.
        fake_dir = self.root / 'fake-bin'
        fake_dir.mkdir()
        git = fake_dir / 'git'
        git.write_text('#!/bin/sh\nexit 98\n')
        git.chmod(0o700)
        with mock.patch.dict(os.environ, {'PATH': str(fake_dir)}):
            self.assertEqual(self.proof()['metadata']['type'], 'non-git')
        self.toml.write_text(self.toml.read_text().replace('non-git', 'bogus'))
        with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
            self.proof()

    def test_root_mismatch_and_missing_rust_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'root mismatch'):
            project.preclaim(self.cfg, 'plain', str(self.base), str(self.bin))
        with self.assertRaises(RuntimeError):
            project.preclaim({**self.cfg, 'bridge_cli': '/missing'}, 'plain', str(self.cwd), str(self.bin))

    def test_non_git_only_flag_and_no_help_at_start(self):
        proof = self.proof()
        spec = {'backend': 'project-screen', 'project': 'plain', 'cwd': str(self.cwd),
                'mode': 'read-only', 'codex': str(self.bin), 'project_validation': proof}
        self.assertIn('--skip-git-repo-check', screen.exec_arguments(spec))
        legacy = {**spec, 'backend': 'screen'}
        self.assertNotIn('--skip-git-repo-check', screen.exec_arguments(legacy))
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        with self.assertRaises(RuntimeError):
            screen.exec_arguments({**spec, 'project_validation': None})

    def test_pinned_executable_survives_path_replacement(self):
        proof = self.proof()
        with project.pinned_executable(self.bin, proof['codex_stamp']) as pinned:
            replacement = self.root / 'replacement'
            replacement.write_text('#!/bin/sh\nexit 97\n')
            replacement.chmod(0o700)
            os.replace(replacement, self.bin)
            completed = subprocess.run([str(self.bin), 'exec', '--help'],
                stdin=subprocess.DEVNULL, capture_output=True, check=False, **pinned)
        self.assertEqual(completed.returncode, 0)
        self.assertIn(b'--skip-git-repo-check', completed.stdout)
        with self.assertRaisesRegex(RuntimeError, 'Executable changed'):
            project.validate_start(proof, 'plain', str(self.cwd), str(self.bin))

    def test_standard_never_skips_or_probes_help(self):
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.cwd)], check=True,
                       env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'})
        self.toml.write_text(self.toml.read_text().replace('non-git', 'standard'))
        proof = self.proof()
        args = screen.exec_arguments({'backend': 'project-screen', 'project': 'plain',
            'cwd': str(self.cwd), 'mode': 'read-only', 'codex': str(self.bin),
            'project_validation': proof})
        self.assertNotIn('--skip-git-repo-check', args)
        self.assertFalse(self.calls.exists())

    def test_unsupported_fails_before_queue_claim(self):
        self.fake_codex(False)
        api = mock.Mock()
        with mock.patch.object(queue, 'require_runnable_backend'), \
             mock.patch.object(queue, 'validated_remote', return_value=self.item), \
             mock.patch.object(project, 'anchor_ready') as anchor, \
             mock.patch.object(screen, 'require_chatgpt') as login:
            with self.assertRaisesRegex(queue.QueueError, 'approval not claimed'):
                queue.execute_claimed(api, self.store, self.cfg, 1, 'project-screen')
        self.assertEqual(self.store.data['jobs']['1']['status'], 'approved-project-screen')
        self.assertNotIn('started_at', self.store.data['jobs']['1'])
        anchor.assert_not_called()
        login.assert_not_called()
        api.comment.assert_not_called()

    def test_changed_validation_before_claim_keeps_approval(self):
        original = project.validate_start
        count = 0
        def changed(*args):
            nonlocal count
            count += 1
            if count == 2:
                self.toml.write_text(self.toml.read_text().replace('non-git', 'standard'))
            return original(*args)
        with mock.patch.object(queue, 'require_runnable_backend'), \
             mock.patch.object(queue, 'validated_remote', return_value=self.item), \
             mock.patch.object(project, 'validate_start', side_effect=changed), \
             mock.patch.object(project, 'anchor_ready', return_value=(
                 {'full_id': '123.g2c-plain'}, {'pid': 123, 'number': 0})), \
             mock.patch.object(screen, 'require_chatgpt'):
            with self.assertRaisesRegex(queue.QueueError, 'preclaim changed'):
                queue.execute_claimed(mock.Mock(), self.store, self.cfg, 1, 'project-screen')
        self.assertEqual(self.store.data['jobs']['1']['status'], 'approved-project-screen')
        self.assertNotIn('started_at', self.store.data['jobs']['1'])

    def test_changed_binary_after_claim_refuses_exec_and_replay(self):
        proof = self.proof()
        self.fake_codex(False)
        slot = self.root / 'stale-slot'
        slot.mkdir(mode=0o700)
        prompt = self.root / 'stale-prompt'
        prompt.write_text('synthetic')
        prompt.chmod(0o600)
        screen.write(slot / 'spec.json', {'backend': 'project-screen', 'number': 1,
            'project': 'plain', 'cwd': str(self.cwd), 'mode': 'read-only',
            'codex': str(self.bin), 'project_validation': proof, 'prompt': str(prompt)})
        with mock.patch.object(screen, 'require_chatgpt'):
            self.assertEqual(screen.runner(slot), 125)
            with self.assertRaises(FileExistsError):
                screen.runner(slot)
        self.assertEqual(screen.read(slot / 'result.json')['turns_started'], 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_changed_binary_or_registry_rejects_immutable_start(self):
        proof = self.proof()
        self.fake_codex(False)
        with self.assertRaisesRegex(RuntimeError, 'Executable changed'):
            project.validate_start(proof, 'plain', str(self.cwd), str(self.bin))
        self.fake_codex()
        proof = self.proof()
        self.toml.write_text(self.toml.read_text().replace('non-git', 'standard'))
        with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
            project.validate_start(proof, 'plain', str(self.cwd), str(self.bin))

    def test_non_git_symlink_root_and_duplicate_registration_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.toml.write_text(self.toml.read_text().replace('directory="plain"', 'directory="alias"'))
        with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
            self.proof()
        self.toml.write_text('[project]\nid="plain"\ndirectory="plain"\ntype="non-git"\n')
        (self.project_configs / 'other.toml').write_text('[project]\nid="other"\ndirectory="plain"\ntype="non-git"\n')
        with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
            self.proof()

    def test_registry_shared_lease_excludes_upgrade(self):
        with project.registry_lease(self.config):
            fd = os.open(self.project_configs, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_valid_non_git_runner_one_exec_and_no_replay(self):
        proof = self.proof()
        slot = self.root / 'slot'
        slot.mkdir(mode=0o700)
        prompt = self.root / 'prompt'
        prompt.write_text('synthetic')
        prompt.chmod(0o600)
        screen.write(slot / 'spec.json', {'backend': 'project-screen', 'number': 1,
            'project': 'plain', 'cwd': str(self.cwd), 'mode': 'read-only',
            'codex': str(self.bin), 'project_validation': proof, 'prompt': str(prompt)})
        with mock.patch.object(screen, 'require_chatgpt'):
            self.assertEqual(screen.runner(slot), 0)
            with self.assertRaises(FileExistsError):
                screen.runner(slot)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        starts = [args for args in calls if args != ['exec', '--help']]
        self.assertEqual(len(starts), 1)
        self.assertIn('--skip-git-repo-check', starts[0])
        self.assertFalse((slot / 'resume.json').exists())
        self.assertEqual(screen.read(slot / 'result.json')['turns_completed'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
