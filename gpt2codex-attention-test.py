#!/usr/bin/env python3
"""P0-M Rust + GUI/CTest in private directories. No Git, real Codex or live sessions."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import sys


# Only synthetic ancestors/metadata; no workspace, session registry or Codex.
NAMESPACE_WORKER = r'''
import json, os, secrets, subprocess, sys
from pathlib import Path
cli, root, bwrap = map(Path, sys.argv[1:])
os.umask(0o077)
def process(pid):
    f = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return int(f[1]), f[19]
pid = os.getpid()
server, ticks = process(pid)
run = secrets.token_hex(16)
d = root / 'gpt2codex' / 'attention' / run
d.mkdir(mode=0o700, parents=True)
b = dict(version=1, project_id='synthetic', multiplexer='screen',
    session_id=str(server)+'.g2c-synthetic', server_pid=str(server),
    server_start_ticks=process(server)[1], codex_pid=str(pid), codex_start_ticks=ticks,
    run_nonce=run, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    pid_namespace=str(Path('/proc/self/ns/pid').stat().st_ino))
(d / 'binding.json').write_text(json.dumps(b, sort_keys=True, separators=(',', ':')))
env = dict(os.environ, XDG_RUNTIME_DIR=str(root), G2C_ATTENTION_RUN=run)
def call(*args):
    return subprocess.run([str(cli), 'attention', *args], env=env,
        capture_output=True, text=True, timeout=10)
def check(value, category):
    if not value: raise RuntimeError(category)
a = call('mark')
check(a.returncode == 0, 'HOST_DESCENDANT_MARK_FAILED')
token_a = a.stdout.strip().split('=', 1)[1]
check(call('clear', '--wait', token_a).returncode == 0, 'HOST_CLEAR_A_FAILED')
bb = call('mark')
check(bb.returncode == 0, 'HOST_MARK_B_FAILED')
token_b = bb.stdout.strip().split('=', 1)[1]
before = (d / 'waiting.json').read_bytes()
check(call('clear', '--wait', token_a).returncode != 0, 'DELAYED_CLEAR_ACCEPTED')
check((d / 'waiting.json').read_bytes() == before, 'DELAYED_CLEAR_CHANGED_B')
print(json.dumps(dict(run=run)), flush=True)
# Parent performs a real sibling-caller rejection while this bound run is live.
check(sys.stdin.readline() == 'continue\n', 'PARENT_HANDSHAKE_FAILED')
check((d / 'waiting.json').read_bytes() == before, 'SIBLING_CHANGED_B')
check(call('clear', '--wait', token_b).returncode == 0, 'HOST_CLEAR_B_FAILED')
check(not (d / 'waiting.json').exists(), 'HOST_CLEAR_LEFT_MARKER')
sandbox = [str(bwrap), '--ro-bind', '/', '/', '--dev', '/dev', '--unshare-user',
    '--unshare-pid', '--unshare-net', '--proc', '/proc', '--die-with-parent',
    '--new-session', '--bind', str(root), str(root), '--']
preflight = subprocess.run([*sandbox, '/usr/bin/true'], env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
if preflight.returncode:
    print(json.dumps(dict(status='NOT RUN', category='BWRAP_PRECONDITION_FAILED')), flush=True)
    sys.exit(2)
# exec preserves the probe PID, including a potential low-PID collision. Select
# the exact expected reason from this same /proc view, not an allowed-error list.
probe = r"""
import json,os,sys
from pathlib import Path
try:
    ns=Path('/proc',sys.argv[2],'ns/pid').stat().st_ino
except OSError:
    ns=None
expected='server_namespace_unavailable' if ns is None else 'server_namespace_mismatch'
assert ns != int(sys.argv[3])
print(json.dumps(dict(category=expected)),flush=True)
os.execv(sys.argv[1],[sys.argv[1],'attention','mark'])
"""
denied = subprocess.run([*sandbox, sys.executable, '-c', probe, str(cli),
    str(server), b['pid_namespace']], env=env, capture_output=True, text=True, timeout=10)
expected = json.loads(denied.stdout)['category']
check(denied.returncode != 0, 'NAMESPACE_MARK_ACCEPTED')
check(denied.stderr.strip() == 'ERROR: attention run identity unconfirmed; no write; reason='+expected,
    'NAMESPACE_REJECTION_CATEGORY_MISMATCH')
check(not (d / 'waiting.json').exists(), 'NAMESPACE_REJECTION_WROTE_MARKER')
print(json.dumps(dict(status='PASS', category=expected, host_mark_clear=True,
    delayed_clear_preserved_b=True, no_waiting_marker=True)), flush=True)
'''


def namespace_regression(cli, work, env, logs):
    """Fail visibly on unavailable bwrap; never substitute a fake namespace."""
    import shutil
    bwrap = shutil.which('bwrap', path=env['PATH'])
    result = {'status': 'FAIL', 'category': 'NAMESPACE_HARNESS_FAILED'}
    if not bwrap:
        result.update(status='NOT RUN', category='BWRAP_UNAVAILABLE')
        (logs / 'namespace.json').write_text(json.dumps(result, indent=2) + '\n')
        raise RuntimeError('NAMESPACE=NOT_RUN BWRAP_UNAVAILABLE')
    runtime = work / 'namespace-runtime'
    runtime.mkdir(mode=0o700)
    child = subprocess.Popen([sys.executable, '-c', NAMESPACE_WORKER, str(cli), str(runtime), bwrap],
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        import select
        if not select.select([child.stdout], [], [], 30)[0]:
            raise RuntimeError('NAMESPACE_WORKER_HANDSHAKE_TIMEOUT')
        ready = child.stdout.readline()
        if not ready:
            raise RuntimeError('NAMESPACE_WORKER_HANDSHAKE_FAILED')
        run = json.loads(ready)['run']
        marker = runtime / 'gpt2codex/attention' / run / 'waiting.json'
        before = marker.read_bytes()
        sibling = subprocess.run([str(cli), 'attention', 'mark', '--run', run],
                                 env={**env, 'XDG_RUNTIME_DIR': str(runtime)},
                                 capture_output=True, text=True, timeout=10)
        assert sibling.returncode != 0
        assert sibling.stderr.strip() == ('ERROR: attention run identity unconfirmed; no write; '
                                           'reason=caller_ancestry_unconfirmed')
        assert marker.read_bytes() == before
        clear = subprocess.run([str(cli), 'attention', 'clear', '--run', run,
                                '--wait', json.loads(before)['wait_nonce']],
                               env={**env, 'XDG_RUNTIME_DIR': str(runtime)},
                               capture_output=True, text=True, timeout=10)
        assert clear.returncode != 0
        assert clear.stderr.strip() == ('ERROR: attention run identity unconfirmed; no write; '
                                        'reason=caller_ancestry_unconfirmed')
        assert marker.read_bytes() == before
        stdout, stderr = child.communicate('continue\n', timeout=40)
        if child.returncode not in (0, 2):
            raise RuntimeError('NAMESPACE_WORKER_FAILED')
        result = json.loads(stdout)
        result['sibling_mark_and_clear_preserved_b'] = True
        if child.returncode != 0 or result['status'] != 'PASS':
            raise RuntimeError('NAMESPACE=NOT_RUN BWRAP_PRECONDITION_FAILED')
        assert not stderr
        return result
    except Exception:
        if child.poll() is None:
            child.kill()  # Only the owned synthetic Python worker, never a session.
        child.communicate()
        raise
    finally:
        (logs / 'namespace.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--diagnostic-only', action='store_true',
                        help='marker + namespace + unstarted backend mouse tests; no application GUI')
    mode.add_argument('--namespace-cli', type=Path,
                        help='only namespace regression using an already built private .tmp CLI')
    options = parser.parse_args()
    root = Path.cwd()
    assert (root / 'gui/main.cpp').is_file() and (root / '.tmp').is_dir() and (root / '.log').is_dir()
    os.umask(0o077)
    work = Path(tempfile.mkdtemp(prefix='attention-gui-', dir=root / '.tmp')).resolve()
    logs = Path(tempfile.mkdtemp(prefix='attention-gui-', dir=root / '.log')).resolve()
    print('REPORT=' + str(logs), flush=True)
    for name in ['home', 'config', 'state', 'runtime', 'cache', 'data', 'screens', 'tmp', 'guard', 'bin']:
        (work / name).mkdir(mode=0o700)
    # Defense in depth: accidental calls cannot reach user tools/state. Record
    # only the fixed tool name, never arguments, environment or terminal data.
    for name in ['git', 'codex', 'screen', 'tmux']:
        script = work / 'guard' / name
        script.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(name)
                          + ' >> ' + shlex.quote(str(work / 'forbidden-calls')) + '\nexit 97\n')
        script.chmod(0o700)
    cli = work / 'fixture-cli'
    cli.write_text('#!/bin/sh\ncase "$1/$2" in\nsession/list) printf "%s\\n" '
                   + shlex.quote('multiplexer=screen generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge')
                   + ';;\n*) exit 89;;\nesac\n')
    cli.chmod(0o700)
    env = {
        'PATH': str(work / 'guard') + os.pathsep + os.environ.get('PATH', os.defpath),
        'HOME': str(work / 'home'), 'XDG_CONFIG_HOME': str(work / 'config'),
        'XDG_STATE_HOME': str(work / 'state'), 'XDG_RUNTIME_DIR': str(work / 'runtime'),
        'XDG_CACHE_HOME': str(work / 'cache'), 'XDG_DATA_HOME': str(work / 'data'),
        'TMPDIR': str(work / 'tmp'), 'SCREENDIR': str(work / 'screens'),
        'SCREENRC': '/dev/null', 'SYSSCREENRC': '/dev/null',
        'QT_QPA_PLATFORM': 'offscreen', 'QT_QPA_PLATFORMTHEME': 'generic',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'G2C_MARKER_FIXTURE_BASE': str((root / '.tmp').resolve()),
    }
    # Existing local toolchain/cache, frozen and with a separate target. The
    # guard prevents any build/test from falling through to live user tools.
    cargo_env = {**env, 'CARGO_TARGET_DIR': str(work / 'rust-target'),
                 'RUSTUP_HOME': os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup')),
                 'CARGO_HOME': os.environ.get('CARGO_HOME', str(Path.home() / '.cargo'))}
    if options.namespace_cli:
        cli_path = options.namespace_cli.resolve(strict=True)
        assert cli_path.is_relative_to((root / '.tmp').resolve()) and cli_path.name == 'gpt2codex'
        result = namespace_regression(cli_path, work, env, logs)
        assert not (work / 'forbidden-calls').exists(), 'a forbidden tool was attempted'
        print('ATTENTION_NAMESPACE=' + result['status'], flush=True)
        return
    rust_results = {}
    commands = [
        ('rust-build', ['cargo', 'build', '--frozen', '--release']),
        ('rust-marker-tests', ['cargo', 'test', '--frozen', '--lib', 'attention_marker::tests']),
        ('rust-cli-tests', ['cargo', 'test', '--frozen', '--bin', 'gpt2codex']),
    ]
    if options.diagnostic_only:
        commands = commands[:2]
    for name, command in commands:
        with (logs / (name + '.log')).open('w') as output:
            result = subprocess.run(command, env=cargo_env, stdout=output, stderr=subprocess.STDOUT, timeout=300)
        rust_results[name] = 'PASS' if result.returncode == 0 else 'FAIL'
        assert result.returncode == 0, name + ' failed; see ' + str(logs / (name + '.log'))
    import shutil
    shutil.copy2(work / 'rust-target/release/gpt2codex', work / 'bin/gpt2codex')
    namespace = namespace_regression(work / 'bin/gpt2codex', work, env, logs)
    steps = [
        ('configure', ['cmake', '-S', str(root / 'gui'), '-B', str(work / 'build'),
                       '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=' + str(work / 'bin'),
                       '-DG2C_REQUIRE_ALL_BACKENDS=ON', '-DBUILD_TESTING=ON']),
        ('build', ['cmake', '--build', str(work / 'build'), '--parallel', '2']),
        ('self-test', [str(work / 'bin/gpt2codex-gui'), '--bridge-cli', str(cli), '--self-test']),
        ('backends', [str(work / 'bin/gpt2codex-gui'), '--bridge-cli', str(cli), '--backend-probe']),
        ('ctest', ['ctest', '--test-dir', str(work / 'build'), '--output-on-failure']),
    ]
    if options.diagnostic_only:
        steps = steps[:1] + [
            ('build-backend-tests', ['cmake', '--build', str(work / 'build'), '--target',
                                     'gpt2codex-backend-tests', '--parallel', '2']),
            ('mouse-tests', [str(work / 'bin/gpt2codex-backend-tests'), 'mouseModes']),
        ]
    summary = {'work': str(work), 'logs': str(logs), 'steps': {}, 'rust': rust_results, 'real_codex_started': 0,
               'live_sessions_accessed': 0, 'production_target_written': False,
               'namespace': namespace, 'diagnostic_only': options.diagnostic_only}
    try:
        for name, command in steps:
            with (logs / (name + '.log')).open('w') as output:
                result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=300)
            summary['steps'][name] = 'PASS' if result.returncode == 0 else 'FAIL'
            assert result.returncode == 0, name + ' failed; see ' + str(logs / (name + '.log'))
        if not options.diagnostic_only:
            assert 'konsole=1 qtermwidget=1' in (logs / 'backends.log').read_text(), 'both runtime backends required'
            assert 'attached=0 started=0' in (logs / 'self-test.log').read_text(), 'unsafe/missing self-test receipt'
        assert not (work / 'forbidden-calls').exists(), 'a forbidden tool was attempted in the isolated fixture'
        summary['forbidden_tool_attempts'] = 0
        print(('ATTENTION_DIAGNOSTIC=PASS' if options.diagnostic_only else
               'ATTENTION_P0M=PASS release_build=PASS ctest=PASS') + ' real_codex=0 live_sessions=0', flush=True)
    finally:
        (logs / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')


if __name__ == '__main__':
    main()
