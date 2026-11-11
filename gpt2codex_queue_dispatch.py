"""Offline candidate for the project dispatcher; NOT a process/session backend.

No Codex launch, screen, GitHub, authentication, or legacy-state operations.
The opt-in RustSupervisorProbe runs only the read-only Rust admission command;
reserve_managed is deny-only. The old reserve/bind/close APIs remain fixtures.
A future runtime must claim atomically, bind its owned process and descendants,
and prove closure. Until all managed start/resume paths participate,
project-screen MUST remain unavailable.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import time
import uuid


ACTIVE = ('running', 'finishing', 'interrupted_unknown')
STATES = ('pending', 'approved', 'waiting', *ACTIVE, 'done', 'error')


class DispatchError(RuntimeError):
    pass


def require_legacy_launch_allowed(environment=None):
    """Match Rust's STOP gate at every Python launch, including old resumes.

    Neither variable grants a lifetime lock. In particular an old screen
    runner must not bypass the gate just because its approval predates it.
    Keep this check before authentication, output creation and process spawn.
    """
    if any(name in os.environ or (environment is not None and name in environment) for name in
           ('G2C_PROJECT_SUPERVISOR', 'G2C_PROJECT_SUPERVISOR_DIRECTORY')):
        raise DispatchError('session_unverified: project supervisor runtime_disabled; no legacy fallback')


class RustSupervisorProbe:
    """Deny-only bridge to the authoritative Rust reservation namespace.

    Explicit paths, no defaults, claim, screen access, token, or launch action.
    The bounded child is only `supervisor admission`. No response can grant
    execution. A replaced namespace or malformed output becomes unverified.
    This does not make the offline SQLite ledger a production supervisor.
    """
    def __init__(self, executable, config, directory):
        self.executable, self.config, self.directory = map(Path, (executable, config, directory))
        if any(not p.is_absolute() for p in (self.executable, self.config, self.directory)):
            raise DispatchError('Explicit absolute supervisor paths required')
        # Pin identity for the lifetime of this adapter, including across calls.
        self.namespace = self._namespace()

    def _namespace(self):
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or self.directory.resolve() != self.directory):
            raise DispatchError('Unverified supervisor namespace')
        return info.st_dev, info.st_ino

    def _read(self, project):
        args = [str(self.executable), 'supervisor', 'admission', '--config', str(self.config),
                '--directory', str(self.directory), '--id', project]
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env={'LANG': 'C', 'PATH': '/usr/bin:/bin'},
                              close_fds=True) as child:
            try:
                data = bytearray()
                deadline = time.monotonic() + 3
                with selectors.DefaultSelector() as poll:
                    poll.register(child.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not poll.select(remaining):
                            raise DispatchError('Supervisor inspection timeout')
                        chunk = os.read(child.stdout.fileno(), 8193 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > 8192:
                            raise DispatchError('Supervisor inspection too large')
                if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                    raise DispatchError('Supervisor inspection failed')
                return json.loads(data)
            finally:
                if child.poll() is None:
                    child.kill()  # Only this adapter's own read-only CLI child.
                child.wait()

    def waiting_reason(self, project, root):
        """Return a local machine reason; never return an execution permission."""
        try:
            if (not isinstance(project, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', project)
                    or not Path(root).is_absolute() or not Path(root).is_dir()
                    or self._namespace() != self.namespace):
                return 'session_unverified'
            root = str(Path(root).resolve(strict=True))
            view = self._read(project)
            fields = {'version', 'project_id', 'root', 'namespace_device', 'namespace_inode',
                      'runtime_enabled', 'launch_permitted', 'waiting_reason', 'detail',
                      'reservation', 'owner', 'recovery_required'}
            if (not isinstance(view, dict) or set(view) != fields
                    or type(view['version']) is not int or view['version'] != 1
                    or view['project_id'] != project or view['root'] != root
                    or view['runtime_enabled'] is not False or view['launch_permitted'] is not False
                    or type(view['namespace_device']) is not int
                    or type(view['namespace_inode']) is not int
                    or (view['namespace_device'], view['namespace_inode']) != self.namespace
                    or self._namespace() != self.namespace):
                return 'session_unverified'
            state = (view['reservation'], view['waiting_reason'], view['detail'])
            if state == ('pending', 'project_busy', 'pending_reservation'):
                if view['owner'] in ('manual', 'queue') and view['recovery_required'] is True:
                    return 'project_busy'
            elif state == ('conflict', 'project_busy', 'overlapping_reservation'):
                if view['owner'] is None and view['recovery_required'] is True:
                    return 'project_busy'
            # Absent, unknown, incompatible future schema and even forged launch
            # grants all deny. An absent claim is NOT proof of absent Codex.
            return 'session_unverified'
        except (OSError, ValueError, TypeError, DispatchError, subprocess.SubprocessError):
            return 'session_unverified'


def process_identity(pid):
    """Metadata of an owned process only; never reads argv, environment or auth."""
    if type(pid) is not int or pid <= 0:
        raise DispatchError('Invalid process id')
    proc = Path('/proc') / str(pid)
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'ticks': int(fields[19]), 'uid': proc.stat().st_uid,
            'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pidns': os.readlink(proc / 'ns/pid')}


def inspect_process(saved):
    """Return live/gone/unknown; inability to inspect is never proof of exit."""
    if not isinstance(saved, dict) or set(saved) != {'pid', 'ticks', 'uid', 'boot', 'pidns'}:
        return 'unknown'
    try:
        if (saved['uid'] != os.getuid() or saved['pidns'] != os.readlink('/proc/self/ns/pid')
                or saved['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
            return 'unknown'
        try:
            current = process_identity(saved['pid'])
        except FileNotFoundError:
            # A missing procfs subfile is not proof that the PID is absent.
            try:
                (Path('/proc') / str(saved['pid'])).lstat()
            except FileNotFoundError:
                return 'gone'
            return 'unknown'
        # A different generation is ambiguity, never permission to release.
        return 'live' if current == saved else 'unknown'
    except (OSError, ValueError, IndexError, DispatchError):
        return 'unknown'


class Ledger:
    """Single local summary and event ledger, deliberately separate from v1 state.

    The durable reservation is also the project lock: it survives dispatcher
    death and cannot expire by timeout. All state and events commit together.
    Capacity counts unresolved queue claims across restarts. Manual/resume
    reservations share exclusivity but do not consume queue capacity.
    Session observations are adapter inputs, not verified by this module.
    """
    def __init__(self, directory, capacity=2):
        if type(capacity) is not int or not 1 <= capacity <= 32:
            raise DispatchError('Invalid capacity')
        self.directory = Path(directory)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise DispatchError('Ledger requires an existing private directory')
        self.path = self.directory / 'project-dispatch.sqlite3'
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise DispatchError('Unsafe ledger file')
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, version INTEGER, capacity INTEGER)')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (1, 1, ?)', (capacity,))
            if db.execute('SELECT version, capacity FROM metadata WHERE id=1').fetchone() != (1, capacity):
                raise DispatchError('Ledger version/capacity mismatch; explicit review required')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY, key TEXT UNIQUE, root TEXT, status TEXT, body TEXT)')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS exclusive_project ON jobs(root) WHERE status IN ('running','finishing','interrupted_unknown')")
            db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, job TEXT, state TEXT, at INTEGER, reason TEXT)')
        self.capacity = capacity

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def _get(self, db, key):
        row = db.execute('SELECT body FROM jobs WHERE key=?', (key,)).fetchone()
        if row is None:
            raise DispatchError('Unknown job')
        return json.loads(row[0])

    def _save(self, db, job, state, reason=None):
        if state not in STATES:
            raise DispatchError('Unknown state')
        job.update(status=state, waiting_reason=reason)
        db.execute('UPDATE jobs SET status=?, body=? WHERE key=?',
                   (state, json.dumps(job, sort_keys=True), job['key']))
        db.execute('INSERT INTO events(job,state,at,reason) VALUES (?,?,?,?)',
                   (job['key'], state, time.time_ns(), reason))

    def submit(self, key, *, issue, digest, repository, project, root, hostname,
               mode, source='queue'):
        if (not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}', key)
                or type(issue) is not int or issue <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', project)
                or not repository or not hostname
                or mode not in ('read-only', 'workspace-write')
                or source not in ('queue', 'manual', 'resume')):
            raise DispatchError('Invalid immutable job binding')
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            raise DispatchError('Invalid root')
        root = str(path.resolve(strict=True))
        binding = dict(key=key, issue=issue, digest=digest, repository=repository,
                       project=project, root=root, hostname=hostname, mode=mode, source=source)
        with self.transaction() as db:
            old = db.execute('SELECT body FROM jobs WHERE key=?', (key,)).fetchone()
            if old:
                job = json.loads(old[0])
                if any(job[k] != v for k, v in binding.items()):
                    raise DispatchError('Job binding changed')
                return job
            for (body,) in db.execute('SELECT body FROM jobs'):
                other = json.loads(body)
                if (source == other['source'] == 'queue' and issue == other['issue']
                        and repository == other['repository'] and digest == other['digest']):
                    raise DispatchError('Duplicate approval identity')
                other_root = Path(other['root'])
                if other_root != path.resolve() and (other_root in path.resolve().parents
                                                     or path.resolve() in other_root.parents):
                    raise DispatchError('Overlapping project roots')
            job = dict(binding, status='pending', waiting_reason=None, thread_uuid=None,
                       screen_session=None, window_id=None, started_at=None, ended_at=None,
                       lifecycle='closed', claim=None, process=None, output=None)
            db.execute('INSERT INTO jobs(key,root,status,body) VALUES (?,?,?,?)',
                       (key, root, 'pending', json.dumps(job)))
            self._save(db, job, 'pending')
            return job

    def approve(self, key):
        # Caller must first perform existing SHA/host/repo/allowlist approval.
        with self.transaction() as db:
            job = self._get(db, key)
            if job['status'] != 'pending':
                raise DispatchError('Approval already consumed')
            self._save(db, job, 'approved')

    def reserve(self, key, observation):
        """Atomically reserve, never launch. observation is a future adapter contract.

        Must represent a registered enabled project, exact canonical root and
        server generation, plus a process scan including active AND idle TUI.
        No observation from screen names alone can authorize a launch.
        """
        with self.transaction() as db:
            job = self._get(db, key)
            if job['status'] not in ('approved', 'waiting'):
                raise DispatchError('Claim consumed or unapproved')
            all_jobs = [(seq, json.loads(body)) for seq, body in
                        db.execute('SELECT seq, body FROM jobs ORDER BY seq')]
            reason = None
            if any(j['root'] == job['root'] and j['status'] in ACTIVE for _, j in all_jobs):
                reason = 'project_busy'
            elif next(j['key'] for _, j in all_jobs if j['root'] == job['root']
                      and j['status'] in ('approved', 'waiting')) != key:
                reason = 'project_fifo'
            elif job['source'] == 'queue' and sum(j['source'] == 'queue' and j['status'] in ACTIVE
                                                 for _, j in all_jobs) >= self.capacity:
                reason = 'capacity'
            elif observation.get('enabled') is False:
                reason = 'project_disabled'
            elif (observation.get('root') != job['root'] or observation.get('project') != job['project']
                  or observation.get('verified') is not True
                  or not isinstance(observation.get('session'), str)
                  or not re.fullmatch(r'[1-9][0-9]*\.g2c-' + re.escape(job['project']), observation['session'])
                  or type(observation.get('server_ticks')) is not int
                  or observation['server_ticks'] <= 0
                  or observation.get('codex') not in ('absent', 'active', 'idle')):
                reason = 'session_unverified'
            elif observation['codex'] != 'absent':
                reason = 'project_busy'
            if reason:
                self._save(db, job, 'waiting', reason)
                return None
            job.update(claim=uuid.uuid4().hex, started_at=time.time_ns(), lifecycle='unknown',
                       screen_session=observation['session'], server_ticks=observation['server_ticks'])
            self._save(db, job, 'running')
            return job

    def reserve_managed(self, key, supervisor):
        """Feature-gated integration hook; current runtime always leaves waiting.

        The legacy fixture `reserve` is intentionally separate. Neither a caller
        supplied `codex=absent` nor a Rust snapshot can authorize managed launch.
        No claim/token consumption or fallback, including after restart.
        """
        with self.transaction() as db:
            job = self._get(db, key)
            if job['status'] not in ('approved', 'waiting'):
                raise DispatchError('Claim consumed or unapproved')
        reason = supervisor.waiting_reason(job['project'], job['root'])
        if reason not in ('project_busy', 'session_unverified'):
            reason = 'session_unverified'
        with self.transaction() as db:
            job = self._get(db, key)
            if job['status'] not in ('approved', 'waiting'):
                raise DispatchError('Claim consumed or unapproved')
            self._save(db, job, 'waiting', reason)
        return None

    def _owned(self, db, key, claim):
        job = self._get(db, key)
        if not claim or job['claim'] != claim or job['status'] not in ACTIVE:
            raise DispatchError('Claim identity mismatch')
        return job

    def bind_process(self, key, claim, window_id, process):
        with self.transaction() as db:
            job = self._owned(db, key, claim)
            if (job['status'] != 'running' or job['process'] is not None
                    or type(window_id) is not int or window_id < 0
                    or inspect_process(process) != 'live'):
                raise DispatchError('Invalid process/window binding')
            job.update(process=process, window_id=window_id, lifecycle='live')
            self._save(db, job, 'running')

    def finish_turn(self, key, claim, thread_uuid, output):
        with self.transaction() as db:
            job = self._owned(db, key, claim)
            if (job['status'] != 'running' or job['process'] is None
                    or str(uuid.UUID(thread_uuid)) != thread_uuid
                    or not isinstance(output, str) or not output or len(output) > 4096):
                raise DispatchError('Invalid completion evidence')
            if any(json.loads(body).get('thread_uuid') == thread_uuid for (body,) in
                   db.execute('SELECT body FROM jobs')):
                raise DispatchError('Thread UUID reused')
            job.update(thread_uuid=thread_uuid, output=output)
            self._save(db, job, 'finishing')

    def close(self, key, claim, session, window_id, *, window_closed, success=True):
        # Future supervisor must also verify its descendants and exact window
        # generation. This module cannot supply those proofs from screen APIs.
        with self.transaction() as db:
            job = self._owned(db, key, claim)
            if (job['status'] != 'finishing' or session != job['screen_session']
                    or type(window_id) is not int or window_id != job['window_id']
                    or window_closed is not True or inspect_process(job['process']) != 'gone'):
                raise DispatchError('Closure not proven; project remains reserved')
            job.update(ended_at=time.time_ns(), lifecycle='closed')
            self._save(db, job, 'done' if success else 'error')

    def recover(self):
        # A restart NEVER launches or releases an unresolved reservation.
        with self.transaction() as db:
            for (body,) in db.execute('SELECT body FROM jobs').fetchall():
                job = json.loads(body)
                if job['status'] in ACTIVE:
                    observed = inspect_process(job['process'])
                    job['lifecycle'] = 'live' if observed == 'live' else 'unknown'
                    if observed == 'gone' and job['status'] == 'finishing':
                        # Completion evidence exists; still hold the reservation
                        # until the adapter proves the exact window is closed.
                        self._save(db, job, 'finishing', 'window_verification')
                    elif observed != 'live':
                        self._save(db, job, 'interrupted_unknown', 'recovery_review')
                    else:
                        self._save(db, job, job['status'], job['waiting_reason'])

    def summary(self):
        with self.transaction() as db:
            rows = [json.loads(body) for (body,) in db.execute('SELECT body FROM jobs ORDER BY seq')]
            if any(row['status'] not in STATES for row in rows):
                raise DispatchError('Unknown state')
            return {'version': 1, 'capacity': self.capacity, 'jobs': rows}
