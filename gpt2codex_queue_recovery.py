#!/usr/bin/env python3
"""Fail-closed recovery of an already registered, dead project-screen anchor.

Only the existing Rust screen lifecycle may archive/start GNU screen. This code
never starts Codex, adopts a screen by name, repeats a claim, or clears an
unresolved Queue job. The existing per-project Queue execution lock is held by
its caller for the entire preclaim and recovery sequence.
"""
import re
import subprocess
from pathlib import Path

import gpt2codex_queue_project as project_backend
import gpt2codex_queue_screen as slots


class RecoveryRefused(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryRefused(message)


def _cli(proof, project, verb, *extra):
    """Invoke only the preclaim-pinned, local Rust CLI screen subcommands."""
    require(verb in ('status', 'recover', 'start'), 'Unrecognized screen recovery operation')
    require(re.fullmatch(r'[a-z0-9_-]{1,64}', project) is not None,
            'Unverified recovery project')
    arguments = [proof['cli'], 'screen', verb, '--config', proof['config'], '--id', project, *extra]
    with project_backend.pinned_executable(proof['cli'], proof['cli_stamp']) as pinned:
        result = subprocess.run(arguments, env=slots.clean_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, check=False, **pinned)
    require(result.returncode == 0 and len(result.stdout) <= 4096
            and len(result.stderr) <= 4096,
            'Rust screen ' + verb + ' refused; manual review required')
    try:
        return result.stdout.decode('utf-8', 'strict').strip()
    except UnicodeError:
        raise RecoveryRefused('Invalid Rust screen output') from None


def _status(proof, project):
    output = _cli(proof, project, 'status')
    before, separator, task = output.partition(' task=')
    require(bool(separator) and len(task) <= 96, 'Malformed Rust screen status')
    try:
        fields = dict(field.split('=', 1) for field in before.split(' '))
    except ValueError:
        raise RecoveryRefused('Malformed Rust screen status fields') from None
    require(set(fields) == {'project_id', 'screen_name', 'state', 'session_id'}
            and fields['project_id'] == project
            and fields['screen_name'] == 'g2c-' + project
            and fields['state'] in ('attached', 'detached', 'interrupted',
                                    'unconfirmed', 'not_registered'),
            'Rust screen status identity invalid')
    return fields


def _check_approval(store, item):
    """Recheck at recovery time, while the caller holds its Queue execution lock."""
    number = item['number']
    with store.lock():
        state = store.load()
        job = state['jobs'].get(str(number))
        require(job is not None and job.get('status') == 'approved-project-screen'
                and job.get('id') == item['id'] and job.get('digest') == item['digest']
                and job.get('project') == item['project']
                and job.get('backend', 'project-screen') == 'project-screen',
                'Queue approval changed; no screen recovery')
        for other in state['jobs'].values():
            if other is job:
                continue
            if other.get('project') == item['project'] and other.get('status') in ('running', 'unknown'):
                raise RecoveryRefused('Unresolved older project claim; no screen recovery')
            if (other.get('project') == item['project']
                    and other.get('status') in ('approved', 'approved-screen', 'approved-project-screen')
                    and type(other.get('number')) is int and other['number'] < number):
                raise RecoveryRefused('Earlier approved project task; no screen recovery')


def _check_dead_anchor(store, project, root, status):
    """Require a verified, historically prepared anchor, never an arbitrary slot."""
    require(status['state'] == 'interrupted',
            'Screen is not provably interrupted; automatic recovery refused')
    expected_root = str(Path(root).resolve(strict=True))
    old = slots.read(project_backend._anchor_file(store, project))
    require(isinstance(old, dict) and old.get('number') == 0,
            'No proven previous Queue anchor; manual preparation required')
    view = old.get('view')
    require(isinstance(view, dict) and view.get('project') == project
            and view.get('root') == expected_root
            and view.get('full_id') == status['session_id']
            and type(view.get('pid')) is int and view['pid'] > 0
            and view['full_id'] == f"{view['pid']}.g2c-{project}"
            and isinstance(view.get('generation'), dict)
            and type(old.get('pid')) is int and old['pid'] > 0
            and isinstance(old.get('generation'), dict),
            'Old Queue anchor and Rust session identity differ')
    require(project_backend._old_process_gone(view['pid'], view['generation'])
            and project_backend._old_process_gone(old['pid'], old['generation']),
            'Old Queue anchor process may still be active')
    return view['full_id']


def ensure_anchor(store, cfg, item, root, proof):
    """Return an idle anchor, restoring only an exactly verified dead predecessor.

    Called before the one-time Queue claim. There is no replay on a failure
    after recover/start: later attempts see a non-interrupted Rust state and
    require an operator to reconcile partial infrastructure work.
    """
    project = item['project']
    try:
        return (*project_backend.anchor_ready(store, project, root), False)
    except (OSError, RuntimeError, ValueError) as original_error:
        # A live but busy/manual/unknown screen cannot be repaired automatically.
        status = _status(proof, project)
        if status['state'] != 'interrupted':
            raise RecoveryRefused('Existing project screen unavailable; manual review required') from original_error

    _check_approval(store, item)
    original_session = _check_dead_anchor(store, project, root, status)
    # Critical second preflight immediately before Rust archive/start. The
    # Rust side itself repeats the PID, generation, registry and name checks.
    project_backend.validate_start(proof, project, root, cfg['codex_bin'])
    require(_status(proof, project) == status, 'Project screen state changed')
    _cli(proof, project, 'recover', '--approve-recover')
    require(_status(proof, project)['state'] == 'not_registered',
            'Rust recovery did not release registered slot')
    _cli(proof, project, 'start', '--anchor-only', '--approve-start', '--task', 'Queue anchor')
    new_status = _status(proof, project)
    require(new_status['state'] in ('attached', 'detached')
            and new_status['session_id'] != original_session,
            'New project screen identity unverified')
    # prepare_anchor verifies a SINGLE /usr/bin/sleep infinity window/process
    # and archives the old durable Queue anchor record without overwriting it.
    project_backend.prepare_anchor(store, project, root)
    view, anchor = project_backend.anchor_ready(store, project, root)
    require(view['full_id'] == new_status['session_id'],
            'Rebuilt Queue anchor has wrong Rust screen identity')
    return view, anchor, True
