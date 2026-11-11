#!/usr/bin/env python3
"""One disposable native session; no login, build or production state access."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess


WORK = Path('/tmp/gpt2codex/tmp/p0m-native-hcgozwql')
PROJECT = WORK / 'repos/attention-native'
LOG = Path.cwd() / '.log/p0m-native-08t2rf74'
CONFIG = WORK / 'config/chatgpt2codex/config.toml'


def environment():
    for path in [WORK, WORK/'home', WORK/'codex', WORK/'runtime', WORK/'state', PROJECT]:
        m = path.lstat()
        assert stat.S_ISDIR(m.st_mode) and m.st_uid == os.geteuid() and not m.st_mode & 0o077, 'UNSAFE_TEST_PATH'
    return {
        'HOME': str(WORK/'home'), 'CODEX_HOME': str(WORK/'codex'),
        'XDG_CONFIG_HOME': str(WORK/'config'), 'XDG_STATE_HOME': str(WORK/'state'),
        'XDG_RUNTIME_DIR': str(WORK/'runtime'), 'XDG_CACHE_HOME': str(WORK/'cache'),
        'XDG_DATA_HOME': str(WORK/'data'), 'TMPDIR': str(WORK/'tmp'),
        'SCREENDIR': str(WORK/'screens'), 'SCREENRC': '/dev/null', 'SYSSCREENRC': '/dev/null',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'PATH': str(WORK/'session-tools') + ':/home/peva/.local/bin:/usr/local/bin:/usr/bin:/bin',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TERM': 'xterm-256color',
    }


def cli(env, *args):
    return subprocess.run([str(WORK/'bin/gpt2codex'), *args, '--config', str(CONFIG)],
                          cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'start'])
    args = parser.parse_args()
    os.umask(0o077)
    env = environment()
    if args.action == 'prepare':
        assert not (PROJECT/'.git').exists(), 'TEST_GIT_ALREADY_EXISTS_REVIEW_REQUIRED'
        assert not Path('/tmp/attention-native').exists(), 'BRIDGE_TEMP_COLLISION'
        tools = WORK/'session-tools'
        tools.mkdir(mode=0o700)
        template = WORK/'empty-git-template'
        template.mkdir(mode=0o700)
        # Exact argv/root allowlist. Real Git executes health checks; no mock success.
        guard = tools/'git'
        guard.write_text(
            '#!/usr/bin/python3\nimport os,sys\n'
            'root=' + repr(str(PROJECT)) + '\n'
            'allowed=' + repr([
                ['-C', str(PROJECT), 'init', '--quiet', '--template='+str(template)],
                ['-C', str(PROJECT), 'ls-files', '--', '.tmp', '.log'],
                ['-C', str(PROJECT), 'ls-files', '--', '.bash_history'],
            ]) + '\n'
            'if sys.argv[1:] not in allowed: sys.exit(97)\n'
            'os.execv("/usr/bin/git",["/usr/bin/git"]+sys.argv[1:])\n')
        guard.chmod(0o700)  # Only this newly created test helper.
        init = subprocess.run([str(guard), '-C', str(PROJECT), 'init', '--quiet', '--template='+str(template)],
                              cwd=PROJECT, env=env, capture_output=True, timeout=15)
        assert init.returncode == 0, 'ISOLATED_GIT_INIT_FAILED'
        (PROJECT/'.git/info').mkdir(mode=0o700, exist_ok=True)
        ensure = cli(env, 'workspace', 'ensure', '--id', 'attention-native')
        assert ensure.returncode == 0, 'BRIDGE_ENSURE_FAILED'
        health = cli(env, 'workspace', 'health', '--id', 'attention-native')
        report = {'ensure_exit': ensure.returncode, 'health_exit': health.returncode,
                  'health_output': health.stdout.decode('utf-8', errors='replace'),
                  'login_repeated': False, 'build_repeated': False}
        (LOG/'workspace-ready.json').write_text(json.dumps(report, indent=2)+'\n')
        assert health.returncode == 0, 'WORKSPACE_HEALTH_FAILED'
        print('WORKSPACE_ENSURE=PASS HEALTH=PASS')
    else:
        health = cli(env, 'workspace', 'health', '--id', 'attention-native')
        assert health.returncode == 0, 'WORKSPACE_HEALTH_FAILED'
        attempted = LOG/'native-start-attempt.json'
        assert not attempted.exists(), 'ONE_START_ALREADY_ATTEMPTED'
        record = WORK/'state/gpt2codex/screen-sessions/attention-native.json'
        assert not record.exists(), 'TEST_SLOT_ALREADY_REGISTERED'
        attempted.write_text(json.dumps({'attempted': True, 'multiplexer': 'screen',
                                         'interactive_codex_permitted': 1})+'\n')
        result = cli(env, 'session', 'start', '--id', 'attention-native', '--multiplexer', 'screen',
                     '--attention', '--approve-start')
        report = {'start_exit_code': result.returncode, 'multiplexer': 'screen',
                  'status': 'STARTED' if result.returncode == 0 else 'START_FAILED',
                  'production_state_accessed': False, 'raw_output_saved': False}
        (LOG/'native-start-result.json').write_text(json.dumps(report, indent=2)+'\n')
        print('NATIVE_SESSION_START='+report['status'])
        print('START_EXIT_CODE='+str(result.returncode))


if __name__ == '__main__':
    main()
