"""Bounded, fail-closed projection of an untrusted per-turn final JSON report.

No logs, issue text, diffs or cumulative handoff files are inputs. This is a
same-UID, trusted-author queue, not a DLP system or proof of model honesty.
"""
import html
import json
import os
import re
import stat
import unicodedata

MAX_HANDOFF = 6 * 1024
MAX_REPORT = 8 * 1024
FIELDS = ('implemented', 'files', 'tests', 'limitations', 'next_step')
# Common prose dashes (including the em dash in the real read-only #14 final).
# Czech prose quotes occurred around SCREEN_WORKER_OK in the real #18 final.
# Keep the allowlist narrow; quotes do not bypass the content/path checks below.
PROSE_PUNCTUATION = ' .,;:!?()/+%_*-–—„“'
SENSITIVE = re.compile(
    r'github_pat_|gh[pousr]_|sk-|xox[baprs]-|AKIA|ASIA|eyJ|'
    r'(?i:secret|password|passwd|credential|authorization|bearer|token|'
    r'private[ _-]?key|BEGIN.*KEY|api[ _-]?key|client[ _-]?secret|'
    r'access[ _-]?key|connection[ _-]?string|db[ _-]?(?:host|user|pass)|'
    r'database[ _-]?(?:url|host|user)|smtp[ _-]?(?:host|user|pass)|'
    r'\.env|\.ssh|\.aws|\.codex|\.config|auth|kubeconfig|netrc|'
    r'internal[ _-]?config|priv[aá]tn[ií].*konfigur|heslo|tajemstv|'
    r'id_rsa|id_ed25519|\.git/|\.npmrc|\.pypirc|\.pem\b|\.key\b|\.kube)'
)


def schema(item):
    text = {'type': 'string'}
    properties = {k: {'type': 'array', 'items': text} for k in FIELDS}
    properties['tests'] = {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'properties': {'name': text, 'status': {'type': 'string', 'enum': ['PASS', 'FAIL', 'NOT RUN']}},
        'required': ['name', 'status']}}
    properties.update({
        'issue': {'type': 'integer', 'enum': [item['number']]},
        'request_sha256': {'type': 'string', 'enum': [item['digest']]},
        'run_nonce': {'type': 'string', 'enum': [item['_handoff_nonce']]},
    })
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(properties)}


def instruction(item):
    return (
        '\nWORKER REPORT CONTRACT (not part of the issue task):\n'
        'Your final answer MUST be one JSON object matching the supplied output schema. '
        'It is automatically projected into a short comment on THIS private queue Issue. '
        'Report only work actually performed in this turn; never copy task instructions, '
        'previous history, logs, code, diffs, configuration values or secrets. '
        'Use short Czech plain-text sentences (at most 220 characters each, 8 entries per list). '
        'implemented describes actual work or read-only findings; files contains only relative '
        'paths changed BY THIS JOB (empty for read-only). tests contains short test names '
        'and their actual PASS/FAIL/NOT RUN status, never guessed results. '
        'limitations and next_step describe unverified work and next action. '
        'Use empty arrays when unknown. Do not use Markdown, URLs, absolute paths, '
        'credentials, private configuration, code or protocol markers. '
        'Do not write a report file yourself: the CLI saves this final answer outside '
        'the model tool sandbox, including for read-only jobs. '
        'Preserve any separately required local historical handoff only if writes are authorized.\n'
        f"issue={item['number']} request_sha256={item['digest']} run_nonce={item['_handoff_nonce']}\n"
    )


def safe_text(value, path=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 220:
        raise ValueError('text_size')
    value = unicodedata.normalize('NFKC', value)
    if len(value) > 220:
        raise ValueError('normalized_size')
    # Reject controls, hidden/bidi text and non-prose syntax rather than trying
    # to salvage arbitrary code/configuration by replacing known token strings.
    if any(not (unicodedata.category(c)[0] in 'LN' or c in PROSE_PUNCTUATION) for c in value):
        raise ValueError('text_syntax')
    value = ' '.join(value.split())
    # Root helper filenames are deliberately descriptive. #24's real final
    # report was rejected solely for gpt2codex-supervisor-admission-test.py.
    # Exempt only this project's lower-case, short-word Python/shell helper
    # naming convention in the files field, never arbitrary prose or blobs.
    helper_path = (path and len(value) <= 80 and re.fullmatch(
        r'gpt2codex(?:-[a-z]{1,16}){2,6}\.(?:py|sh)', value))
    if (not value or SENSITIVE.search(value)
            or (re.search(r'[A-Za-z0-9_+-]{32,}', value) and not helper_path)
            or re.search(r'(?i)G2C[ –—-]|https?:|(?<![A-Za-z0-9_.-])/|[A-Za-z]:/|\.\.', value)):
        raise ValueError('sensitive_or_ambiguous')
    if path and (not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value)
                 or any(p in ('.', '..') for p in value.split('/'))):
        raise ValueError('relative_path')
    # Escaping is defense in depth after rejecting multiline/protocol syntax.
    return re.sub(r'([\\`*_{}\[\]()#+.!|>-])', r'\\\1', html.escape(value))


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o077 or st.st_nlink != 1 or st.st_size > MAX_REPORT):
            raise ValueError('report_file')
        raw = f.read(MAX_REPORT + 1)
    if len(raw) > MAX_REPORT:
        raise ValueError('report_size')

    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError('duplicate_field')
            result[k] = v
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique)


def marker(job):
    return f"<!-- g2c-handoff:issue={job['number']}:id={job['id']}:sha256={job['digest']} -->"


def render(job, report_path=None):
    """Return only safe text and a fixed diagnostic code; no exception payloads."""
    exit_code = job['exit_code'] if job['status'] != 'unknown' else 'nedostupný'
    header = (f"G2C-HANDOFF/1\nIssue: #{job['number']}\nStatus: {job['status'].upper()}\n"
              f"Exit code: {exit_code}\nRequest SHA256: {job['digest']}\n")
    # This describes the worker result, never a claim about tests or findings.
    outcome = f"Výsledek: {job['status'].upper()}, exit code: {exit_code}\n"
    fallback = (header + 'Podrobný handoff: nedostupný\n'
                + 'Provedeno: Nelze ověřit.\n' + outcome
                + 'Změněné soubory: Nelze ověřit.\nTesty: Neuvedeno.\n'
                + 'Omezení: Věcný report není dostupný nebo bezpečně ověřitelný.\n'
                + 'Další krok: Lokálně ověřit výstup úlohy.\n' + marker(job))
    try:
        if report_path is None:
            return fallback, 'missing_report'
        obj = read_report(report_path)
        if not isinstance(obj, dict) or set(obj) != set(FIELDS) | {'issue', 'request_sha256', 'run_nonce'}:
            raise ValueError('schema')
        if (type(obj['issue']) is not int or obj['issue'] != job['number']
                or obj['request_sha256'] != job['digest'] or obj['run_nonce'] != job['handoff_nonce']):
            raise ValueError('identity')
        if any(not isinstance(obj[k], list) or len(obj[k]) > 8 for k in FIELDS):
            raise ValueError('lists')
        if job['mode'] == 'read-only' and obj['files']:
            raise ValueError('readonly_changes')
        if not obj['implemented']:
            raise ValueError('empty_report')
        parts = [header, 'Zdroj detailu: strukturovaný report Codexu; nezávisle neověřen.\n', outcome]
        for key, label in zip(FIELDS, ('Provedeno', 'Změněné soubory', 'Testy',
                                      'Omezení', 'Další krok')):
            parts.append(label + ':\n')
            values = []
            for value in obj[key]:
                if key == 'tests':
                    if (not isinstance(value, dict) or set(value) != {'name', 'status'}
                            or value['status'] not in ('PASS', 'FAIL', 'NOT RUN')):
                        raise ValueError('tests')
                    values.append(safe_text(value['name']) + ': ' + value['status'])
                else:
                    values.append(safe_text(value, path=key == 'files'))
            parts.extend('- ' + v + '\n' for v in values)
            if not values:
                parts.append('- Žádné dle reportu.\n' if key == 'files' else '- Neuvedeno.\n')
        body = ''.join(parts) + marker(job)
        if len(body.encode('utf-8')) > MAX_HANDOFF:
            raise ValueError('comment_size')
        return body, 'structured_report'
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return fallback, 'report_rejected'
