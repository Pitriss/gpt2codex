#!/usr/bin/env python3
"""Offline candidate tests. Only owned fixture processes; no mux/model/network."""
import importlib.util
import multiprocessing
import os
from pathlib import Path

# Repository imports remain explicit when this test is run directly.
import sys as _g2c_sys
_G2C_REPO = Path(__file__).resolve().parents[2]
if str(_G2C_REPO) not in _g2c_sys.path:
    _g2c_sys.path.insert(0, str(_G2C_REPO))
import tempfile
import unittest
import uuid
from unittest import mock

import gpt2codex_queue_dispatch as dispatch

spec = importlib.util.spec_from_file_location('queue_base', Path(__file__).with_name('gpt2codex-github-queue-test.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
queue = base.module


def fixture_worker(directory, key, observation, ready, release, complete=True):
    ledger = dispatch.Ledger(directory)
    job = ledger.reserve(key, observation)
    if job:
        ledger.bind_process(key, job['claim'], 7, dispatch.process_identity(os.getpid()))
        ready.put((key, job['claim']))
        if not release.wait(10):
            raise RuntimeError('Fixture timed out')
        if complete:
            ledger.finish_turn(key, job['claim'], str(uuid.uuid4()), key + '.output')
    else:
        ready.put((key, None))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        # tempfile parent explicitly stays in the existing project .tmp.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ledger = dispatch.Ledger(self.root)
        for name in ('a', 'b', 'c'):
            (self.root / name).mkdir()

    def submit(self, key, project='a', mode='read-only', source='queue', root=None):
        self.ledger.submit(key, issue=int(key[1:]), digest='a' * 64, repository='owner/repo',
                           project=project, root=root or str(self.root / project),
                           hostname='fixture', mode=mode, source=source)
        self.ledger.approve(key)

    def observation(self, project='a', **changes):
        return dict(root=str(self.root / project), project=project, verified=True,
                    enabled=True, session='123.g2c-' + project, server_ticks=123,
                    codex='absent', **changes)

    def job(self, key):
        return next(j for j in self.ledger.summary()['jobs'] if j['key'] == key)

    def test_two_projects_have_simultaneously_live_processes(self):
        self.submit('j1')
        self.submit('j2', 'b', 'workspace-write')
        context = multiprocessing.get_context('fork')
        ready, release = context.Queue(), context.Event()
        procs = [context.Process(target=fixture_worker,
                                 args=(str(self.root), key, self.observation(project), ready, release))
                 for key, project in (('j1', 'a'), ('j2', 'b'))]
        try:
            for proc in procs:
                proc.start()
            claims = dict(ready.get(timeout=10) for _ in procs)
            self.assertTrue(all(claims.values()))
            self.assertTrue(all(proc.is_alive() for proc in procs))
            self.assertEqual([j['status'] for j in self.ledger.summary()['jobs']], ['running', 'running'])
            self.assertTrue(all(dispatch.inspect_process(j['process']) == 'live'
                                for j in self.ledger.summary()['jobs']))
            release.set()
            for proc in procs:
                proc.join(10)
                self.assertEqual(proc.exitcode, 0)
            self.ledger = dispatch.Ledger(self.root)
            self.ledger.recover()
            for job in self.ledger.summary()['jobs']:
                self.assertEqual(job['status'], 'finishing')
                self.assertEqual(job['waiting_reason'], 'window_verification')
                for session, window in (('123.g2c-wrong', 7), (job['screen_session'], 8)):
                    with self.assertRaises(dispatch.DispatchError):
                        self.ledger.close(job['key'], claims[job['key']], session, window,
                                          window_closed=True)
                self.ledger.close(job['key'], claims[job['key']], job['screen_session'],
                                  7, window_closed=True)
            jobs = self.ledger.summary()['jobs']
            self.assertEqual([j['status'] for j in jobs], ['done', 'done'])
            self.assertEqual(len({j['thread_uuid'] for j in jobs}), 2)
            self.assertTrue(all(j['lifecycle'] == 'closed' for j in jobs))
        finally:
            release.set()
            for proc in procs:
                if proc.pid is not None:
                    proc.join(10)
                    if proc.is_alive():
                        proc.terminate()  # Only this test's owned fixture.
                        proc.join(5)
            ready.close()

    def test_simultaneous_same_project_reservations_never_overlap(self):
        self.submit('j1')
        self.submit('j2', mode='workspace-write')
        context = multiprocessing.get_context('fork')
        ready, release = context.Queue(), context.Event()
        procs = [context.Process(target=fixture_worker,
                                 args=(str(self.root), key, self.observation(), ready, release))
                 for key in ('j1', 'j2')]
        try:
            for proc in procs:
                proc.start()
            claims = dict(ready.get(timeout=10) for _ in procs)
            self.assertTrue(claims['j1'])
            self.assertIsNone(claims['j2'])
            release.set()
            for proc in procs:
                proc.join(10)
                self.assertEqual(proc.exitcode, 0)
            self.ledger.close('j1', claims['j1'], '123.g2c-a', 7, window_closed=True)
            self.assertIsNotNone(self.ledger.reserve('j2', self.observation()))
        finally:
            release.set()
            for proc in procs:
                if proc.pid is not None:
                    proc.join(10)
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(5)
            ready.close()

    def test_fifo_even_when_later_job_attempts_first(self):
        self.submit('j9')
        self.submit('j1', mode='workspace-write')
        self.assertIsNone(self.ledger.reserve('j1', self.observation()))
        self.assertEqual(self.job('j1')['waiting_reason'], 'project_fifo')
        self.assertIsNotNone(self.ledger.reserve('j9', self.observation()))
        self.assertIsNone(self.ledger.reserve('j1', self.observation()))
        self.assertEqual(self.job('j1')['waiting_reason'], 'project_busy')

    def test_manual_active_and_idle_both_block(self):
        self.submit('j1')
        for state in ('active', 'idle'):
            obs = self.observation()
            obs['codex'] = state
            self.assertIsNone(self.ledger.reserve('j1', obs))
            self.assertEqual(self.job('j1')['waiting_reason'], 'project_busy')

    def test_queue_blocks_manual_and_resume_reservations(self):
        self.submit('j1')
        self.assertIsNotNone(self.ledger.reserve('j1', self.observation()))
        for key, source in (('j2', 'manual'), ('j3', 'resume')):
            self.submit(key, source=source)
            self.assertIsNone(self.ledger.reserve(key, self.observation()))
            self.assertEqual(self.job(key)['waiting_reason'], 'project_busy')

    def test_manual_blocks_other_manual_and_queue(self):
        self.submit('j1', source='manual')
        self.ledger.reserve('j1', self.observation())
        for key, source in (('j2', 'manual'), ('j3', 'queue')):
            self.submit(key, source=source)
            self.assertIsNone(self.ledger.reserve(key, self.observation()))

    def test_capacity_and_restart_keep_unresolved_claims(self):
        self.submit('j1')
        self.submit('j2', 'b')
        self.submit('j3', 'c')
        self.ledger.reserve('j1', self.observation())
        self.ledger.reserve('j2', self.observation('b'))
        self.ledger = dispatch.Ledger(self.root)
        self.ledger.recover()
        self.assertIsNone(self.ledger.reserve('j3', self.observation('c')))
        self.assertEqual(self.job('j3')['waiting_reason'], 'capacity')
        self.assertEqual(self.job('j1')['status'], 'interrupted_unknown')
        with self.assertRaises(dispatch.DispatchError):
            self.ledger.reserve('j1', self.observation())
        with self.assertRaises(dispatch.DispatchError):
            dispatch.Ledger(self.root, capacity=3)

    def test_one_crashed_project_does_not_block_other_project(self):
        self.submit('j1')
        self.ledger.reserve('j1', self.observation())
        self.ledger.recover()
        self.submit('j2', 'b')
        self.assertIsNotNone(self.ledger.reserve('j2', self.observation('b')))

    def test_process_dies_without_completion_no_replay_or_release(self):
        self.submit('j1')
        context = multiprocessing.get_context('fork')
        ready, release = context.Queue(), context.Event()
        release.set()
        proc = context.Process(target=fixture_worker,
                               args=(str(self.root), 'j1', self.observation(), ready, release, False))
        proc.start()
        try:
            key, claim = ready.get(timeout=10)
            proc.join(10)
            self.assertEqual(proc.exitcode, 0)
            self.ledger = dispatch.Ledger(self.root)
            self.ledger.recover()
            self.assertEqual(self.job(key)['status'], 'interrupted_unknown')
            self.assertIsNone(self.job(key)['thread_uuid'])
            with self.assertRaises(dispatch.DispatchError):
                self.ledger.reserve(key, self.observation())
            with self.assertRaises(dispatch.DispatchError):
                self.ledger.close(key, claim, '123.g2c-a', 7, window_closed=True)
            self.submit('j2', mode='workspace-write')
            self.assertIsNone(self.ledger.reserve('j2', self.observation()))
            self.submit('j3', 'b')
            self.assertIsNotNone(self.ledger.reserve('j3', self.observation('b')))
        finally:
            proc.join(10)
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
            ready.close()

    def test_session_and_disabled_fail_closed(self):
        self.submit('j1')
        for field, value, reason in (
                ('enabled', False, 'project_disabled'), ('verified', False, 'session_unverified'),
                ('session', '123.g2c-q18', 'session_unverified'),
                ('server_ticks', 0, 'session_unverified'), ('codex', 'unknown', 'session_unverified'),
                ('root', str(self.root / 'b'), 'session_unverified')):
            obs = self.observation()
            obs[field] = value
            self.assertIsNone(self.ledger.reserve('j1', obs))
            self.assertEqual(self.job('j1')['waiting_reason'], reason)

    def test_only_owned_identity_can_finish_and_live_process_cannot_release(self):
        self.submit('j1')
        claim = self.ledger.reserve('j1', self.observation())['claim']
        proc = dispatch.process_identity(os.getpid())
        self.ledger.bind_process('j1', claim, 7, proc)
        with self.assertRaises(dispatch.DispatchError):
            self.ledger.finish_turn('j1', 'wrong', str(uuid.uuid4()), 'output')
        self.ledger.finish_turn('j1', claim, str(uuid.uuid4()), 'output')
        for session, window, closed in (('123.g2c-a', 7, True),
                                        ('123.g2c-b', 7, True), ('123.g2c-a', 8, True),
                                        ('123.g2c-a', 7, False)):
            with self.assertRaises(dispatch.DispatchError):
                self.ledger.close('j1', claim, session, window, window_closed=closed)
        self.assertEqual(self.job('j1')['status'], 'finishing')
        self.ledger.recover()
        self.assertEqual(self.job('j1')['lifecycle'], 'live')

    def test_uuid_reuse_rejected_and_no_context_carried(self):
        thread = str(uuid.uuid4())
        for key, project in (('j1', 'a'), ('j2', 'b')):
            self.submit(key, project)
            claim = self.ledger.reserve(key, self.observation(project))['claim']
            self.assertIsNone(self.job(key)['thread_uuid'])
            self.ledger.bind_process(key, claim, 7, dispatch.process_identity(os.getpid()))
            if key == 'j1':
                self.ledger.finish_turn(key, claim, thread, 'output')
            else:
                with self.assertRaises(dispatch.DispatchError):
                    self.ledger.finish_turn(key, claim, thread, 'output')

    def test_alias_root_uses_same_exclusive_reservation(self):
        self.submit('j1')
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'a', target_is_directory=True)
        self.submit('j2', 'b', root=str(alias))
        self.ledger.reserve('j1', self.observation())
        self.assertIsNone(self.ledger.reserve('j2', self.observation('b')))
        self.assertEqual(self.job('j2')['waiting_reason'], 'project_busy')

    def test_duplicate_identity_and_nested_roots_rejected(self):
        self.submit('j1')
        with self.assertRaises(dispatch.DispatchError):
            self.ledger.submit('other', issue=1, digest='a' * 64, repository='owner/repo',
                               project='a', root=str(self.root / 'a'), hostname='fixture', mode='read-only')
        (self.root / 'a' / 'nested').mkdir()
        with self.assertRaises(dispatch.DispatchError):
            self.submit('j2', 'b', root=str(self.root / 'a' / 'nested'))

    def test_process_probe_unknown_is_not_exit(self):
        self.assertEqual(dispatch.inspect_process(None), 'unknown')
        saved = dispatch.process_identity(os.getpid())
        self.assertEqual(dispatch.inspect_process(saved), 'live')
        with mock.patch.object(dispatch, 'process_identity', side_effect=FileNotFoundError):
            self.assertEqual(dispatch.inspect_process(saved), 'unknown')
        saved['pidns'] = 'unverified'
        self.assertEqual(dispatch.inspect_process(saved), 'unknown')

    def test_reused_pid_cannot_release_finished_reservation(self):
        self.submit('j1')
        job = self.ledger.reserve('j1', self.observation())
        saved = dispatch.process_identity(os.getpid())
        self.ledger.bind_process('j1', job['claim'], 3, saved)
        self.ledger.finish_turn('j1', job['claim'], str(uuid.uuid4()), 'output')
        reused = {**saved, 'ticks':saved['ticks'] + 1}
        with mock.patch.object(dispatch, 'process_identity', return_value=reused):
            self.assertEqual(dispatch.inspect_process(saved), 'unknown')
            with self.assertRaises(dispatch.DispatchError):
                self.ledger.close('j1', job['claim'], job['screen_session'], 3,
                                  window_closed=True)
            self.ledger.recover()
        self.assertEqual(self.job('j1')['status'], 'interrupted_unknown')
        self.submit('j2')
        self.assertIsNone(self.ledger.reserve('j2', self.observation()))

    def test_state_and_event_rollback_are_atomic(self):
        self.submit('j1')
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction() as db:
                job = self.ledger._get(db, 'j1')
                self.ledger._save(db, job, 'running')
                raise RuntimeError('simulated worker crash before commit')
        self.assertEqual(self.job('j1')['status'], 'approved')
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM events ORDER BY seq').fetchall(),
                             [('pending',), ('approved',)])

    def test_unknown_state_refused(self):
        self.submit('j1')
        with self.ledger.transaction() as db:
            job = self.ledger._get(db, 'j1')
            with self.assertRaises(dispatch.DispatchError):
                self.ledger._save(db, job, 'surprise')


class BackendGateTests(base.QueueTests):
    def test_worker_preserves_explicit_approval_backend_after_restart(self):
        api = base.FakeGitHub([base.make_issue()])
        queue.scan(api, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state['jobs']['1'].update(status='approved', backend='project-screen')
            self.store.save(state)
        before = self.store.file.read_bytes()
        # Current default exec must never reinterpret an explicit new-backend
        # approval, including after re-opening the worker's persistent Store.
        with mock.patch.object(queue, 'run_exec') as execute, \
                mock.patch.object(queue.subprocess, 'Popen') as launch:
            for _ in range(2):
                queue.run_approved(api, queue.Store(self.root), self.cfg)
        execute.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.store.file.read_bytes(), before)
        self.assertFalse(api.comments)
        self.assertFalse(api.closed)

    def test_watch_restart_never_reinterprets_new_backend_as_exec(self):
        api = base.FakeGitHub([base.make_issue()])
        queue.scan(api, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state['jobs']['1'].update(status='approved', backend='project-screen')
            self.store.save(state)
        before = self.store.file.read_bytes()
        with mock.patch.object(queue, 'load_config', return_value=self.cfg), \
                mock.patch.object(queue, 'read_token', return_value='unused-fixture'), \
                mock.patch.object(queue, 'GitHub', return_value=api), \
                mock.patch.object(queue.time, 'sleep', side_effect=KeyboardInterrupt), \
                mock.patch.object(queue.subprocess, 'Popen') as launch:
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    queue.main(['--repo-root', str(self.root), 'watch', '--interval', '30'])
        launch.assert_not_called()
        self.assertEqual(self.store.file.read_bytes(), before)
        self.assertFalse(api.comments)
        self.assertFalse(api.closed)

    def test_backend_status_mismatch_denies_before_remote_or_claim(self):
        for status, binding, requested in (
                ('approved', 'screen', 'screen'),
                ('approved-screen', 'exec', 'exec'),
                ('approved', 'exec', 'screen')):
            with self.subTest(status=status, binding=binding, requested=requested):
                with self.store.lock():
                    state = self.store.load()
                    state['jobs']['1'] = dict(number=1, status=status, backend=binding)
                    self.store.save(state)
                before = self.store.file.read_bytes()
                api = mock.Mock()
                with self.assertRaises(queue.QueueError):
                    queue.execute_claimed(api, self.store, self.cfg, 1, requested)
                self.assertEqual(api.mock_calls, [])
                self.assertEqual(self.store.file.read_bytes(), before)

    def test_shared_stop_gate_covers_python_launches_before_side_effects(self):
        import gpt2codex_queue_screen as screen
        for flag in ('G2C_PROJECT_SUPERVISOR', 'G2C_PROJECT_SUPERVISOR_DIRECTORY'):
            with self.subTest(flag=flag), mock.patch.dict(os.environ, {flag: ''}), \
                    mock.patch.object(queue.subprocess, 'Popen') as launch, \
                    mock.patch.object(screen, 'require_chatgpt') as auth:
                before = self.store.load()
                api = mock.Mock()
                with self.assertRaises(queue.QueueError):
                    queue.execute_claimed(api, self.store, self.cfg, 1, 'exec')
                out, err = self.root / 'out', self.root / 'err'
                with self.assertRaises(queue.QueueError):
                    queue.run_exec([], self.project, {}, {}, out, err)
                with self.assertRaises(dispatch.DispatchError):
                    screen.launch(self.store, {}, str(self.bin), str(self.project), '')
                with self.assertRaises(dispatch.DispatchError):
                    screen.runner(self.root)
                self.assertEqual(api.mock_calls, [])
                self.assertEqual(self.store.load(), before)
                self.assertFalse(out.exists())
                self.assertFalse(err.exists())
                self.assertFalse((self.root / 'runner.claim').exists())
                launch.assert_not_called()
                auth.assert_not_called()

    def test_child_environment_stop_gate_cannot_be_bypassed(self):
        import gpt2codex_queue_screen as screen
        env = {'G2C_PROJECT_SUPERVISOR_DIRECTORY': ''}
        with mock.patch.object(queue.subprocess, 'Popen') as launch, \
                mock.patch.object(screen, 'require_chatgpt') as auth:
            with self.assertRaises(queue.QueueError):
                queue.run_exec([], self.project, env, {}, self.root / 'out', self.root / 'err')
            with self.assertRaises(dispatch.DispatchError):
                screen.launch(self.store, {}, str(self.bin), str(self.project), '', env)
        launch.assert_not_called()
        auth.assert_not_called()

    def test_completed_dummy_turn_cannot_resume_past_new_stop_gate(self):
        import io
        import json
        import gpt2codex_queue_screen as screen
        slot = self.root / 'owned-slot'
        slot.mkdir(mode=0o700)
        prompt = self.root / 'prompt'
        prompt.write_text('dummy')
        prompt.chmod(0o600)
        thread = str(uuid.uuid4())
        screen.write(slot / 'spec.json', dict(number=1, codex=str(self.bin), mode='read-only',
                                             cwd=str(self.project), prompt=str(prompt)))
        child = mock.Mock()
        child.stdout = io.BytesIO(b''.join(json.dumps(event).encode() + b'\n' for event in (
            {'type': 'thread.started', 'thread_id': thread},
            {'type': 'turn.started'}, {'type': 'turn.completed'})))
        def finish(*args, **kwargs):
            os.environ['G2C_PROJECT_SUPERVISOR_DIRECTORY'] = ''
            return 0
        child.wait.side_effect = finish
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(screen, 'require_chatgpt'), \
                mock.patch.object(screen.subprocess, 'Popen', return_value=child) as launch:
            self.assertEqual(screen.runner(slot), 2)
        self.assertEqual(launch.call_count, 1)
        result = screen.read(slot / 'result.json')
        self.assertEqual((result['thread_id'], result['turns_completed']), (thread, 1))
        self.assertEqual(screen.read(slot / 'phase.json')['phase'], 'tui_failed')
        self.assertFalse((slot / 'resume.json').exists())
        # Exclusive runner marker still prevents a replay after gate removal.
        with self.assertRaises(FileExistsError):
            screen.runner(slot)

    def test_new_backend_unavailable_before_any_remote_or_claim(self):
        self.cfg['execution_backend'] = 'project-screen'
        self.assertEqual(queue.execution_backend(self.cfg), 'project-screen')
        api = mock.Mock()
        before = self.store.load()
        with self.assertRaises(queue.QueueError):
            queue.approve(api, self.store, self.cfg, 1, 'a' * 12)
        with self.assertRaises(queue.QueueError):
            queue.run_approved(api, self.store, self.cfg)
        with self.assertRaises(queue.QueueError):
            queue.execute_claimed(api, self.store, self.cfg, 1, 'project-screen')
        self.assertEqual(api.mock_calls, [])
        self.assertEqual(self.store.load(), before)
        self.assertEqual(queue.worker_ready(self.cfg, self.store), 2)

    def test_cli_gate_never_reads_token_or_uses_legacy_fallback(self):
        self.cfg['execution_backend'] = 'project-screen'
        with mock.patch.object(queue, 'load_config', return_value=self.cfg), \
                mock.patch.object(queue, 'read_token') as token, \
                mock.patch.object(queue, 'run_exec') as execute:
            for args in (['watch'], ['run-approved'], ['approve', '1', 'a' * 12], ['check']):
                with self.assertRaises(queue.QueueError):
                    queue.main(['--repo-root', str(self.root), *args])
        token.assert_not_called()
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
