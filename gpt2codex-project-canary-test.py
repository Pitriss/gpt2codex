#!/usr/bin/env python3
"""Offline regression checks for the opt-in project-screen Queue canary.

Uses no GitHub, Codex, screen, state outside temporary directories or network.
"""
import contextlib
import importlib.util
import io
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import gpt2codex_queue_screen as slots
import gpt2codex_queue_project as project


def queue_module():
    # The uploaded archive intentionally excludes the original handoff module.
    # Inject a harmless stub only for offline control-plane tests.
    sys.modules.setdefault('gpt2codex_queue_handoff', types.ModuleType('gpt2codex_queue_handoff'))
    spec = importlib.util.spec_from_file_location('gpt2codex_queue_under_test',
                                                  Path(__file__).with_name('gpt2codex-github-queue.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Store:
    def __init__(self, root):
        self.log_dir = root / '.log/github-queue'
        self.tmp_dir = root / '.tmp/github-queue'
        self.state_dir = root / '.state'
        for name in [self.log_dir, self.tmp_dir, self.state_dir]:
            name.mkdir(parents=True, mode=0o700)
        self.data = {'version': 1, 'jobs': {}}

    @contextlib.contextmanager
    def lock(self):
        yield

    def load(self):
        return self.data

    def save(self, value):
        self.data = value


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.item = {'number': 27, 'id': 1010, 'title': 'Example', 'project': 'gpt2codex',
                     'digest': 'a' * 64, 'mode': 'read-only', 'task': 'Summarize project'}
        self.store.data['jobs']['27'] = {'status': 'running', 'number': 27,
                                         'digest': 'a' * 64, 'backend': 'project-screen'}

    def test_unenabled_backend_rejected_and_status_exact(self):
        q = queue_module()
        with self.assertRaises(q.QueueError):
            q.require_runnable_backend('project-screen', {'project_screen_canary': False})
        self.assertEqual(q.approved_backend({'status': 'approved-project-screen',
                                              'backend': 'project-screen'}), 'project-screen')
        with self.assertRaises(q.QueueError):
            q.approved_backend({'status': 'approved-project-screen', 'backend': 'exec'})
        with self.assertRaises(q.QueueError):
            q.approved_backend({'status': 'approved', 'backend': 'project-screen'})

    def test_list_active_includes_pending_waiting_running_and_unknown(self):
        q = queue_module()
        self.store.data['jobs'] = {
            str(i): {'number': i, 'status': state, 'project': 'gpt2codex', 'mode': 'read-only',
                     'title': 'Issue', 'digest': 'a' * 64}
            for i, state in enumerate(['pending', 'running', 'waiting', 'unknown',
                                       'done', 'rejected', 'failed'], 1)
        }
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            q.show_jobs(self.store, active_only=True)
        result = out.getvalue()
        for number in (1, 2, 3, 4):
            self.assertIn(f'#{number} ', result)
        for number in (5, 6, 7):
            self.assertNotIn(f'#{number} ', result)

    def test_prepare_slot_binds_project_before_any_screen_spawn(self):
        slot = slots.prepare_slot(self.store, self.item, '/usr/bin/true', str(self.root),
                                  'read only task', backend='project-screen')
        spec = slots.read(slot / 'spec.json')
        self.assertEqual(spec['backend'], 'project-screen')
        self.assertEqual(self.store.data['jobs']['27']['screen_slot'], slot.name)
        self.assertEqual(slots.read(slot / 'phase.json')['phase'], 'issue_accepted')
        self.assertFalse((slot / 'runner.claim').exists())
        self.assertFalse((slot / 'result.json').exists())

    def test_project_runner_does_not_resume_interactive_codex(self):
        slot = self.store.log_dir / 'screens/q27-aaaaaaaaaaaa'
        slot.mkdir(parents=True, mode=0o700)
        prompt = self.store.tmp_dir / 'task.prompt'
        prompt.write_text('task\n')
        prompt.chmod(0o600)
        slots.write(slot / 'spec.json', {'backend': 'project-screen', 'number': 27,
                                         'project': 'gpt2codex', 'digest': 'a' * 64,
                                         'mode': 'read-only', 'codex': '/not/a/real/codex',
                                         'cwd': str(self.root), 'prompt': str(prompt)})
        tid = '01a1c4cd-dac1-7531-af18-8f8f8bf95677'
        data = b''.join(json.dumps(e).encode() + b'\n' for e in [
            {'type': 'thread.started', 'thread_id': tid},
            {'type': 'turn.started'}, {'type': 'turn.completed'}])
        process = mock.Mock()
        process.stdout = io.BytesIO(data)
        process.wait.return_value = 0
        process.pid = 19
        with mock.patch.object(slots, 'require_chatgpt'), \
             mock.patch.object(slots, 'require_legacy_launch_allowed'), \
             mock.patch.object(slots, 'record_thread'), \
             mock.patch.object(slots.subprocess, 'Popen', return_value=process) as popen:
            self.assertEqual(slots.runner(slot), 0)
        self.assertEqual(popen.call_count, 1)  # No second Codex TUI process.
        result = slots.read(slot / 'result.json')
        self.assertEqual(result['thread_id'], tid)
        self.assertEqual(result['turns_completed'], 1)
        self.assertFalse((slot / 'resume.json').exists())

    def test_project_launch_never_falls_back_to_exec_and_waits_for_claim(self):
        view = {'root': str(self.root), 'full_id': '123.g2c-gpt2codex', 'project': 'gpt2codex'}
        anchor = {'number': 1, 'pid': 991}
        def action(_v, command):
            self.assertEqual(command[0:3], ['-X', 'screen', '-t'])
            self.assertEqual(command[-2:], ['project-runner', captured['slot'].name])
            (captured['slot'] / 'runner.claim').touch()
        captured = {}
        original = slots.prepare_slot
        def prepared(*args, **kwargs):
            captured['slot'] = original(*args, **kwargs)
            return captured['slot']
        with mock.patch.object(project, 'anchor_ready', return_value=(view, anchor)), \
             mock.patch.object(slots, 'require_chatgpt'), \
             mock.patch.object(project, '_screen', side_effect=action), \
             mock.patch.object(slots, 'prepare_slot', side_effect=prepared):
            slot, actual_view, _, title = project.launch(self.store, self.item,
                          '/usr/bin/true', str(self.root), 'task')
        self.assertEqual(actual_view, view)
        self.assertTrue(title.startswith('q27-'))
        self.assertEqual(slots.read(slot / 'spec.json')['backend'], 'project-screen')

    def test_busy_project_does_not_consume_approval_or_post(self):
        q = queue_module()
        self.store.data['jobs']['27']['status'] = 'approved-project-screen'
        api = mock.Mock()
        cfg = {'project_roots': {'gpt2codex': str(self.root)},
               'codex_bin': '/usr/bin/true', 'worker_hostname': 'test',
               'project_screen_canary': True}
        with mock.patch.object(q, 'require_runnable_backend'), \
             mock.patch.object(q, 'validated_remote', return_value=self.item), \
             mock.patch.object(project, 'anchor_ready',
                               side_effect=project.ProjectScreenError('project_busy')):
            with self.assertRaises(q.QueueError):
                q.execute_claimed(api, self.store, cfg, 27, 'project-screen')
        self.assertEqual(self.store.data['jobs']['27']['status'], 'approved-project-screen')
        api.comment.assert_not_called()

    def test_project_completion_never_routes_to_exec(self):
        q = queue_module()
        self.store.data['jobs']['27']['status'] = 'approved-project-screen'
        api = mock.Mock()
        cfg = {'project_roots': {'gpt2codex': str(self.root)},
               'codex_bin': '/usr/bin/true', 'worker_hostname': 'test',
               'project_screen_canary': True}
        slot = self.root / 'dummy-slot'
        result = {'exit_code': 0, 'thread_id': '01a1c4cd-dac1-7531-af18-8f8f8bf95677',
                  'turns_started': 1, 'turns_completed': 1}
        with mock.patch.object(q, 'require_runnable_backend'), \
             mock.patch.object(q, 'validated_remote', return_value=self.item), \
             mock.patch.object(project, 'anchor_ready', return_value=({}, {})), \
             mock.patch.object(slots, 'require_chatgpt'), \
             mock.patch.object(project, 'launch', return_value=(slot, {}, {}, 'q27-a')), \
             mock.patch.object(project, 'wait_turn', return_value=result), \
             mock.patch.object(q, 'prepare_report'), \
             mock.patch.object(q.handoff, 'render', return_value=('safe handoff', 'ok'), create=True), \
             mock.patch.object(q.handoff, 'instruction', return_value='', create=True), \
             mock.patch.object(q, 'run_exec', side_effect=AssertionError('exec fallback')), \
             mock.patch.object(q, 'publish_handoff', return_value=True), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            rc = q.execute_claimed(api, self.store, cfg, 27, 'project-screen')
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.data['jobs']['27']['status'], 'done')
        self.assertEqual(self.store.data['jobs']['27']['thread_id'], result['thread_id'])
        api.comment.assert_called_once()
        api.close.assert_called_once()

    def test_unix_peer_pid_is_verified(self):
        path = self.root / 'test.socket'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            project._peer(path, os.getpid())
            with self.assertRaises(project.ProjectScreenError):
                project._peer(path, os.getpid() + 1000)

    def test_anchor_ready_refuses_a_second_window(self):
        view = {'project': 'gpt2codex', 'root': str(self.root), 'pid': 123}
        anchor = {'view': view, 'number': 1, 'pid': 456,
                  'generation': {'ticks': 789}}
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, '_anchor_file', return_value=self.root / 'anchor'), \
             mock.patch.object(project.slots, 'read', return_value=anchor), \
             mock.patch.object(project.slots, 'generation', return_value={'ticks': 789}), \
             mock.patch.object(project, '_same_binary', return_value=True), \
             mock.patch.object(project, '_proc_stat', return_value=(123, 789)), \
             mock.patch.object(project.os, 'readlink', return_value=str(self.root)), \
             mock.patch.object(project, 'windows', return_value=['1:g2c-anchor', '2:manual-codex']):
            with self.assertRaisesRegex(project.ProjectScreenError, 'project_busy'):
                project.anchor_ready(self.store, 'gpt2codex', self.root)

    def test_window_parser_rejects_ambiguous_and_unexpected(self):
        v = {'project': 'p'}
        with mock.patch.object(project, '_screen', return_value='1:g2c-anchor'):
            self.assertEqual(project.windows(v), ['1:g2c-anchor'])
        for value in ('1:g2c-anchor 1:evil', '0:foo*', '0:foo 0:bar'):
            with mock.patch.object(project, '_screen', return_value=value):
                with self.assertRaises(project.ProjectScreenError):
                    project.windows(v)

    def _anchor_only_fixture(self, stale=True):
        view = {'project': 'gpt2codex', 'root': str(self.root),
                'pid': 27800, 'generation': {'pid': 27800, 'ticks': 900, 'boot': 'boot'}}
        new_gen = {'pid': 27801, 'ticks': 901, 'boot': 'boot'}
        anchor_dir = self.store.state_dir / 'project-anchors'
        anchor_dir.mkdir(mode=0o700)
        anchor_path = anchor_dir / 'gpt2codex.json'
        if stale:
            old = {'view': {'project': 'gpt2codex', 'root': str(self.root),
                            'pid': 18869, 'generation': {'pid': 18869, 'ticks': 120, 'boot': 'boot'}},
                   'number': 1, 'pid': 19310,
                   'generation': {'pid': 19310, 'ticks': 125, 'boot': 'boot'}}
            slots.write(anchor_path, old)
        return view, new_gen, anchor_path

    def _anchor_gen(self, pid):
        if pid in (18869, 19310):
            raise FileNotFoundError(pid)
        if pid == 27801:
            return {'pid': 27801, 'ticks': 901, 'boot': 'boot'}
        raise AssertionError(f'unexpected pid {pid}')

    def test_anchor_first_registers_without_spawning_any_window(self):
        view, generation, record = self._anchor_only_fixture(stale=False)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-anchor']), \
             mock.patch.object(project, '_anchor_pid', return_value=27801), \
             mock.patch.object(project, '_children', return_value=[27801]), \
             mock.patch.object(slots, 'generation', side_effect=self._anchor_gen), \
             mock.patch.object(project, '_screen', side_effect=AssertionError('no screen command')):
            saved = project.prepare_anchor(self.store, 'gpt2codex', self.root)
        self.assertEqual(saved['number'], 0)
        self.assertEqual(saved['pid'], 27801)
        self.assertEqual(slots.read(record), saved)

    def test_anchor_first_archives_dead_predecessor_and_retains_evidence(self):
        view, _, record = self._anchor_only_fixture(stale=True)
        previous = slots.read(record)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-anchor']), \
             mock.patch.object(project, '_anchor_pid', return_value=27801), \
             mock.patch.object(project, '_children', return_value=[27801]), \
             mock.patch.object(slots, 'generation', side_effect=self._anchor_gen), \
             mock.patch.object(project, '_screen', side_effect=AssertionError('no screen command')):
            saved = project.prepare_anchor(self.store, 'gpt2codex', self.root)
        archived = list(record.parent.glob('gpt2codex.stale-*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(slots.read(archived[0]), previous)
        self.assertEqual(slots.read(record), saved)

    def test_anchor_first_refuses_stale_record_while_old_process_lives(self):
        view, _, record = self._anchor_only_fixture(stale=True)
        previous = slots.read(record)
        original_gen = self._anchor_gen
        def old_live(pid):
            if pid == 18869:
                return previous['view']['generation']
            return original_gen(pid)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-anchor']), \
             mock.patch.object(project, '_anchor_pid', return_value=27801), \
             mock.patch.object(project, '_children', return_value=[27801]), \
             mock.patch.object(slots, 'generation', side_effect=old_live), \
             mock.patch.object(project, '_screen', side_effect=AssertionError('no screen command')):
            with self.assertRaisesRegex(project.ProjectScreenError, 'Old anchor record'):
                project.prepare_anchor(self.store, 'gpt2codex', self.root)
        self.assertEqual(slots.read(record), previous)
        self.assertFalse(list(record.parent.glob('gpt2codex.stale-*.json')))

    def test_anchor_first_refuses_unrelated_stale_record(self):
        view, _, record = self._anchor_only_fixture(stale=True)
        stale = slots.read(record)
        stale['view']['root'] = '/some/other/project'
        slots.write(record, stale)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-anchor']), \
             mock.patch.object(project, '_anchor_pid', return_value=27801), \
             mock.patch.object(project, '_children', return_value=[27801]), \
             mock.patch.object(slots, 'generation', side_effect=self._anchor_gen):
            with self.assertRaisesRegex(project.ProjectScreenError, 'Old anchor record'):
                project.prepare_anchor(self.store, 'gpt2codex', self.root)
        self.assertEqual(slots.read(record), stale)

    def test_old_manual_codex_screen_is_never_modified(self):
        view, _, record = self._anchor_only_fixture(stale=True)
        previous = slots.read(record)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-gpt2codex']), \
             mock.patch.object(project, '_screen', side_effect=AssertionError('must not spawn')):
            with self.assertRaisesRegex(project.ProjectScreenError, 'not anchor-first'):
                project.prepare_anchor(self.store, 'gpt2codex', self.root)
        self.assertEqual(slots.read(record), previous)

    def test_anchor_first_rejects_unexpected_child_and_extra_windows(self):
        view, _, record = self._anchor_only_fixture(stale=True)
        original = slots.read(record)
        with mock.patch.object(project, 'session', return_value=view), \
             mock.patch.object(project, 'windows', return_value=['0:g2c-anchor']), \
             mock.patch.object(project, '_anchor_pid', return_value=27801), \
             mock.patch.object(project, '_children', return_value=[27801, 27802]), \
             mock.patch.object(slots, 'generation', side_effect=self._anchor_gen):
            with self.assertRaisesRegex(project.ProjectScreenError, 'process tree'):
                project.prepare_anchor(self.store, 'gpt2codex', self.root)
        self.assertEqual(slots.read(record), original)

    def test_result_requires_window_closed(self):
        slot = self.root / 'result'
        slot.mkdir()
        slots.write(slot / 'result.json', {'phase': 'turn_finished', 'thread_id': 'u',
                                           'turns_started': 1, 'turns_completed': 1,
                                           'exit_code': 0})
        view = {'pid': 123}
        anchor = {'number': 1, 'pid': 456, 'generation': {'ticks': 789}}
        with mock.patch.object(project, '_recheck'), \
             mock.patch.object(project, '_children', return_value=[456]), \
             mock.patch.object(project.slots, 'generation', return_value={'ticks': 789}), \
             mock.patch.object(project, 'windows', side_effect=[['1:g2c-anchor', '2:q27-abc'],
                                                                  ['1:g2c-anchor']]):
            self.assertEqual(project.wait_turn(slot, view, anchor, 'q27-abc')['exit_code'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
