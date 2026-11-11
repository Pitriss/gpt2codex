#!/usr/bin/env python3
"""Offline tests for opt-in project-parallel Queue admission.

No GitHub, Codex, GNU Screen, home state, worker service or network access.
"""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import gpt2codex_queue_project as project
import gpt2codex_queue_screen as slots


def queue_module():
    sys.modules.setdefault('gpt2codex_queue_handoff', types.ModuleType('gpt2codex_queue_handoff'))
    source = Path(__file__).with_name('gpt2codex-github-queue.py')
    spec = importlib.util.spec_from_file_location('gpt2codex_parallel_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Store:
    def __init__(self, root):
        self.state_dir = root / '.state'
        self.tmp_dir = root / '.tmp' / 'github-queue'
        self.log_dir = root / '.log' / 'github-queue'
        for path in (self.state_dir, self.tmp_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data = {'version': 1, 'jobs': {}}
        self.guard = threading.RLock()

    @contextlib.contextmanager
    def lock(self):
        with self.guard:
            yield

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, value):
        self.data = copy.deepcopy(value)


class QueueParallelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dir_a = self.root / 'alpha'
        self.dir_b = self.root / 'beta'
        self.dir_a.mkdir()
        self.dir_b.mkdir()
        self.store = Store(self.root)
        self.q = queue_module()
        self.cfg = {'project_screen_canary': True,
                    'project_parallel_canary': True,
                    'project_parallel_capacity': 2,
                    'project_roots': {'alpha': str(self.dir_a), 'beta': str(self.dir_b)},
                    'codex_bin': '/usr/bin/true', 'worker_hostname': 'test'}
        self.api = mock.Mock()

    @staticmethod
    def item(number, name):
        return {'number': number, 'id': number + 1000, 'title': 'Fake task',
                'author': 'Pitriss', 'project': name, 'mode': 'read-only',
                'digest': str(number) * 64, 'task': 'Do nothing'}

    def approve_item(self, number, project_name, *, status='approved-project-screen'):
        item = self.item(number, project_name)
        self.store.data['jobs'][str(number)] = {
            'number': number, 'id': item['id'], 'title': item['title'],
            'author': 'Pitriss', 'project': project_name, 'mode': 'read-only',
            'digest': item['digest'], 'status': status, 'backend': 'project-screen',
            'worker_host': 'test'
        }
        return item

    def test_parallel_mode_is_strict_opt_in(self):
        self.assertFalse(self.q.parallel_enabled({}))
        self.assertEqual(self.q.parallel_capacity({}), 2)
        with self.assertRaises(self.q.QueueError):
            self.q.parallel_capacity({'project_parallel_capacity': True})
        with self.assertRaises(self.q.QueueError):
            self.q.parallel_capacity({'project_parallel_capacity': 5})

    def test_parallel_roots_must_not_overlap(self):
        self.q.validate_parallel_roots(self.cfg)
        nested = self.dir_a / 'inner'
        nested.mkdir()
        self.cfg['project_roots']['beta'] = str(nested)
        with self.assertRaisesRegex(self.q.QueueError, 'overlap'):
            self.q.validate_parallel_roots(self.cfg)

    def test_distinct_project_locks_are_simultaneous(self):
        with self.q.project_execution_lock(self.store, 'alpha'):
            with self.q.project_execution_lock(self.store, 'beta'):
                pass
        self.assertTrue((self.store.state_dir / 'project-alpha.execution.lock').exists())
        self.assertTrue((self.store.state_dir / 'project-beta.execution.lock').exists())

    def test_same_project_excluded_without_queuing_duplicate(self):
        with self.q.project_execution_lock(self.store, 'alpha'):
            with self.assertRaisesRegex(self.q.QueueError, 'project_busy'):
                with self.q.project_execution_lock(self.store, 'alpha'):
                    pass

    def test_legacy_and_parallel_exclude_each_other(self):
        with self.q.project_execution_lock(self.store, 'alpha'):
            with self.assertRaisesRegex(self.q.QueueError, 'Another queue execution'):
                with self.q.execution_lock(self.store):
                    pass
        with self.q.execution_lock(self.store):
            with self.assertRaisesRegex(self.q.QueueError, 'Legacy queue execution'):
                with self.q.project_execution_lock(self.store, 'beta'):
                    pass

    def test_unexpected_earlier_approval_blocks_same_project_fifo(self):
        self.approve_item(3, 'alpha')
        newer = self.approve_item(4, 'alpha')
        with mock.patch.object(self.q, 'require_runnable_backend'), \
             mock.patch.object(self.q, 'validated_remote', return_value=newer), \
             mock.patch.object(project, 'anchor_ready', return_value=({}, {})), \
             mock.patch.object(slots, 'require_chatgpt'):
            with self.assertRaisesRegex(self.q.QueueError, 'FIFO'):
                self.q.execute_claimed(self.api, self.store, self.cfg, 4, 'project-screen')
        self.assertEqual(self.store.data['jobs']['4']['status'], 'approved-project-screen')

    def test_unknown_claim_same_project_blocks_later_work(self):
        self.approve_item(3, 'alpha', status='unknown')
        newer = self.approve_item(4, 'alpha')
        with mock.patch.object(self.q, 'require_runnable_backend'), \
             mock.patch.object(self.q, 'validated_remote', return_value=newer), \
             mock.patch.object(project, 'anchor_ready', return_value=({}, {})), \
             mock.patch.object(slots, 'require_chatgpt'):
            with self.assertRaisesRegex(self.q.QueueError, 'unresolved project claim'):
                self.q.execute_claimed(self.api, self.store, self.cfg, 4, 'project-screen')
        self.assertEqual(self.store.data['jobs']['4']['status'], 'approved-project-screen')

    def test_legacy_running_claim_blocks_other_project(self):
        old = self.approve_item(3, 'alpha', status='running')
        self.store.data['jobs']['3']['backend'] = 'screen'
        newer = self.approve_item(4, 'beta')
        with mock.patch.object(self.q, 'require_runnable_backend'), \
             mock.patch.object(self.q, 'validated_remote', return_value=newer), \
             mock.patch.object(project, 'anchor_ready', return_value=({}, {})), \
             mock.patch.object(slots, 'require_chatgpt'):
            with self.assertRaisesRegex(self.q.QueueError, 'legacy claim'):
                self.q.execute_claimed(self.api, self.store, self.cfg, 4, 'project-screen')
        self.assertEqual(self.store.data['jobs']['4']['status'], 'approved-project-screen')

    def test_simulated_two_project_runs_actually_overlap(self):
        a = self.approve_item(7, 'alpha')
        b = self.approve_item(8, 'beta')
        tasks = {7: a, 8: b}
        gate = threading.Barrier(2, timeout=4)
        launched = []
        mutex = threading.Lock()

        def launch(store, item, codex, root, prompt, env):
            with mutex:
                launched.append(item['project'])
            gate.wait(timeout=4)  # Serial implementation fails this test.
            return (self.root / 'fake-slot', {}, {}, 'q-test')

        with mock.patch.object(self.q, 'require_runnable_backend'), \
             mock.patch.object(self.q, 'validated_remote', side_effect=lambda api, store, cfg, n, states: tasks[n]), \
             mock.patch.object(project, 'anchor_ready', return_value=({}, {})), \
             mock.patch.object(slots, 'require_chatgpt'), \
             mock.patch.object(project, 'launch', side_effect=launch), \
             mock.patch.object(project, 'wait_turn', return_value={
                 'exit_code': 0, 'thread_id': '01a1c4cd-dac1-7531-af18-8f8f8bf95677',
                 'turns_started': 1, 'turns_completed': 1}), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_a = pool.submit(self.q.run_one, self.api, self.store, self.cfg, 7, 'project-screen')
                future_b = pool.submit(self.q.run_one, self.api, self.store, self.cfg, 8, 'project-screen')
                self.assertEqual(future_a.result(timeout=5), 0)
                self.assertEqual(future_b.result(timeout=5), 0)
        self.assertCountEqual(launched, ['alpha', 'beta'])
        self.assertEqual(self.store.data['jobs']['7']['status'], 'done')
        self.assertEqual(self.store.data['jobs']['8']['status'], 'done')
        self.assertEqual(self.api.close.call_count, 2)

    def test_one_shot_drains_capacity_overflow_and_project_fifo(self):
        self.approve_item(7, 'alpha')
        self.approve_item(8, 'alpha')
        self.approve_item(9, 'beta')
        gamma = self.root / 'gamma'
        gamma.mkdir()
        self.cfg['project_roots']['gamma'] = str(gamma)
        self.approve_item(10, 'gamma')
        active = set()
        finished = []
        max_active = [0]
        guard = threading.Lock()
        sync = threading.Barrier(2, timeout=3)

        def fake_run(api, store, cfg, number, backend):
            job = self.store.data['jobs'][str(number)]
            project_name = job['project']
            with guard:
                self.assertNotIn(project_name, active)
                active.add(project_name)
                max_active[0] = max(max_active[0], len(active))
            if number in (7, 9):
                sync.wait(timeout=3)
            with guard:
                active.remove(project_name)
                finished.append(number)
            with store.lock():
                data = store.load()
                data['jobs'][str(number)]['status'] = 'done'
                store.save(data)

        with mock.patch.object(self.q, 'run_one', side_effect=fake_run), \
             mock.patch.object(self.q, 'reconcile_handoffs'), \
             mock.patch.object(self.q, 'require_runnable_backend'):
            self.q.run_approved(self.api, self.store, self.cfg)
        self.assertCountEqual(finished, [7, 8, 9, 10])
        self.assertGreaterEqual(max_active[0], 2)
        self.assertLessEqual(max_active[0], 2)
        self.assertLess(finished.index(7), finished.index(8))

    def test_one_shot_never_retries_blocked_fifo_head(self):
        self.approve_item(7, 'alpha')
        self.approve_item(8, 'alpha')
        self.approve_item(9, 'beta')
        called = []

        def fake_run(api, store, cfg, number, backend):
            called.append(number)
            if number == 9:
                with store.lock():
                    data = store.load()
                    data['jobs']['9']['status'] = 'done'
                    store.save(data)
        with mock.patch.object(self.q, 'run_one', side_effect=fake_run), \
             mock.patch.object(self.q, 'reconcile_handoffs'), \
             mock.patch.object(self.q, 'require_runnable_backend'):
            self.q.run_approved(self.api, self.store, self.cfg)
        self.assertCountEqual(called, [7, 9])
        self.assertEqual(self.store.data['jobs']['7']['status'], 'approved-project-screen')
        self.assertEqual(self.store.data['jobs']['8']['status'], 'approved-project-screen')

    def test_dispatcher_never_skips_blocked_first_project_issue(self):
        first = self.approve_item(7, 'alpha')
        second = self.approve_item(8, 'alpha')
        third = self.approve_item(9, 'beta')
        active = {}
        calls = []
        wait = threading.Event()

        def fake_run(api, store, cfg, number, backend):
            calls.append(number)
            wait.wait(3)
        with mock.patch.object(self.q, 'run_one', side_effect=fake_run), \
             mock.patch.object(self.q, 'reconcile_handoffs'), \
             mock.patch.object(self.q, 'require_runnable_backend'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.q.run_approved(self.api, self.store, self.cfg, pool=pool, inflight=active)
                self.assertEqual(set(active), {'alpha', 'beta'})
                self.assertEqual({v[0] for v in active.values()}, {7, 9})
                self.q.run_approved(self.api, self.store, self.cfg, pool=pool, inflight=active)
                self.assertEqual({v[0] for v in active.values()}, {7, 9})
                wait.set()
        self.assertNotIn(8, calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
