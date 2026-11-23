#!/usr/bin/env python3
"""Jednorázová lokální kontrola nepotřebných projektových screenů.

Výchozí --dry-run. Současný CLI screen adapter negarantuje idle a nemá stop;
proto je --apply záměrně omezen na znovuověření a SKIP, nikdy na odhadovaný kill.
Nečte token, transcript, queue sloty ani sockety; nemění durable registry.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROW = re.compile(r'^multiplexer=(screen|tmux) generation=(\S+) project_id=([a-z0-9_-]+) screen_name=(\S+) state=(\S+) session_id=(\S+) program=(\S+) exit_code=\S+ exit_signal=\S+ exited_at=\S+ task=.*$')


def allowed_ids(path):
    with path.open(encoding='utf-8') as stream:
        data = json.load(stream)
    roots = data.get('project_roots')
    if data.get('version') != 1 or not isinstance(roots, dict) or not roots:
        raise ValueError('Queue config: neplatný project_roots')
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key)
           or not isinstance(root, str) or not Path(root).is_absolute()
           for key, root in roots.items()):
        raise ValueError('Queue config: neplatné ID/root')
    return set(roots)


def evaluate(row, allowed):
    mux, generation, project, name, state, session, program = row
    # Only CLI-returned managed slots are evaluated. Never derive ownership from names.
    if project == 'bridge':
        reason = 'interní Bridge'
    elif project in allowed:
        reason = 'projekt povolený v Queue'
    elif project.startswith('q') and re.fullmatch(r'q[0-9]+-[0-9a-f]+', project):
        reason = 'Queue slot chráněn'
    elif name == 'codex-mouse-test' or session.endswith('.codex-mouse-test'):
        reason = 'cizí testovací session'
    elif mux != 'screen':
        reason = 'mimo rozsah screen úklidu'
    elif state == 'not_registered':
        reason = 'žádná registrovaná session; nic k ukončení'
    elif state == 'attached':
        reason = 'připojená session'
    elif state != 'detached' or session == '-' or generation == '-':
        reason = 'existence/identita živého screenu neověřena'
    else:
        reason = 'idle nelze prokázat; screen CLI nemá řízený stop'
    # screen program=unknown is NOT evidence of an idle model turn.
    activity = 'unknown (idle neprokázáno)'
    return (project, session, state, activity, 'SKIP: ' + reason)


def snapshot(cli, config, queue_config):
    allowed = allowed_ids(queue_config)  # failure aborts; no fallback to an empty set
    result = subprocess.run([str(cli), 'session', 'list', '--program-status',
                             '--config', str(config)], capture_output=True,
                            text=True, timeout=10, check=True)
    if len(result.stdout) > 65536:
        raise ValueError('Příliš dlouhý CLI výstup')
    rows, seen = [], set()
    for line in result.stdout.splitlines():
        match = ROW.fullmatch(line)
        if not match:
            raise ValueError('Neznámý CLI formát; žádný zásah')
        row = match.groups()
        key = row[0], row[2]
        if key in seen:
            raise ValueError('Duplicitní CLI identita; žádný zásah')
        seen.add(key)
        if row[0] == 'screen':
            rows.append(evaluate(row, allowed))
    if not rows:
        raise ValueError('Prázdný CLI seznam; žádný zásah')
    return allowed, rows


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--bridge-cli', type=Path, default=repo/'target/release/gpt2codex')
    # None delegates default config discovery to CLI (including XDG_CONFIG_HOME).
    parser.add_argument('--config', type=Path)
    parser.add_argument('--queue-config', type=Path, default=Path.home()/'.config/gpt2codex/github-queue/config.json')
    args = parser.parse_args()
    if args.config is None:
        import os
        xdg = Path(os.environ.get('XDG_CONFIG_HOME', ''))
        base = xdg if xdg.is_absolute() else Path.home()/'.config'
        args.config = base/'chatgpt2codex/config.toml'
    initial = snapshot(args.bridge_cli, args.config, args.queue_config)
    print('ALLOWED=' + ','.join(sorted(initial[0])))
    print('PROJECT ID | SESSION ID | ATTACHED/DETACHED | ACTIVE/IDLE | VÝSLEDEK')
    for row in initial[1]:
        print(' | '.join(row))
    print('K ukončení: 0. Registry neprokazuje neaktivitu modelového turnu.')
    print('codex-mouse-test, cizí screeny a Queue #8: SKIP; helper je nevyhledává ani neovládá.')
    if args.apply:
        if not sys.stdin.isatty():
            raise ValueError('--apply vyžaduje interaktivní terminál; nic nebylo ukončeno')
        if input('Znovu ověřit jednotlivé uvedené SKIP bez ukončení? Napište OVERIT: ') != 'OVERIT':
            raise ValueError('Nepotvrzeno; nic nebylo ukončeno')
        if snapshot(args.bridge_cli, args.config, args.queue_config) != initial:
            raise ValueError('Stav se změnil; spusťte znovu --dry-run')
        print('Ověřeno. Vše SKIP; žádný bezpečně prokazatelný kandidát k ukončení.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('BLOKOVÁNO: ' + str(error), file=sys.stderr)
        sys.exit(2)
