# P0-D: read-only project registry and session-name configuration

## Current extension: per-project multiplexer (2026-10-22)

Project TOML accepts `[sessions].multiplexer = "screen"` (default when absent)
or `"tmux"`. There is no global multiplexer setting. The preference only selects
a future explicitly approved start; existing records retain their actual
multiplexer. `session projects` lists preferences; local `session prefer --id
ID --multiplexer screen|tmux` saves one without invoking any session operation.
Old configuration/records remain valid without migration. For the independent
GUI backend setting and safe manual test see [GUI](../gui/README.md) and
[comparison](TMUX_COMPARISON.md). Historical P0-D scope below predates this extension.

Status: implementation candidate, awaiting a successful local `cargo test`,
release build, and smoke test against Codex 0.163.0. No MCP gateway or writing
operation is present.

## Global configuration

The bridge reads `~/.config/chatgpt2codex/config.toml` (or the absolute path
provided with `--config`). If `XDG_CONFIG_HOME` is set to an absolute path,
the XDG equivalent is used. Project files are read from the adjacent `projects/`
folder, as `<project_id>.toml`. Samples are in `.ai/examples/`.

Example:

```toml
[workspace]
project_dir = "/home/peva/TEMP/mycode"
rules_dir = ".ai"
global_context_files = ["CODEX_GLOBAL.md", "BOOTSTRAP.md", "PROJECT_DEFAULTS.md"]

[codex]
name = "Kóďa"

[sessions]
name_template = "{project_id} | {codex_name} | {task}"
```

For each project, configure a direct child of `project_dir`:

```toml
[project]
id = "k2-smb"
directory = "k2-smb"
name = "K2 SMB"

[codex]
name = "Kóďa SMB"

[contexts]
files = ["AGENTS.md", ".ai/.project_codex.md", ".ai/PROJECT_CONTEXT.md", ".ai/CODEX_RESULTS.md"]
```

The project-level `[codex].name` and `[sessions].name_template` may override
global values. The name template supports exactly `{project_id}`, `{codex_name}`
and `{task}`, and **must include `{project_id}`**. The preview does not create,
rename or resume a Codex session. Future managed task execution will apply it
after `thread/start`, under its own approval policy.

## Supported local commands

Run from the repository root after building:

```sh
cargo test
cargo build --release
./target/release/gpt2codex projects list --config /absolute/config.toml
./target/release/gpt2codex project info --config /absolute/config.toml --id k2-smb
./target/release/gpt2codex project contexts --config /absolute/config.toml --id k2-smb
./target/release/gpt2codex project name --config /absolute/config.toml --id k2-smb --task 'Fix KIO'
./target/release/gpt2codex project sessions --config /absolute/config.toml --id k2-smb --limit 25
./target/release/gpt2codex project read --config /absolute/config.toml --id k2-smb --scope project --file .ai/.project_codex.md
```

`project read` is explicitly local and prints the selected text to stdout. Do
not send its raw output to ChatGPT automatically. The P0-D test helper avoids
recording any document contents. `project sessions` prints only validated
metadata using the existing read-only app-server adapter.

The registry refuses invalid/duplicate IDs, unknown TOML keys, project roots
outside `project_dir`, file/path symlinks, path traversal, non-allowlisted files,
large contexts, malformed name templates and control characters. Missing
optional Markdown files are listed as `missing`; they are not created.

## Important boundary: not a security sandbox

This implementation is a read-only local CLI and is **not ready to expose** as
MCP. The path checks do not yet eliminate time-of-check/time-of-use races or
all same-user filesystem access. A future remote read interface must use
kernel-enforced path resolution and proper outbound sanitization. Markdown
rules also do not grant executable permissions.

## Agreed bootstrap convention, deferred from P0-D

The eventual approved Workspace Manager will own project creation and validate
or create exactly these symlinks:

```text
project/.tmp -> /tmp/<project_id>/tmp
project/.log -> /tmp/<project_id>/log
```

It will validate ownership of `/tmp/<project_id>` and safely create private
subdirectories. A conflicting preexisting path fails closed. Changes to `.git`
ignore/exclude also belong to that separately authorized write phase. Codex
will be **informed** through Markdown but will not create/repair the layout.

The shared rules live in `project_dir/.ai/`, and templates for a future managed
installation live in `.ai/templates/` of this repository (template version in
`.ai/templates/VERSION`). P0-D does not write
to `project_dir/.ai/` or any target project.

## Explicit project types (Issue 32, 2026-11-11)

`[project].type` accepts only `standard` and `non-git`. Missing type retains
`standard` for old TOML. IDs, filename identity, unique canonical roots, owned
root directories and path/symlink checks apply to both. Standard registration
requires real Git metadata and a matching Git top-level. Read-only linked
worktree registration remains supported; workspace writes still require a
regular checkout. Non-git registration never runs Git against that project,
even when an operator has subsequently added a `.git` directory. The type in
the Rust registry is authoritative; existence of `.git` never upgrades it.

Local CLI additions:

```sh
gpt2codex workspace adopt --id PROJECT_ID --directory DIRECTORY --type non-git --dry-run
gpt2codex workspace adopt --id PROJECT_ID --directory DIRECTORY --type non-git
gpt2codex workspace ensure --id PROJECT_ID
gpt2codex workspace health --id PROJECT_ID
gpt2codex project metadata --id PROJECT_ID
```

The adoption default is still standard. Non-git skips Git identity, tracked-file
checks and excludes. Rules/context, history creation, owned volatile directories
and exact `.tmp`/`.log` link checks remain common. Existing user files and valid
managed links are preserved; unsafe/conflicting managed paths fail before writes.
Both adoption and upgrade dry runs perform validation without filesystem writes.
`project metadata` is a separate bounded JSON interface containing `id`, `root`
and `type`; existing project listing output is unchanged.

### One-way upgrade

The operator must first prepare a valid regular Git checkout and its local
`.git/info/exclude` outside the upgrade command. It must contain all managed
entries: `/.tmp`, `/.log`, `/.ai/.project_codex.md`, `/.ai/CODEX_RESULTS.md`,
`/CODEX_RESULTS.md`, `/.bash_history`. Upgrade refuses missing entries rather
than changing Git or publishing a falsely ready standard project.

```sh
gpt2codex workspace upgrade --id PROJECT_ID --to standard --dry-run
gpt2codex workspace upgrade --id PROJECT_ID --to standard
```

Only non-git to standard is supported. The standard workspace preflight runs
again before replacement; missing Git, tracked reserved paths, unsafe paths,
identity conflicts and changed config refuse the operation. The comment-preserving
TOML edit shares the preference writer's directory lock, writes/fsyncs a new
file and uses Linux atomic exchange. The old inode remains available until the
directory sync succeeds; a sync error attempts atomic rollback. If both storage
sync and rollback fail, diagnostics require manual inspection and preserve both
files. No software transaction can promise recovery from a failing filesystem.
No history, context, Git configuration, remote, session or Queue state is edited.
Already-standard requests and downgrade targets are rejected.

### Queue project-screen admission

Only project-screen uses the new type gate. Configure optional absolute
`bridge_cli` and `registry_config` in the existing private Queue configuration;
defaults are `gpt2codex` from PATH and the Rust CLI's existing XDG/HOME registry
location. An old/missing CLI or registry mismatch fails closed for standard and
non-git alike. This does not change the backend default or existing approvals.

Before the durable Queue claim, the worker holds a shared registry-directory
lease, queries Rust JSON metadata and requires exact ID/canonical-root agreement
with Queue project_roots. For non-git only, it checks the configured executable's
`exec --help` for the exact `--skip-git-repo-check` flag. No login or model call
is part of this capability probe. Unsupported CLI fails before claim with an
explicit message. Existing ChatGPT identity, SHA/backend/host approval, anchor,
process, window, FIFO and project execution lock checks still apply.

The claimed job and private slot retain the validation proof, including executable
SHA256/generation. Launch and runner revalidate it; changed/unknown evidence
fails without resetting or replaying the claim. The runner acquires its own
shared lease before start. Managed upgrades require the exclusive lock and fail
while a Queue lease is held; different projects may hold shared leases together.
Verified executable descriptors are used for metadata, capability probing and
Codex exec, avoiding a pathname replacement between validation and execution.
Only a verified non-git project-screen exec gets the skip flag. Standard and
legacy exec/screen paths never get it. These cooperative locks do not provide
an OS sandbox against a hostile process of the same UID editing files directly.

### Later operator steps for txt-proc — not executed or authorized here

After separate approval of enrollment, run each command individually and inspect
its result. Use the newly built CLI and the operator's normal registry config:

```sh
gpt2codex workspace adopt --id txt-proc --directory txt-proc --type non-git --dry-run
gpt2codex workspace adopt --id txt-proc --directory txt-proc --type non-git
gpt2codex workspace ensure --id txt-proc
gpt2codex workspace health --id txt-proc
gpt2codex screen start --id txt-proc --anchor-only --approve-start
```

The final command is a separate live operator action, only after ready health
and existing session/slot checks. Then follow the existing explicit Queue anchor
preparation and approval workflow, with Rust root matching the future Queue
allowlist. No txt-proc enrollment, Git initialization, screen start or Issue 30
claim was performed in Issue 32. Issue 30 remains unapproved. A later upgrade
uses the two upgrade commands above only after the operator prepares Git/excludes.
Host live E2E, real txt-proc readiness and live anchor/Queue interoperability
remain unverified; synthetic tests are not host evidence.
