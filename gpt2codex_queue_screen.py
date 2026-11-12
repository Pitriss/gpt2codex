#!/usr/bin/env python3
"""Opt-in queue task slots. Never queries or controls the project mux registry."""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

from gpt2codex_queue_dispatch import DispatchError, require_legacy_launch_allowed
from gpt2codex_queue_codex import codex_options

SLOT_RE = re.compile(r"q[0-9]+-[0-9a-f]{12}\Z")


def private(path, directory=False):
    st = Path(path).lstat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not (
            stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
        raise RuntimeError("Invalid queue slot ownership/type/mode")
    return Path(path)


def read(path):
    with private(path).open() as f:
        raw = f.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Oversized slot metadata")
    return json.loads(raw)


def write(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def clean_env():
    env = os.environ.copy()
    for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'GH_ENTERPRISE_TOKEN', 'OPENAI_API_KEY',
                'CODEX_API_KEY', 'XDG_STATE_HOME', 'STY', 'WINDOW', 'SCREENDIR', 'TMUX', 'TMUX_PANE'):
        env.pop(key, None)
    env["XDG_STATE_HOME"] = str(Path.home() / ".local/state")
    return env


def generation(pid):
    base = Path('/proc') / str(pid)
    if base.stat().st_uid != os.getuid():
        raise RuntimeError('Foreign screen process')
    fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'ticks': int(fields[19]),
            'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def slots_root(repo):
    return Path(repo).resolve() / '.log/github-queue/screens'


def find_slot(repo, name):
    if not SLOT_RE.fullmatch(name):
        raise RuntimeError('Invalid queue slot id')
    root = private(slots_root(repo), True)
    return private(root / name, True)


def identity(slot):
    saved = read(slot / 'identity.json')
    if saved['full_id'] != f"{saved['pid']}.g2c{slot.name}":
        raise RuntimeError('Screen id is not bound to this task slot')
    if generation(saved['pid']) != {k: saved[k] for k in ('pid', 'ticks', 'boot')}:
        raise RuntimeError('Screen process generation changed')
    if Path(os.readlink(f"/proc/{saved['pid']}/exe")).name != 'screen':
        raise RuntimeError('Not the recorded screen server')
    private(slot / 's', True)
    st = (slot / 's' / saved['full_id']).lstat()
    if (not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid()
            or [st.st_dev, st.st_ino] != saved['socket']):
        raise RuntimeError('Screen socket identity changed')
    return saved


def require_chatgpt(codex, env):
    # Metadata-only CLI check; never read or copy auth/config files. No login/logout.
    proc = subprocess.run([codex, 'login', 'status'], env=env, capture_output=True, timeout=15)
    status = (proc.stdout + proc.stderr).decode(errors='replace')
    if proc.returncode or 'Logged in using ChatGPT' not in status or 'API key' in status:
        raise RuntimeError('Existing ChatGPT login not confirmed; no API fallback')


def prepare_slot(store, item, codex, cwd, prompt, backend='screen'):
    """Bind a single-use result slot BEFORE starting any managed task.

    A project-screen slot is metadata only: this function never creates a
    screen server or attaches to an existing one.
    """
    if backend not in ('screen', 'project-screen'):
        raise RuntimeError('Unknown task slot backend')
    root = store.log_dir / 'screens'
    root.mkdir(mode=0o700, exist_ok=True)
    private(root, True)
    slot = root / f"q{item['number']}-{uuid.uuid4().hex[:12]}"
    slot.mkdir(mode=0o700)
    slot = slot.resolve()
    (slot / 's').mkdir(mode=0o700)
    prompt_path = store.tmp_dir / (slot.name + '.prompt')
    fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(prompt)
    spec = {'number': item['number'], 'project': item['project'], 'digest': item['digest'],
            'mode': item['mode'], 'cwd': cwd, 'codex': codex, 'backend': backend,
            'prompt': str(prompt_path.resolve()), 'name': 'g2c' + slot.name}
    if backend == 'project-screen':
        spec['project_validation'] = item.get('_project_validation')
    if '_approved_project_root' in item:
        spec['approved_project_root'] = item['_approved_project_root']
    spec['queue_repo_root'] = str(store.log_dir.parent.parent.resolve())
    spec['issue_id'] = item['id']
    spec['worker_host'] = socket.gethostname()
    if '_handoff_report_path' in item:
        spec['report_path'] = str(item['_handoff_report_path'].resolve())
        spec['schema_path'] = str(item['_handoff_schema_path'].resolve())
    write(slot / 'spec.json', spec)
    # Persist slot binding before starting screen, so a worker crash cannot lose
    # its association or authorize a second launch. UUID is filled after result.
    with store.lock():
        state = store.load()
        job = state['jobs'][str(item['number'])]
        if job['status'] != 'running' or job['digest'] != item['digest'] or job.get('screen_slot'):
            raise RuntimeError('Slot claim mismatch; no launch')
        job['screen_slot'] = slot.name
        job['worker_host'] = spec['worker_host']
        store.save(state)
    write(slot / 'phase.json', {'phase': 'issue_accepted', 'thread_id': None,
                               'turns_started': 0, 'turns_completed': 0})
    return slot


def launch(store, item, codex, cwd, prompt, env=None):
    require_legacy_launch_allowed(env)
    env = clean_env()
    require_chatgpt(codex, env)
    slot = prepare_slot(store, item, codex, cwd, prompt, backend='screen')
    spec = read(slot / 'spec.json')
    env['SCREENDIR'] = str(slot / 's')
    with (slot / 'screen.log').open('xb') as log:
        os.chmod(slot / 'screen.log', 0o600)
        try:
            proc = subprocess.Popen(['screen', '-D', '-m', '-c', '/dev/null', '-S', spec['name'],
                                     sys.executable, str(Path(__file__).resolve()),
                                     '--repo-root', str(store.log_dir.parent.parent),
                                     'runner', slot.name], env=env, cwd=cwd,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True)
        except OSError as exc:
            write(slot / 'launch-error.json', {'error_type': type(exc).__name__, 'retry': False})
            write(slot / 'phase.json', {'phase': 'launch_failed', 'thread_id': None,
                                       'turns_started': 0, 'turns_completed': 0})
            raise RuntimeError('Cannot create new screen; claim consumed, no retry') from None
    # -D -m does not fork: recorded PID is this NEW server, never a discovered slot.
    for _ in range(100):
        sockets = list((slot / 's').iterdir())
        if sockets:
            full_id = f"{proc.pid}.{spec['name']}"
            st = (slot / 's' / full_id).lstat()
            write(slot / 'identity.json', {**generation(proc.pid), 'full_id': full_id,
                                           'socket': [st.st_dev, st.st_ino]})
            identity(slot)
            return slot
        if proc.poll() is not None:
            write(slot / 'launch-error.json', {'screen_exit_code': proc.returncode, 'retry': False})
            write(slot / 'phase.json', {'phase': 'launch_failed', 'thread_id': None,
                                       'turns_started': 0, 'turns_completed': 0})
            raise RuntimeError(f'New screen failed (exit {proc.returncode}); claim consumed, no retry')
        time.sleep(.05)
    write(slot / 'phase.json', {'phase': 'launch_unknown', 'thread_id': None,
                               'turns_started': 0, 'turns_completed': 0})
    raise RuntimeError('New screen socket unavailable; no retry')


class Events:
    def __init__(self):
        self.thread_id = None
        self.started = self.completed = 0
        self.failed = False

    def accept(self, event):
        kind = event.get('type')
        if kind == 'thread.started':
            value = event.get('thread_id')
            if self.thread_id is not None or str(uuid.UUID(value)) != value:
                raise RuntimeError('Ambiguous/invalid thread UUID')
            self.thread_id = value
        elif kind == 'turn.started':
            if not self.thread_id or self.started:
                raise RuntimeError('Unexpected extra turn')
            self.started += 1
        elif kind == 'turn.completed':
            if self.started != 1 or self.completed:
                raise RuntimeError('Unexpected completion')
            self.completed += 1
        elif kind in ('turn.failed', 'error'):
            self.failed = True

    def metadata(self, phase):
        return {'phase': phase, 'thread_id': self.thread_id,
                'turns_started': self.started, 'turns_completed': self.completed}

    def success(self, rc):
        return rc == 0 and self.thread_id and self.started == self.completed == 1 and not self.failed


def record_thread(spec, slot, thread_id):
    # New slots persist the UUID immediately, even if the watch process dies
    # before wait_turn returns. Legacy/manual specs remain readable.
    if 'queue_repo_root' not in spec:
        return
    module_spec = importlib.util.spec_from_file_location(
        '_queue_state', Path(__file__).with_name('gpt2codex-github-queue.py'))
    queue = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(queue)
    store = queue.Store(Path(spec['queue_repo_root']))
    with store.lock():
        state = store.load()
        job = state['jobs'].get(str(spec['number']))
        if not job or any(job.get(k) != v for k, v in (
                ('id', spec['issue_id']), ('digest', spec['digest']),
                ('project', spec['project']), ('worker_host', spec['worker_host']),
                ('screen_slot', slot.name), ('backend', spec.get('backend', 'screen')))):
            raise RuntimeError('Thread binding mismatch; no resume')
        if job.get('thread_id') not in (None, thread_id):
            raise RuntimeError('Thread identity changed; no resume')
        job['thread_id'] = thread_id
        store.save(state)


def monitor_event(spec, event):
    """Best-effort metadata from observed runner transitions, never raw events."""
    if 'queue_repo_root' not in spec:
        return  # Old/manual slots have no queue monitor binding.
    try:
        module_spec = importlib.util.spec_from_file_location(
            '_queue_monitor', Path(__file__).with_name('gpt2codex-github-queue.py'))
        queue = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(queue)
        queue.worker_event(queue.Store(Path(spec['queue_repo_root'])), event,
                           spec['number'], spec.get('backend', 'screen'), spec['mode'])
    except Exception:
        # Monitoring cannot interrupt a turn or change its result.
        pass


def slot_options(spec):
    approved_root = spec.get('approved_project_root')
    if spec.get('backend') == 'project-screen':
        import gpt2codex_queue_project as project
        kind = project.validate_start(spec.get('project_validation'), spec['project'],
                                      spec['cwd'], spec['codex'])
        if kind == 'non-git':
            approved_root = spec['cwd']
    return codex_options(spec['mode'], spec['cwd'], approved_root)


def exec_arguments(spec):
    return [spec['codex'], 'exec', *slot_options(spec), '--json']


def resume_arguments(spec, thread_id):
    return [spec['codex'], 'resume', *slot_options(spec), thread_id]


def runner(slot):
    require_legacy_launch_allowed()
    # Single-run marker is exclusive even if somebody invokes the runner twice.
    fd = os.open(slot / 'runner.claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    spec = read(slot / 'spec.json')
    monitor_event(spec, 'screen_started')
    events = Events()
    proc = None
    leases = contextlib.ExitStack()
    rc = 127
    try:
        require_legacy_launch_allowed()
        env = clean_env()
        require_chatgpt(spec['codex'], env)
        if spec.get('backend') == 'project-screen':
            import gpt2codex_queue_project as project
            leases.enter_context(project.registry_lease(spec['project_validation']['config']))
        args = exec_arguments(spec)
        if 'report_path' in spec:
            args += ['--output-schema', spec['schema_path'], '--output-last-message', spec['report_path']]
        args += ['-']
        with private(spec['prompt']).open('rb') as prompt, \
                (slot / 'events.jsonl').open('xb') as output, \
                (slot / 'exec.stderr').open('xb') as err:
            os.chmod(slot / 'events.jsonl', 0o600)
            os.chmod(slot / 'exec.stderr', 0o600)
            with contextlib.ExitStack() as executables:
                pinned = {}
                if spec.get('backend') == 'project-screen':
                    pinned = executables.enter_context(project.pinned_executable(
                        spec['codex'], spec['project_validation']['codex_stamp']))
                proc = subprocess.Popen(args, stdin=prompt, stdout=subprocess.PIPE, stderr=err,
                                        cwd=spec['cwd'], env=env, start_new_session=True, **pinned)
            write(slot / 'phase.json', events.metadata('exec_starting'))
            # Alarm bounds the whole pipe stream, including a hung/oversized event.
            def timeout(signum, frame):
                raise TimeoutError('Codex turn timeout')
            signal.signal(signal.SIGALRM, timeout)
            signal.alarm(3600)
            total = 0
            for line in iter(lambda: proc.stdout.readline(1024 * 1024 + 1), b''):
                total += len(line)
                if len(line) > 1024 * 1024 or total > 32 * 1024 * 1024:
                    raise RuntimeError('Exec output limit')
                output.write(line)
                output.flush()
                was_started = events.started
                events.accept(json.loads(line))
                if events.started and not was_started:
                    monitor_event(spec, 'turn_started')
                if events.thread_id and not spec.get('_thread_recorded'):
                    record_thread(spec, slot, events.thread_id)
                    spec['_thread_recorded'] = True
                write(slot / 'phase.json', events.metadata('turn_running'))
            rc = proc.wait(timeout=10)
            signal.alarm(0)
        if not events.success(rc):
            rc = rc or 125
    except Exception as exc:
        signal.alarm(0)
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        rc = 125
        write(slot / 'error.json', {'error_type': type(exc).__name__, 'retry': False})
    leases.close()
    result = {**events.metadata('turn_finished'), 'exit_code': rc, 'finished_at': int(time.time())}
    write(slot / 'phase.json', result)
    write(slot / 'result.json', result)  # Worker waits for this, NEVER screen startup/exit.
    if rc or spec.get('backend') == 'project-screen':
        return rc
    try:
        require_legacy_launch_allowed()
        args = resume_arguments(spec, events.thread_id)
        write(slot / 'resume.json', {'thread_id': events.thread_id, 'prompt_supplied': False,
                                    'verification': 'NOT PROVEN', 'argv': args})
        # The runner already owns the screen PTY. No stuffing, shell, or second prompt.
        print(f"\r\nQueue #{spec['number']}: turn finished; resuming SAME thread {events.thread_id}\r\n", flush=True)
        tui = subprocess.Popen(args, cwd=spec['cwd'], env=env)
        write(slot / 'phase.json', {**result, 'phase': 'tui_starting', 'tui_pid': tui.pid,
                                   'tui_ready': 'NOT PROVEN'})
        tui_rc = tui.wait()
        write(slot / 'phase.json', {**result, 'phase': 'tui_exited', 'tui_exit_code': tui_rc})
    except (OSError, RuntimeError, DispatchError) as exc:
        write(slot / 'phase.json', {**result, 'phase': 'tui_failed',
                                   'error_type': type(exc).__name__})
        return 2
    return 0


def wait_turn(slot):
    deadline = time.monotonic() + 3650
    while time.monotonic() < deadline:
        if (slot / 'result.json').exists():
            return read(slot / 'result.json')
        try:
            identity(slot)
        except (OSError, RuntimeError):
            if (slot / 'result.json').exists():
                return read(slot / 'result.json')
            raise RuntimeError('Screen lost before terminal result; no replay') from None
        time.sleep(.2)
    raise RuntimeError('Result unknown after timeout; no replay')


def completed_turn(slot, phase):
    """Read durable terminal evidence only. Never repair state or replay a job."""
    try:
        result = read(slot / 'result.json')
        thread = result['thread_id']
        uuid_ok = isinstance(thread, str) and str(uuid.UUID(thread)) == thread
        return (uuid_ok and phase.get('thread_id') == thread
                and result.get('phase') == 'turn_finished'
                and type(result.get('exit_code')) is int and result['exit_code'] == 0
                and type(result.get('finished_at')) is int and result['finished_at'] > 0
                and all(type(result.get(k)) is int and result[k] == 1
                        for k in ('turns_started', 'turns_completed'))
                and all(phase.get(k) == result[k] for k in
                        ('exit_code', 'finished_at', 'turns_started', 'turns_completed')))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


# Queue activity is intentionally sanitized before it crosses into the GUI.
# In particular, never serialize command lines, tool output, model responses,
# file paths, or raw Codex event objects to the presentation process.
_EVENT_LABELS = {
    'thread.started': 'Vlákno Codexu založeno',
    'turn.started': 'Zpracování zahájeno',
    'turn.completed': 'Zpracování dokončeno',
    'turn.failed': 'Zpracování selhalo',
    'error': 'Codex ohlásil chybu',
}
_ITEM_LABELS = {
    'command_execution': 'Práce s terminálovým nástrojem',
    'file_change': 'Změny souborů',
    'agent_message': 'Zpráva Codexu',
    'reasoning': 'Vyhodnocování postupu',
    'web_search': 'Vyhledávání',
    'mcp_tool_call': 'Volání nástroje',
    'collab_agent_tool_call': 'Spolupracující agent',
}


def recent_activity(slot):
    """Return only allowlisted event categories from a bounded log tail."""
    path = slot / 'events.jsonl'
    if not path.exists():
        return []
    with private(path).open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        start = max(0, stream.tell() - 2 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()  # A possibly incomplete leading event.
        chunk = stream.read(2 * 1024 * 1024)
    if not chunk.endswith(b'\n'):
        chunk = chunk.rsplit(b'\n', 1)[0] + b'\n' if b'\n' in chunk else b''
    labels = []
    for line in chunk.splitlines():
        if len(line) > 1024 * 1024:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError):
            continue  # The writer may still be publishing the last record.
        if not isinstance(obj, dict):
            continue
        kind = obj.get('type')
        label = _EVENT_LABELS.get(kind) if isinstance(kind, str) else None
        if kind in ('item.started', 'item.completed'):
            item = obj.get('item')
            if isinstance(item, dict):
                category = item.get('type')
                action = _ITEM_LABELS.get(category) if isinstance(category, str) else None
                if action:
                    label = action + (' - zahájeno' if kind == 'item.started' else ' - dokončeno')
        if label:
            labels.append(label)
    return labels[-30:]


def project_window_alive(slot, spec):
    """Read-only verification of a project-screen job, never an attach."""
    if spec.get('backend') != 'project-screen' or spec.get('project_window_title') != slot.name:
        return False
    try:
        import gpt2codex_queue_project as project
        view = project.session(spec['project'], spec['cwd'])
        if view['full_id'] != spec.get('project_session'):
            return False
        return any(window.endswith(':' + slot.name) for window in project.windows(view))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def runtime_verified(slot, spec):
    """Confirm server/window presence only. This cannot prove Codex progress."""
    if spec.get('backend') == 'project-screen':
        return project_window_alive(slot, spec)
    if spec.get('backend', 'screen') == 'screen':
        try:
            identity(slot)
            return True
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return False
    return False


def listing(repo):
    root = slots_root(repo)
    if not root.exists():
        return []
    private(root, True)
    rows = []
    for path in sorted(root.iterdir()):
        if not SLOT_RE.fullmatch(path.name):
            continue
        try:
            slot = find_slot(repo, path.name)
            spec = read(slot / 'spec.json')
            phase = read(slot / 'phase.json')
            turn_done = completed_turn(slot, phase)
            if spec.get('backend') == 'project-screen':
                # A project slot deliberately has no legacy identity.json or
                # socket. Do not relabel a live turn as interrupted merely
                # because the *legacy* identity check cannot succeed.
                screen_alive = (not turn_done and phase.get('phase') in
                                ('issue_accepted', 'exec_starting', 'turn_running')
                                and project_window_alive(slot, spec))
                attachable = False  # A project-screen exec is NEVER a TUI.
            else:
                try:
                    identity(slot)
                    screen_alive = True
                    attachable = phase.get('phase') == 'tui_starting'
                except (OSError, RuntimeError, ValueError, KeyError):
                    screen_alive = False
                    attachable = False
                    old_phase = phase.get('phase')
                    if old_phase in ('issue_accepted', 'exec_starting', 'turn_running',
                                     'tui_starting', 'tui_ready', 'interrupted_unknown'):
                        if turn_done:
                            phase = {**phase, 'last_phase': phase.get('last_phase', old_phase),
                                     'phase': 'turn_done', 'tui_state': 'tui_closed'}
                        else:
                            phase = {**phase, 'last_phase': phase.get('last_phase', old_phase),
                                     'phase': 'interrupted_unknown'}
            if attachable and phase['phase'] == 'tui_starting' and (slot / 'ready.json').exists():
                ready = read(slot / 'ready.json')
                if ready.get('thread_id') == phase.get('thread_id'):
                    phase = {**phase, 'phase': 'tui_ready', 'tui_ready': 'operator_visual'}
            rows.append({'slot': path.name, 'number': spec['number'], 'project': spec['project'],
                         **phase, 'turn_done': turn_done,
                         'screen_alive': screen_alive, 'attachable': attachable})
        except (OSError, RuntimeError, ValueError, KeyError):
            continue
    return rows


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', default=str(Path(__file__).resolve().parent))
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    for name in ('attach', 'runner', 'project-runner', 'confirm-ready'):
        sub.add_parser(name).add_argument('slot')
    args = parser.parse_args()
    if args.action == 'list':
        print(json.dumps(listing(args.repo_root)))
        return 0
    slot = find_slot(args.repo_root, args.slot)
    if args.action == 'runner':
        if read(slot / 'spec.json').get('backend', 'screen') != 'screen':
            raise RuntimeError('Legacy runner requires a legacy screen slot')
        return runner(slot)
    if args.action == 'project-runner':
        spec = read(slot / 'spec.json')
        if (spec.get('backend') != 'project-screen'
                or spec.get('project_session') != os.environ.get('STY')
                or not os.environ.get('WINDOW', '').isdigit()
                or not spec.get('project_window_title')):
            raise RuntimeError('Project runner/session identity mismatch')
        return runner(slot)
    saved = identity(slot)
    phase = read(slot / 'phase.json')
    if phase.get('phase') != 'tui_starting':
        raise RuntimeError('TUI is not running; exec has no live interactive attachment')
    if args.action == 'confirm-ready':
        phase = read(slot / 'phase.json')
        if phase['phase'] != 'tui_starting' or not sys.stdin.isatty():
            raise RuntimeError('Requires local terminal and running TUI')
        if input(f"Visually verified SAME thread {phase['thread_id']} and idle input in Qt6? Type READY: ") != 'READY':
            raise RuntimeError('Readiness not confirmed')
        current = read(slot / 'phase.json')
        if current != phase:
            raise RuntimeError('TUI changed during confirmation')
        write(slot / 'ready.json', {'thread_id': phase['thread_id'], 'source': 'operator_visual',
                                   'at': int(time.time())})
        # Separate evidence file avoids racing the runner's lifecycle updates.
        return 0
    env = clean_env()
    env['SCREENDIR'] = str(slot / 's')
    # -r refuses an attached session; never force-detach another GUI/client.
    os.execvpe('screen', ['screen', '-r', saved['full_id']], env)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f'QUEUE_SCREEN_ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(2)
