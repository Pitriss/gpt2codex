#!/usr/bin/env python3
"""Local GitHub Issues inbox for gpt2codex. No remote shell or session injection.

An issue is only a REQUEST. Execution always requires local, digest-bound,
interactive approval and runs via a separate non-interactive Codex process.
"""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
import gpt2codex_queue_handoff as handoff
import gpt2codex_queue_dispatch as dispatch
from gpt2codex_queue_codex import git_check_options

VERSION = 1
MAX_RESPONSE = 8 * 1024 * 1024
MAX_TASK = 12000
MAX_SUMMARY = 12000
MAX_ISSUES_PAGES = 20
API = "https://api.github.com"
TITLE_PREFIX = "[g2c] "
FORM_RE = re.compile(
    r"\A### Projekt\s*\n\s*([^\n]+)\s*\n+"
    r"### (?:Rezim|Re\u017eim)\s*\n\s*([^\n]+)\s*\n+"
    r"### (?:Zadani|Zad\u00e1n\u00ed)\s*\n([\s\S]*)\Z"
)


class QueueError(Exception):
    pass


class PostRejected(QueueError):
    """GitHub explicitly rejected a write; safe to retry publication only."""


def secure_private_file(path, max_size=64 * 1024):
    path = Path(path).expanduser()
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise QueueError(f"Private file ownership/mode invalid: {path.name}")
    if st.st_size > max_size:
        raise QueueError("Private file unexpectedly large")
    return path


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise QueueError(f"Private directory ownership/mode invalid: {path.name}")
    return path


def secure_project_dir(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or not p.is_dir():
        raise QueueError("Configured project root missing or relative")
    return str(p.resolve(strict=True))


def default_config_path():
    # Keep queue credentials in their own private directory; do not chmod
    # the pre-existing ~/.config/gpt2codex directory used by other software.
    return Path.home() / ".config/gpt2codex/github-queue/config.json"


def config_default(repo):
    base = str(Path.home() / "TEMP/mycode")
    return {
        "version": VERSION,
        "repository": repo,
        "worker_hostname": socket.gethostname(),
        "execution_backend": "exec",
        "allowed_authors": ["Pitriss"],
        "token_file": str(Path.home() / ".config/gpt2codex/github-queue.token"),
        "codex_bin": str(Path.home() / ".local/bin/codex"),
        "project_roots": {name: f"{base}/{name}" for name in (
            "gpt2codex", "kencoder", "k2-smb", "m365-tui", "reddit2tg"
        )},
    }


def load_config(filename):
    path = secure_private_file(filename)
    with path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if type(cfg) is not dict or cfg.get("version") != VERSION:
        raise QueueError("Config version invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg.get("repository", "")):
        raise QueueError("Repository format invalid")
    if cfg.get("worker_hostname") != socket.gethostname():
        raise QueueError("Worker hostname mismatch; this machine is not authorized")
    authors = cfg.get("allowed_authors")
    if not isinstance(authors, list) or not authors or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", x) for x in authors):
        raise QueueError("allowed_authors invalid")
    roots = cfg.get("project_roots")
    if not isinstance(roots, dict) or not roots or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or not isinstance(root, str) or not Path(root).is_absolute() for name, root in roots.items()):
        raise QueueError("project_roots invalid")
    bin_path = cfg.get("codex_bin")
    if not isinstance(bin_path, str) or not Path(bin_path).is_absolute():
        raise QueueError("codex_bin must be an absolute path")
    if not isinstance(cfg.get("token_file"), str) or not Path(cfg["token_file"]).expanduser().is_absolute():
        raise QueueError("token_file must be an absolute path")
    for key in ('bridge_cli', 'registry_config'):
        if key in cfg and (not isinstance(cfg[key], str) or not Path(cfg[key]).is_absolute()):
            raise QueueError(f'{key} must be an absolute path')
    execution_backend(cfg)
    if 'project_screen_canary' in cfg and type(cfg['project_screen_canary']) is not bool:
        raise QueueError('project_screen_canary must be boolean')
    if 'project_parallel_canary' in cfg and type(cfg['project_parallel_canary']) is not bool:
        raise QueueError('project_parallel_canary must be boolean')
    if cfg.get('project_parallel_canary') is True and cfg.get('project_screen_canary') is not True:
        raise QueueError('Parallel canary requires project_screen_canary=true')
    parallel_capacity(cfg)
    return cfg


def parallel_capacity(cfg):
    value = cfg.get('project_parallel_capacity', 2)
    if type(value) is not int or not 1 <= value <= 4:
        raise QueueError('project_parallel_capacity must be integer 1..4')
    return value


def parallel_enabled(cfg):
    return cfg.get('project_parallel_canary') is True


def execution_backend(cfg):
    value = cfg.get("execution_backend", "exec")
    if value not in ("exec", "screen", "project-screen"):
        raise QueueError("execution_backend must be exec, screen or project-screen")
    return value


def require_runnable_backend(backend, cfg=None):
    if backend == "project-screen":
        # Deliberate local canary: requires an extra explicit private setting,
        # a registered screen and an operator-prepared non-Codex anchor.
        # This is NOT the still-disabled Rust production supervisor.
        if cfg is None or cfg.get("project_screen_canary") is not True:
            raise QueueError("project-screen canary not enabled in private config")
        if not Path('/usr/bin/screen').is_file():
            raise QueueError("GNU screen unavailable for project canary")
    elif backend not in ("exec", "screen"):
        raise QueueError("Unknown execution backend")
    try:
        dispatch.require_legacy_launch_allowed()
    except dispatch.DispatchError as exc:
        raise QueueError(str(exc)) from None


def approved_backend(job):
    """Use the approval's backend, never the worker's current default.

    Missing backend is supported only for legacy approvals. An explicit
    backend must agree with the status that was approved; no conversion.
    """
    status = job.get("status")
    state_to_backend = {"approved": "exec", "approved-screen": "screen",
                        "approved-project-screen": "project-screen"}
    if status not in state_to_backend:
        raise QueueError("Job is not approved")
    backend = job.get("backend", state_to_backend[status])
    if backend != state_to_backend[status]:
        raise QueueError("Approval backend/status mismatch")
    return backend


def read_token(cfg):
    with secure_private_file(cfg["token_file"]).open(encoding="utf-8") as f:
        token = f.read().strip()
    if not re.fullmatch(r"(?:github_pat_|ghp_|ghu_|ghs_)[A-Za-z0-9_]+", token):
        raise QueueError("GitHub token format invalid")
    return token


class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo = token, repo

    def request(self, method, path, payload=None):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gpt2codex-github-queue/1",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                raw = res.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise QueueError("GitHub response too large")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if method == "POST" and exc.code in (400, 401, 403, 404, 405, 410, 422, 429):
                raise PostRejected(f"GitHub rejected POST: HTTP {exc.code}") from None
            raise QueueError(f"GitHub HTTP {exc.code}; check repo access/token permissions") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise QueueError("GitHub response unavailable; write outcome may be unknown") from None

    def validate_repo(self):
        value = self.request("GET", f"/repos/{self.repo}")
        if (not value.get("private") or value.get("archived") or
                not value.get("has_issues") or value.get("full_name", "").casefold() != self.repo.casefold()):
            raise QueueError("Queue repository MUST be private, active, and have Issues enabled")

    def issues(self):
        for page in range(1, MAX_ISSUES_PAGES + 1):
            q = urllib.parse.urlencode({"state": "open", "sort": "created", "direction": "desc", "per_page": 100, "page": page})
            values = self.request("GET", f"/repos/{self.repo}/issues?{q}")
            for value in values:
                if "pull_request" not in value:
                    yield value
            if len(values) < 100:
                return
        raise QueueError("Issue scan pagination cap reached; refuse partial scan")

    def issue(self, number):
        return self.request("GET", f"/repos/{self.repo}/issues/{number}")

    def comment(self, number, body):
        self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def actor_id(self):
        value = self.request("GET", "/user")
        if not isinstance(value, dict) or type(value.get("id")) is not int:
            raise QueueError("GitHub publisher identity unavailable")
        return value["id"]

    def issue_comments(self, number):
        for page in range(1, MAX_ISSUES_PAGES + 1):
            values = self.request("GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}")
            if not isinstance(values, list):
                raise QueueError("GitHub comments invalid")
            yield from values
            if len(values) < 100:
                return
        raise QueueError("Comment pagination cap reached; refuse partial reconciliation")

    def close(self, number):
        self.request("PATCH", f"/repos/{self.repo}/issues/{number}", {"state": "closed"})


def parse_issue(issue, cfg):
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None
    if issue.get("state") != "open" or not str(issue.get("title", "")).startswith(TITLE_PREFIX):
        return None
    login = (issue.get("user") or {}).get("login", "")
    if login.casefold() not in {x.casefold() for x in cfg["allowed_authors"]}:
        return None
    text = issue.get("body")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TASK + 1024:
        return None
    match = FORM_RE.fullmatch(text.strip() + "\n")
    if not match:
        return None
    project, mode, task = (x.strip() for x in match.groups())
    if project not in cfg["project_roots"] or mode not in ("read-only", "workspace-write"):
        return None
    if not task or task == "No response" or len(task.encode("utf-8")) > MAX_TASK or "\0" in task:
        return None
    number, issue_id = issue.get("number"), issue.get("id")
    if type(number) is not int or type(issue_id) is not int:
        return None
    canonical = json.dumps({"issue_id": issue_id, "number": number,
                             "author": login, "title": issue["title"],
                             "body": text, "repository": cfg["repository"]},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"number": number, "id": issue_id, "title": issue["title"],
            "author": login, "project": project, "mode": mode,
            "task": task, "digest": digest}


EVENT_NAMES = frozenset(("poll", "approved", "claimed", "screen_started",
                         "turn_started", "turn_completed", "handoff_published",
                         "failed", "worker_started", "poll_failed", "blocked", "unknown", "launch_started"))
EVENT_MAX_BYTES = 1024 * 1024


def worker_event(store, event, number=None, backend=None, mode=None):
    """Best-effort, allowlisted operational metadata; never exception/payload text.

    One bounded backup and an atomic compact snapshot, serialized across CLI and
    worker threads/processes. Monitoring failure never changes job authorization.
    """
    if event not in EVENT_NAMES:
        return
    try:
        now = int(time.time())
        row = {"v": 1, "ts": now, "event": event}
        if type(number) is int and number > 0:
            row["issue"] = number
        if backend in ("exec", "screen", "project-screen"):
            row["backend"] = backend
        if mode in ("read-only", "workspace-write"):
            row["mode"] = mode
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(store.log_dir / "events.lock", flags, 0o600), "r+") as lock:
            st = os.fstat(lock.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise QueueError("Monitor lock invalid")
            fcntl.flock(lock, fcntl.LOCK_EX)
            snapshot = store.log_dir / "worker-monitor.json"
            view = {"v": 1, "active": {}}
            if snapshot.exists():
                secure_private_file(snapshot)
                view = json.loads(snapshot.read_text())
                if view.get("v") != 1 or not isinstance(view.get("active"), dict):
                    view = {"v": 1, "active": {}}
            if event == "worker_started":
                # Atomic queue state is the authority after a worker restart.
                # Never replay unresolved claims; surface them for operator review.
                view["active"] = {}
                for job in store.load()["jobs"].values():
                    if job.get("status") in ("running", "unknown"):
                        n = job.get("number")
                        if type(n) is int and n > 0:
                            view["active"][str(n)] = {"issue": n, "event": "unknown",
                                **{k: job[k] for k in ("backend", "mode")
                                   if job.get(k) in ("exec", "screen", "project-screen", "read-only", "workspace-write")}}
            view["updated_at"] = now
            if event in ("poll", "worker_started", "poll_failed"):
                view["host"] = socket.gethostname()[:128]
                view["backend"] = row.get("backend", "unknown")
                if event == "poll":
                    view["last_poll"] = now
                view["poll_error"] = event == "poll_failed"
            key = str(number)
            if event in ("claimed", "screen_started", "turn_started", "unknown", "launch_started") and "issue" in row:
                view["active"][key] = {k: v for k, v in row.items() if k in ("issue", "backend", "mode", "event")}
            elif event in ("turn_completed", "failed"):
                view["active"].pop(key, None)
            data = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            path = store.log_dir / "worker-events.jsonl"
            if path.exists():
                secure_private_file(path, EVENT_MAX_BYTES + 1024)
                if path.stat().st_size + len(data) > EVENT_MAX_BYTES:
                    os.replace(path, store.log_dir / "worker-events.jsonl.1")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                                  getattr(os, "O_NOFOLLOW", 0), 0o600), "ab") as log:
                log.write(data)
            temp = store.log_dir / "worker-monitor.json.new"
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                                  getattr(os, "O_NOFOLLOW", 0), 0o600), "w") as output:
                json.dump(view, output, separators=(",", ":"))
            os.replace(temp, snapshot)
    except (OSError, ValueError, TypeError, AttributeError, QueueError):
        # Fixed warning only: no paths, token material or remote error bodies.
        print("WORKER_MONITOR_UNAVAILABLE", file=sys.stderr, flush=True)


class Store:
    def __init__(self, repo_root):
        self.tmp_dir = private_dir(repo_root / ".tmp" / "github-queue")
        self.log_dir = private_dir(repo_root / ".log" / "github-queue")
        # Use one durable store for the local CLI and the systemd worker.
        # Codex shells may override XDG_STATE_HOME to a temporary sandbox;
        # honoring that override would split approval/idempotency state.
        self.state_dir = private_dir(Path.home() / ".local/state" / "gpt2codex" / "github-queue")
        self.file = self.state_dir / "state.json"
        self.lockfile = self.state_dir / "state.lock"

    @contextlib.contextmanager
    def lock(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lockfile, flags, 0o600)
        try:
            with os.fdopen(fd, "r+") as lock_file:
                st = os.fstat(lock_file.fileno())
                if st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise QueueError("State lock ownership/mode invalid")
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                yield
        finally:
            pass

    def load(self):
        if not self.file.exists():
            return {"version": 1, "jobs": {}}
        secure_private_file(self.file, max_size=MAX_RESPONSE)
        with self.file.open(encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("version") != 1 or not isinstance(obj.get("jobs"), dict):
            raise QueueError("State format invalid")
        return obj

    def save(self, obj):
        data = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        temp = self.state_dir / f".state.{os.getpid()}.{time.monotonic_ns()}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.file)
            dfd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if temp.exists():
                temp.unlink()


def compact(item):
    return {k: item[k] for k in ("number", "id", "title", "author", "project", "mode", "digest")}


def scan(api, store, cfg):
    api.validate_repo()
    issues = list(api.issues())
    added = []
    with store.lock():
        state = store.load()
        for issue in issues:
            item = parse_issue(issue, cfg)
            if not item:
                continue
            key = str(item["number"])
            old = state["jobs"].get(key)
            if old and old["id"] != item["id"]:
                raise QueueError("Issue number identity changed")
            if not old:
                state["jobs"][key] = {**compact(item), "status": "pending"}
                added.append(key)
            elif old["digest"] != item["digest"]:
                if old["status"] == "pending":
                    state["jobs"][key] = {**compact(item), "status": "pending"}
                elif old["status"] in ("approved", "approved-screen", "approved-project-screen", "running"):
                    old["status"] = "stale"
        store.save(state)
    for key in added:
        print(f"NEW #{key} (local approval required)")
    return added


def gui_list(store):
    """Complete untruncated local issue metadata; no token/network needed."""
    with store.lock():
        jobs = store.load()['jobs'].values()
        data = [{k: job.get(k) for k in ('number', 'status', 'project', 'mode', 'digest', 'title')}
                for job in jobs]
    print(json.dumps(sorted(data, key=lambda j: j['number']), ensure_ascii=False))


def gui_activity(store, cfg):
    """Read-only, network-free activity for the visible GUI project tabs.

    The stored queue status is admission metadata, not proof that Codex is
    alive. The separate runtime_verified flag represents only verified screen
    server/window presence. Never expose raw event payloads or prompt files.
    """
    import gpt2codex_queue_screen as slots
    with store.lock():
        active = [dict(job) for job in store.load()['jobs'].values()
                  if job.get('status') == 'running']
    rows = []
    for job in sorted(active, key=lambda j: j['number']):
        number, project = job.get('number'), job.get('project')
        if type(number) is not int or number <= 0 or project not in cfg['project_roots']:
            continue
        row = {'number': number, 'project': project, 'title': job.get('title', ''),
               'queue_status': 'running', 'backend': job.get('backend', 'exec'),
               'phase': 'claim_pending', 'runtime_verified': False,
               'turns_started': 0, 'turns_completed': 0, 'events': []}
        slot_name = job.get('screen_slot')
        if slot_name:
            try:
                slot = slots.find_slot(store.log_dir.parent.parent, slot_name)
                spec = slots.read(slot / 'spec.json')
                if (spec.get('number') != number or spec.get('project') != project
                        or spec.get('digest') != job.get('digest')
                        or spec.get('backend', 'screen') != row['backend']):
                    raise RuntimeError('Queue slot identity mismatch')
                phase = slots.read(slot / 'phase.json')
                row['phase'] = phase.get('phase', 'unverified')
                row['turns_started'] = phase.get('turns_started', 0)
                row['turns_completed'] = phase.get('turns_completed', 0)
                row['runtime_verified'] = slots.runtime_verified(slot, spec)
                row['events'] = slots.recent_activity(slot)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                row['phase'] = 'unverified'
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False))


def show_jobs(store, active_only=False):
    with store.lock():
        values = list(store.load()["jobs"].values())
    if active_only:
        terminal = {"done", "failed", "rejected"}
        values = [job for job in values if job["status"] not in terminal]
    for job in sorted(values, key=lambda a: a["number"]):
        print(f'#{job["number"]} {job["status"]:9} {job["project"]:14} '
              f'{job["mode"]:15} {job["digest"][:12]} {job["title"][:70]}')
    if not values:
        print("No active/pending queue requests" if active_only else "No accepted queue requests")


def validated_remote(api, store, cfg, number, allowed_states):
    issue = api.issue(number)
    item = parse_issue(issue, cfg)
    if not item:
        raise QueueError("Remote issue invalid, closed, or untrusted")
    with store.lock():
        job = store.load()["jobs"].get(str(number))
    if not job or job["status"] not in allowed_states:
        raise QueueError("Issue not in expected local state")
    if job["id"] != item["id"] or job["digest"] != item["digest"]:
        raise QueueError("Issue edited after ingestion; stop and re-scan")
    return item


def show_item(item):
    print(f'Issue:   #{item["number"]} {item["title"]}')
    print(f'Author:  {item["author"]}')
    print(f'Project: {item["project"]}')
    print(f'Mode:    {item["mode"]}')
    print(f'SHA256:  {item["digest"]}')
    print("--- TASK BEGIN ---")
    print(item["task"])
    print("--- TASK END ---")


def approve(api, store, cfg, number, prefix, backend=None):
    backend = execution_backend(cfg) if backend is None else backend
    require_runnable_backend(backend, cfg)
    item = validated_remote(api, store, cfg, number, ("pending",))
    if len(prefix) < 12 or not item["digest"].startswith(prefix.lower()):
        raise QueueError("Digest mismatch: use first 12+ characters shown by show")
    if not sys.stdin.isatty():
        raise QueueError("Approval requires a local interactive terminal")
    show_item(item)
    if backend == "screen":
        print("Backend: screen (NEW task slot, same-thread TUI after exec completion)")
    elif backend == "project-screen":
        print("Backend: project-screen CANARY (existing project screen, NO idle resume)")
    answer = input(f'For LOCAL approval type SCHVALUJI {number}: ')
    if answer != f"SCHVALUJI {number}":
        raise QueueError("Approval cancelled")
    # Re-fetch after the human decision. An edited issue cannot be approved.
    check = validated_remote(api, store, cfg, number, ("pending",))
    if check["digest"] != item["digest"]:
        raise QueueError("Issue changed during local approval")
    with store.lock():
        state = store.load()
        job = state["jobs"].get(str(number))
        if not job or job["status"] != "pending" or job["digest"] != item["digest"]:
            raise QueueError("Local approval raced with another state change")
        job["backend"] = backend
        job["worker_host"] = cfg["worker_hostname"]
        job["status"] = {"exec": "approved", "screen": "approved-screen",
                         "project-screen": "approved-project-screen"}[backend]
        store.save(state)
    worker_event(store, "approved", number, backend, item["mode"])
    print(f"APPROVED #{number}; will run only on configured host")
    print(f"Backend: {backend}; watch / run-approved will claim automatically")


def gui_detail(api, store, cfg, number):
    """JSON detail for a selected issue; approval identity stays remote-bound."""
    issue = api.issue(number)
    if not isinstance(issue, dict):
        raise QueueError("Remote issue unavailable")
    # Closed issues can be displayed too. They cannot be approved.
    check = dict(issue)
    check['state'] = 'open'
    item = parse_issue(check, cfg)
    with store.lock():
        job = store.load()['jobs'].get(str(number))
    if not job:
        raise QueueError("Issue is not in local queue")
    if not item or job['id'] != item['id'] or job['digest'] != item['digest']:
        raise QueueError("Remote Issue changed or invalid; refresh required")
    print(json.dumps({**item, 'status': job['status'],
                      'remote_state': issue.get('state'),
                      'approved_backend': job.get('backend')}, ensure_ascii=False))


def gui_approve(api, store, cfg, number, digest, confirmation):
    """Explicit local GUI confirmation, identical digest/race rules as TTY approve.

    GUI must supply the exact confirmation only after an operator checks the
    checkbox and clicks OK. This interface is local-only, not remotely exposed.
    """
    if confirmation != f'SCHVALUJI {number}':
        raise QueueError('GUI approval confirmation mismatch')
    backend = 'project-screen'
    require_runnable_backend(backend, cfg)
    item = validated_remote(api, store, cfg, number, ('pending',))
    if digest != item['digest']:
        raise QueueError('Issue SHA-256 changed; no approval')
    # Re-fetch immediately before the durable local write.
    check = validated_remote(api, store, cfg, number, ('pending',))
    if check['digest'] != digest:
        raise QueueError('Issue changed during GUI approval')
    with store.lock():
        state = store.load()
        job = state['jobs'].get(str(number))
        if not job or job['status'] != 'pending' or job['digest'] != digest:
            raise QueueError('Local approval changed; refresh required')
        job['backend'] = backend
        job['worker_host'] = cfg['worker_hostname']
        job['status'] = 'approved-project-screen'
        store.save(state)
    print(f'APPROVED #{number}; project-screen; worker will claim automatically')


def reject(store, number):
    with store.lock():
        state = store.load()
        job = state["jobs"].get(str(number))
        if not job or job["status"] not in ("pending", "approved", "approved-screen",
                                             "approved-project-screen", "stale"):
            raise QueueError("Issue cannot be rejected from its current state")
        job["status"] = "rejected"
        store.save(state)
    print(f"REJECTED #{number} (local state only)")


def prompt_for(item):
    return (
        "This is a locally approved gpt2codex queue task. "
        "Respect all project and global instructions, including AGENTS.md and .ai rules.\n"
        "Never commit, push, tag, change branches, install packages, change global configuration, "
        "modify unrelated projects or inspect credentials without separate authorization.\n"
        "Use existing .log for logs and .tmp for temporary files. "
        "Do not touch existing GNU screen/tmux sessions, their registries or sockets.\n"
        f'Approved project: {item["project"]}; sandbox: {item["mode"]}.\n'
        "Do the task only within the explicitly approved scope. If blocked, report the reason.\n"
        "USER TASK:\n" + item["task"] + "\n"
        + (handoff.instruction(item) if "_handoff_report_path" in item else "")
    )


@contextlib.contextmanager
def _queue_file_lock(path, kind, conflict):
    """Cross-process flock, with verified private inode and no symlink fallback."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o077 or st.st_nlink != 1):
            raise QueueError('Queue lock ownership/type/mode invalid')
        try:
            fcntl.flock(f, kind | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError(conflict) from None
        yield


@contextlib.contextmanager
def execution_lock(store):
    # Legacy exec/screen runs and operator setup require global exclusivity.
    with _queue_file_lock(store.state_dir / 'execution.lock', fcntl.LOCK_EX,
                          'Another queue execution is active'):
        yield


@contextlib.contextmanager
def project_execution_lock(store, project):
    """Allow multiple project jobs, but exclude legacy runs and same-project jobs.

    The global shared lock is taken first, followed by the project exclusive
    lock. Both stay held until the Codex turn, receipt and handoff finish.
    This only serializes our own Queue entry points; manual screen starts are
    independently checked by anchor_ready and are NOT atomic with launch.
    """
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', project):
        raise QueueError('Invalid project lock identity')
    with _queue_file_lock(store.state_dir / 'execution.lock', fcntl.LOCK_SH,
                          'Legacy queue execution is active'):
        with _queue_file_lock(store.state_dir / f'project-{project}.execution.lock',
                              fcntl.LOCK_EX, f'project_busy: Queue owns {project}'):
            yield


def validate_parallel_roots(cfg):
    """Separate lock names must never refer to overlapping workspace trees."""
    roots = [(name, Path(secure_project_dir(root)))
             for name, root in cfg['project_roots'].items()]
    for index, (first, a) in enumerate(roots):
        for second, b in roots[index + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise QueueError(f'Parallel project roots overlap: {first} / {second}')


def run_one(api, store, cfg, number, backend='exec'):
    # Bind the lock name to durable approval state, not to a caller argument.
    with store.lock():
        job = store.load()['jobs'].get(str(number))
        if not job or approved_backend(job) != backend:
            raise QueueError('Approval backend mismatch')
        project = job['project']
    if parallel_enabled(cfg) and backend == 'project-screen':
        validate_parallel_roots(cfg)
        with project_execution_lock(store, project):
            return execute_claimed(api, store, cfg, number, backend)
    with execution_lock(store):
        return execute_claimed(api, store, cfg, number, backend)


def execute_claimed(api, store, cfg, number, backend):
    with contextlib.ExitStack() as leases:
        return _execute_claimed(api, store, cfg, number, backend, leases)


def _execute_claimed(api, store, cfg, number, backend, leases):
    require_runnable_backend(backend, cfg)
    # Reject changed or incompatible backend bindings before any remote access.
    with store.lock():
        job = store.load()["jobs"].get(str(number))
        if not job or approved_backend(job) != backend:
            raise QueueError("Approval backend mismatch")
    api.validate_repo()
    allowed = ({"exec": ("approved",), "screen": ("approved-screen",),
                "project-screen": ("approved-project-screen",)})[backend]
    item = validated_remote(api, store, cfg, number, allowed)
    project_dir = secure_project_dir(cfg["project_roots"][item["project"]])
    item['_approved_project_root'] = project_dir
    options = git_check_options(project_dir, project_dir)
    codex = Path(cfg["codex_bin"])
    if not codex.is_file() or not os.access(codex, os.X_OK):
        raise QueueError("Configured codex binary unavailable")
    if backend == "project-screen":
        # An occupied project is WAITING, never an unknown/consumed approval.
        # Admission is covered by the Queue lock (global or project-scoped).
        # Recheck again inside the launcher immediately before screen -X.
        import gpt2codex_queue_project as project_backend
        import gpt2codex_queue_screen as screen_backend
        try:
            # PRECLAIM validation is read-only. Hold the registry lease through
            # completion; the persisted proof fixes type/root/executable binding.
            leases.enter_context(project_backend.registry_lease(project_backend.registry_config(cfg)))
            item['_project_validation'] = project_backend.preclaim(cfg, item['project'], project_dir, str(codex))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise QueueError(f"project-screen preclaim refused: {exc}") from None
        try:
            project_backend.anchor_ready(store, item["project"], project_dir)
            screen_backend.require_chatgpt(str(codex), screen_backend.clean_env())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise QueueError(f"project-screen waiting: {exc}") from None
    # Commit the single-use claim before the process begins. Crashes require manual review.
    with store.lock():
        state = store.load()
        job = state["jobs"].get(str(number))
        if not job or job["status"] not in allowed or job["digest"] != item["digest"]:
            raise QueueError("Execution claim rejected")
        if job.get("backend", backend) != backend or job.get("worker_host", cfg["worker_hostname"]) != cfg["worker_hostname"]:
            raise QueueError("Approval backend/host mismatch")
        if backend == 'project-screen':
            earlier = [j for j in state['jobs'].values()
                       if j.get('project') == item['project']
                       and j.get('status') in ('approved', 'approved-screen',
                                                    'approved-project-screen')
                       and j.get('number', 0) < number]
            if earlier:
                raise QueueError('Earlier approved project task must run first (FIFO)')
        unresolved = [j for j in state['jobs'].values()
                      if j['status'] in ('running', 'unknown')]
        if parallel_enabled(cfg) and backend == 'project-screen':
            for other in unresolved:
                if other.get('project') == item['project']:
                    raise QueueError('project_busy: unresolved project claim; manual review required')
                if other.get('backend') != 'project-screen':
                    raise QueueError('Unresolved legacy claim; cannot overlap')
        elif any(j['status'] == 'running' for j in unresolved):
            raise QueueError('Unresolved running claim; review before starting another job')
        if backend == "project-screen":
            try:
                project_backend.validate_start(item["_project_validation"], item["project"], project_dir, str(codex))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                raise QueueError(f"project-screen preclaim changed: {exc}") from None
            job["project_validation"] = item["_project_validation"]
        job["status"] = "running"
        job["backend"] = backend
        job["worker_host"] = cfg["worker_hostname"]
        job["started_at"] = int(time.time())
        # Opt in only at a NEW claim, never by migrating terminal/running jobs.
        # Issues 1--12 are explicitly outside the automatic publication scope.
        if number >= 13:
            job["handoff_version"] = 1
            job["handoff_nonce"] = uuid.uuid4().hex
            item["_handoff_nonce"] = job["handoff_nonce"]
        store.save(state)
    worker_event(store, "claimed", number, backend, item["mode"])
    report_path = None
    if "_handoff_nonce" in item:
        name = f"handoff-{number}-{item['_handoff_nonce']}"
        item["_handoff_schema_path"] = store.tmp_dir / (name + ".schema.json")
        report_path = store.log_dir / (name + ".final.json")
        item["_handoff_report_path"] = report_path
    outpath = store.log_dir / f"issue-{number}.stdout.log"
    errpath = store.log_dir / f"issue-{number}.stderr.log"
    env = os.environ.copy()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    cmd = [str(codex), "exec", "--sandbox", item["mode"], "-C", project_dir, *options, "-"]
    exit_code = 127
    result_known = backend == "exec"
    try:
        if "_handoff_nonce" in item:
            prepare_report(item)
        if backend in ("screen", "project-screen"):
            sys.dont_write_bytecode = True
            if backend == "screen":
                import gpt2codex_queue_screen as screen_backend
                slot = screen_backend.launch(store, item, str(codex), project_dir,
                                             prompt_for(item), env)
                # This legacy state write is retained for compatibility.
                with store.lock():
                    state = store.load()
                    state["jobs"][str(number)]["screen_slot"] = slot.name
                    state["jobs"][str(number)]["backend"] = "screen"
                    store.save(state)
                result = screen_backend.wait_turn(slot)
            else:
                import gpt2codex_queue_project as project_backend
                slot, view, anchor, title = project_backend.launch(
                    store, item, str(codex), project_dir, prompt_for(item), env)
                result = project_backend.wait_turn(slot, view, anchor, title)
            exit_code = result["exit_code"]
            if exit_code == 0 and (not result.get("thread_id") or
                    result.get("turns_started") != 1 or result.get("turns_completed") != 1):
                raise QueueError("Screen completion identity/count invalid; no replay")
            with store.lock():
                state = store.load()
                state["jobs"][str(number)]["thread_id"] = result.get("thread_id")
                store.save(state)
            result_known = True
        elif backend == "exec":
            worker_event(store, "launch_started", number, backend, item["mode"])
            exit_code = run_exec(cmd, project_dir, env, item, outpath, errpath)
        else:
            raise QueueError("Unrecognized backend: execution refused")
    except Exception as exc:
        print(f"Local Codex launch failed: {type(exc).__name__}", file=sys.stderr)
    status = ("done" if exit_code == 0 else "failed") if result_known else "unknown"
    with store.lock():
        state = store.load()
        job = state["jobs"].get(str(number))
        if job and job["status"] == "running":
            job["status"] = status
            job["exit_code"] = exit_code
            job["finished_at"] = int(time.time())
            if job.get("handoff_version") == 1:
                body, diagnostic = handoff.render(job, report_path)
                # Safe outbox and terminal result are durable in ONE transaction.
                # Retries never read raw artifacts or invoke another model turn.
                job["handoff"] = {"phase": "pending", "body": body,
                                  "report_diagnostic": diagnostic,
                                  "diagnostic": "publication_pending", "attempts": 0}
            store.save(state)
    worker_event(store, "turn_completed" if status == "done" else "unknown" if status == "unknown" else "failed", number, backend, item["mode"])
    # Keep the legacy receipt and all its public metadata.
    note = (f"G2C-QUEUE/1\nworker={cfg['worker_hostname']}\n"
            f"request_sha256={item['digest']}\nstatus={status}\nexit_code={exit_code}\n"
            "Output is stored locally. A short automatic handoff is separate; "
            "longer manual summaries use publish-summary.")
    try:
        api.comment(number, note)
        api.close(number)
    except QueueError:
        print(f"WARNING #{number}: local result saved; GitHub receipt update failed", file=sys.stderr)
    if "_handoff_nonce" in item:
        publish_handoff(api, store, cfg, number)
    print(f"FINISHED #{number} status={status} exit={exit_code}")
    return exit_code


def prepare_report(item):
    # Host CLI writes outside the model tool sandbox, including read-only jobs.
    for path, data in (
        (item["_handoff_schema_path"], json.dumps(handoff.schema(item)).encode("utf-8")),
        (item["_handoff_report_path"], b""),
    ):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


def run_exec(cmd, project_dir, env, item, outpath, errpath):
    # Direct callers and resumed/old workers must obey the same STOP gate.
    try:
        dispatch.require_legacy_launch_allowed(env)
    except dispatch.DispatchError as exc:
        raise QueueError(str(exc)) from None
    exit_code = 127
    try:
        if '_approved_project_root' in item:
            # Recheck immediately before launch; a changed root must not reuse
            # a trust decision made before the single-use claim.
            expected = git_check_options(project_dir, item['_approved_project_root'])
            if ('--skip-git-repo-check' in cmd) != bool(expected):
                raise RuntimeError('Git metadata changed before launch')
        fd_out = os.open(outpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fd_err = os.open(errpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except Exception:
            os.close(fd_out)
            raise
        with os.fdopen(fd_out, "wb") as fout, os.fdopen(fd_err, "wb") as ferr:
            if "_handoff_nonce" in item:
                # The host CLI writes the final artifact, not a model tool. This
                # needs no extra writable sandbox paths for read-only requests.
                cmd = cmd[:-1] + ["--output-schema", str(item["_handoff_schema_path"]),
                                   "--output-last-message", str(item["_handoff_report_path"]), "-"]
            proc = subprocess.Popen(cmd, cwd=project_dir, env=env, stdin=subprocess.PIPE,
                                    stdout=fout, stderr=ferr, start_new_session=True)
            try:
                proc.communicate(prompt_for(item).encode("utf-8"), timeout=60 * 60)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                exit_code = 124
    except Exception as exc:
        print(f"Local Codex launch failed: {type(exc).__name__}", file=sys.stderr)
    return exit_code


def _run_one_logged(api, store, cfg, number, backend):
    try:
        return run_one(api, store, cfg, number, backend)
    except QueueError as exc:
        worker_event(store, "blocked", number, backend)
        print(f'BLOCKED #{number}: {exc}', file=sys.stderr, flush=True)
    except Exception as exc:
        worker_event(store, "unknown", number, backend)
        # Never print process environment, GitHub responses or task contents.
        print(f'QUEUE_ERROR #{number}: {type(exc).__name__}', file=sys.stderr, flush=True)


def _reap_parallel(inflight):
    for project, (number, future) in list(inflight.items()):
        if future.done():
            try:
                future.result()
            except Exception as exc:
                print(f'QUEUE_ERROR #{number}: unexpected {type(exc).__name__}',
                      file=sys.stderr, flush=True)
            finally:
                del inflight[project]


def _dispatch_parallel(api, store, cfg, pool, inflight, jobs, attempted=None):
    """One issue per project per pass; never skip an earlier approved issue."""
    _reap_parallel(inflight)
    selected = set()
    capacity = parallel_capacity(cfg)
    for number, job in sorted(jobs):
        project = job.get('project')
        if project in selected:
            continue
        selected.add(project)
        if attempted is not None and number in attempted:
            # An earlier approved job failed preflight this one-shot invocation.
            # It is still the FIFO head, so no later job in that project runs.
            continue
        try:
            backend = approved_backend(job)
        except QueueError as exc:
            worker_event(store, "blocked", number)
            print(f'BLOCKED #{number}: {exc}', file=sys.stderr, flush=True)
            continue
        if backend == 'project-screen':
            if project in inflight or len(inflight) >= capacity:
                continue
            inflight[project] = (number, pool.submit(_run_one_logged, api, store, cfg,
                                                    number, backend))
            if attempted is not None:
                attempted.add(number)
        elif not inflight:
            # Preserve serial legacy semantics; a global exclusive flock also
            # prevents concurrent manual run-screen vs project workers.
            _run_one_logged(api, store, cfg, number, backend)
            if attempted is not None:
                attempted.add(number)


def _approved_jobs(store):
    with store.lock():
        return [(job['number'], dict(job)) for job in store.load()['jobs'].values()
                if job['status'] in ('approved', 'approved-screen', 'approved-project-screen')]


def run_approved(api, store, cfg, *, pool=None, inflight=None):
    require_runnable_backend(execution_backend(cfg), cfg)
    reconcile_handoffs(api, store, cfg)
    jobs = _approved_jobs(store)
    if not parallel_enabled(cfg):
        for number, job in sorted(jobs):
            try:
                backend = approved_backend(job)
            except QueueError as exc:
                worker_event(store, "blocked", number)
                print(f'BLOCKED #{number}: {exc}', file=sys.stderr, flush=True)
                continue
            _run_one_logged(api, store, cfg, number, backend)
        return
    validate_parallel_roots(cfg)
    if pool is not None:
        if inflight is None:
            raise QueueError('Parallel worker flight registry missing')
        _dispatch_parallel(api, store, cfg, pool, inflight, jobs)
        return
    # One-shot drains all eligible approvals, including capacity overflow and
    # sequential jobs from the same project. A blocked approval is tried once
    # per invocation and remains the FIFO head, never silently replayed.
    local = {}
    attempted = set()
    with ThreadPoolExecutor(max_workers=parallel_capacity(cfg), thread_name_prefix='g2c-q') as owned:
        while True:
            before = len(attempted)
            _dispatch_parallel(api, store, cfg, owned, local, jobs, attempted=attempted)
            if len(attempted) == before:
                break
            for number, future in list(local.values()):
                future.result()
            _reap_parallel(local)
            jobs = _approved_jobs(store)


def publish_handoff(api, store, cfg, number):
    """Serialized publication/reconciliation ONLY. Never executes Codex.

    GitHub has no idempotency key for comments. Persist posting BEFORE POST;
    after uncertain delivery, a missing marker must NOT authorize another POST.
    """
    with store.lock():
        state = store.load()
        job = state["jobs"].get(str(number))
        out = job.get("handoff") if job else None
        if not out or job.get("handoff_version") != 1:
            return False  # Legacy jobs, including already-running #13, untouched.
        if out["phase"] == "published":
            return True
        try:
            api.validate_repo()
            issue = api.issue(number)  # Closed and subsequently edited issues are OK.
            if not isinstance(issue, dict) or issue.get("id") != job["id"] or issue.get("number") != number:
                raise QueueError("Issue identity mismatch")
            actor = api.actor_id()
            # Consume ALL pages before deciding absence. A lookalike from another
            # account, partial body or raw task text cannot confirm publication.
            comments = list(api.issue_comments(number))
            if any(not isinstance(c, dict) or not isinstance(c.get("user"), dict) for c in comments):
                raise QueueError("Comment identity unavailable")
            matches = [c for c in comments if c.get("body") == out["body"]
                       and (c.get("user") or {}).get("id") == actor]
            if matches:
                out["phase"] = "published"
                out["diagnostic"] = "remote_confirmed"
                store.save(state)
                worker_event(store, "handoff_published", number)
                return True
            if out["phase"] in ("posting", "uncertain"):
                out["phase"] = "uncertain"
                out["diagnostic"] = "delivery_unknown_no_repost"
                store.save(state)
                print(f"HANDOFF #{number}: delivery unknown; reconciliation only", file=sys.stderr)
                return False
            out["phase"] = "posting"
            out["attempts"] += 1
            out["diagnostic"] = "post_in_flight"
            store.save(state)
            try:
                api.comment(number, out["body"])
            except PostRejected:
                out["phase"] = "pending"
                out["diagnostic"] = "post_rejected_retry_publication"
            except Exception:
                # No exception text, URLs, payloads, tokens or raw network data.
                out["phase"] = "uncertain"
                out["diagnostic"] = "delivery_unknown_no_repost"
            else:
                out["phase"] = "published"
                out["diagnostic"] = "post_acknowledged"
            store.save(state)
        except (QueueError, OSError, ValueError, TypeError):
            out["diagnostic"] = "reconciliation_unavailable"
            store.save(state)
        if out["phase"] == "published":
            worker_event(store, "handoff_published", number)
        print(f"HANDOFF #{number}: {out['phase']} ({out['diagnostic']})", flush=True)
        return out["phase"] == "published"


def reconcile_handoffs(api, store, cfg):
    with store.lock():
        jobs = [j["number"] for j in store.load()["jobs"].values()
                if j.get("handoff") and j["handoff"]["phase"] != "published"]
    for number in sorted(jobs):
        publish_handoff(api, store, cfg, number)


def handoff_status(store, number):
    with store.lock():
        job = store.load()["jobs"].get(str(number))
    out = job.get("handoff") if job else None
    if not out:
        print(f"HANDOFF #{number}: unavailable (no new-format completion)")
        return
    print(f"HANDOFF #{number}: {out['phase']} attempts={out['attempts']} "
          f"diagnostic={out['diagnostic']} report={out['report_diagnostic']}")
    print(out["body"])


def publish_summary(api, store, cfg, number, path):
    if not sys.stdin.isatty():
        raise QueueError("Summary publishing requires local interactive authorization")
    with store.lock():
        job = store.load()["jobs"].get(str(number))
    if not job or job["status"] not in ("done", "failed"):
        raise QueueError("Only completed jobs may publish a summary")
    path = Path(path).expanduser()
    try:
        path.resolve(strict=True).relative_to(store.tmp_dir.resolve(strict=True))
    except (OSError, ValueError):
        raise QueueError("Summary must be within project .tmp/github-queue") from None
    if path.suffix != ".md":
        raise QueueError("Summary must be an explicit .md file")
    path = secure_private_file(path)
    body = path.read_text(encoding="utf-8")
    if any(secret in body for secret in ("github_pat_", "ghp_", "BEGIN PRIVATE KEY")):
        raise QueueError("Summary contains a credential marker; publication refused")
    if not body.strip() or len(body.encode("utf-8")) > MAX_SUMMARY:
        raise QueueError("Summary empty or too large")
    print("--- EXACT OUTGOING GITHUB SUMMARY ---")
    print(body)
    print("--- END ---")
    if input(f'To publish to GitHub type PUBLIKUJI {number}: ') != f"PUBLIKUJI {number}":
        raise QueueError("Summary publication cancelled")
    api.validate_repo()
    api.comment(number, "G2C-SUMMARY/1\n" + body)
    print(f"SUMMARY PUBLISHED #{number}")


def init_config(filename, repo):
    path = Path(filename).expanduser()
    private_dir(path.parent)
    if path.exists():
        raise QueueError("Config exists; refusing overwrite")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config_default(repo), f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Created private config: {path}")
    print("Create the token file (mode 0600) using vim; never paste a token into a command")


def worker_ready(cfg, store):
    backend = execution_backend(cfg)
    if backend == "project-screen" and cfg.get("project_screen_canary") is not True:
        print("WORKER_READY=NO BACKEND=project-screen IDLE=UNKNOWN")
        print("Private canary switch disabled; no launch or fallback")
        return 2
    codex = Path(cfg["codex_bin"])
    ready = codex.is_file() and os.access(codex, os.X_OK)
    if backend in ("screen", "project-screen"):
        ready = ready and shutil.which("screen") is not None
    with store.lock():
        jobs = list(store.load()["jobs"].values())
    idle = not any(j["status"] in ("approved", "approved-screen", "approved-project-screen",
                                   "running", "unknown", "stale") for j in jobs)
    try:
        with execution_lock(store):
            pass
    except QueueError:
        idle = False
    print(f"WORKER_READY={'YES' if ready else 'NO'} BACKEND={backend} IDLE={'YES' if idle else 'NO'}")
    if backend == "project-screen":
        print("STATE=home-local-state CANARY=opt-in PROJECT_SCREEN=existing-registered-anchor")
    if parallel_enabled(cfg):
        print(f"PROJECT_PARALLEL=CANARY CAPACITY={parallel_capacity(cfg)} (per worker)")
    else:
        print("STATE=home-local-state XDG_STATE_HOME=ignored SCREENDIR=isolated-per-slot")
    print("Readiness is offline only; screen creation, authentication and GUI E2E are NOT PROVEN")
    return 0 if ready else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="gpt2codex private GitHub Issues queue worker")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent))
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init-config")
    init.add_argument("--repository", default="Pitriss/gpt2codex-queue")
    sub.add_parser("check")
    sub.add_parser("worker-ready", help="Offline readiness and idle diagnostics; no credentials or sessions")
    sub.add_parser("poll")
    sub.add_parser("list")
    sub.add_parser("list-active", help="Only pending, waiting, blocked and running jobs")
    sub.add_parser("gui-list", help="Full local queue metadata as JSON")
    sub.add_parser("gui-activity", help="Read-only sanitized activity of running project jobs")
    for name in ("project-ready", "project-prepare-anchor"):
        project_command = sub.add_parser(name, help="Project-screen canary operator preflight")
        project_command.add_argument("project")
    show = sub.add_parser("show")
    show.add_argument("number", type=int)
    app = sub.add_parser("approve")
    app.add_argument("number", type=int)
    app.add_argument("digest_prefix")
    app.add_argument("--backend", choices=("exec", "screen", "project-screen"), default=None)
    rej = sub.add_parser("reject")
    rej.add_argument("number", type=int)
    gd = sub.add_parser('gui-detail', help='JSON full task, verified against local queue identity')
    gd.add_argument('number', type=int)
    ga = sub.add_parser('gui-approve', help='Local GUI operator approval, SHA-bound')
    ga.add_argument('number', type=int)
    ga.add_argument('--digest', required=True)
    ga.add_argument('--confirmation', required=True)
    sub.add_parser("run-approved")
    screen = sub.add_parser("run-screen", help="One approved Issue in a NEW isolated screen slot")
    screen.add_argument("number", type=int)
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=int, default=60)
    pub = sub.add_parser("publish-summary")
    pub.add_argument("number", type=int)
    pub.add_argument("file")
    for name in ("publish-handoff", "handoff-status"):
        hand = sub.add_parser(name, help="Publication-only reconciliation / offline safe preview")
        hand.add_argument("number", type=int)
    args = parser.parse_args(argv)
    if args.action == "init-config":
        init_config(args.config, args.repository)
        return 0
    cfg = load_config(args.config)
    repo_root = Path(args.repo_root).resolve(strict=True)
    if not (repo_root / ".tmp").is_dir() or not (repo_root / ".log").is_dir():
        raise QueueError("Project .tmp/.log managed directories missing; no bootstrap attempted")
    if args.action in ("list", "list-active"):
        show_jobs(Store(repo_root), active_only=args.action == "list-active")
        return 0
    if args.action == "gui-list":
        gui_list(Store(repo_root))
        return 0
    if args.action == "gui-activity":
        gui_activity(Store(repo_root), cfg)
        return 0
    if args.action in ("project-ready", "project-prepare-anchor"):
        import gpt2codex_queue_project as project_backend
        if args.project not in cfg["project_roots"]:
            raise QueueError("Project not in approved project roots")
        store = Store(repo_root)
        project_dir = secure_project_dir(cfg["project_roots"][args.project])
        try:
            if args.action == "project-ready":
                view, anchor = project_backend.anchor_ready(store, args.project, project_dir)
                print(f"PROJECT_READY=YES PROJECT={args.project} SESSION={view['full_id']} "
                      f"ANCHOR_WINDOW={anchor['number']}")
            else:
                if not sys.stdin.isatty():
                    raise QueueError("Anchor preparation requires operator terminal")
                print(f"This records a registered anchor-only {args.project} screen; it never starts or exits Codex")
                if input(f"Type PREPARE {args.project}: ") != f"PREPARE {args.project}":
                    raise QueueError("Anchor preparation cancelled")
                with execution_lock(store):
                    project_backend.prepare_anchor(store, args.project, project_dir)
                print(f"ANCHOR_PREPARED={args.project}; run project-ready")
        except (OSError, ValueError, RuntimeError) as exc:
            raise QueueError(f"Project preflight refused: {exc}") from None
        return 0
    if args.action == "reject":
        reject(Store(repo_root), args.number)
        return 0
    if args.action == "handoff-status":
        handoff_status(Store(repo_root), args.number)
        return 0
    if args.action == "worker-ready":
        return worker_ready(cfg, Store(repo_root))
    if args.action in ("check", "watch", "run-approved", "approve"):
        require_runnable_backend(args.backend if args.action == "approve" and args.backend
                                 else execution_backend(cfg), cfg)
    token = read_token(cfg)
    api = GitHub(token, cfg["repository"])
    store = Store(repo_root)
    if args.action == "check":
        api.validate_repo()
        codex_path = Path(cfg["codex_bin"])
        if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
            raise QueueError("Configured Codex executable missing")
        for project_name, project_path in cfg["project_roots"].items():
            try:
                secure_project_dir(project_path)
            except QueueError:
                raise QueueError(f"Configured project missing: {project_name}") from None
        print(f'QUEUE_READY=YES PRIVATE=YES WORKER={cfg["worker_hostname"]} PROJECTS={len(cfg["project_roots"])}')
    elif args.action == "poll":
        scan(api, store, cfg)
    elif args.action == "show":
        show_item(validated_remote(api, store, cfg, args.number,
                                   ("pending", "approved", "approved-screen", "approved-project-screen")))
    elif args.action == "approve":
        api.validate_repo()
        approve(api, store, cfg, args.number, args.digest_prefix, args.backend)
    elif args.action == 'gui-detail':
        gui_detail(api, store, cfg, args.number)
    elif args.action == 'gui-approve':
        api.validate_repo()
        gui_approve(api, store, cfg, args.number, args.digest, args.confirmation)
    elif args.action == "run-screen":
        return run_one(api, store, cfg, args.number, backend="screen")
    elif args.action == "run-approved":
        run_approved(api, store, cfg)
    elif args.action == "publish-summary":
        publish_summary(api, store, cfg, args.number, args.file)
    elif args.action == "publish-handoff":
        return 0 if publish_handoff(api, store, cfg, args.number) else 2
    elif args.action == "watch":
        if args.interval < 30:
            raise QueueError("Minimum poll interval is 30 seconds")
        # A single watch worker per machine; local reviewers can still use the state lock.
        fd = os.open(store.state_dir / "watch.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QueueError("Another watcher is running") from None
            worker_event(store, "worker_started", backend=execution_backend(cfg))
            print("WATCHING private GitHub queue (new tasks always require local approval)", flush=True)
            with contextlib.ExitStack() as stack:
                pool = (stack.enter_context(ThreadPoolExecutor(
                    max_workers=parallel_capacity(cfg), thread_name_prefix='g2c-q'))
                    if parallel_enabled(cfg) else None)
                inflight = {} if pool is not None else None
                while True:
                    try:
                        added = scan(api, store, cfg)
                        worker_event(store, "poll", backend=execution_backend(cfg))
                        if added and shutil.which("notify-send"):
                            with store.lock():
                                new_jobs = {k: store.load()["jobs"].get(k) for k in added}
                            for key, job in new_jobs.items():
                                if not job:
                                    continue
                                title = " ".join(str(job["title"]).split())[:160]
                                try:
                                    subprocess.run(["notify-send", "--app-name=g2c queue",
                                                    "Změna queue",
                                                    f"Byla načtena úloha číslo {key} - {title}"],
                                                   timeout=5, check=False)
                                except (OSError, subprocess.TimeoutExpired):
                                    pass
                        run_approved(api, store, cfg, pool=pool, inflight=inflight)
                    except QueueError as exc:
                        worker_event(store, "poll_failed", backend=execution_backend(cfg))
                        print(f"QUEUE_WARNING: {exc}", file=sys.stderr, flush=True)
                    time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except QueueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)
