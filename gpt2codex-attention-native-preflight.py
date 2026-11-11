#!/usr/bin/env python3
"""Private build/auth gate only; never starts a Codex turn or multiplexer."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def processes():
    """Numeric provenance only. No cmdline, environment, sockets or registries."""
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            name = (entry / 'comm').read_text().strip()
            if name not in {'codex', 'screen', 'tmux', 'tmux: server', 'gpt2codex-gui'}:
                continue
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            result[entry.name] = fields[19]
        except (OSError, IndexError):
            continue
    return result


def main():
    root = Path.cwd()
    assert (root / '.ai/ATTENTION_OPT_IN.md').is_file()
    assert (root / '.tmp').is_dir() and (root / '.log').is_dir()
    os.umask(0o077)
    work = Path(tempfile.mkdtemp(prefix='p0m-native-', dir=root / '.tmp')).resolve()
    logs = Path(tempfile.mkdtemp(prefix='p0m-native-', dir=root / '.log')).resolve()
    print('REPORT=' + str(logs), flush=True)
    for name in ['home', 'config/chatgpt2codex/projects', 'state', 'runtime',
                 'cache', 'data', 'screens', 'tmp', 'codex', 'guard', 'bin',
                 'repos/attention-native']:
        (work / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    # Only the disposable registry is written. No bootstrap/health: those call Git.
    config = work / 'config/chatgpt2codex'
    (config / 'config.toml').write_text('[workspace]\nproject_dir=' + json.dumps(str(work / 'repos')) + '\n')
    (config / 'projects/attention-native.toml').write_text(
        '[project]\nid="attention-native"\ndirectory="attention-native"\n'
        '[git]\nidentity="preserve"\n[sessions]\nmultiplexer="screen"\n')
    # No OS keyring lookup or production auth/config fallback is allowed.
    (work / 'codex/config.toml').write_text('cli_auth_credentials_store = "file"\n')
    for tool in ['git', 'screen', 'tmux']:
        guard = work / 'guard' / tool
        guard.write_text('#!/bin/sh\nexit 97\n')
        guard.chmod(0o700)
    codex = shutil.which('codex')
    assert codex, 'CODEX_EXECUTABLE_UNAVAILABLE'
    baseline = processes()
    report = {'work': str(work), 'status': 'PREPARING', 'codex_version': None,
              'build': {}, 'authentication': 'NOT_CHECKED',
              'interactive_codex_starts': 0, 'multiplexer_starts': 0,
              'test_gui_starts': 0, 'production_store_accessed': False,
              'credentials_copied': False, 'sandbox_permissions_extended': False,
              'native_marker_roundtrip': 'NOT_RUN'}
    env = {'PATH': str(work / 'guard') + ':' + os.environ.get('PATH', os.defpath),
           'HOME': str(work / 'home'), 'CODEX_HOME': str(work / 'codex'),
           'XDG_CONFIG_HOME': str(work / 'config'), 'XDG_STATE_HOME': str(work / 'state'),
           'XDG_RUNTIME_DIR': str(work / 'runtime'), 'XDG_CACHE_HOME': str(work / 'cache'),
           'XDG_DATA_HOME': str(work / 'data'), 'TMPDIR': str(work / 'tmp'),
           'SCREENDIR': str(work / 'screens'), 'SCREENRC': '/dev/null', 'SYSSCREENRC': '/dev/null',
           'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'QT_QPA_PLATFORM': 'offscreen',
           'QT_QPA_PLATFORMTHEME': 'generic'}
    # An already injected credential may be used in memory, never persisted/dumped.
    for name in ['OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN']:
        if name in os.environ:
            env[name] = os.environ[name]
    cargo_env = {**env, 'CARGO_TARGET_DIR': str(work / 'rust-target'),
                 'RUSTUP_HOME': os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup')),
                 'CARGO_HOME': os.environ.get('CARGO_HOME', str(Path.home() / '.cargo'))}
    try:
        for name, command in [
            ('rust-release', ['cargo', 'build', '--frozen', '--release']),
            ('gui-configure', ['cmake', '-S', str(root / 'gui'), '-B', str(work / 'build'),
                               '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_TESTING=OFF',
                               '-DG2C_REQUIRE_ALL_BACKENDS=ON',
                               '-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=' + str(work / 'bin')]),
            ('gui-release', ['cmake', '--build', str(work / 'build'), '--parallel', '2']),
        ]:
            with (logs / (name + '.log')).open('w') as output:
                result = subprocess.run(command, env=cargo_env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=300)
            report['build'][name] = 'PASS' if result.returncode == 0 else 'FAIL'
            assert result.returncode == 0, 'PRIVATE_BUILD_FAILED'
        shutil.copy2(work / 'rust-target/release/gpt2codex', work / 'bin/gpt2codex')
        report['binaries'] = {name: hashlib.sha256((work / 'bin' / name).read_bytes()).hexdigest()
                              for name in ['gpt2codex', 'gpt2codex-gui']}
        # Raw CLI output is kept only long enough to classify; never logged/printed.
        version = subprocess.run([codex, '--version'], cwd=work / 'repos/attention-native',
                                 env=env, capture_output=True, timeout=15)
        if version.returncode != 0 or not re.fullmatch(rb'codex-cli 0\.163\.0\s*', version.stdout):
            report['status'] = 'CODEX_VERSION_UNVERIFIED'
            return
        report['codex_version'] = '0.163.0'
        status = subprocess.run([codex, 'login', 'status'], cwd=work / 'repos/attention-native',
                                env=env, capture_output=True, timeout=15)
        if status.returncode == 0:
            report['authentication'] = 'CREDENTIALS_PRESENT_NETWORK_UNVERIFIED'
            report['status'] = 'AUTH_GATE_READY'
        elif b'not logged in' in (status.stdout + status.stderr).lower():
            report['authentication'] = 'UNAVAILABLE_IN_ISOLATED_CONTEXT'
            report['status'] = 'STOP_AUTH_UNAVAILABLE'
        else:
            report['authentication'] = 'STATUS_CHECK_FAILED'
            report['status'] = 'STOP_AUTH_STATUS_ERROR'
        report['auth_status_exit_code'] = status.returncode
    finally:
        current = processes()
        report['monitored_production_processes'] = len(baseline)
        report['monitored_production_processes_unchanged'] = all(current.get(pid) == ticks for pid, ticks in baseline.items())
        (logs / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print('NATIVE_PREFLIGHT=' + report['status'], flush=True)


if __name__ == '__main__':
    main()
