# gpt2codex

Local Rust bridge and optional Qt6 GUI for explicitly registered projects and
persistent Codex terminals. GNU screen is the default multiplexer; tmux is an
optional per-project preference. Konsole KPart and QTermWidget are independent
terminal backends. Codex is supplied separately by the operator.

The Debian package release target is **0.1.3** (tag `v0.1.3`).
A pushed stable tag starts isolated Debian package builds and release tests.
Publication happens only after mandatory checks and the configured
GitHub environment and RELEASE_PUBLISH_ENABLED gate. Debian 13 amd64 is
release-blocking. Arm64 and armhf remain experimental. The armhf follow-up
runs all package tests under a full-system ARMv7 kernel and may supplement an
existing approved release only after its own validation. Consult the actual
release assets and CI results; preparing a version does not publish it or
establish native ARM hardware or Devuan compatibility.

## Safety and current limits

The GUI only attaches verified existing sessions. Starts need explicit local
approval. Changing a project's multiplexer preference never migrates, stops,
detaches or replaces an existing session. Reconnect never restarts Codex.
Installation/removal supplies no service, autostart, session recovery or
registry/socket migration. No particular init system is required.

The original yellow artifacts in live Codex TUI remain unresolved; synthetic
render tests are not proof of their elimination. Attention Manager is not
implemented. See [GUI behavior](gui/README.md), [tmux lifecycle and archival](.ai/TMUX_LIFECYCLE.md)
and [manual TUI comparison](.ai/TMUX_COMPARISON.md).

## Build and validate

Debian 13 development packages: Rust/Cargo >= 1.85, CMake, g++, Qt6 >= 6.8,
`libqtermwidget-dev`, `libutf8proc-dev`, `libkf6parts-dev`,
`libkf6coreaddons-dev`, and `konsole-kpart` for integration tests.

```sh
cargo test --locked --offline --all-targets
cargo build --locked --offline --release
python3 ./tests/integration/gpt2codex-project-icons-test.py
```

Offline Cargo needs an already populated cache; fetch with `cargo fetch
--locked` first when network access is explicitly allowed. The GUI helper
uses the established CMake workflow and isolated configuration. No Codex or
multiplexer server is started by the icon/build tests.

Debian packaging, exact CI image/action/archive pins, dependency policy,
clean install tests and the separate release approval gate are described in
[packaging and CI](.ai/PLATFORMS_CI.md). Do not run Debian clean/build targets
in a valuable dirty checkout; stage the current files in an isolated tree.

## Configuration and operator use

See [configuration example](.ai/examples/config.toml) and
[project example](.ai/examples/projects/example.toml). Examples are templates,
not live local configuration. Projects/workspace setup is a separate explicit
local CLI operation. Start with `gpt2codex --help`; the package does not run
setup, install Codex or create projects automatically.

The GUI executable is `gpt2codex-gui`. `screen` and `git` are required by the
package; `tmux` is suggested. Konsole's runtime plugin and SVG/theme support
are recommended. Without Konsole's KPart the packaged QTermWidget backend
remains available. Read-only `--backend-probe` reports actual usability.

## Contributing

Read [AGENTS.md](AGENTS.md) and [.ai/README.md](.ai/README.md). Sources may be
untracked in the current local preparation; preserve the actual worktree.
The historic alpha candidate inventory is
[ALPHA_COMMIT_FILES.md](.ai/ALPHA_COMMIT_FILES.md). Runtime logs, local configs,
session registries, caches and the private handoff are excluded.

License: [MIT](LICENSE); packaged Rust dependency notices are installed with
the binaries. Qt/KDE/QTermWidget remain system-provided dynamic libraries.
