#!/usr/bin/env python3
"""Cílené offline hodnocení; žádný skutečný proces/screen se neovládá."""
import importlib.util
from pathlib import Path

# Repository imports remain explicit when this test is run directly.
import sys as _g2c_sys
_G2C_REPO = Path(__file__).resolve().parents[2]
if str(_G2C_REPO) not in _g2c_sys.path:
    _g2c_sys.path.insert(0, str(_G2C_REPO))
import unittest
import io
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('cleanup', (_G2C_REPO / 'tools/maintenance/gpt2codex-project-cleanup.py'))
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    allowed = {'gpt2codex', 'k2-smb', 'kencoder', 'm365-tui', 'reddit2tg'}

    def row(self, project, state='detached', program='unknown'):
        return ('screen', '123', project, 'g2c-'+project, state, '101.g2c-'+project, program)

    def test_nonallowed_detached_is_not_idle(self):
        row = cleanup.evaluate(self.row('app-memory-bar'), self.allowed)
        self.assertIn('idle neprokázáno', row[3])
        self.assertIn('idle nelze prokázat', row[4])
        # Even a coarse running/exited program label is not proof of no active turn.
        self.assertTrue(cleanup.evaluate(self.row('deb-gh', program='running'), self.allowed)[4].startswith('SKIP'))

    def test_attached_and_unverified(self):
        self.assertIn('připojená', cleanup.evaluate(self.row('deb-gh', 'attached'), self.allowed)[4])
        self.assertIn('neověřena', cleanup.evaluate(self.row('deb-gh', 'unconfirmed'), self.allowed)[4])

    def test_bridge_queue8_test_and_allowed_protected(self):
        for project in self.allowed | {'bridge', 'q8-1282176b7c79'}:
            result = cleanup.evaluate(self.row(project), self.allowed)
            self.assertTrue(result[4].startswith('SKIP'))
            self.assertNotIn('idle nelze', result[4])
        foreign = list(self.row('demo')); foreign[5] = '4169520.codex-mouse-test'
        self.assertIn('testovací', cleanup.evaluate(foreign, self.allowed)[4])

    def test_missing_allowlist_never_queries_cli(self):
        with patch.object(cleanup, 'allowed_ids', side_effect=OSError('missing')), patch.object(cleanup.subprocess, 'run') as run:
            with self.assertRaises(OSError):
                cleanup.snapshot(Path('/cli'), Path('/config'), Path('/queue'))
            run.assert_not_called()

    def test_snapshot_only_queries_metadata(self):
        line = 'multiplexer=screen generation=123 project_id=deb-gh screen_name=g2c-deb-gh state=detached session_id=101.g2c-deb-gh program=unknown exit_code=- exit_signal=- exited_at=- task=Fixture\n'
        with patch.object(cleanup, 'allowed_ids', return_value=self.allowed), patch.object(cleanup.subprocess, 'run', return_value=Mock(stdout=line)) as run:
            allowed, rows = cleanup.snapshot(Path('/cli'), Path('/config'), Path('/queue'))
            self.assertEqual(allowed, self.allowed)
            self.assertEqual(len(rows), 1)
            self.assertIn('SKIP', rows[0][4])
            self.assertEqual(run.call_args.args[0], ['/cli', 'session', 'list', '--program-status', '--config', '/config'])

    def test_apply_requires_interactive_confirmation_and_only_rechecks(self):
        snapshot = (self.allowed, [cleanup.evaluate(self.row('deb-gh'), self.allowed)])
        with patch.object(cleanup.sys, 'argv', ['cleanup', '--apply']), \
             patch.object(cleanup, 'snapshot', return_value=snapshot) as query, \
             patch.object(cleanup.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', return_value='OVERIT') as confirm, \
             patch.object(cleanup.sys, 'stdout', io.StringIO()):
            self.assertEqual(cleanup.main(), 0)
            confirm.assert_called_once()
            self.assertEqual(query.call_count, 2)
        with patch.object(cleanup.sys, 'argv', ['cleanup', '--apply']), \
             patch.object(cleanup, 'snapshot', return_value=snapshot) as query, \
             patch.object(cleanup.sys.stdin, 'isatty', return_value=False), \
             patch('builtins.input') as confirm, \
             patch.object(cleanup.sys, 'stdout', io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'interaktivní'):
                cleanup.main()
            confirm.assert_not_called()
            self.assertEqual(query.call_count, 1)


if __name__ == '__main__':
    unittest.main()
