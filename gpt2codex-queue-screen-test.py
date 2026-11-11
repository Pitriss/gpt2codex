#!/usr/bin/env python3
"""Targeted offline regressions plus optional own-screen fixture (no real Codex)."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import gpt2codex_queue_screen as screen

spec = importlib.util.spec_from_file_location('queue_tests', Path(__file__).with_name('gpt2codex-github-queue-test.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
queue = base.module
THREAD = '0199a213-81c0-7800-8aa1-bbab2a035a53'


class EventTests(unittest.TestCase):
    def test_exactly_one_completed_turn(self):
        events = screen.Events()
        for value in ({'type':'thread.started', 'thread_id':THREAD}, {'type':'turn.started'}, {'type':'turn.completed'}):
            events.accept(value)
        self.assertTrue(events.success(0))
        self.assertFalse(events.success(1))
        with self.assertRaises(RuntimeError):
            events.accept({'type':'turn.started'})

    def test_ambiguous_and_missing_completion(self):
        events = screen.Events()
        events.accept({'type':'thread.started', 'thread_id':THREAD})
        with self.assertRaises(RuntimeError):
            events.accept({'type':'thread.started', 'thread_id':THREAD})
        events.accept({'type':'turn.started'})
        self.assertFalse(events.success(0))
        events.accept({'type':'turn.completed'})
        events.accept({'type':'turn.failed'})
        self.assertFalse(events.success(0))

    def test_completion_before_start_and_bad_uuid(self):
        with self.assertRaises(RuntimeError):
            screen.Events().accept({'type':'turn.completed'})
        with self.assertRaises(ValueError):
            screen.Events().accept({'type':'thread.started', 'thread_id':'not-a-uuid'})

    def test_environment_and_policy(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY':'test', 'CODEX_API_KEY':'test', 'STY':'test', 'TMUX':'test', 'XDG_STATE_HOME':'/tmp/dirty', 'SCREENDIR':'/tmp/dirty'}):
            env = screen.clean_env()
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('STY', env)
        self.assertNotIn('TMUX', env)
        self.assertNotIn('SCREENDIR', env)
        self.assertEqual(env['XDG_STATE_HOME'], str(Path.home() / '.local/state'))
        self.assertIn('read-only', screen.codex_options('read-only', '/work'))
        with self.assertRaises(RuntimeError):
            screen.codex_options('danger-full-access', '/work')

    def test_api_auth_refused(self):
        with mock.patch.object(screen.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=b'', stderr=b'Logged in using an API key')):
            with self.assertRaises(RuntimeError):
                screen.require_chatgpt('/mock', {})

    def test_slot_path_validation(self):
        for bad in ('../project', 'bridge', 'q1-abc', '/tmp/other'):
            with self.assertRaises(RuntimeError):
                screen.find_slot(Path.cwd(), bad)


class QueueScreenTests(base.QueueTests):
    def prepare_screen(self):
        api = base.FakeGitHub([base.make_issue()])
        queue.scan(api, self.store, self.cfg)
        with mock.patch.object(queue.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='SCHVALUJI 1'):
            digest = self.store.load()['jobs']['1']['digest']
            queue.approve(api, self.store, self.cfg, 1, digest[:12], 'screen')
        return api

    def test_screen_approval_consumed_by_poll_worker(self):
        api = self.prepare_screen()
        slot = Path('q1-0123456789ab')
        result = {'exit_code':0, 'thread_id':THREAD, 'turns_started':1, 'turns_completed':1}
        with mock.patch.object(screen, 'launch', return_value=slot) as launch, \
                mock.patch.object(screen, 'wait_turn', return_value=result), \
                mock.patch.object(queue, 'run_exec') as execute:
            queue.run_approved(api, self.store, self.cfg)
            queue.scan(api, self.store, self.cfg)
            queue.run_approved(api, self.store, self.cfg)
        launch.assert_called_once()
        execute.assert_not_called()
        job = self.store.load()['jobs']['1']
        self.assertEqual((job['status'], job['backend'], job['thread_id']), ('done', 'screen', THREAD))

    def test_watch_automatically_dispatches_approved_screen(self):
        api = self.prepare_screen()
        result = {'exit_code':0, 'thread_id':THREAD, 'turns_started':1, 'turns_completed':1}
        with mock.patch.object(queue, 'load_config', return_value=self.cfg), \
                mock.patch.object(queue, 'read_token', return_value='synthetic'), \
                mock.patch.object(queue, 'GitHub', return_value=api), \
                mock.patch.object(queue, 'Store', return_value=self.store), \
                mock.patch.object(queue.time, 'sleep', side_effect=KeyboardInterrupt), \
                mock.patch.object(screen, 'launch', return_value=Path('q1-0123456789ab')) as launch, \
                mock.patch.object(screen, 'wait_turn', return_value=result):
            with self.assertRaises(KeyboardInterrupt):
                queue.main(['--repo-root', str(self.root), 'watch'])
        launch.assert_called_once()
        self.assertEqual(self.store.load()['jobs']['1']['status'], 'done')

    def test_offline_readiness_missing_screen_and_config_legacy(self):
        import io
        cfg_path = self.root / 'queue.json'
        cfg = queue.config_default('Pitriss/gpt2codex-queue')
        del cfg['execution_backend']
        cfg_path.write_text(json.dumps(cfg))
        cfg_path.chmod(0o600)
        self.assertEqual(queue.execution_backend(queue.load_config(cfg_path)), 'exec')
        self.cfg['execution_backend'] = 'screen'
        output = io.StringIO()
        with mock.patch.object(queue.shutil, 'which', return_value=None), \
                mock.patch.object(queue, 'read_token') as credentials, \
                mock.patch.object(queue.sys, 'stdout', output):
            self.assertEqual(queue.worker_ready(self.cfg, self.store), 2)
        credentials.assert_not_called()
        self.assertIn('WORKER_READY=NO', output.getvalue())
        self.assertIn('IDLE=YES', output.getvalue())

    def test_approval_binds_config_default_and_exec_override(self):
        self.assertEqual(queue.execution_backend(self.cfg), 'exec')
        with self.assertRaises(queue.QueueError):
            queue.execution_backend({'execution_backend':'auto'})
        api = base.FakeGitHub([base.make_issue()])
        queue.scan(api, self.store, self.cfg)
        self.cfg['execution_backend'] = 'screen'
        with mock.patch.object(queue.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='SCHVALUJI 1'):
            queue.approve(api, self.store, self.cfg, 1, self.store.load()['jobs']['1']['digest'][:12])
        self.cfg['execution_backend'] = 'exec'
        self.assertEqual(self.store.load()['jobs']['1']['backend'], 'screen')
        with self.assertRaises(queue.QueueError):
            queue.run_one(api, self.store, self.cfg, 1, 'exec')
        api.values[2] = base.make_issue(number=2)
        queue.scan(api, self.store, self.cfg)
        self.cfg['execution_backend'] = 'screen'
        with mock.patch.object(queue.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='SCHVALUJI 2'):
            queue.approve(api, self.store, self.cfg, 2, self.store.load()['jobs']['2']['digest'][:12], 'exec')
        self.assertEqual(self.store.load()['jobs']['2']['backend'], 'exec')

    def test_shared_execution_lock_and_restart_no_replay(self):
        api = self.prepare_screen()
        with queue.execution_lock(self.store), mock.patch.object(screen, 'launch') as launch:
            with self.assertRaises(queue.QueueError):
                queue.run_one(api, self.store, self.cfg, 1, 'screen')
        launch.assert_not_called()
        with mock.patch.object(screen, 'launch', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                queue.run_one(api, self.store, self.cfg, 1, 'screen')
        restarted = queue.Store(self.root)
        with mock.patch.object(screen, 'launch') as launch:
            queue.scan(api, restarted, self.cfg)
            queue.run_approved(api, restarted, self.cfg)
        launch.assert_not_called()
        self.assertEqual(restarted.load()['jobs']['1']['status'], 'running')

    def test_slot_binding_precedes_launch_and_ignores_project_registry(self):
        api = self.prepare_screen()
        sentinel = self.project / 'project-session.json'
        sentinel.write_text('UNCHANGED')
        def cannot_spawn(*args, **kwargs):
            job = self.store.load()['jobs']['1']
            spec = screen.read(self.store.log_dir / 'screens' / job['screen_slot'] / 'spec.json')
            self.assertEqual((spec['issue_id'], spec['digest'], spec['project']),
                             (job['id'], job['digest'], job['project']))
            self.assertEqual(spec['worker_host'], job['worker_host'])
            self.assertNotEqual(kwargs['env']['SCREENDIR'], '/tmp/dirty')
            raise OSError('synthetic launch failure')
        with mock.patch.object(screen, 'require_chatgpt'), \
                mock.patch.object(screen.subprocess, 'Popen', side_effect=cannot_spawn) as spawn:
            self.assertEqual(queue.run_one(api, self.store, self.cfg, 1, 'screen'), 127)
        spawn.assert_called_once()
        self.assertEqual(self.store.load()['jobs']['1']['status'], 'unknown')
        self.assertEqual(sentinel.read_text(), 'UNCHANGED')

    def test_listing_and_attach_only_synthetic_slot(self):
        root = self.store.log_dir / 'screens'
        root.mkdir(mode=0o700)
        slot = root / 'q1-0123456789ab'
        slot.mkdir(mode=0o700)
        screen.write(slot / 'spec.json', {'number':1, 'project':'kencoder'})
        screen.write(slot / 'phase.json', {'phase':'tui_starting', 'thread_id':THREAD})
        with mock.patch.object(screen, 'identity', return_value={'full_id':'synthetic'}) as identity:
            rows = screen.listing(self.root)
            self.assertEqual((rows[0]['thread_id'], rows[0]['attachable']), (THREAD, True))
            with mock.patch.object(screen.sys, 'argv', ['helper', '--repo-root', str(self.root), 'attach', slot.name]), \
                    mock.patch.object(screen.os, 'execvpe') as attach:
                screen.main()
            attach.assert_called_once()
            self.assertEqual(attach.call_args.args[1], ['screen', '-r', 'synthetic'])
            self.assertEqual(attach.call_args.args[2]['SCREENDIR'], str(slot.resolve() / 's'))
        self.assertTrue(all(call.args[0].resolve() == slot.resolve() for call in identity.call_args_list))

    def test_screen_waits_result_not_server_and_never_replays(self):
        api = self.prepare_screen()
        slot = Path('q1-0123456789ab')
        with mock.patch.object(screen, 'launch', return_value=slot), mock.patch.object(screen, 'wait_turn', return_value={'exit_code':0, 'thread_id':THREAD, 'turns_started':1, 'turns_completed':1}) as wait:
            self.assertEqual(queue.run_one(api, self.store, self.cfg, 1, 'screen'), 0)
        wait.assert_called_once_with(slot)
        self.assertEqual(self.store.load()['jobs']['1']['status'], 'done')
        with mock.patch.object(screen, 'launch') as launch:
            with self.assertRaises(queue.QueueError):
                queue.run_one(api, self.store, self.cfg, 1, 'screen')
        launch.assert_not_called()

    def test_closed_tui_preserves_durable_turn_without_rewriting_history(self):
        root = self.store.log_dir / 'screens'
        root.mkdir(mode=0o700)
        slot = root / 'q1-0123456789ab'
        slot.mkdir(mode=0o700)
        screen.write(slot / 'spec.json', {'number':1, 'project':'kencoder'})
        result = {'phase':'turn_finished', 'thread_id':THREAD, 'exit_code':0,
                  'finished_at':123, 'turns_started':1, 'turns_completed':1}
        screen.write(slot / 'result.json', result)
        for old in ('tui_starting', 'tui_ready', 'interrupted_unknown'):
            phase = {**result, 'phase':old}
            if old == 'interrupted_unknown':
                phase['last_phase'] = 'tui_starting'
            screen.write(slot / 'phase.json', phase)
            before = {p.name:p.read_bytes() for p in slot.iterdir()}
            with mock.patch.object(screen, 'identity', side_effect=RuntimeError('gone')), \
                    mock.patch.object(screen.subprocess, 'Popen') as launch:
                row = screen.listing(self.root)[0]
                self.assertEqual((row['phase'], row['tui_state'], row['turn_done']),
                                 ('turn_done', 'tui_closed', True))
                self.assertEqual(row['thread_id'], THREAD)
                self.assertFalse(row['attachable'])
                launch.assert_not_called()
            self.assertEqual(before, {p.name:p.read_bytes() for p in slot.iterdir()})

    def test_lost_screen_without_consistent_completion_stays_unknown(self):
        root = self.store.log_dir / 'screens'
        root.mkdir(mode=0o700)
        slot = root / 'q1-0123456789ab'
        slot.mkdir(mode=0o700)
        screen.write(slot / 'spec.json', {'number':1, 'project':'kencoder'})
        good = {'phase':'turn_finished', 'thread_id':THREAD, 'exit_code':0,
                'finished_at':123, 'turns_started':1, 'turns_completed':1}
        for bad in (None, {}, {**good, 'turns_completed':0}, {**good, 'exit_code':125},
                    {**good, 'finished_at':None}, {**good, 'thread_id':'bad'},
                    {**good, 'thread_id':'0199a213-81c0-7800-8aa1-bbab2a035a54'},
                    {**good, 'turns_completed':True}):
            if bad is not None:
                screen.write(slot / 'result.json', bad)
            screen.write(slot / 'phase.json', {**good, 'phase':'tui_starting'})
            with mock.patch.object(screen, 'identity', side_effect=RuntimeError('gone')):
                row = screen.listing(self.root)[0]
                self.assertEqual(row['phase'], 'interrupted_unknown')
                self.assertFalse(row['turn_done'])
        screen.write(slot / 'result.json', good)
        screen.write(slot / 'phase.json', {'phase':'turn_running', 'thread_id':THREAD})
        with mock.patch.object(screen, 'identity', side_effect=RuntimeError('gone')):
            self.assertEqual(screen.listing(self.root)[0]['phase'], 'interrupted_unknown')

    def test_screen_launch_uncertainty_is_not_completion(self):
        api = self.prepare_screen()
        with mock.patch.object(screen, 'launch', side_effect=RuntimeError('unknown')):
            self.assertEqual(queue.run_one(api, self.store, self.cfg, 1, 'screen'), 127)
        self.assertEqual(self.store.load()['jobs']['1']['status'], 'unknown')

    def test_attach_refuses_running_exec(self):
        with mock.patch.object(screen.sys, 'argv', ['helper', 'attach', 'q1-0123456789ab']), \
                mock.patch.object(screen, 'find_slot', return_value=self.root), \
                mock.patch.object(screen, 'identity', return_value={'full_id':'synthetic'}), \
                mock.patch.object(screen, 'read', return_value={'phase':'turn_running'}), \
                mock.patch.object(screen.os, 'execvpe') as attach:
            with self.assertRaises(RuntimeError):
                screen.main()
        attach.assert_not_called()

    def test_identity_cannot_select_other_slot(self):
        slot = self.root / 'q1-0123456789ab'
        slot.mkdir(mode=0o700)
        screen.write(slot / 'identity.json', {'pid':123, 'full_id':'123.g2c-project'})
        with mock.patch.object(screen, 'generation') as procfs:
            with self.assertRaises(RuntimeError):
                screen.identity(slot)
        procfs.assert_not_called()

    def test_failed_turn_with_exit_zero_never_resumes(self):
        slot = self.store.log_dir / 'failed-runner'
        slot.mkdir(mode=0o700)
        prompt = self.store.tmp_dir / 'failed-prompt'
        prompt.write_text('APPROVED_TEST')
        prompt.chmod(0o600)
        self.bin.write_text("#!/bin/sh\nif [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; elif [ \"$1\" = exec ]; then echo '{\"type\":\"turn.failed\"}'; else exit 99; fi\n")
        screen.write(slot / 'spec.json', {'number':1, 'codex':str(self.bin), 'prompt':str(prompt), 'mode':'read-only', 'cwd':str(self.project)})
        self.assertEqual(screen.runner(slot), 125)
        self.assertFalse((slot / 'resume.json').exists())
        self.assertEqual(screen.read(slot / 'result.json')['turns_completed'], 0)

    def test_runner_uses_stdin_same_uuid_resume_no_prompt_and_single_claim(self):
        slot = self.store.log_dir / 'test-runner'
        slot.mkdir(mode=0o700)
        prompt = self.store.tmp_dir / 'prompt'
        prompt.write_text('PRIVATE_PROMPT_NOT_ARGV')
        prompt.chmod(0o600)
        calls = self.root / 'calls.jsonl'
        self.bin.write_text('''#!/usr/bin/env python3
import json, sys, time
from pathlib import Path
with Path(%r).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:]==['login','status']:
 print('Logged in using ChatGPT', file=sys.stderr)
elif sys.argv[1]=='exec':
 assert sys.argv[-1]=='-' and sys.stdin.read()=='PRIVATE_PROMPT_NOT_ARGV'
 for e in [{'type':'thread.started','thread_id':%r},{'type':'turn.started'},{'type':'turn.completed'}]: print(json.dumps(e))
elif sys.argv[1]=='resume':
 assert sys.argv[-1]==%r and 'PRIVATE_PROMPT_NOT_ARGV' not in sys.argv
 time.sleep(.1)
''' % (str(calls), THREAD, THREAD))
        screen.write(slot / 'spec.json', {'number':1, 'codex':str(self.bin), 'prompt':str(prompt), 'mode':'read-only', 'cwd':str(self.project)})
        self.assertEqual(screen.runner(slot), 0)
        result = screen.read(slot / 'result.json')
        self.assertEqual((result['thread_id'], result['turns_started'], result['turns_completed']), (THREAD, 1, 1))
        self.assertEqual(screen.read(slot / 'phase.json')['phase'], 'tui_exited')
        argv = [json.loads(x) for x in calls.read_text().splitlines()]
        self.assertEqual(sum(x[0]=='exec' for x in argv), 1)
        self.assertEqual(sum(x[0]=='resume' for x in argv), 1)
        with self.assertRaises(FileExistsError):
            screen.runner(slot)


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main(verbosity=2)
