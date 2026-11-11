# Issue 20: project-exclusive Queue, stage 1 (2026-11-10)

The approved target supersedes the per-issue screen design: one dispatcher,
one persistent registered screen per project, at most one Codex process per
working tree across Queue, manual start and explicit historical resume.
Read-only receives the same exclusion as workspace-write. Different projects
may execute concurrently. An idle manual TUI still owns the project.

## Implemented locally

`gpt2codex_queue_dispatch.py` is an **offline candidate**, not a launch backend.
It has no Codex launch, GNU screen, GitHub or authentication calls. Since #24,
an explicit deny-only probe can spawn the read-only Rust admission CLI.
It is not imported by the production worker. Its SQLite ledger requires a
separately supplied existing private directory. Tests supply only isolated
directories under the existing `.tmp`; no live ledger was created.

Transactions commit job state and event evidence together, with FULL SQLite
synchronization. A partial unique index enforces one reserved canonical root
for running/finishing/interrupted_unknown. Aliased roots share exclusion;
nested overlapping roots are rejected. Reservation and global queue capacity
(default 2, configurable 1–32) are durable, not in-memory semaphores. Existing
capacity cannot silently change on restart. Unknown claims continue to count.
Manual and resume candidate reservations use the same exclusion; their own
project reservations do not consume Queue capacity.

FIFO is insertion order among approved/waiting jobs for a root, not issue
number. An immutable binding records key, issue number, SHA, repository,
project, canonical root, hostname, mode and source. Duplicate Queue identity
cannot be submitted with another key, including after completion. Claim nonce
is distinct from thread UUID. New records have no inherited thread UUID.
Completion accepts one canonical UUID and rejects reuse in another record.

The local summary API returns pending/approved/waiting/running/finishing/done/
error/interrupted_unknown, waiting reason, project, session/window, timestamps,
thread UUID, process generation, output reference and live/closed/unknown
lifecycle. Summary does not perform GitHub polling. It is not yet the GUI feed.

Reservation requires an enabled, verified project/session observation with
exact canonical root, registered full screen ID and server creation ticks.
An observation of either active or idle Codex yields project_busy. Missing or
inconsistent metadata yields session_unverified; disabled yields
project_disabled. These are **adapter inputs**, not actual screen/process
discovery. No live safety guarantee follows from a caller asserting verified.

A bound process includes PID, creation ticks, owner, boot ID and PID namespace.
Closure refuses live/unknown processes, wrong claims, wrong session/window or
unconfirmed window closure. Restart keeps reservations: absent process without
completion becomes interrupted_unknown; a completed turn remains finishing
until the adapter confirms closure. No automatic replay, timeout-based lease
expiry or release on worker exit exists. No reconciliation API for releasing
interrupted_unknown is implemented. Operator review must not merely delete it.

`execution_backend=project-screen` and the explicit approval choice are now
recognized but intentionally unavailable. Readiness returns NO. Approval and
automatic dispatch reject this choice before claims, token reads or remote
calls. There is no fallback to screen/exec and no automatic conversion of
approved-screen. Old explicit backend selection and old approvals retain
their prior behavior; they do **not** gain project exclusivity from this stage.
Do not treat the old screen backend as an implementation of issue 20.

## Required next implementation

1. Add a Rust project process supervisor shared by every managed start/resume
   and Queue launch, including attention wrapper paths. Use one authoritative
   reservation namespace/lock protocol rather than independent Python and Rust
   locks. Bind repository issue ID as well as number and SHA at the adapter
   boundary. Revalidate host/repo/allowlist/approval immediately before claim.
2. Extend the real `screen.rs`/`session.rs` APIs. Current start creates a whole
   project screen whose main program is Codex; attach and state directory locks
   do not provide window lifecycle control or process-lifetime exclusion.
   Existing per-multiplexer slots also do not exclude another mux for a root.
   Verify registration, configured enabled/preferred mux, PID generation,
   session and exact created window generation. Preserve a non-Codex project
   anchor so the server survives the last Queue Codex window closing.
3. Integrate a supervisor launch handshake: persist claim before launch, hold
   exclusion through exec and owned descendants, record process before the
   first turn, and fail closed on ambiguous pre-bind crash. Scan for unmanaged
   Codex conservatively without claiming arbitrary external starts are locked.
   Never feed a prompt into an existing interactive TUI or terminate a manual
   process. Do not derive control merely from a screen name or window number.
4. Replace the legacy global execution lock only for the new backend. A single
   dispatcher must continue polling while supervising several distinct-project
   turns; current run_approved blocks synchronously. Persist per-job outputs
   and UUID early, isolate failures, reconcile after restart without launch.
   Preserve legacy state, screen slots and exec behavior; no forced migration.
5. Persist output and terminal evidence, publish existing receipt/handoff
   outbox, then close only the owned Codex/window and release after verified
   process/descendant exit. Handle publication outages and finishing recovery
   without replay or retaining an idle TUI. The candidate's window_closed
   parameter is only an adapter contract, not proof supplied by a real API.
6. Make Qt6 Queue an overview/approval/control tab. Current QueuePage still
   owns terminal attachment behavior. Wire local evidence refresh (manual and
   15 seconds) independently of GitHub poll interval; no automatic project
   terminal switching. Historical resume must explicitly acquire the same
   supervisor reservation. Worker monitor remains separate issue 17.
7. Only after all fixture gates pass, enable project-screen explicitly. Do not
   enable or restart production solely because the candidate tests passed.

## Verification boundaries

The new tests run actual concurrent **fixture Python processes**, not Codex.
They exercise SQLite reservations, first-approved FIFO, read-only/write
exclusion, capacity across reopen, process identity/exit, stale claims,
finish-before-restart recovery, UUID reuse rejection, transactional rollback,
and reserved backend rejection. Active/idle manual observations and window
closure are supplied fixtures. They do not prove Rust, screen or Qt6 behavior.

Actual project screen anchor preservation, automatic window closure, zero idle
Codex, manual GUI/resume exclusion, unmanaged process discovery and concurrent
model turns are NOT RUN / not implemented. Historical q18/q19 were not opened,
controlled, migrated or replayed. Existing offline screen and handoff suites
exercise their synthetic compatibility paths, including issue 19 punctuation,
6 KiB limits, idempotent publication and exec regression.

## Rollback and one future operator E2E

No live config, service, multiplexer registry/socket or GUI changed. Keeping
the existing execution_backend preserves the existing implementation now.
To roll back a future explicit project-screen selection, the operator first
verifies no live or unresolved reservation, then explicitly selects exec or
legacy screen. Never translate approval states, delete reservations or close
historical TUI to make rollback appear idle. This candidate needs no migration.

One combined future E2E, **blocked until steps 1–7 above are implemented**:
after this job finishes, the operator verifies all managed projects/processes
and approvals are idle with no unknown state, explicitly selects the verified
new backend and restarts the existing user worker. No second Queue screen.
Use NEW approved read-only smoke issues in two separately authorized projects,
and a second issue for the first project. Observe concurrent first turns in
different projects and FIFO with zero overlap in the same project. During a
turn attempt managed manual start/resume and confirm blocking; separately
leave a manual idle TUI and confirm Queue project_busy. Verify fresh UUIDs,
durable bindings, exactly one receipt and factual non-fallback handoff per
issue, closed owned windows/processes, surviving project anchor/main windows,
and explicit later UUID resume under the same lock. Exercise dispatcher
restart/GUI disconnect using the completed fixture/recovery design before
repeating this production smoke. Never replay historical issues. This combines
the pending issue 19 read-only report smoke with the new lifecycle smoke; it
has not been performed here and is not authorized for automatic execution.

## Issue 21 / S2 reservation kernel (2026-11-10): INCOMPLETE, runtime disabled

This increment does **not** meet the S2 Definition of Done. It supplies a Rust
reservation kernel and a deny-only opt-in integration gate. It does not yet
supply a verified process/window supervisor. Neither a claim nor a successful
inspect is proof that a project has no existing Codex. Do not connect it to a
launcher, enable project-screen, restart a service, or remove historical slots.
The seven integration requirements above remain applicable.

### Actual local API

`src/project_supervisor.rs`, exported from the existing Rust library:

- `Supervisor::open(directory)`: opens an existing absolute private directory;
  creates no directory. Every cooperating caller MUST use the same directory
  inode. There is deliberately no production default or new runtime setting.
- `inspect(project_id, root) -> Option<Claim>`: read only, including when the
  journal is absent. A missing claim means only no reservation in this journal.
  It does not inspect a screen or report the project as safe to launch.
- `claim(project_id, root, Owner::{Queue,Manual}, Option<IssueBinding>)`: mutation
  that persists a **pending reservation only**, not process ownership or launch
  permission. Queue requires repository, issue ID, number and full SHA256.
  Manual start and resume use the same manual owner type. No sandbox/activity
  parameter can bypass reservation exclusion. FIFO remains the #20 planner's
  job; this API has no queue ordering or timeout-based retry/release.
- `poll_verify(&Claim)`: compares the entire immutable binding against durable
  state, including nonce, project, canonical root/device/inode, hostname, boot
  and PID namespace. Currently returns only `reserved_runtime_disabled`.
- `start_in_project_screen(&Claim, &[String])`: verifies the claim, then returns
  `session_unverified: runtime_disabled`, without executing the command.
- `finish_and_release(&Claim)`: verifies the claim, then returns
  `session_unverified: launch_and_exit_proof_missing`, without closing or
  releasing anything. No caller-provided boolean can stand in for exit/window
  evidence. Pre-bind crash ambiguity remains reserved indefinitely.
- `ProcessIdentity::read(pid)` / `verify()`: Linux procfs metadata only; returns
  live/gone/unverified. PID generation mismatch, foreign UID, boot or namespace
  is unverified. This is an identity probe, not Codex discovery or a release
  authorization. No signals are sent and no arbitrary process is adopted.

The version 1 claim schema reserves fields for process, window and thread UUID.
They MUST remain null in this candidate. Loading fabricated non-null fields
fails closed; those fields are not implemented lifecycle evidence. In particular,
WindowIdentity is a schema placeholder, not verified GNU screen window identity.
No registration migration or active-claim migration is implemented.

The existing CLI adds only these local operations (no MCP exposure):

```
gpt2codex supervisor inspect --id ID --config ABS_CONFIG --directory ABS_PRIVATE_DIR
gpt2codex supervisor claim --id ID --config ABS_CONFIG --directory ABS_PRIVATE_DIR --owner manual --reservation-only
gpt2codex supervisor claim --id ID --config ABS_CONFIG --directory ABS_PRIVATE_DIR --owner queue --reservation-only --issue-json JSON
```

`JSON` has exactly repository, issue_id, number, sha. Project/root come from
ProjectRegistry, never from a caller-supplied command or workspace argument.
Success prints a version 1 JSON object with runtime_enabled=false and claim.
Invalid/missing arguments or refusal return nonzero with a fixed reason. Claim
without --reservation-only is refused before creating a journal. CLI start,
finish, adoption and force-release do not exist; use the Rust API errors above.
No credentials, screen query, command launch, GitHub operation or config write
occurs in this CLI. Use fixture directories only until namespace deployment is
separately designed. Unused reservations currently cannot be cancelled safely.

### Storage, recovery and boundaries

One descriptor-pinned directory flock serializes all roots, including across
processes. A per-instance mutex also serializes threads sharing one descriptor.
The lock protects only metadata transitions, not the duration of work in other
projects. Pending claims in one bounded versioned journal survive lock-holder
exit. Writes use random create_new private files, fsync, atomic rename and
parent fsync. Inspect uses a shared lock and never initializes the journal.
A malformed/foreign/stale journal is an error, never idle. Orphan pending write
files are not replayed, adopted or deleted. No lock file is unlinked/recreated.
Canonical aliases and nested roots conflict; the same project cannot silently
move to another root. Root inode, host, boot or namespace mismatch blocks the
journal until separately reviewed. This intentionally trades availability for
safety, including blocking other roots when the shared journal is unverified.

This is a cooperating same-UID local protocol, not a sandbox against that UID
editing/replacing state or using a different state directory. #20's SQLite
reservations remain an **offline planner candidate**, not another live authority.
No production path imports this kernel from Python. Future launch permission
must come from this Rust supervisor alone, after planner approval/FIFO selection;
never equate the two independent candidate reservations with one deployed lock.

### Managed paths inspected and gate coverage

Actual start functions are screen::start_with_attention,
session::start_with_attention (screen/tmux), and attention_marker::run.
Presence of G2C_PROJECT_SUPERVISOR (even empty) now makes each return
session_unverified/runtime_disabled before process or multiplexer discovery.
This is an opt-in STOP gate, **not** an enable flag or runtime implementation.
No environment or installed configuration was changed. With the variable absent,
legacy behavior remains as before; it does not gain project-wide exclusion.

Qt6's ordinary project UI lists and attaches through session list/attach with
expected session ID/process generation. It has no ordinary project start/resume
command to wire here. Attach does not start Codex. The Bridge shell can still
start arbitrary commands, and raw terminal/manual Codex starts are unsupported.
The Python legacy exec and per-issue screen/resume paths do not participate in
the new journal or this Rust gate. Their behavior is unchanged. An existing
manual active/idle TUI is not automatically registered in the new journal.
No unmanaged-process discovery is implemented. **Absolute live exclusion is not
claimed**, regardless of sandbox mode. New project-screen remains unavailable.

Read-only inspection of existing project-session JSON extracted identity fields
only. Historical/duplicate project records exist; no record was rewritten,
recovered, adopted or treated as sufficient proof for screen control. No screen
listing, socket connection or production screen command was needed for this work.

### Exact next implementation gate

Implement a descriptor-bound launch handshake in Rust while the durable claim
is held: prove the existing configured/enabled project screen server and anchor,
create and bind one fresh nonce-bearing window and owned foreground Codex before
its first turn, track owned descendants, and refuse ambiguous crash recovery.
Provide independent revalidation of server/socket/window/process generation
before any close, then prove the owned process and descendants exited before
closing only its Queue window and releasing. Test the adapter with fake GNU
screen/process fixtures, including reused window numbers and surviving anchor.
Do not add a runtime enable flag until this proof path exists.

Next, wire every managed manual start and explicit historical resume (including
legacy Python/attention wrappers that will be supported) to that same Rust
namespace. Add conservative unverified-session discovery and describe external
starts that remain outside the protocol. Only then adapt #20 reserve to request
supervisor permission after revalidating issue ID/SHA/host/repo/allowlist, keep
FIFO/capacity in the planner, and make the user worker multiplex nonblocking
polling across different roots. Completion must persist evidence/outbox before
verified close/release and must never replay a turn. None of this connection is
implemented here. Live E2E is NOT RUN and project-screen is still blocked.

## Issue 22 / S3: incomplete, diagnostic probe only (2026-11-10)

S3 is NOT complete. S2's reservation-only API and deny-only start/release remain
in force. No runtime enable flag, launcher, release, worker bridge or namespace
deployment was added. project-screen remains unavailable; legacy exec/qN behavior
does not acquire common project exclusivity. All uncovered paths listed under
S2 above remain uncovered. Qt6 still only attaches existing sessions; its Queue
attachment is to the legacy per-issue backend, not a supervised project job.

The opt-in G2C_PROJECT_SUPERVISOR STOP gate now also covers
CodexClient::connect, before spawning the read-only CLI/MCP app-server. With the
flag absent this route still has no common project lock; the client API has no
project-root binding. The gate is a refusal, not app-server ownership/adoption.

The additional local diagnostic command is:

```
gpt2codex supervisor inspect-screen --id ID --config ABS_CONFIG --socket-directory ABS_PRIVATE_SCREEN_DIRECTORY
```

It does not use the reservation journal. It validates the selected registered
screen record/root/preferred mux and recorded server generation/UID/boot/namespace,
compares the executable inode with /usr/bin/screen, pins the explicit private
socket directory, checks the full socket ID/device/inode, and connects to that
one socket to check Linux SO_PEERCRED. It sends no screen protocol bytes or
commands, closes the connection, and collects bounded procfs descendant metadata
(parent, process group/session, starttime, TTY, cwd-match). It rereads identity
and metadata and rejects ambiguity. It never scans other socket directories,
reads process argv/environment or adopts a process. The registered JSON is
reread unchanged before return. Linux installations with a different screen
executable inode/path fail closed. This diagnostic does connect to the selected
socket; it was NOT invoked against any existing user session in this job.

This is deliberately NOT a trusted window/Codex discovery or absence proof.
Any observed descendant (including a shell or idle synthetic program) means
project_busy; no descendants means session_unverified. launch_permitted and
runtime_enabled are always false, window_identity_verified is always false.
It cannot recognize a detached/reparented Codex elsewhere or distinguish an
anchor shell from manual TUI. Do not consume it as permission for claim/start.
An inspect result is a point-in-time diagnostic, never a lease or exit proof.

The isolated real-screen helper gpt2codex-project-screen-test.py uses a new
private SCREENDIR, HOME, registry and synthetic sleep processes only. In this
workspace sandbox, four cases FAIL during setup: own screen start exits 1.
Independent capability probe confirms AF_UNIX socket creation fails EPERM;
PTY allocation works. No screen/window assertions ran. Do not relabel these
as PASS, use existing sessions as a workaround, or enable runtime based on them.

Two independently verified fixes are usable without the runtime:
- Legacy screen listing reads consistent result.json evidence (one completed
  turn, canonical thread UUID, exit 0, finished_at and matching phase evidence).
  A lost post-turn TUI is projected as turn_done/tui_closed, retaining UUID and
  last_phase. No history/state/GitHub write or replay occurs. Missing, malformed,
  mismatched or incomplete terminal evidence still projects interrupted_unknown.
- Offline dispatch treats reused PID generation or a missing procfs subfile
  while the PID still exists as unknown, never gone/release permission.

Parallel Rust attention failure was reproduced (75/79 in this run). A separate
deterministic fork fixture failed before the fix: closing the parent's directory
fd alone leaves flock held by an inherited child fd until exec. An RAII guard
now explicitly unlocks at the end of each critical section, including error
returns. Nonblocking refusal, identity checks and exact-nonce clear are retained.
This is a real transient lock-lifetime defect exposed by the parallel harness,
not independent failures of the four poisoned tests. No retry/sleep or forced
global serial testing hides the collision. Final parallel and serial Rust runs
both pass 80 library + 12 CLI tests, including the fork regression.

S4 remains conditional on finishing S3: prove an explicitly registered persistent
anchor and a fresh window generation, bind foreground process before execution,
track all owned descendants across manager crash/reparenting, verify exit/window
closure, and only then release. Deploy one common namespace and cover every
supported manual start/resume before connecting dispatcher FIFO/capacity to the
worker. Persist terminal evidence/outbox before finish. Qt6 should then consume
read-only project/job evidence instead of gaining a separate launcher. Never
automatically migrate old sessions, delete unknown claims or replay old jobs.
Rollback of this increment needs no production action: the backend/config/service
were unchanged. Keep runtime disabled; the diagnostic has no stored state to
roll back and history changes are projections only.

## Issue 23 / operator-run synthetic S3 path (2026-11-10)

S3 remains INCOMPLETE and production project-screen remains STOP-GATED. This
increment adds an executable verification path, not a production enable switch.
No change to execution_backend, worker/service, existing screens or registries.
The prior four AF_UNIX-denied screen attempts were not repeated. No host screen
lifecycle was run by Codex. A successful --check is NOT a screen/lifecycle PASS.

### Concrete API and coverage

Production Supervisor::open/inspect/claim/poll_verify retain the S2 contracts
above. start_in_project_screen still refuses runtime_disabled;
finish_and_release still refuses launch_and_exit_proof_missing. Version 1
journal process/window/thread fields must still be null. No production lease
expiry, process adoption, positive release, recovery replay or migration exists.
project_screen::inspect is still diagnostic only: it is neither an absence
proof nor a window ownership grant. No production backend consumes the new test.

The private child module src/supervisor_smoke.rs implements SyntheticLifecycle
start / observe / verify_tree / finish_release, invoked through the Rust
supervisor CLI by the root helper. Its CLI accepts only synthetic-smoke --run;
it accepts no configurable root, registry, server ID or command. The dummy
entry point accepts only the freshly generated token-bound directory under this
checkout's existing canonical .tmp and a fixed anchor/job role. It never runs
Codex, a shell, a network command, arbitrary exec, or a real Queue issue.

The test constructs a NEW synthetic project and a new private foreground screen
Child with an s23-random name and SCREENDIR. No g2c-*/qN session is used, created,
listed or controlled. A fixed single-process dummy in window 0 is the persistent
anchor. The supervisor registers that own session in project/.ai/session.json
before job creation. It calls the REAL Supervisor inspect and claim using
project/.ai/state, creates only window 1 in this already registered synthetic
server, and verifies the dummy handshake. A separate Supervisor descriptor must
refuse both manual and queue second claims while that dummy remains live.
There is no read-only/writer bypass parameter in this kernel.

Binding evidence includes canonical root and its device/inode in Claim,
owner kind, synthetic issue ID/number/repository/full SHA, claim nonce,
server PID/starttime/UID/boot/PID namespace, executable inode, full session ID,
private socket path/device/inode and directory identity, SO_PEERCRED,
window number plus fresh nonce/token and title, child PID/starttime/parent,
process group, TTY, cwd and executable inode. A fixed dummy has no descendants;
the server tree must contain exactly anchor or anchor+job. The binding and
completed evidence are private create_new/fsync JSON under synthetic .ai.
No thread UUID is invented for the dummy. Real Queue issue+SHA+thread UUID
binding before the first turn remains a production S3 blocker.

Before every screen command the session record is reread and compared, server
and socket generations/peer are revalidated. Window inventory uses an explicit
untruncated numeric/title format. Finish checks the job binding and exact live
process/window, targets only its window with a title command, then cooperatively
exits the fixed dummy. It requires that PID generation to be gone, the job window
to be absent, the anchor/window/server to remain, and no extra child. Completion
is persisted before fixture-only release; claim and exit/tree/window evidence
are checked again under the REAL journal flock before removing that fixture
claim. This private release cannot accept an arbitrary journal or production
Claim. Production finish_and_release is still deny-only.

Success cleanup asks only the verified anchor to exit and reaps the direct
screen Child; both processes and socket must be gone. No kill/quit/pkill,
wipe, directory removal, history deletion or production state action exists.
Errors retain all scratch, pending/bound records and unresolved claims. There
is no fallback cleanup. The own dummy TTL of 120 seconds bounds orphan life;
TTL is NOT permission to release a claim. If identity cannot be established,
manually inspect the retained evidence and procfs before taking any action;
never kill by name or delete a claim to report success. Startup evidence is
saved in server-pending.json, with own screen stderr in the retained scratch.
This cooperating same-UID synthetic test is not a security sandbox against the
same UID rewriting files or starting an unrelated process outside the protocol.

### Safe operator commands and interpretation

From the reviewed checkout root, run `./gpt2codex-supervisor-smoke.sh --check`.
Default invocation is also --check. It checks tools, existing .tmp/.log, source
and procfs prerequisites and prints the plan. It creates no log, scratch,
registry, socket or screen, and performs no build or state initialization.
It does not assert AF_UNIX capability or cached dependency availability.

Only after code review, the USER on the real host outside the Codex sandbox may
run `./gpt2codex-supervisor-smoke.sh --run`. This performs a locked offline Cargo
build with target/TMPDIR under existing .tmp, then calls the implemented Rust
supervisor synthetic API. No install or network fetch is allowed; missing
cached dependencies cause failure. This is not the older four-case diagnostic
helper and does not merely assert success from naked screen -X commands.

The wrapper prints LOG pointing to .log/supervisor23-host-*.log and SCRATCH
pointing to .tmp/s23-*. Require zero exit and ALL PASS stages:
host_AF_UNIX, start_verify, project_exclusion, finish_release, own_cleanup,
confinement, and final synthetic-only success. In particular, project_exclusion
means both independent manual and queue claim calls actually returned
project_busy while the owned child was live. Cross-check job-binding.json,
completed.json and the now empty project-owners-v1.json in that same scratch;
server/window generation and claim/root must agree. This proves only the
synthetic project, not manual frontend integration or a real Codex run.
The confinement check follows from env_clear/private SCREENDIR/exact own PID
and no foreign enumeration/control, not a scan of the user's live registry.
Review those boundaries too; the test never connects to foreign sockets.

AF_UNIX EPERM/EACCES/unsupported is reported NOT RUN / BLOCKED before any screen
start, with nonzero exit; any other unexpected failure is FAIL, never a partial
E2E PASS. A host result may be labeled synthetic lifecycle PASS only after an
operator actually obtains all identity/exit/window checks. As of this job it
is NOT RUN. S3 cannot be declared done even after that synthetic PASS alone.

### Exact uncovered managed entries and S4 gate

- screen::start -> screen::start_with_attention: launches an entire project
  screen with Codex as its main program, without a persistent non-Codex anchor
  or the common reservation. It is not the new window lifecycle.
- session::start / start_with_options -> start_with_attention, both Screen and
  Tmux: legacy per-mux slots are not one project-root reservation.
- attention_marker::run: verifies its attention identity and execs Codex, but
  does not hold a common project lifetime claim through exit/recovery.
- CodexClient::connect -> connect_command: starts the read-only CLI/MCP
  app-server; connect has no project-root parameter and no project claim.
  G2C_PROJECT_SUPERVISOR only refuses these Rust entries before spawning;
  with it absent their legacy behavior is unchanged.
- gpt2codex-github-queue.py run_approved -> run_one -> execute_claimed -> run_exec (legacy exec Popen), and
  gpt2codex_queue_screen.py launch -> runner (exec Popen -> resume Popen): no
  Rust journal. The legacy resume is not an exclusive managed resume API.
- Qt6 attachArguments -> session attach and QueuePage::openSelected
  -> legacy queue attach: attachment only. No implemented Qt6 start/resume
  acquisition hook exists. Commands typed in Bridge/TUI and external Codex
  CLI/GUI starts cannot be safely enclosed by attach/status observations.

These entries cannot be made safe by guessing process names, adopting a manual
window, or wrapping just the short-lived start call in flock. Required next S3
work is a production registered-anchor/nonce window handshake, pre-execution
process+UUID binding, descendant containment through crash/reparenting,
verified positive finish/release/recovery, and one deployed lock namespace
used by every supported managed start/resume. Unknown legacy/manual activity
must conservatively block Queue and must never be killed/adopted. Only after
those gates and host proof may S4 connect the Python planner to this common Rust
supervisor, retain FIFO/capacity, persist terminal evidence/outbox before finish,
and add Qt6 read-only project/job status. No additional worker or Queue screen.
Issue 17 list-active/libnotify remains deferred.

## Issue 24 / S3c: PARTIAL, common deny-only admission (2026-11-10)

The operator completed #23's host synthetic smoke. Read-only review of
`.log/supervisor23-host-2CSgjhGW.log` confirms all six PASS stages. The retained
`.tmp/s23-ec4a8ec414867277` binding and completion agree on claim, root, server,
window and dummy identity; the synthetic journal is empty after release.
Neither evidence was changed or removed. The smoke was NOT repeated. These
observations supersede #23's historical NOT RUN, but prove only its fixed
single-process dummy lifecycle, not Codex, descendants, Qt6 or worker integration.

### Implemented in this increment

`Supervisor::admission(project, root)` and the local command
`gpt2codex supervisor admission --id ID --config ABS_CONFIG --directory ABS_PRIVATE_DIR`
read the existing journal through its descriptor-pinned namespace and flock.
No journal or state directory is created. The version 1 JSON response includes
canonical root/project, namespace device/inode, runtime_enabled=false,
launch_permitted=false, waiting_reason, detail, reservation, owner and
recovery_required. No claim nonce, issue payload or process command line is
needed in this view. Exact cases:

| Observation | waiting_reason | detail / reservation |
| --- | --- | --- |
| Exact pending manual or Queue claim | project_busy | pending_reservation / pending |
| Conflicting project ID or overlapping canonical root | project_busy | overlapping_reservation / conflict |
| Empty journal | session_unverified | runtime_disabled / absent |
| Invalid journal, changed boot/root identity or scope | session_unverified | reservation_unverified / unknown |

Pending/conflicting/unknown states require recovery review. Absent does NOT
mean no Codex, and neither pending owner nor recovery_required proves a live
TUI. Errors opening the namespace or resolving the project return nonzero;
consumers must interpret these as session_unverified. No auto-release exists.

The opt-in `G2C_PROJECT_SUPERVISOR_DIRECTORY` hook reads the explicit SAME
namespace for screen::start_with_attention, session::start_with_attention
(Screen and Tmux), and attention_marker::run before launcher/registry writes.
A Queue reservation now gives these entries project_busy; absent or invalid
state still refuses. The directory variable alone also trips the existing
rootless CodexClient::connect STOP gate, before app-server spawn. The original
G2C_PROJECT_SUPERVISOR flag remains a deny-only flag; neither variable enables
anything. No installed environment or production namespace was configured.
Without both variables the legacy behavior remains; it has no lifetime lock.

Python `RustSupervisorProbe` invokes only that read-only admission command,
using explicit absolute binary/config/namespace paths, a minimal environment,
closed stdin, discarded stderr, a 3-second deadline and 8-KiB output bound.
It validates exact schema, project/root, namespace identity before/after,
false runtime/grant booleans and consistent busy reasons. It pins the expected
namespace identity for subsequent calls; replacement, corruption, incompatible
schema, process failure or fabricated grants deny with session_unverified.
This is a local status adapter, not a new process-launch backend.

`Ledger.reserve_managed(key, probe)` is an explicit deny-only integration hook:
it records waiting plus the Rust reason, never consumes a claim, binds a process,
launches, calls the fixture reserve API, or falls back. It rejects already
running/finishing/done/unknown records. The existing reserve/bind/close APIs
remain OFFLINE fixtures, including their asserted session/window observations.
The live worker does not import this candidate. Therefore the two ledgers have
NOT become one deployed lock and no real project exclusivity is claimed.

### Common runtime contract to implement next (not a working runtime)

Rust alone must own atomic reservation transitions for canonical root plus
root device/inode, one shared namespace and immutable owner/nonce/host/boot/PID
namespace. The global flock protects short metadata transitions; durable claims
hold exclusion through active AND idle TUI, not merely the start call. Manual
new/resume and Queue share this acquisition regardless of read-only/write mode.
Queue adds repository, issue ID/number, approved SHA and canonical UUID. Planner
FIFO/capacity and publication outbox remain separate from launch authority.

The launch capability must additionally bind a verified existing registered
project server PID/starttime, socket directory/device/inode/peer, executable,
persistent non-Codex anchor, exact new window generation/TTY, direct child and
all descendants before executing an approved turn. An existing ordinary Codex
without a claim, including idle TUI, must deny; uncertain discovery also denies.
No snapshot-to-launch race may be treated as solved by admission. Explicit
session disabled must deny; the current Rust registry has no enabled/disabled
session field, so the planner's enabled fixture is not such production policy.
Never create a replacement project screen merely to avoid a busy/disabled one.

Completion is separate from TUI life: persist first-turn UUID/output/result and
idempotent factual Czech handoff/outbox, then close only the proved own process
and window. Release requires durable proof of all owned descendants gone and
window closed while the project anchor survives, revalidated under the journal
lock. A dead parent, missing PID or a current empty screen tree is insufficient:
children can reparent or escape between snapshots. PID reuse, changed socket,
window/owner mismatch and incomplete pre-bind recovery retain the reservation
as interrupted_unknown/session_unverified, never adopt or kill foreign tasks.
Completed finishing recovery may publish/verify closure, never run another turn.

### Exact remaining entries and sequential S4 work

S4.1: implement the production anchor/window/pre-execution handshake and durable
owned-descendant containment/recovery proof. The smoke's private dummy control
must not be generalized by accepting arbitrary existing session IDs or commands.
project_screen::inspect remains diagnostic; it walks leader children and cannot
prove absence of detached/reparented or nonleader-thread children. No positive
launch or release is possible until these problems have fixture coverage.

S4.2: deploy one explicitly configured namespace and route managed manual
start AND historical UUID resume through the same acquisition and lifetime
guard. Current CLI has no managed UUID-resume command. Raw `codex resume` typed
in Bridge/project TUI or an external terminal is outside the protocol. Qt6
attachArguments/session attach and QueuePage::openSelected only attach existing
sessions; they are not start/resume hooks or process owners. Qt6 metadata reads
use rootless app-server and only gain STOP refusal under the flag. No GUI
start/resume UI or GUI lifecycle proof was added. Existing manual Codex must
be detected conservatively, not adopted or stopped to manufacture exclusivity.

S4.3: replace only the new backend's fixture reservation/closure adapter with
Rust atomic capabilities; revalidate auth binding, repository/host/allowlist,
issue ID and SHA before acquisition. Wire actual run_approved/run_one/
execute_claimed into one nonblocking dispatcher with FIFO and cross-project
capacity. Legacy run_exec Popen and queue_screen.launch/runner exec Popen and
automatic resume Popen STILL DO NOT use the Rust namespace OR its opt-in gate.
No protection for these paths is claimed even with the new directory variable.
The live project-screen backend remains rejected before claims/tokens/remote
calls, so these legacy paths cannot serve as a fallback for it.

S4.4: persist UUID/output/outbox and idempotent publication before owned finish;
add no-replay restart reconciliation, then expose the common local read-only
project/job status to #17/Qt6. Keep #19 punctuation/sanitizer/protocol/auth/SHA,
#20 FIFO/isolation and #22 DONE-after-TUI-close regressions. Do not translate
legacy approvals, migrate old screen slots or delete unresolved reservations.

S4.5: pass real process/descendant, screen identity and all frontend integration
fixtures. Only then request a separate controlled operator Codex/Qt6 E2E.
No new backend activation, user-service restart or E2E is authorized by this
increment or the existing synthetic PASS. No further host synthetic rerun is
needed merely to reconfirm #23. Production rollout remains separately gated.

### Actual verification

- PASS new cross-language admission suite 10/10: real Rust CLI in isolated
  namespaces; pending manual -> Queue in both modes, Queue -> seven Rust entry
  variants, directory-only app-server refusal, two roots, restart, corruption,
  namespace replacement/symlink, schema/grant forgery and transport errors.
  These are pending claims/deny hooks, NOT active/idle Codex integration tests.
- PASS Rust parallel and serial: 87 library + 12 CLI each. Two new admission
  scenarios extend #23's 85 library tests. Embedded fork child is not an extra
  scenario. Existing PID/window/owner guards, pending crash, real fixture claim
  races and synthetic socket/session negative tests pass, not live screen E2E.
- PASS dispatcher 35, legacy screen 36, handoff 44, base Queue 15; counts overlap
  inherited scenarios. DONE/closed-TUI, FIFO, SHA/auth/protocol/sanitizer and
  idempotent handoff regression behavior remains. No real model/network calls.
- Build PASS offline/locked. Logs: .log/queue24-build.log,
  queue24-admission.log, queue24-rust-verified-{parallel,serial}.log,
  queue24-{dispatch,screen,handoff,base}.log and queue24-host-evidence-review.log.
- Earlier Rust harness runs FAILED (81/87 library) because this job omitted
  TMPDIR, then supplied its symlink path. Attention fixture requires a canonical
  no-symlink path. Retained queue24-rust-parallel.log and
  queue24-rust-final-{parallel,serial}.log record the failures. Correct runs use
  TMPDIR="$(realpath .tmp)"; no source safety check was weakened or bypassed.
- NOT RUN: real manual active/idle TUI exclusion, positive production
  start/inspect/verify/finish/release, escaped-descendant recovery, actual Queue
  worker integration, Qt6 runtime, actual Codex E2E. These are S4 blockers.

Definition of Done is NOT met: S3c remains PARTIAL and STOP-GATED. Rollback needs
no live action; keep the current backend/environment. Any source rollback must
be selective because the checkout already contained extensive user work.


## 2026-11-10 — Issue 25: dodávka FAIL, nezapínat project-screen

Tento aktuální stav nahrazuje výše uvedené návrhy dalších etap. Nevznikla nová
architektura, supervisor, rezervační vrstva ani PoC. Funkční napojení
ChatGPT → GitHub → persistentní projektový screen → Codex → GitHub není dodáno.
Nejde pouze o neprovedený produkční test: implementace pro něj není připravená.
Zákaz live zásahů brání E2E, ale není důvodem chybějícího zdrojového propojení.

Přesně nutné opravy přímo ve stávající integraci:
1. `src/project_supervisor.rs::start_in_project_screen/finish_and_release`
   stále vždy odmítají; CLI nemá produkční start/finish/resume. Doplnit ověřené
   vlastní okno v existujícím registrovaném serveru, lifecycle skutečného Codexu
   a potomků, durable UUID/completion a ověřený exit před release. Syntetický
   dummy ze #23 není takovým launcherem; jeho host PASS se neopakuje.
2. `gpt2codex-github-queue.py::run_approved/execute_claimed/watch` nevolají
   `gpt2codex_queue_dispatch.Ledger` ani produkční Rust lifecycle. Doplnit právě
   toto propojení, ne nový worker. Zachovat schválení a durable claim; FIFO
   jednoho projektu a neblokující souběh různých projektů musí platit v tomto
   skutečném watch. `reserve_managed` nyní pouze ukládá čekání.
3. Stávající ruční start/resume a již idle Codex nejsou společně chráněné;
   opt-in Rust hooky jen odmítají, Python legacy spawn se jich neúčastní.
   Doplnit společné vynucení a konzervativní detekci neřízeného procesu.
   Neznámý stav musí blokovat bez adopce, psaní do TUI či ukončování cizích PID.

Tyto tři body nejsou návrh další generace. Jsou to konkrétní neimplementované
části požadované dodávky. Neaktivovat backend, dokud nejsou opravené a cílené
integrační testy skutečného watch neprojdou. Fixture testy Ledgeru ani úspěšný
starý per-issue screen tuto podmínku nesplňují.

### Jediný host-side operátorský postup

Nyní pouze DRY RUN po code review; žádný příkaz níže nezapíná backend. Proměnné
BRIDGE, REGISTRY, NAMESPACE a SOCKET_DIRECTORY nastavuje operátor na schválené
absolutní cesty existujícího Rust executable, TOML registry, supervisor namespace
a screen socket directory. Nevytvářet ani adoptovat je podle odhadu. V tomto
jobu nebyl čten live config, stav služby ani screen registry/socket.

1. V rootu projektu `python3 -B ./gpt2codex-github-queue.py worker-ready`.
   Kontroluje aktuální JSON config a queue idle bez token read/GitHub/modelu;
   může založit běžné Store/lock soubory. Jde o host příkaz, zde NOT RUN.
   IDLE=NO/UNKNOWN znamená STOP, nikoli oprávnění ukončit vlastníka.
2. `systemctl --user show gpt2codex-github-queue.service --property=ActiveState,SubState,MainPID`.
   Pouze metadata workeru; žádný restart/stop ani dump environment. Zde NOT RUN.
3. Pro každý povolený projekt, například gpt2codex a kencoder:
   `"$BRIDGE" supervisor admission --config "$REGISTRY" --directory "$NAMESPACE" --id gpt2codex`.
   Dále `"$BRIDGE" supervisor inspect-screen --config "$REGISTRY" --socket-directory "$SOCKET_DIRECTORY" --id gpt2codex`.
   Nahrazení id pro druhý projekt je samostatná kontrola. Pouze operátor na
   hostu, zde NOT RUN. Nejde o screen -X, attach ani převzetí TUI. Současný
   Rust vrací launch_permitted=false i při úspěšné inspekci: celkový DRY RUN
   pro aktivaci je FAIL. Žádný údaj o nepřítomnosti procesu z toho neodvozovat.
4. STOP. Bezpečný aktivační krok zatím neexistuje a není zde vydáván za hotový.
   Po opravě tří bodů výše musí jedna kontrolovaná změna execution_backend
   u JEDINÉHO stávajícího workeru následovat až po výslovném souhlasu operátora
   a ověřeném idle. Nepouštět druhý watch ani jednorázový launcher vedle něj.

Přesný produkční akceptační test po opravě a schválení aktivace (vše NOT RUN):
- Místně schválit dvě různé read-only Issues se SHA: jednu pro gpt2codex,
  druhou pro kencoder. Zadání každé: přečíst krátký soubor v jejím projektu
  a vrátit věcné české shrnutí bez změn a bez vedlejších modelových volání.
  Schválení provést běžným poll/show/approve se známým SHA; neschvalovat
  hromadně, neměnit backend starých schválení. Třetí schválená read-only
  Issue patří opět gpt2codex, ve FIFO po první. Nepouštět nové run-screen.
- Existuje-li již ruční idle Codex v gpt2codex, pouze pozorovat čekání obou
  jeho Issues. Nic do jeho TUI neposílat. kencoder smí běžet jen pokud je
  samostatně ověřen jako volný. Pokud volné nejsou oba projekty, souběh zůstane
  NOT RUN do operátorem zvoleného idle okna; pracovní TUI se neobětuje testu.
- V idle okně ověřit současné živé vlastní procesy obou projektů v jejich
  původních plných screen ID, žádný nový qN server; druhý job téhož projektu
  musí čekat a následně běžet v pořadí. Krátké joby bez prokázaného překryvu
  nejsou důkaz souběhu. Neprodlužovat je dalším modelovým turnem jen pro test.
- Po každém prvním turnu ověřit DONE, zachované UUID a věcný automatický
  handoff, zánik právě vlastního procesu/okna a přežití původního screen
  serveru i ostatních oken. Žádný automatický idle resume. Až operátor dovolí
  restart idle workeru, ověřit nulový replay a stejná uložená UUID/DONE/outbox.
  Neověřený screen musí zůstat čekající bez spawnu. Explicitní resume stejného
  UUID musí znovu respektovat projektový lock.

Rollback: tento job nic live nepřepnul, není co vracet. Pro případ budoucího
schváleného rollout zachovat předchozí config; při problému neclaimovat další
úlohy, neukončovat běžící Codex a nereplayovat unresolved claim. Po prokázaném
idle operátor obnoví předchozí execution_backend screen nebo exec a použije
pouze tutéž službu. Nevracet celý rozpracovaný strom, nemazat UUID ani historii.

### Opravený automatický handoff

Retained `.log/github-queue/handoff-24-6bc819399dff43128403bbe4e0263780.final.json`
je platný per-job report; selhalo výhradně files[5], název
`gpt2codex-supervisor-admission-test.py`: dlouhý běžný název odpovídal heuristice
32 znaků neprůhledného textu. Chyba nebyla způsobena ztrátou reportu, dokončením
turnu ani historickým handoffem. `safe_text` nyní pouze ve files povoluje kořenové
Python/shell helpery s prefixem gpt2codex a 2–6 krátkými malými slovy. SENSITIVE,
prose, UUID identita, absolutní cesty, traversal a limity se nemění. Skutečný
report #24 se po opravě lokálně promítne jako structured_report; nic nebylo
publikováno ani zpětně přepsáno. Krátký report #25 uloží CLI z finálního JSON,
model samostatný report soubor nevytváří.
