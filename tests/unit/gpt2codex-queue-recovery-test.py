#!/usr/bin/env python3
"""Offline crash recovery tests: no real GNU screen, Codex, GitHub or Queue state."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path

# Repository imports remain explicit when this test is run directly.
import sys as _g2c_sys
_G2C_REPO = Path(__file__).resolve().parents[2]
if str(_G2C_REPO) not in _g2c_sys.path:
    _g2c_sys.path.insert(0, str(_G2C_REPO))
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


# Keep imports isolated from the real project backend and all /proc state.
project = types.ModuleType('gpt2codex_queue_project')
slots = types.ModuleType('gpt2codex_queue_screen')
path = (_G2C_REPO / 'gpt2codex_queue_recovery.py')
spec = importlib.util.spec_from_file_location('recovery_under_test', path)
recovery = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'gpt2codex_queue_project': project,
                                   'gpt2codex_queue_screen': slots}):
    spec.loader.exec_module(recovery)


class Store:
    def __init__(self, job):
        self.jobs = {str(job['number']): copy.deepcopy(job)}

    @contextlib.contextmanager
    def lock(self):
        yield

    def load(self):
        return {'version': 1, 'jobs': copy.deepcopy(self.jobs)}

    def save(self, state):
        self.jobs = copy.deepcopy(state['jobs'])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.item = {'number': 49, 'id': 1049, 'project': 'k2-smb', 'digest': 'a' * 64}
        self.store = Store({**self.item, 'status': 'approved-project-screen', 'backend': 'project-screen'})
        self.cfg = {'codex_bin': '/usr/bin/true'}
        self.proof = {'cli': '/usr/bin/gpt2codex', 'config': '/tmp/config.toml',
                      'cli_stamp': ['mock'], 'metadata': {'type': 'standard'}}
        self.old_session = '101.g2c-k2-smb'
        self.new_session = '202.g2c-k2-smb'
        self.old = {'view': {'project': 'k2-smb', 'root': self.root,
                             'pid': 101, 'full_id': self.old_session,
                             'generation': {'pid': 101, 'ticks': 10, 'boot': 'oldboot'}},
                    'number': 0, 'pid': 111,
                    'generation': {'pid': 111, 'ticks': 13, 'boot': 'oldboot'}}
        self.status = {'project_id': 'k2-smb', 'screen_name': 'g2c-k2-smb',
                       'state': 'interrupted', 'session_id': self.old_session}
        self.history = []
        self.alive = False
        self.recovered = False
        self.prepared = False
        self.noop = False
        self.anchor = {'pid': 222, 'number': 0}
        self.view = {'full_id': self.new_session, 'project': 'k2-smb', 'root': self.root}

        def anchor_ready(store, project_id, root):
            if self.prepared or self.noop:
                return self.view, self.anchor
            raise FileNotFoundError('/proc/101')

        def fake_cli(proof, project_id, action, *flags):
            self.history.append((action, flags))
            if action == 'recover':
                self.assertEqual(flags, ('--approve-recover',))
                self.recovered = True
                return 'screen_recover=ok'
            if action == 'start':
                self.assertTrue(self.recovered)
                self.assertEqual(flags, ('--anchor-only', '--approve-start', '--task', 'Queue anchor'))
                self.prepared = True
                return 'project_id=k2-smb state=detached'
            raise AssertionError('unexpected CLI verb')

        for name, value in [('anchor_ready', anchor_ready),
                             ('_anchor_file', lambda _s, _p: Path(self.root) / 'k2-smb.json'),
                             ('_old_process_gone', lambda *_args: not self.alive),
                             ('validate_start', lambda *_args: 'standard'),
                             ('prepare_anchor', lambda *_args: None),
                             ('pinned_executable', lambda *_args: contextlib.nullcontext({}))]:
            setattr(project, name, value)
        slots.read = lambda *_args: copy.deepcopy(self.old)
        slots.clean_env = lambda: {}
        self.cli_patcher = mock.patch.object(recovery, '_cli', side_effect=fake_cli)
        self.cli_patcher.start()
        self.addCleanup(self.cli_patcher.stop)
        self.status_patcher = mock.patch.object(recovery, '_status', side_effect=self.fake_status)
        self.status_patcher.start()
        self.addCleanup(self.status_patcher.stop)

    def fake_status(self, _proof, _project):
        result = self.status.copy()
        if self.prepared:
            result.update(state='detached', session_id=self.new_session)
        elif self.recovered:
            result.update(state='not_registered', session_id='-')
        return result

    def test_auto_recovers_only_verified_stale_anchor(self):
        view, anchor, changed = recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertTrue(changed)
        self.assertEqual(view['full_id'], self.new_session)
        self.assertEqual(anchor['pid'], 222)
        self.assertEqual([v for v, _ in self.history], ['recover', 'start'])
        self.assertEqual(self.store.jobs['49']['status'], 'approved-project-screen')

    def test_ready_anchor_is_not_restarted(self):
        self.noop = True
        view, anchor, changed = recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertFalse(changed)
        self.assertEqual(self.history, [])

    def test_non_interrupted_status_cannot_trigger_recovery(self):
        for state in ('attached', 'detached', 'not_registered', 'unconfirmed'):
            with self.subTest(state=state):
                self.status['state'] = state
                with self.assertRaises(recovery.RecoveryRefused):
                    recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
                self.assertEqual(self.history, [])

    def test_existing_unknown_claim_blocks_repair(self):
        self.store.jobs['18'] = {**self.item, 'number': 18, 'status': 'unknown'}
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'Unresolved'):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_claimed_task_never_recovers_or_replays(self):
        self.store.jobs['49']['status'] = 'running'
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'approval changed'):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_wrong_old_record_refuses_recovery(self):
        self.old['view']['full_id'] = '888.g2c-k2-smb'
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'identity differ'):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_missing_verified_anchor_never_bootstraps_screen(self):
        slots.read = lambda *_args: {'view': {'project': 'k2-smb'}, 'number': 0}
        with self.assertRaises(recovery.RecoveryRefused):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_old_anchor_still_alive_blocks_repair(self):
        self.alive = True
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'still be active'):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_earlier_approval_blocks_recovery_fifo(self):
        self.store.jobs['48'] = {**self.item, 'number': 48, 'status': 'approved-project-screen'}
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'Earlier approved'):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.history, [])

    def test_partial_restore_cannot_start_claim_or_recover_again(self):
        def fail_start(_proof, _project, action, *args):
            self.history.append(action)
            if action == 'recover':
                self.recovered = True
                return 'screen_recover=ok'
            raise recovery.RecoveryRefused('start refused')
        with mock.patch.object(recovery, '_cli', side_effect=fail_start):
            with self.assertRaisesRegex(recovery.RecoveryRefused, 'start refused'):
                recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)
        self.assertEqual(self.store.jobs['49']['status'], 'approved-project-screen')
        self.assertEqual(self.history, ['recover', 'start'])
        with self.assertRaises(recovery.RecoveryRefused):
            recovery.ensure_anchor(self.store, self.cfg, self.item, self.root, self.proof)

    def test_screen_cli_exec_is_pinned_and_sanitized(self):
        self.cli_patcher.stop()
        self.status_patcher.stop()
        captured = {}
        def fake_run(argv, **kwargs):
            captured['args'] = argv
            captured['kwargs'] = kwargs
            return subprocess.CompletedProcess(argv, 0, b'project_id=k2-smb screen_name=g2c-k2-smb state=interrupted session_id=101.g2c-k2-smb task=Queue anchor\n', b'')
        with mock.patch.object(recovery.subprocess, 'run', side_effect=fake_run):
            fields = recovery._status(self.proof, 'k2-smb')
        self.assertEqual(fields['state'], 'interrupted')
        self.assertEqual(captured['args'][:3], ['/usr/bin/gpt2codex','screen','status'])
        self.assertEqual(captured['kwargs']['stdin'], subprocess.DEVNULL)
        self.assertEqual(captured['kwargs']['env'], {})


class WorkerOperatorTests(unittest.TestCase):
    """Exercise the live worker logic when running inside the project checkout."""

    @classmethod
    def setUpClass(cls):
        filename = (_G2C_REPO / 'gpt2codex-github-queue.py')
        if not filename.is_file():
            raise unittest.SkipTest('Worker source not available outside repository checkout')
        spec = importlib.util.spec_from_file_location('queue_recovery_worker_under_test', filename)
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def setUp(self):
        self.job = {'number': 49, 'id': 1049, 'project': 'k2-smb', 'digest': 'a'*64,
                    'status': 'approved-project-screen', 'backend': 'project-screen',
                    'title': 'Queue recovery fixture', 'mode': 'read-only'}
        self.store = Store(self.job)

    def test_block_is_diagnostic_not_consumed_approval(self):
        q = self.worker
        q._remember_block(self.store, 49, 'project-screen',
                          'project-screen waiting: original screen unavailable')
        job = self.store.jobs['49']
        self.assertEqual(job['status'], 'approved-project-screen')
        self.assertEqual(job['block']['code'], 'environment_missing')
        self.assertNotIn('/proc', json.dumps(job['block']))
        with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            q.gui_list(self.store)
        listed = json.loads(output.getvalue())
        self.assertEqual(listed[0]['display_status'], 'blocked')
        self.assertEqual(listed[0]['block']['code'], 'environment_missing')

    def test_resume_only_clears_unclaimed_block(self):
        q = self.worker
        q._remember_block(self.store, 49, 'project-screen', 'project-screen waiting: anchor absent')
        with mock.patch('sys.stdout', new_callable=io.StringIO):
            q.operator_action(self.store, 49, 'resume', 'a'*64, 'RESUME 49')
        job = self.store.jobs['49']
        self.assertEqual(job['status'], 'approved-project-screen')
        self.assertNotIn('block', job)
        self.assertIn('operator_retry_at', job)

    def test_discard_preserves_metadata_and_denies_future_claim(self):
        q = self.worker
        q._remember_block(self.store, 49, 'project-screen', 'project-screen waiting: anchor absent')
        with mock.patch('sys.stdout', new_callable=io.StringIO):
            q.operator_action(self.store, 49, 'discard', 'a'*64, 'DISCARD 49')
        job = self.store.jobs['49']
        self.assertEqual(job['status'], 'discarded')
        self.assertIn('block', job)
        self.assertIn('discarded_at', job)
        with self.assertRaises(q.QueueError):
            q.approved_backend(job)

    def test_unknown_or_running_may_not_resume_or_discard(self):
        q = self.worker
        for status in ('running', 'unknown'):
            for decision in ('resume', 'discard'):
                with self.subTest(status=status, decision=decision):
                    self.store.jobs['49']['status'] = status
                    with self.assertRaises(q.QueueError):
                        q.operator_action(self.store, 49, decision, 'a'*64,
                                          decision.upper()+' 49')
                    self.assertEqual(self.store.jobs['49']['status'], status)

    def test_digest_and_confirmation_are_exact(self):
        q = self.worker
        for digest, confirmation in [('b'*64,'DISCARD 49'),('a'*64,'DISCARD 50'),
                                     ('a'*63,'DISCARD 49')]:
            with self.subTest(digest=digest[:4], confirmation=confirmation):
                with self.assertRaises(q.QueueError):
                    q.operator_action(self.store, 49, 'discard', digest, confirmation)
        self.assertEqual(self.store.jobs['49']['status'], 'approved-project-screen')


if __name__ == '__main__':
    unittest.main(verbosity=2)
