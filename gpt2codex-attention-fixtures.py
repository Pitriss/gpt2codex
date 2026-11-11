#!/usr/bin/env python3
"""Prepare only p0m/Bridge fixtures with the existing CLI. No session/login/build."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path('/tmp/gpt2codex/tmp/p0m-native-hcgozwql')
CONFIG = ROOT/'config/chatgpt2codex/config.toml'
CLI = ROOT/'bin/gpt2codex'
GUARD = ROOT/'session-tools/git'


def fingerprint(path):
    m = path.lstat()
    value = [m.st_dev, m.st_ino, m.st_mode, m.st_size, m.st_mtime_ns]
    if stat.S_ISLNK(m.st_mode):
        value.append(os.readlink(path))
    elif stat.S_ISREG(m.st_mode):
        value.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return value


def main():
    os.umask(0o077)
    assert Path.cwd() == Path('/home/peva/TEMP/mycode/gpt2codex')
    for path in [ROOT, ROOT/'repos', ROOT/'state', ROOT/'codex', ROOT/'runtime']:
        m = path.lstat()
        assert stat.S_ISDIR(m.st_mode) and m.st_uid == os.geteuid() and not m.st_mode & 0o077
    report_path = ROOT/'fixture-p0m-bridge-setup.json'
    assert not report_path.exists(), 'FIXTURE_SETUP_ALREADY_ATTEMPTED'
    assert hashlib.sha256(GUARD.read_bytes()).hexdigest() == '96a9f0b601599264a7af05d095e46f23ca2936001beb17efca1f94f9b1e6379a'
    legacy = ROOT/'repos/attention-native'
    paths = [CONFIG, ROOT/'config/chatgpt2codex/projects/attention-native.toml',
             ROOT/'state/gpt2codex/screen-sessions/attention-native.json',
             legacy/'AGENTS.md', legacy/'.ai/.project_codex.md', legacy/'.ai/PROJECT_CONTEXT.md',
             legacy/'.git/config', legacy/'.git/info/exclude', legacy/'.git/HEAD',
             legacy/'.tmp', legacy/'.log']
    paths += sorted(Path('.log/p0m-native-08t2rf74').glob('*.json'))
    before = {str(p): fingerprint(p) for p in paths}
    # Only stat credentials: never read/hash their contents or copy them.
    auth = ROOT/'codex/auth.json'
    auth_before = auth.stat()
    shared = [Path('/tmp/gpt2codex'), Path('/tmp/gpt2codex/tmp'), Path('/tmp/gpt2codex/log')]
    shared_before = {str(p): fingerprint(p) for p in shared}
    for name in ['p0m', 'gpt2codex']:
        assert not (ROOT/'repos'/name).exists(), 'DISPOSABLE_CHECKOUT_ALREADY_EXISTS'
        assert not (ROOT/'config/chatgpt2codex/projects'/(name+'.toml')).exists()
    assert not Path('/tmp/p0m').exists(), 'P0M_TEMP_COLLISION'

    allowed = []
    template = ROOT/'empty-git-template'
    for name in ['attention-native', 'p0m', 'gpt2codex']:
        checkout = ROOT/'repos'/name
        allowed += [['-C', str(checkout), 'init', '--quiet', '--template='+str(template)],
                    ['-C', str(checkout), 'ls-files', '--', '.tmp', '.log'],
                    ['-C', str(checkout), 'ls-files', '--', '.bash_history']]
    content = ('#!/usr/bin/python3\nimport os,sys\nallowed='+repr(allowed)+'\n'
               'if sys.argv[1:] not in allowed: sys.exit(97)\n'
               'os.execv("/usr/bin/git",["/usr/bin/git"]+sys.argv[1:])\n')
    temporary = GUARD.with_name('git.fixture-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, GUARD)
    env = {'HOME': str(ROOT/'home'), 'CODEX_HOME': str(ROOT/'codex'),
           'XDG_CONFIG_HOME': str(ROOT/'config'), 'XDG_STATE_HOME': str(ROOT/'state'),
           'XDG_RUNTIME_DIR': str(ROOT/'runtime'), 'XDG_CACHE_HOME': str(ROOT/'cache'),
           'XDG_DATA_HOME': str(ROOT/'data'), 'TMPDIR': str(ROOT/'tmp'),
           'SCREENDIR': str(ROOT/'screens'), 'SCREENRC': '/dev/null', 'SYSSCREENRC': '/dev/null',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'PATH': str(ROOT/'session-tools')+':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    report = {'registration_command': 'workspace adopt (projects register is not implemented)',
              'fixtures': {}, 'git_guard_sha256': hashlib.sha256(GUARD.read_bytes()).hexdigest(),
              'sessions_started': 0, 'codex_started': 0, 'gui_started': 0, 'socket_bind_attempted': False}
    try:
        for name in ['p0m', 'gpt2codex']:
            checkout = ROOT/'repos'/name
            checkout.mkdir(mode=0o700)
            initialized = subprocess.run([str(GUARD), '-C', str(checkout), 'init', '--quiet', '--template='+str(template)],
                                         cwd=checkout, env=env, capture_output=True, timeout=15)
            assert initialized.returncode == 0, 'DISPOSABLE_GIT_INIT_FAILED'
            (checkout/'.git/info').mkdir(mode=0o700, exist_ok=True)
            results = {}
            for operation, options in [('adopt', ['--directory', name]), ('ensure', []), ('health', [])]:
                result = subprocess.run([str(CLI), 'workspace', operation, '--id', name, *options, '--config', str(CONFIG)],
                                        cwd=checkout, env=env, capture_output=True, timeout=15)
                assert result.returncode == 0, name+':'+operation+'_FAILED'
                assert b'workspace_ready=true\n' in result.stdout, 'WORKSPACE_NOT_READY'
                results[operation] = 'PASS'
                if operation == 'health':
                    results['health_output'] = result.stdout.decode()
            results['tmux_socket_bytes'] = len(os.fsencode(ROOT/'state/gpt2codex/screen-sessions/tmux-sockets'/(name+'.sock')))
            report['fixtures'][name] = results
        assert before == {str(p): fingerprint(p) for p in paths}, 'LEGACY_OR_RECEIPT_CHANGED'
        assert shared_before == {str(p): fingerprint(p) for p in shared}, 'SHARED_INFRA_METADATA_CHANGED'
        a = auth.stat()
        assert (auth_before.st_dev, auth_before.st_ino, auth_before.st_size, auth_before.st_mtime_ns) == (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns), 'AUTH_METADATA_CHANGED'
        report['legacy_and_receipts_unchanged'] = True
        report['auth_metadata_unchanged'] = True
        report['existing_shared_infra_unchanged'] = True
    finally:
        report_path.write_text(json.dumps(report, indent=2)+'\n')
    print('FIXTURE_P0M=READY FIXTURE_BRIDGE=READY HEALTH=PASS SOCKET_BYTES=92 LEGACY=PRESERVED AUTH=PRESERVED')
    print('GIT_GUARD_SHA256='+report['git_guard_sha256'])


if __name__ == '__main__':
    main()
