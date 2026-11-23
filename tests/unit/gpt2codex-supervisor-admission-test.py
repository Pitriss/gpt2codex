#!/usr/bin/env python3
"""Real Rust/Python deny-only integration; no screen, Codex, network or live state.

Build with cargo build --offline --locked before running. All fixtures are under
the existing .tmp; the helper never invokes the host synthetic smoke.
"""
import copy
import json
import os
from pathlib import Path

# Repository imports remain explicit when this test is run directly.
import sys as _g2c_sys
_G2C_REPO = Path(__file__).resolve().parents[2]
if str(_G2C_REPO) not in _g2c_sys.path:
    _g2c_sys.path.insert(0, str(_G2C_REPO))
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
import gpt2codex_queue_dispatch as dispatch


REPO = Path(__file__).resolve().parents[2]
BRIDGE = REPO / 'target/debug/gpt2codex'


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='s24-admission-', dir=REPO / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.namespace = self.base / 'supervisor'
        self.namespace.mkdir(mode=0o700)
        self.home = self.base / 'home'
        self.home.mkdir()
        (self.base / '.ai').mkdir()
        self.config = self.base / 'cfg/config.toml'
        self.config.parent.mkdir()
        (self.config.parent / 'projects').mkdir()
        self.config.write_text('[workspace]\nproject_dir = ' + json.dumps(str(self.base))
                               + '\nrules_dir = ".ai"\nglobal_context_files = []\n')
        for name in ('one', 'two'):
            root = self.base / name
            root.mkdir()
            (self.config.parent / 'projects' / (name + '.toml')).write_text(
                '[project]\nid = "' + name + '"\ndirectory = "' + name + '"\ntype = "non-git"\n')
        self.probe = dispatch.RustSupervisorProbe(BRIDGE, self.config, self.namespace)
        self.ledger = dispatch.Ledger(self.base)
        self.env = {'HOME': str(self.home), 'PATH': '/nonexistent', 'LANG': 'C'}

    def cli(self, *args, env=None):
        return subprocess.run([str(BRIDGE), *args, '--config', str(self.config)],
                              env=env or self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=5)

    def claim(self, owner='manual', project='one'):
        args = ['supervisor', 'claim', '--id', project, '--directory', str(self.namespace),
                '--owner', owner, '--reservation-only']
        if owner == 'queue':
            args += ['--issue-json', json.dumps(dict(repository='fixture/repo', issue_id=124,
                                                    number=24, sha='a' * 64))]
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['claim']

    def view(self, project='one'):
        result = self.cli('supervisor', 'admission', '--id', project,
                          '--directory', str(self.namespace))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def submit(self, project='one', mode='read-only', key='job', issue=24):
        self.ledger.submit(key, issue=issue, digest='a' * 64, repository='fixture/repo',
                           project=project, root=str(self.base / project), hostname='fixture',
                           mode=mode)
        self.ledger.approve(key)

    def test_empty_namespace_read_only_never_grants(self):
        before = list(self.namespace.iterdir())
        view = self.view()
        self.assertEqual(view['reservation'], 'absent')
        self.assertIs(view['runtime_enabled'], False)
        self.assertIs(view['launch_permitted'], False)
        self.assertEqual(self.probe.waiting_reason('one', self.base / 'one'), 'session_unverified')
        self.assertEqual(list(self.namespace.iterdir()), before)

    def test_manual_pending_blocks_queue_across_real_cli_exit_and_restart(self):
        self.claim()
        for index, mode in enumerate(('read-only', 'workspace-write')):
            with self.subTest(mode=mode):
                key = 'job' + str(index)
                self.submit(mode=mode, key=key, issue=24 + index)
                journal = self.namespace / 'project-owners-v1.json'
                before = journal.read_bytes()
                self.ledger = dispatch.Ledger(self.base)
                self.ledger.recover()
                self.assertIsNone(self.ledger.reserve_managed(key, self.probe))
                job = self.ledger.summary()['jobs'][index]
                self.assertEqual((job['status'], job['waiting_reason']), ('waiting', 'project_busy'))
                self.assertIsNone(job['claim'])
                self.assertIsNone(job['process'])
                self.assertEqual(journal.read_bytes(), before)
        # This is a pending reservation, NOT a live manual idle/active TUI test.
        self.assertEqual(self.view()['owner'], 'manual')

    def test_queue_pending_blocks_every_available_manual_launch_hook(self):
        self.claim('queue')
        env = dict(self.env, G2C_PROJECT_SUPERVISOR_DIRECTORY=str(self.namespace))
        paths = [
            ['screen', 'start', '--approve-start'],
            ['session', 'start', '--multiplexer', 'screen', '--approve-start'],
            ['session', 'start', '--multiplexer', 'tmux', '--approve-start'],
            ['session', 'start', '--multiplexer', 'screen', '--attention', '--approve-start'],
            ['session', 'start', '--multiplexer', 'tmux', '--attention', '--approve-start'],
            ['attention', 'run', '--multiplexer', 'screen', '--approve-run'],
            ['attention', 'run', '--multiplexer', 'tmux', '--approve-run'],
        ]
        journal = (self.namespace / 'project-owners-v1.json').read_bytes()
        for args in paths:
            with self.subTest(path=args):
                result = self.cli(*args, '--id', 'one', env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'project_busy: pending_reservation', result.stderr)
        self.assertEqual((self.namespace / 'project-owners-v1.json').read_bytes(), journal)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_unbound_app_server_denies_with_directory_flag_alone(self):
        env = dict(self.env, G2C_PROJECT_SUPERVISOR_DIRECTORY=str(self.namespace))
        result = subprocess.run([str(BRIDGE), 'codex', 'probe'], env=env,
                                capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'runtime_disabled', result.stderr)
        self.assertEqual(list(self.namespace.iterdir()), [])

    def test_distinct_project_independent_but_still_stop_gated(self):
        self.claim()
        self.assertEqual(self.probe.waiting_reason('one', self.base / 'one'), 'project_busy')
        self.assertEqual(self.probe.waiting_reason('two', self.base / 'two'), 'session_unverified')
        self.claim('queue', 'two')
        self.assertEqual(self.probe.waiting_reason('two', self.base / 'two'), 'project_busy')

    def test_corruption_and_scope_change_preserve_evidence_and_deny(self):
        self.claim()
        journal = self.namespace / 'project-owners-v1.json'
        value = json.loads(journal.read_bytes())
        value['claims'][0]['boot_id'] = 'other'
        for content in (json.dumps(value).encode(), b'incomplete'):
            journal.write_bytes(content)
            self.assertEqual(self.view()['reservation'], 'unknown')
            self.assertEqual(self.probe.waiting_reason('one', self.base / 'one'), 'session_unverified')
            self.assertEqual(journal.read_bytes(), content)

    def test_namespace_replacement_and_symlink_are_rejected(self):
        self.namespace.rename(self.base / 'retained')
        self.namespace.mkdir(mode=0o700)
        self.assertEqual(self.probe.waiting_reason('one', self.base / 'one'), 'session_unverified')
        self.namespace.rmdir()
        self.namespace.symlink_to(self.base / 'retained')
        with self.assertRaises(dispatch.DispatchError):
            dispatch.RustSupervisorProbe(BRIDGE, self.config, self.namespace)

    def test_schema_identity_and_fabricated_grant_fail_closed(self):
        self.claim()
        view = self.view()
        changes = [{'version': True}, {'launch_permitted': True}, {'runtime_enabled': True},
                   {'project_id': 'two'}, {'root': str(self.base / 'two')},
                   {'namespace_inode': view['namespace_inode'] + 1}, {'owner': 'other'},
                   {'recovery_required': False}, {'detail': 'ready'}, {'reservation': 'live'}]
        for change in changes:
            bad = dict(copy.deepcopy(view), **change)
            with self.subTest(change=change), mock.patch.object(self.probe, '_read', return_value=bad):
                self.assertEqual(self.probe.waiting_reason('one', self.base / 'one'), 'session_unverified')

    def test_transport_failure_is_machine_readable_wait_without_claim(self):
        self.submit()
        for failure in (OSError('fixture'), ValueError('fixture'), subprocess.TimeoutExpired('fixture', 3)):
            with self.subTest(failure=type(failure)), mock.patch.object(self.probe, '_read', side_effect=failure):
                self.assertIsNone(self.ledger.reserve_managed('job', self.probe))
                job = self.ledger.summary()['jobs'][0]
                self.assertEqual(job['waiting_reason'], 'session_unverified')
                self.assertIsNone(job['claim'])

    def test_missing_project_or_namespace_no_legacy_fallback(self):
        env = dict(self.env, G2C_PROJECT_SUPERVISOR_DIRECTORY=str(self.base / 'missing'))
        result = self.cli('screen', 'start', '--id', 'one', '--approve-start', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'session_unverified', result.stderr)
        self.assertEqual(list(self.namespace.iterdir()), [])


if __name__ == '__main__':
    if not BRIDGE.is_file():
        raise SystemExit('Build the local Rust binary offline first')
    unittest.main()
