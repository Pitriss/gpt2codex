#!/usr/bin/env python3
"""Isolated real GNU screen diagnostic tests. No Codex, live state or registry.

Run with the freshly built CLI path as the sole argument. This tests diagnostic
inspection, NOT START/POLL/FINISH or project-wide manual exclusion.
"""
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
import time
import unittest

CLI = Path(sys.argv.pop(1)).resolve()
BASE = Path(__file__).resolve().parents[2] / '.tmp' / 'queue22'


def ticks(pid):
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])


class RealScreenProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='screen22-', dir=BASE.resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('s', 'one', '.ai', 'cfg', 'cfg/projects', 'state',
                     'state/gpt2codex', 'state/gpt2codex/screen-sessions'):
            (self.root / name).mkdir(mode=0o700)
        self.env = {**os.environ, 'HOME':str(self.root), 'XDG_STATE_HOME':str(self.root / 'state'),
                    'SCREENDIR':str(self.root / 's'), 'TERM':'xterm'}
        for name in ('STY', 'WINDOW', 'TMUX', 'TMUX_PANE'):
            self.env.pop(name, None)
        self.config = self.root / 'cfg/config.toml'
        self.config.write_text('[workspace]\nproject_dir = ' + json.dumps(str(self.root))
                               + '\nrules_dir = ".ai"\nglobal_context_files = []\n')
        self.project_config = self.root / 'cfg/projects/one.toml'
        self.project_config.write_text('[project]\nid="one"\ndirectory="one"\n')
        self.server = subprocess.Popen(['/usr/bin/screen', '-c', '/dev/null', '-D', '-m',
                                        '-S', 'g2c-one', '-t', 'same-title', '/bin/sleep', '120'],
                                       cwd=self.root / 'one', env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        self.start_ticks = ticks(self.server.pid)
        self.full = f'{self.server.pid}.g2c-one'
        self.addCleanup(self.close_owned)
        deadline = time.monotonic() + 5
        while not (self.root / 's' / self.full).exists():
            if self.server.poll() is not None or time.monotonic() > deadline:
                if self.server.poll() is not None:
                    output, error = self.server.communicate()
                    self.fail(f'Isolated GNU screen failed to start ({self.server.returncode}): '
                              + (output + error).decode())
                self.fail('Isolated GNU screen startup timeout')
            time.sleep(.02)
        self.record_path = self.root / 'state/gpt2codex/screen-sessions/one.json'
        self.record = {'version':1, 'project_id':'one', 'screen_name':'g2c-one',
                       'task':'synthetic', 'root':str(self.root / 'one'),
                       'full_id':self.full, 'pid':self.server.pid,
                       'process_start_ticks':self.start_ticks}
        self.save_record()

    def save_record(self):
        self.record_path.write_text(json.dumps(self.record))
        self.record_path.chmod(0o600)

    def close_owned(self):
        if self.server.poll() is None:
            # Only this Popen generation, in this NEW private SCREENDIR.
            if ticks(self.server.pid) != self.start_ticks:
                raise RuntimeError('Synthetic screen generation changed; no signal')
            subprocess.run(['/usr/bin/screen', '-S', self.full, '-X', 'quit'],
                           env=self.env, capture_output=True, timeout=5, check=True)
            self.server.wait(timeout=5)
        self.server.stdout.close()
        self.server.stderr.close()

    def probe(self):
        return subprocess.run([str(CLI), 'supervisor', 'inspect-screen', '--id', 'one',
                               '--config', str(self.config), '--socket-directory', str(self.root / 's')],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def test_peer_process_parent_tty_cwd_and_manager_restart(self):
        for _ in range(2):  # independent managers, no adoption or new journal
            result = self.probe()
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertFalse(view['launch_permitted'])
            self.assertFalse(view['runtime_enabled'])
            self.assertFalse(view['window_identity_verified'])
            self.assertEqual(view['availability'], 'project_busy')
            self.assertEqual(view['session_id'], self.full)
            self.assertEqual(view['server']['start_ticks'], self.start_ticks)
            self.assertEqual(len(view['descendants']), 1)
            process = view['descendants'][0]
            self.assertEqual(process['parent_pid'], self.server.pid)
            self.assertTrue(process['cwd_matches_root'])
            self.assertNotEqual(process['tty'], 0)
        self.assertFalse((self.record_path.parent / 'project-owners-v1.json').exists())

    def test_pid_generation_and_root_rebinding_refused(self):
        for key, value in (('process_start_ticks', self.start_ticks + 1),
                           ('root', str(self.root)), ('full_id', f'{self.server.pid}.other')):
            old = self.record[key]
            self.record[key] = value
            self.save_record()
            self.assertNotEqual(self.probe().returncode, 0)
            self.record[key] = old
        self.save_record()
        self.assertEqual(self.probe().returncode, 0)

    def test_same_window_title_never_authorizes_window_ownership(self):
        subprocess.run(['/usr/bin/screen', '-S', self.full, '-X', 'screen', '-t',
                        'same-title', '/bin/sleep', '120'], env=self.env,
                       capture_output=True, timeout=5, check=True)
        deadline = time.monotonic() + 5
        while True:
            result = self.probe()
            if result.returncode == 0:
                view = json.loads(result.stdout)
                if len(view['descendants']) == 2:
                    break
            if time.monotonic() > deadline:
                self.fail('Two synthetic windows not observed')
            time.sleep(.02)
        self.assertFalse(view['window_identity_verified'])
        self.assertFalse(view['launch_permitted'])
        self.assertEqual(len({p['tty'] for p in view['descendants']}), 2)
        self.assertIsNone(self.server.poll())

    def test_missing_socket_and_wrong_preference_fail_closed(self):
        self.project_config.write_text('[project]\nid="one"\ndirectory="one"\n'
                                       '[sessions]\nmultiplexer="tmux"\n')
        self.assertNotEqual(self.probe().returncode, 0)
        self.project_config.write_text('[project]\nid="one"\ndirectory="one"\n')
        self.close_owned()
        self.assertNotEqual(self.probe().returncode, 0)


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main()
