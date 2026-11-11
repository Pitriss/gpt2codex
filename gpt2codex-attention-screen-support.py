#!/usr/bin/env python3
"""Private screen smoke support. No model turn in prepare/probe modes."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tomllib

ROOT = Path('/tmp/gpt2codex/tmp/p0m-native-hcgozwql')
BASE = ROOT/'screen-smoke'
PROJECT = ROOT/'repos/p0s'
CONFIG = BASE/'config/config.toml'
NATIVE = '/home/peva/.local/bin/codex'
PROFILE = 'g2c-screen'
PROFILE_FILE = ROOT/'codex/g2c-screen.config.toml'
STATE = ROOT/'state/gpt2codex/screen-sessions'
CLI = ROOT/'bin/gpt2codex'


def write_new(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as file:
        file.write(data); file.flush(); os.fsync(file.fileno())


def stamp(path):
    m = path.lstat()
    value = [m.st_dev, m.st_ino, m.st_mode, m.st_uid, m.st_size, m.st_mtime_ns]
    if stat.S_ISREG(m.st_mode):
        value.append(hashlib.sha256(path.read_bytes()).hexdigest())
    elif stat.S_ISLNK(m.st_mode):
        value.append(os.readlink(path))
    return value


def env():
    return {'HOME': str(ROOT/'home'), 'CODEX_HOME': str(ROOT/'codex'),
            'XDG_CONFIG_HOME': str(BASE/'gui-config'), 'XDG_STATE_HOME': str(ROOT/'state'),
            'XDG_RUNTIME_DIR': str(ROOT/'runtime'), 'XDG_CACHE_HOME': str(BASE/'cache'),
            'XDG_DATA_HOME': str(BASE/'data'), 'TMPDIR': str(PROJECT/'.attention-tmp'),
            'SCREENDIR': str(ROOT/'screens'), 'SCREENRC': '/dev/null', 'SYSSCREENRC': '/dev/null',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'PATH': str(BASE/'bin')+':/home/peva/.local/bin:/usr/bin:/bin:/usr/local/bin',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TERM': 'xterm-256color'}


def cli(*args):
    return subprocess.run([str(CLI), *args, '--config', str(CONFIG)], cwd=PROJECT, env=env(),
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=20)


def verify_plan():
    plan = json.loads((BASE/'plan.json').read_text())
    for filename, expected in plan['protected'].items():
        if stamp(Path(filename)) != expected:
            raise RuntimeError('PREVIOUS_EVIDENCE_CHANGED')
    for filename, digest in plan['new_files'].items():
        path = Path(filename)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('SCREEN_SMOKE_CONFIGURATION_CHANGED')
    return plan


def policy_arguments(runtime):
    # Override a whole TOML table: CLI dotted keys do not unquote path segments.
    roots = '{'+json.dumps(str(runtime))+'=true}'
    return ['-p', PROFILE, '-c', 'permissions.g2c-screen.workspace_roots='+roots]


PROBE = r'''
import errno,json,os,secrets,sys
from pathlib import Path
paths=[Path(p) for p in sys.argv[1:]]
result={}
for index,parent in enumerate(paths):
    path=parent/('.permission-probe-'+secrets.token_hex(8))
    created=False
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.write(fd,b'policy-probe');os.close(fd);created=True
        result[str(index)]='write'
    except OSError as error:
        result[str(index)]='denied' if error.errno in (errno.EACCES,errno.EPERM,errno.EROFS) else 'error'
    finally:
        if created:path.unlink()
print(json.dumps(result))
sys.exit(0 if list(result.values())==['write','write','write','write','denied','denied'] else 7)
'''


def probe(runtime):
    verify_plan()
    args = [NATIVE, 'sandbox', *policy_arguments(runtime), '--permission-profile', PROFILE,
            '--include-managed-config', '--cd', str(PROJECT), '--', '/usr/bin/python3', '-c', PROBE,
            str(PROJECT), str(runtime), '/tmp/p0s/tmp', '/tmp/p0s/log', str(ROOT/'tmp'), str(BASE)]
    p = subprocess.run(args, cwd=PROJECT, env=env(), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
    result = {'exit': p.returncode, 'status': 'FAIL', 'category': 'POLICY_PROBE_FAILED'}
    if p.returncode == 0:
        try:
            values = json.loads(p.stdout)
            if [values[str(i)] for i in range(6)] == ['write']*4+['denied']*2:
                result.update(status='PASS', category='WRITES_CONFINED', checks=values)
        except (ValueError, KeyError, TypeError):
            pass
    elif b'app-server socket directory must be' in p.stderr:
        result['category'] = 'NATIVE_SANDBOX_SOCKET_DIRECTORY_PRECONDITION'
    elif any(t in p.stderr.lower() for t in [b'operation not permitted', b'namespace', b'permission denied']):
        result['category'] = 'NATIVE_SANDBOX_RUNTIME_RESTRICTED'
    return result


def run_codex():
    verify_plan()
    nonce = os.environ.get('G2C_ATTENTION_RUN', '')
    if len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise RuntimeError('INSTRUMENTED_RUN_REQUIRED')
    runtime = ROOT/'runtime/gpt2codex/attention'/nonce
    if sys.argv[1:] != ['--add-dir', str(runtime)] or Path.cwd().resolve() != PROJECT:
        raise RuntimeError('WRAPPER_ARGUMENT_SCOPE_MISMATCH')
    b = json.loads((runtime/'binding.json').read_text())
    if b.get('project_id') != 'p0s' or b.get('multiplexer') != 'screen' or b.get('run_nonce') != nonce or b.get('codex_pid') != str(os.getpid()):
        raise RuntimeError('WRAPPER_BINDING_MISMATCH')
    if os.environ.get('G2C_ATTENTION_HELPER') != str(CLI) or os.environ.get('G2C_ATTENTION_INSTRUCTIONS') != str(runtime/'instructions.md'):
        raise RuntimeError('WRAPPER_ENVIRONMENT_SCOPE_MISMATCH')
    result = probe(runtime)
    write_new(runtime/'screen-policy-proof.json', json.dumps(result).encode())
    if result['status'] != 'PASS':
        raise RuntimeError(result['category'])
    # This exec is reachable only in a later explicitly approved screen start.
    # It preserves the bound PID, adds only this run root and bypasses no sandbox.
    args = [NATIVE, '--no-daemon', '--strict-config', *policy_arguments(runtime),
            '--ask-for-approval', 'on-request', '--cd', str(PROJECT), '--add-dir', str(runtime)]
    os.execv(NATIVE, args)


def prepare():
    os.umask(0o077)
    if (BASE/'plan.json').exists() or (PROJECT/'.git').exists():
        raise RuntimeError('SCREEN_FIXTURE_ALREADY_PREPARED')
    protected = {}
    candidates = list(ROOT.glob('*.json'))
    candidates += list(Path('/home/peva/TEMP/mycode/gpt2codex/.log/p0m-native-08t2rf74').glob('*.json'))
    candidates += list(STATE.glob('*.json'))
    sockets = STATE/'tmux-sockets'
    if sockets.is_dir():candidates += list(sockets.iterdir())
    attention = ROOT/'runtime/gpt2codex/attention'
    if attention.is_dir():
        for directory in attention.iterdir():
            candidates.append(directory)
            candidates += list(directory.iterdir())
    candidates += [ROOT/'config/chatgpt2codex/config.toml',ROOT/'session-tools/git']
    candidates += list((ROOT/'config/chatgpt2codex/projects').glob('*.toml'))
    for path in candidates:protected[str(path)] = stamp(path)
    auth = (ROOT/'codex/auth.json').stat()
    for directory in ['bin','config/projects','gui-config','cache','data','probe-runtime']:
        (BASE/directory).mkdir(mode=0o700,parents=True,exist_ok=True)
    (PROJECT/'.attention-tmp').mkdir(mode=0o700)
    if Path('/tmp/p0s').exists():raise RuntimeError('P0S_TEMP_COLLISION')
    write_new(CONFIG, ('[workspace]\nproject_dir='+json.dumps(str(ROOT/'repos'))+'\n').encode())
    # Reuse only validated fixture registration metadata; never copy credentials.
    bridge = ROOT/'config/chatgpt2codex/projects/gpt2codex.toml'
    registration = tomllib.loads(bridge.read_text())
    if registration['project'] != {'id':'gpt2codex','directory':'gpt2codex'}:
        raise RuntimeError('ISOLATED_BRIDGE_REGISTRATION_CHANGED')
    write_new(BASE/'config/projects/gpt2codex.toml', bridge.read_bytes())
    template = ROOT/'empty-git-template'
    allowed = [['-C',str(PROJECT),'init','--quiet','--template='+str(template)],
               ['-C',str(PROJECT),'ls-files','--','.tmp','.log'],
               ['-C',str(PROJECT),'ls-files','--','.bash_history']]
    guard = '#!/usr/bin/python3\nimport os,sys\nallowed='+repr(allowed)+'\nif sys.argv[1:] not in allowed: sys.exit(97)\nos.execv("/usr/bin/git",["/usr/bin/git"]+sys.argv[1:])\n'
    write_new(BASE/'bin/git', guard.encode(),0o700)
    init = subprocess.run([str(BASE/'bin/git'),*allowed[0]],cwd=PROJECT,env=env(),capture_output=True,timeout=15)
    if init.returncode:raise RuntimeError('SCREEN_FIXTURE_GIT_INIT_FAILED')
    (PROJECT/'.git/info').mkdir(mode=0o700,exist_ok=True)
    for operation, extra in [('adopt',['--directory','p0s']),('ensure',[]),('health',[])]:
        p = cli('workspace',operation,'--id','p0s',*extra)
        if p.returncode or b'workspace_ready=true\n' not in p.stdout:
            raise RuntimeError('SCREEN_FIXTURE_WORKSPACE_'+operation.upper()+'_FAILED')
    profile = ['approval_policy="on-request"','approvals_reviewer="user"',
               'default_permissions="g2c-screen"','allow_login_shell=false',
               '[projects.'+json.dumps(str(PROJECT))+']','trust_level="trusted"',
               '[permissions.g2c-screen]','extends=":workspace"',
               '[permissions.g2c-screen.filesystem]', '":slash_tmp"="read"',
               json.dumps('/tmp/p0s/tmp')+'="write"',json.dumps('/tmp/p0s/log')+'="write"',
               '[permissions.g2c-screen.network]','enabled=false',
               '[shell_environment_policy]','inherit="all"','ignore_default_excludes=false',
               '[shell_environment_policy.filters]']
    for key in [*env(),'G2C_ATTENTION_RUN','G2C_ATTENTION_HELPER','G2C_ATTENTION_INSTRUCTIONS']:
        profile.append(json.dumps(key)+'="include"')
    write_new(PROFILE_FILE, ('\n'.join(profile)+'\n').encode())
    support = Path(__file__).read_bytes()
    write_new(BASE/'support.py', support)
    wrapper = '#!/usr/bin/python3\nimport runpy\nns=runpy.run_path('+repr(str(BASE/'support.py'))+')\ntry: ns["run_codex"]()\nexcept Exception: raise SystemExit("SCREEN_SMOKE_POLICY_OR_IDENTITY_FAILED")\n'
    write_new(BASE/'bin/codex',wrapper.encode(),0o700)
    new_files = [CONFIG, *list((BASE/'config/projects').glob('*.toml')), PROFILE_FILE,
                 BASE/'bin/git',BASE/'bin/codex',BASE/'support.py']
    plan = {'project_id':'p0s','multiplexer':'screen','protected':protected,
            'new_files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in new_files},
            'selected_policy':{'extends':':workspace','approval':'on-request','slash_tmp':'read',
                               'run_root':'exact per-run --add-dir only','network':False}}
    write_new(BASE/'plan.json', json.dumps(plan,indent=2).encode())
    verify_plan()
    now = (ROOT/'codex/auth.json').stat()
    if (auth.st_dev,auth.st_ino,auth.st_size,auth.st_mtime_ns)!=(now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns):
        raise RuntimeError('TEST_AUTH_METADATA_CHANGED')
    result = probe(BASE/'probe-runtime')
    write_new(BASE/'prepared-policy-probe.json',json.dumps(result,indent=2).encode())
    print('SCREEN_FIXTURE=READY HEALTH=8/8_PASS OLD_EVIDENCE=PRESERVED')
    print('POLICY_PROBE='+result['status']+' CATEGORY='+result['category'])


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare']:prepare()
    else:raise SystemExit('only explicit prepare is supported')
