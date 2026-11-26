# Platforms, service managers, and CI

## Current stable release target

Package version target `0.1.3`, tag `v0.1.3`; workflow `.github/workflows/release.yml`.
Publication remains operator-controlled: `RELEASE_PUBLISH_ENABLED=true` and
GitHub environment `stable-release` are required for the publish job.
Tag pushes run mandatory Rust/Qt tests, build and Debian 13 install/remove
verification before automatic publication. A normal branch push does not
run the release workflow. Manual dispatch is diagnostic, never publishing.
Existing Debian amd64 acceptance gates and experimental ARM builds remain.
The independent ARMHF follow-up requires validated arm64 evidence from the
same source commit. It may add only new assets to an existing approved tag
release; no replacement of published assets or manual-dispatch publication.

## ARMHF experimental execution (2026-11-25)

QEMU linux-user 9.2.2 synthesizes the start ticks of a process reading its own
`/proc/PID/stat`. CI run 45588962319 measured 155414 versus the kernel value
155413 read by another process. The generation comparison must stay exact.
ARMHF follow-up therefore uses linux-user only to prepare a frozen Debian
ARMHF guest, then runs the unchanged complete package build and all tests
under its ARMv7 LPAE kernel in QEMU full-system mode. Both the guest kernel
and system emulator are installed from the signed CI snapshot. A successful
guest exit, consistent proc generation, and normal package validation are
required for acceptance/publication. Hosted run 45617259657 (`349d02d`)
booted Debian `6.12.113+deb13-armmp-lpae`, verified consistent process
identity, passed 92 library and 13 CLI Rust tests (one temporary diagnostic
ignored), all three CTest targets including the previously failing marker
regressions, Python tests and guest package inspection, and exited zero.
Final host inspection failed only because the clean checkout lacked `.tmp`;
commit `deb04f1` provides disposable runner-local inspection scratch.
The temporary cross-process probe is no longer needed and is removed for
0.1.3; strict generation checks, categorical errors and functional tests
remain. The full v0.1.3 tag build and release are still to be verified.
ARMHF remains experimental and non-blocking, and manual dispatch cannot
publish. This evidence is from a VM, not a physical ARMHF device.

## Historical first-alpha preparation (2026-10-28)

The original alpha package version was `0.1.0~alpha1`; its workflow was `.github/workflows/alpha.yml`.
All builds use Debian 13: amd64 blocking, arm64/armhf experimental with
fail-fast=false and continue-on-error. Quick checks run on each push/PR;
full amd64 CI runs for relevant changes or uncertain detection. Release/manual
full runs also request both ARM builds. One package per architecture is shared
with Devuan Excalibur. The Debian amd64 smoke gates future release; optional
Devuan `run_devuan=false` smoke is non-blocking. There is no Devuan-specific
rebuild. This is the approved 2026-10-31 optimization; previous CI #5 passed
all builds and both smokes, but this new workflow is not yet a passed hosted run.

Build inputs are frozen in `packaging/ci-lock.json` and Cargo.lock, with actual
package versions retained in `.buildinfo`/architecture evidence. Both backend
modules are required for complete alpha artifacts. ABI libraries (including
libsystemd0 where used by the distribution) do not imply a systemd init
requirement. No service or maintainer script is installed. The Debian CLI is
dynamically linked for this first alpha; the MUSL goals below remain future work.
See [packaging and release gates](ALPHA_PACKAGING.md) for commands and limitations.

## Required target matrix

| Distribution | Debian package arch | Priority | CI policy |
| --- | --- | --- | --- |
| Debian 13 (Trixie) | amd64 | Main / release-blocking | Must pass |
| Debian 13 (Trixie) | armhf (ARMv7) | Experimental | Non-blocking |
| Debian 13 (Trixie) | arm64 | Experimental | Non-blocking |
| Devuan 6 (Excalibur) | amd64 | Shared-package compatibility | Optional manual install smoke, non-blocking |
| Devuan 6 (Excalibur) | armhf (ARMv7) | Experimental | Non-blocking |
| Devuan 6 (Excalibur) | arm64 | Experimental | Non-blocking |

GitHub Actions should set `fail-fast: false` and `continue-on-error` for experimental matrix entries. Preserve failed experimental results visibly; do not silently label untested configurations as supported. A release is gated on Debian 13 / amd64 only.

## Rust/MUSL targets (verify toolchains in P0)

- `x86_64-unknown-linux-musl` -> Debian `amd64`.
- `armv7-unknown-linux-musleabihf` -> Debian `armhf`.
- `aarch64-unknown-linux-musl` -> Debian `arm64`.

Aim for a standalone MUSL-linked core. Verify cross-linking, third-party crates, Codex binaries, tunnel client availability, runtime testing, and package dependencies per architecture rather than assuming all platforms work automatically. Qt6 approval UI may be dynamically linked and should remain optional for headless systems.

## Service managers

- **Debian 13:** systemd user service for normal desktop operation.
- **Devuan Excalibur desktop:** XDG Autostart as an unprivileged session alternative, with explicit lifecycle limitations.
- **Devuan Excalibur headless/server:** SysVinit service supervision, installed with administrative rights but executing the daemon as a dedicated unprivileged user.

The Rust process must offer foreground execution independent of systemd. OpenRC/runit integration is an optional future extension, not a prerequisite. Support Qt6 and local CLI approval paths; unavailable approval agents must result in pending or denied requests, not implicit permission.

## Packaging and validation

Target separate `amd64`, `armhf`, and `arm64` artifacts, including Debian `.deb` packaging once approved. Validate binary execution, package dependencies, install/remove behavior and available service integrations. Run suitable smoke/integration tests per target; plain container builds do not establish working init supervision. Do not publish releases without a separately approved workflow.
