#!/usr/bin/env python3
"""Experimental single-job project-screen Queue backend.

No project-screen server is created, adopted, killed, or replaced. A human must
first explicitly add a non-Codex anchor to the existing registered screen and
exit any manual Codex. The legacy backends are not modified. The canary is
opt-in per private config and is deliberately single-job, not a concurrent
project dispatcher. It is not a substitute for the unfinished Rust supervisor.
"""
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

import gpt2codex_queue_screen as slots

PROJECT_RE = re.compile(r'[a-z0-9_-]{1,64}\Z')
WINDOW_RE = re.compile(r'[0-9]+:[a-zA-Z0-9_-]+\Z')
ANCHOR_TITLE = 'g2c-anchor'


class ProjectScreenError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise ProjectScreenError(reason)


def _proc_stat(pid):
    raw = (Path('/proc') / str(pid) / 'stat').read_text()
    fields = raw.rsplit(')', 1)[1].split()
    return int(fields[1]), int(fields[19])  # parent pid, start ticks


def _children(pid):
    raw = (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').read_text()
    return [int(value) for value in raw.split()]


def _same_binary(pid, path):
    observed = (Path('/proc') / str(pid) / 'exe').stat()
    expected = Path(path).stat()
    return (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)


def _socket_directory():
    # Queue canary currently supports Debian's user-owned GNU screen sockets.
    user = pwd.getpwuid(os.getuid()).pw_name
    base = Path('/run/screen') / ('S-' + user)
    info = base.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and base.resolve() == base,
            'Project screen socket directory is not verified')
    return base


def _peer(socket_path, server_pid):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(0.5)
        conn.connect(str(socket_path))
        pid, uid, _gid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(pid == server_pid and uid == os.getuid(), 'GNU screen socket peer changed')


def session(project, root):
    """Only the Rust-registered PID/full socket is accepted, never name search."""
    require(isinstance(project, str) and PROJECT_RE.fullmatch(project), 'Project ID invalid')
    root = Path(root).resolve(strict=True)
    record_path = Path.home() / '.local/state/gpt2codex/screen-sessions' / (project + '.json')
    record = slots.read(record_path)
    require(isinstance(record, dict) and record.get('version') == 1
            and record.get('multiplexer', 'screen') == 'screen'
            and record.get('project_id') == project and record.get('root') == str(root)
            and not record.get('tmux') and not record.get('tmux_lifecycle'),
            'Project screen registry identity mismatch')
    expected_name = 'g2c-' + project
    pid = record.get('pid')
    require(type(pid) is int and pid > 0 and record.get('screen_name') == expected_name
            and record.get('full_id') == f'{pid}.{expected_name}',
            'Project screen server identity mismatch')
    gen = slots.generation(pid)
    require(gen['ticks'] == record.get('process_start_ticks') and _same_binary(pid, '/usr/bin/screen'),
            'Project screen process generation/executable changed')
    directory = _socket_directory()
    socket_path = directory / record['full_id']
    st = socket_path.lstat()
    require(stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid(),
            'Project screen socket identity invalid')
    _peer(socket_path, pid)
    # A second registry read closes the read-then-connect identity race.
    require(slots.read(record_path) == record and slots.generation(pid) == gen,
            'Project screen changed during verification')
    return {'project': project, 'root': str(root), 'pid': pid, 'generation': gen,
            'full_id': record['full_id'], 'socket': [st.st_dev, st.st_ino],
            'directory': str(directory)}


def _recheck(view):
    current = session(view['project'], view['root'])
    require(current == view, 'Project screen server/socket changed')


def _screen(view, arguments):
    _recheck(view)
    env = slots.clean_env()
    env.update({'SCREENDIR': view['directory'], 'LC_ALL': 'C', 'TERM': 'xterm'})
    completed = subprocess.run(['/usr/bin/screen', '-S', view['full_id'], *arguments],
                               cwd=view['root'], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=6, check=False)
    require(completed.returncode == 0, 'Scoped screen command rejected')
    require(len(completed.stdout) <= 4096, 'Screen response oversized')
    _recheck(view)
    return completed.stdout.decode('utf-8', 'strict').strip()


def windows(view):
    output = _screen(view, ['-Q', 'windows', '%n:%t '])
    values = output.split()
    require(len(values) <= 32 and all(WINDOW_RE.fullmatch(v) for v in values)
            and len({v.split(':')[0] for v in values}) == len(values),
            'Unverified GNU screen window inventory')
    return values


def _anchor_file(store, project, create=False):
    anchors = store.state_dir / 'project-anchors'
    if create and not anchors.exists():
        anchors.mkdir(mode=0o700)
    slots.private(anchors, True)
    return anchors / (project + '.json')


def _anchor_pid(view):
    # Other windows/children may exist during opt-in setup, but only ONE
    # directly-owned /usr/bin/sleep infinity may be adopted by this helper.
    candidates = []
    for pid in _children(view['pid']):
        try:
            if not _same_binary(pid, '/usr/bin/sleep'):
                continue
            argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
            cwd = os.readlink(f'/proc/{pid}/cwd')
            if argv[-2:] not in ([b'infinity', b''], [b'--', b'infinity']):
                continue
            if cwd != view['root'] or _proc_stat(pid)[0] != view['pid']:
                continue
            candidates.append(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            raise ProjectScreenError('Cannot verify anchor process') from None
    require(len(candidates) == 1, 'Expected exactly one private screen anchor')
    return candidates[0]


def _old_process_gone(pid, generation):
    """Only an absent PID or a provably different generation is safe to retire."""
    require(type(pid) is int and pid > 0 and isinstance(generation, dict)
            and generation.get('pid') == pid
            and type(generation.get('ticks')) is int
            and isinstance(generation.get('boot'), str),
            'Unsafe old anchor generation; manual inspection required')
    try:
        current = slots.generation(pid)
    except (FileNotFoundError, ProcessLookupError):
        return True
    return current != generation


def _archive_stale_anchor(path, project, view):
    """Preserve a dead predecessor's record; do not overwrite/reuse it."""
    if not path.exists():
        return
    old = slots.read(path)
    before = old.get('view') if isinstance(old, dict) else None
    require(isinstance(before, dict) and before.get('project') == project
            and before.get('root') == view['root']
            and isinstance(before.get('generation'), dict)
            and before != view and _old_process_gone(before.get('pid'), before['generation'])
            and _old_process_gone(old.get('pid'), old.get('generation')),
            'Old anchor record is live, ambiguous or unrelated; refuse replacement')
    archive = path.with_name(f'{project}.stale-{uuid.uuid4().hex}.json')
    slots.private(path)
    os.link(path, archive, follow_symlinks=False)  # Exclusive, never overwrite evidence.
    os.unlink(path)
    # A failed write afterwards leaves the old record archived and re-runnable.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_anchor(store, project, root):
    """Record an existing *anchor-first* Rust-managed project screen only.

    No Codex, screen window, or server is started, replaced or stopped here.
    A dead predecessor record is preserved by explicit operator approval.
    """
    view = session(project, root)
    before = windows(view)
    require(before == [f'0:{ANCHOR_TITLE}'],
            'Project screen is not anchor-first; use screen start --anchor-only after safe recovery')
    pid = _anchor_pid(view)
    require(_children(view['pid']) == [pid],
            'Unverified anchor-only screen process tree')
    path = _anchor_file(store, project, create=True)
    _archive_stale_anchor(path, project, view)
    require(not path.exists(), 'Anchor record still exists after archival')
    saved = {'view': view, 'number': 0, 'pid': pid,
             'generation': slots.generation(pid)}
    slots.write(path, saved)
    return saved


def anchor_ready(store, project, root, *, idle=True):
    view = session(project, root)
    record = slots.read(_anchor_file(store, project))
    require(record.get('view') == view and type(record.get('number')) is int,
            'Anchor registry/server mismatch')
    pid = record['pid']
    require(type(pid) is int and pid > 0 and slots.generation(pid) == record['generation']
            and _same_binary(pid, '/usr/bin/sleep')
            and _proc_stat(pid)[0] == view['pid']
            and os.readlink(f'/proc/{pid}/cwd') == view['root'],
            'Anchor process identity mismatch')
    values = windows(view)
    title = f"{record['number']}:{ANCHOR_TITLE}"
    require(title in values, 'Anchor window lost')
    if idle:
        require(values == [title] and _children(view['pid']) == [pid],
                'project_busy: existing manual screen window/process')
        _no_external_codex(view['root'])
    return view, record


def _no_external_codex(root):
    """Best-effort managed-process admission, never an arbitrary kill API."""
    base = Path(root)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'status').stat().st_uid != os.getuid():
                continue
            comm = (proc / 'comm').read_text().strip()
            if not comm.startswith('codex'):
                continue
            cwd = Path(os.readlink(proc / 'cwd'))
            if cwd == base or base in cwd.parents:
                raise ProjectScreenError('project_busy: Codex already running in workspace')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ProjectScreenError('Codex process inspection denied') from None


def launch(store, item, codex, root, prompt, env=None):
    """Single-use launch into an operator-prepared, otherwise idle project screen."""
    if env and any(k.startswith('G2C_PROJECT_SUPERVISOR') for k in env):
        raise ProjectScreenError('Supervisor STOP gate set; project canary forbidden')
    slots.require_legacy_launch_allowed(env)
    view, anchor = anchor_ready(store, item['project'], root)
    slots.require_chatgpt(codex, slots.clean_env())
    # A second check immediately before launch prevents the common manual race.
    anchor_ready(store, item['project'], root)
    slot = slots.prepare_slot(store, item, codex, str(Path(root).resolve()), prompt,
                              backend='project-screen')
    spec = slots.read(slot / 'spec.json')
    title = 'q' + str(item['number']) + '-' + slot.name.rsplit('-', 1)[1]
    require(re.fullmatch(r'q[0-9]+-[0-9a-f]{12}', title), 'Invalid project job title')
    spec.update(project_session=view['full_id'], project_window_title=title,
                project_anchor=anchor)
    slots.write(slot / 'spec.json', spec)
    _screen(view, ['-X', 'screen', '-t', title, sys.executable, '-B',
                   str(Path(slots.__file__).resolve()), '--repo-root',
                   str(store.log_dir.parent.parent), 'project-runner', slot.name])
    # Never resubmit this command, even if the runner did not start: the claim
    # and slot have already been committed to durable local state.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if (slot / 'runner.claim').exists() or (slot / 'result.json').exists():
            return slot, view, anchor, title
        _recheck(view)
        time.sleep(0.05)
    raise ProjectScreenError('Project runner startup unknown; no replay')


def wait_turn(slot, view, anchor, title):
    deadline = time.monotonic() + 3660
    while time.monotonic() < deadline:
        if (slot / 'result.json').exists():
            result = slots.read(slot / 'result.json')
            require(result.get('phase') == 'turn_finished', 'Project result identity invalid')
            # The runner writes the result before exiting; wait for only its
            # own screen window to disappear and the anchor to remain.
            for _ in range(200):
                current = windows(view)
                if not any(w.endswith(':' + title) for w in current):
                    expected_anchor = f"{anchor['number']}:{ANCHOR_TITLE}"
                    require(current == [expected_anchor]
                            and slots.generation(anchor['pid']) == anchor['generation']
                            and _children(view['pid']) == [anchor['pid']],
                            'Project window closed but workspace is not exclusively idle')
                    return result
                time.sleep(0.05)
            raise ProjectScreenError('Project runner completed but job window still alive')
        _recheck(view)
        time.sleep(0.2)
    raise ProjectScreenError('Project result unknown after timeout; no replay')
