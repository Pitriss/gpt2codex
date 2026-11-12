//! Reservation kernel for S2. This is NOT a launch authorization service yet.
//! No process adoption, signals, screen commands, expiry, or automatic recovery.
use crate::registry::{validate_id, ProjectRegistry};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

// Operator-only synthetic lifecycle. No production launcher or recovery grant.
#[path = "supervisor_smoke.rs"]
mod smoke;

type Result<T> = std::result::Result<T, &'static str>;
const MAX: u64 = 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub start_ticks: u64,
    pub uid: u32,
    pub boot_id: String,
    pub pid_namespace: u64,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProcessState {
    Live,
    Gone,
    Unverified,
}

fn host() -> Result<String> {
    let mut name = [0u8; 256];
    if unsafe { libc::gethostname(name.as_mut_ptr().cast(), name.len()) } != 0 {
        return Err("session_unverified");
    }
    let end = name
        .iter()
        .position(|b| *b == 0)
        .ok_or("session_unverified")?;
    String::from_utf8(name[..end].to_vec()).map_err(|_| "session_unverified")
}

impl ProcessIdentity {
    /// Metadata only; no command line, environment, conversation or auth reads.
    pub fn read(pid: u32) -> Result<Self> {
        if pid == 0 {
            return Err("session_unverified");
        }
        let proc = PathBuf::from(format!("/proc/{pid}"));
        let stat = fs::read_to_string(proc.join("stat")).map_err(|_| "session_unverified")?;
        let fields: Vec<_> = stat
            .rsplit_once(')')
            .ok_or("session_unverified")?
            .1
            .split_whitespace()
            .collect();
        let ticks = fields
            .get(19)
            .ok_or("session_unverified")?
            .parse()
            .map_err(|_| "session_unverified")?;
        Ok(Self {
            pid,
            start_ticks: ticks,
            uid: fs::metadata(&proc).map_err(|_| "session_unverified")?.uid(),
            boot_id: fs::read_to_string("/proc/sys/kernel/random/boot_id")
                .map_err(|_| "session_unverified")?
                .trim()
                .into(),
            pid_namespace: fs::metadata(proc.join("ns/pid"))
                .map_err(|_| "session_unverified")?
                .ino(),
        })
    }

    /// A reused PID is deliberately NOT sufficient exit proof for release.
    /// Zombies remain live until reaped. Reboot/namespace changes require review.
    pub fn verify(&self) -> ProcessState {
        let scope = match Self::read(std::process::id()) {
            Ok(v) => v,
            Err(_) => return ProcessState::Unverified,
        };
        if self.uid != unsafe { libc::geteuid() }
            || self.boot_id != scope.boot_id
            || self.pid_namespace != scope.pid_namespace
        {
            return ProcessState::Unverified;
        }
        match Self::read(self.pid) {
            Ok(value) if value == *self => ProcessState::Live,
            Ok(_) => ProcessState::Unverified,
            Err(_) => match fs::symlink_metadata(format!("/proc/{}", self.pid)) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => ProcessState::Gone,
                _ => ProcessState::Unverified,
            },
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Owner {
    Queue,
    Manual,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct IssueBinding {
    pub repository: String,
    pub issue_id: u64,
    pub number: u64,
    pub sha: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WindowIdentity {
    pub session_id: String,
    pub server: ProcessIdentity,
    pub socket_device: u64,
    pub socket_inode: u64,
    pub number: u32,
    pub generation: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Claim {
    pub nonce: String,
    pub project_id: String,
    pub root: PathBuf,
    pub root_device: u64,
    pub root_inode: u64,
    pub hostname: String,
    pub boot_id: String,
    pub pid_namespace: u64,
    pub owner: Owner,
    pub issue: Option<IssueBinding>,
    pub thread_uuid: Option<String>,
    pub process: Option<ProcessIdentity>,
    pub window: Option<WindowIdentity>,
}

/// Read-only admission snapshot, never a launch grant or a substitute for an
/// atomic claim. Pending reservations survive their creator, including idle or
/// crashed callers. No absence observation can enable the unfinished runtime.
#[derive(Debug, Serialize)]
pub struct Admission {
    pub version: u32,
    pub project_id: String,
    pub root: PathBuf,
    pub namespace_device: u64,
    pub namespace_inode: u64,
    pub runtime_enabled: bool,
    pub launch_permitted: bool,
    pub waiting_reason: &'static str,
    pub detail: &'static str,
    pub reservation: &'static str,
    pub owner: Option<Owner>,
    pub recovery_required: bool,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    version: u32,
    claims: Vec<Claim>,
}

/// Existing private directory only. Pin its descriptor and flock the directory
/// itself (never unlink/recreate a lock file). All cooperating roots share it.
/// Choosing another directory is NOT the same exclusion namespace.
pub struct Supervisor {
    directory: File,
    mutex: std::sync::Mutex<()>,
}
impl Supervisor {
    pub fn open(directory: &Path) -> Result<Self> {
        if !directory.is_absolute() {
            return Err("session_unverified");
        }
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(directory)
            .map_err(|_| "session_unverified")?;
        let meta = file.metadata().map_err(|_| "session_unverified")?;
        if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
            return Err("session_unverified");
        }
        Ok(Self {
            directory: file,
            mutex: std::sync::Mutex::new(()),
        })
    }

    fn path(&self, name: &str) -> PathBuf {
        PathBuf::from(format!("/proc/self/fd/{}", self.directory.as_raw_fd())).join(name)
    }

    fn locked<T>(&self, write: bool, action: impl FnOnce() -> Result<T>) -> Result<T> {
        let _guard = self.mutex.lock().map_err(|_| "session_unverified")?;
        let operation = if write { libc::LOCK_EX } else { libc::LOCK_SH };
        if unsafe { libc::flock(self.directory.as_raw_fd(), operation) } != 0 {
            return Err("session_unverified");
        }
        let result = action();
        unsafe {
            libc::flock(self.directory.as_raw_fd(), libc::LOCK_UN);
        }
        result
    }

    fn load(&self) -> Result<Journal> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
            .open(self.path("project-owners-v1.json"));
        let mut file = match file {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Journal {
                    version: 1,
                    claims: vec![],
                })
            }
            Err(_) => return Err("session_unverified"),
        };
        let meta = file.metadata().map_err(|_| "session_unverified")?;
        if !meta.is_file()
            || meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0
            || meta.nlink() != 1
            || meta.len() > MAX
        {
            return Err("session_unverified");
        }
        let mut bytes = Vec::new();
        (&mut file)
            .take(MAX + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "session_unverified")?;
        if bytes.len() as u64 > MAX {
            return Err("session_unverified");
        }
        let journal: Journal = serde_json::from_slice(&bytes).map_err(|_| "session_unverified")?;
        if journal.version != 1 {
            return Err("session_unverified");
        }
        for (i, c) in journal.claims.iter().enumerate() {
            check_scope(c)?;
            validate_id(&c.project_id).map_err(|_| "session_unverified")?;
            if !c.root.is_absolute()
                || c.nonce.len() != 64
                || !c.nonce.bytes().all(|b| b.is_ascii_hexdigit())
                || c.hostname.is_empty()
            {
                return Err("session_unverified");
            }
            // Pending reservations are the ONLY schema state this version can
            // create. Reject fabricated bindings until the launch handshake exists.
            if c.process.is_some() || c.window.is_some() || c.thread_uuid.is_some() {
                return Err("session_unverified");
            }
            if journal.claims[..i].iter().any(|other| {
                overlaps(&other.root, &c.root)
                    || other.project_id == c.project_id
                    || other.nonce == c.nonce
            }) {
                return Err("session_unverified");
            }
        }
        Ok(journal)
    }

    fn persist(&self, journal: &Journal) -> Result<()> {
        let bytes = serde_json::to_vec(journal).map_err(|_| "session_unverified")?;
        if bytes.len() as u64 > MAX {
            return Err("session_unverified");
        }
        let temp = self.path(&format!(".owners-{}.pending", nonce()?));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temp)
            .map_err(|_| "session_unverified")?;
        // On ambiguous failure preserve evidence. A pending file never authorizes
        // launch or replaces the journal during recovery.
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|_| "session_unverified")?;
        fs::rename(temp, self.path("project-owners-v1.json")).map_err(|_| "session_unverified")?;
        self.directory.sync_all().map_err(|_| "session_unverified")
    }

    /// INSPECT never creates files or changes stale claims. Root aliases conflict;
    /// project ID rebinding, host change and boot changes fail closed.
    pub fn inspect(&self, project: &str, root: &Path) -> Result<Option<Claim>> {
        let root = canonical(root)?;
        validate_id(project).map_err(|_| "session_unverified")?;
        self.locked(false, || {
            let journal = self.load()?;
            let found = journal
                .claims
                .iter()
                .find(|c| c.project_id == project || overlaps(&root, &c.root));
            match found {
                Some(c) if c.project_id == project && c.root == root => {
                    check_scope(c)?;
                    Ok(Some(c.clone()))
                }
                Some(_) => Err("project_busy"),
                None => Ok(None),
            }
        })
    }

    pub fn admission(&self, project: &str, root: &Path) -> Result<Admission> {
        validate_id(project).map_err(|_| "session_unverified")?;
        let root = canonical(root)?;
        let namespace = self
            .directory
            .metadata()
            .map_err(|_| "session_unverified")?;
        let mut view = Admission {
            version: 1,
            project_id: project.into(),
            root: root.clone(),
            namespace_device: namespace.dev(),
            namespace_inode: namespace.ino(),
            runtime_enabled: false,
            launch_permitted: false,
            waiting_reason: "session_unverified",
            detail: "runtime_disabled",
            reservation: "absent",
            owner: None,
            recovery_required: false,
        };
        match self.inspect(project, &root) {
            Ok(Some(claim)) => {
                view.waiting_reason = "project_busy";
                view.detail = "pending_reservation";
                view.reservation = "pending";
                view.owner = Some(claim.owner);
                view.recovery_required = true;
            }
            Ok(None) => (),
            Err("project_busy") => {
                view.waiting_reason = "project_busy";
                view.detail = "overlapping_reservation";
                view.reservation = "conflict";
                view.recovery_required = true;
            }
            Err(_) => {
                view.detail = "reservation_unverified";
                view.reservation = "unknown";
                view.recovery_required = true;
            }
        }
        Ok(view)
    }

    /// Persist ONLY a pending reservation; this does not assert that the screen
    /// or workspace is empty and never authorizes a process launch. No expiry.
    /// Queue FIFO/approval remain #20 planner responsibilities.
    pub fn claim(
        &self,
        project: &str,
        root: &Path,
        owner: Owner,
        issue: Option<IssueBinding>,
    ) -> Result<Claim> {
        validate_id(project).map_err(|_| "session_unverified")?;
        let root = canonical(root)?;
        if owner == Owner::Queue && issue.is_none() {
            return Err("session_unverified");
        }
        if let Some(i) = &issue {
            if i.issue_id == 0
                || i.number == 0
                || i.repository.split('/').count() != 2
                || i.repository.split('/').any(|s| {
                    s.is_empty()
                        || !s
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
                })
                || i.sha.len() != 64
                || !i.sha.bytes().all(|b| b.is_ascii_hexdigit())
            {
                return Err("session_unverified");
            }
        }
        self.locked(true, || {
            let mut journal = self.load()?;
            if journal
                .claims
                .iter()
                .any(|c| c.project_id == project || overlaps(&root, &c.root))
            {
                return Err("project_busy");
            }
            let scope = ProcessIdentity::read(std::process::id())?;
            let meta = fs::metadata(&root).map_err(|_| "session_unverified")?;
            let claim = Claim {
                nonce: nonce()?,
                project_id: project.into(),
                root,
                hostname: host()?,
                root_device: meta.dev(),
                root_inode: meta.ino(),
                boot_id: scope.boot_id,
                pid_namespace: scope.pid_namespace,
                owner,
                issue,
                thread_uuid: None,
                process: None,
                window: None,
            };
            journal.claims.push(claim.clone());
            self.persist(&journal)?;
            Ok(claim)
        })
    }

    pub fn poll_verify(&self, claim: &Claim) -> Result<&'static str> {
        let current = self
            .inspect(&claim.project_id, &claim.root)?
            .ok_or("session_unverified")?;
        if current != *claim {
            return Err("session_unverified");
        }
        Ok("reserved_runtime_disabled")
    }

    pub fn start_in_project_screen(&self, claim: &Claim, _command: &[String]) -> Result<()> {
        self.poll_verify(claim)?;
        Err("session_unverified: runtime_disabled")
    }

    /// No close/release based on caller-provided window_closed/process_gone flags.
    /// Pre-bind crashes remain reserved until a real recovery proof is implemented.
    pub fn finish_and_release(&self, claim: &Claim) -> Result<()> {
        self.poll_verify(claim)?;
        Err("session_unverified: launch_and_exit_proof_missing")
    }
}

fn overlaps(a: &Path, b: &Path) -> bool {
    a.starts_with(b) || b.starts_with(a)
}
fn canonical(root: &Path) -> Result<PathBuf> {
    if !root.is_absolute() || !root.is_dir() {
        return Err("session_unverified");
    }
    root.canonicalize().map_err(|_| "session_unverified")
}
fn check_scope(claim: &Claim) -> Result<()> {
    let scope = ProcessIdentity::read(std::process::id())?;
    if claim.hostname != host()?
        || claim.boot_id != scope.boot_id
        || claim.pid_namespace != scope.pid_namespace
    {
        return Err("session_unverified");
    }
    let root = canonical(&claim.root)?;
    let meta = fs::metadata(&root).map_err(|_| "session_unverified")?;
    if root != claim.root || meta.dev() != claim.root_device || meta.ino() != claim.root_inode {
        return Err("session_unverified");
    }
    Ok(())
}
fn nonce() -> Result<String> {
    let mut bytes = [0u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| "session_unverified")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Explicit opt-in STOP gate. This is not an enable switch. Legacy defaults are
/// preserved, but no instrumented start may silently fall back to legacy launch.
pub(crate) fn manual_start_gate() -> crate::screen::Result<()> {
    if std::env::var_os("G2C_PROJECT_SUPERVISOR").is_some()
        || std::env::var_os("G2C_PROJECT_SUPERVISOR_DIRECTORY").is_some()
    {
        return Err(crate::screen::err(
            "session_unverified: project supervisor runtime_disabled; no legacy fallback",
        ));
    }
    Ok(())
}

/// Opt-in managed entry hook. Resolves the project from the registry, reads the
/// SAME namespace as the Queue adapter and denies before any launcher activity.
/// This is deliberately deny-only: a read-only check cannot lock an idle TUI.
pub(crate) fn manual_project_start_gate(
    config: &Path,
    target: crate::screen::Target<'_>,
) -> crate::screen::Result<()> {
    if let Some(directory) = std::env::var_os("G2C_PROJECT_SUPERVISOR_DIRECTORY") {
        let view = (|| {
            let registry = ProjectRegistry::load(config).map_err(|_| "session_unverified")?;
            let id = match target {
                crate::screen::Target::Bridge => "gpt2codex",
                crate::screen::Target::Project(id) => id,
            };
            let project = registry.project(id).map_err(|_| "session_unverified")?;
            Supervisor::open(Path::new(&directory))?.admission(id, &project.root)
        })();
        return Err(crate::screen::err(match view {
            Ok(view) => format!("{}: {}", view.waiting_reason, view.detail),
            Err(_) => "session_unverified: supervisor_namespace_or_project_unverified".into(),
        }));
    }
    manual_start_gate()
}

/// Local versioned CLI. Explicit state directory and reservation-only flag avoid
/// silently creating a second authority or touching a production namespace.
pub fn cli(args: impl Iterator<Item = String>) -> Result<()> {
    let mut args = args;
    let action = args.next().ok_or("supervisor_action_required")?;
    if action == "synthetic-smoke" || action == "synthetic-dummy" {
        return smoke::cli(&action, args.collect());
    }
    let mut config = None;
    let mut directory = None;
    let mut socket_directory = None;
    let mut project = None;
    let mut owner = None;
    let mut issue = None;
    let mut reservation_only = false;
    while let Some(flag) = args.next() {
        if flag == "--reservation-only" && !reservation_only {
            reservation_only = true;
            continue;
        }
        let value = args.next().ok_or("supervisor_option_value_required")?;
        match flag.as_str() {
            "--config" if config.is_none() => config = Some(PathBuf::from(value)),
            "--directory" if directory.is_none() => directory = Some(PathBuf::from(value)),
            "--socket-directory" if socket_directory.is_none() => {
                socket_directory = Some(PathBuf::from(value))
            }
            "--id" if project.is_none() => project = Some(value),
            "--owner" if owner.is_none() => {
                owner = Some(match value.as_str() {
                    "manual" => Owner::Manual,
                    "queue" => Owner::Queue,
                    _ => return Err("supervisor_owner_invalid"),
                })
            }
            "--issue-json" if issue.is_none() => {
                issue = Some(
                    serde_json::from_str::<IssueBinding>(&value)
                        .map_err(|_| "supervisor_issue_invalid")?,
                )
            }
            _ => return Err("supervisor_option_invalid"),
        }
    }
    if !matches!(
        action.as_str(),
        "inspect" | "admission" | "inspect-screen" | "claim"
    ) || (action != "claim" && (owner.is_some() || issue.is_some() || reservation_only))
        || (action == "inspect-screen" && directory.is_some())
        || (action != "inspect-screen" && socket_directory.is_some())
    {
        return Err("supervisor_operation_invalid");
    }
    let config = config.ok_or("supervisor_config_required")?;
    if !config.is_absolute() {
        return Err("session_unverified");
    }
    let registry = ProjectRegistry::load(&config).map_err(|_| "session_unverified")?;
    let project = project.ok_or("supervisor_project_required")?;
    if action == "inspect-screen" {
        let view = crate::project_screen::inspect(
            &config,
            &project,
            &socket_directory.ok_or("supervisor_socket_directory_required")?,
        )?;
        println!(
            "{}",
            serde_json::to_string(&view).map_err(|_| "session_unverified")?
        );
        return Ok(());
    }
    let root = &registry
        .project(&project)
        .map_err(|_| "session_unverified")?
        .root;
    let supervisor = Supervisor::open(&directory.ok_or("supervisor_directory_required")?)?;
    if action == "admission" {
        println!(
            "{}",
            serde_json::to_string(&supervisor.admission(&project, root)?)
                .map_err(|_| "session_unverified")?
        );
        return Ok(());
    }
    let claim = if action == "inspect" {
        supervisor.inspect(&project, root)?
    } else {
        if !reservation_only {
            return Err("session_unverified: runtime_disabled");
        }
        Some(supervisor.claim(
            &project,
            root,
            owner.ok_or("supervisor_owner_required")?,
            issue,
        )?)
    };
    println!(
        "{}",
        serde_json::json!({"version":1,"runtime_enabled":false,"claim":claim})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Barrier};

    struct Fixture {
        path: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(".tmp")
                .canonicalize()
                .unwrap();
            let path = base.join(format!("supervisor21-{}", nonce().unwrap()));
            fs::create_dir(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            for child in ["one", "two"] {
                fs::create_dir(path.join(child)).unwrap();
            }
            Self { path }
        }
        fn store(&self) -> Supervisor {
            Supervisor::open(&self.path).unwrap()
        }
        fn root(&self, name: &str) -> PathBuf {
            self.path.join(name)
        }
        fn edit(&self, change: impl FnOnce(&mut Journal)) {
            let s = self.store();
            s.locked(true, || {
                let mut j = s.load()?;
                change(&mut j);
                s.persist(&j)
            })
            .unwrap();
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.path).unwrap();
        }
    }
    fn issue() -> Option<IssueBinding> {
        Some(IssueBinding {
            repository: "fixture/project".into(),
            issue_id: 123,
            number: 21,
            sha: "a".repeat(64),
        })
    }
    fn claim(f: &Fixture, owner: Owner) -> Claim {
        f.store()
            .claim("one", &f.root("one"), owner, issue())
            .unwrap()
    }

    #[test]
    fn inspect_is_read_only_and_empty() {
        let f = Fixture::new();
        assert_eq!(f.store().inspect("one", &f.root("one")).unwrap(), None);
        assert!(!f.path.join("project-owners-v1.json").exists());
        assert_eq!(fs::read_dir(&f.path).unwrap().count(), 2);
    }

    #[test]
    fn admission_never_grants_and_survives_owner_exit_or_reopen() {
        for owner in [Owner::Manual, Owner::Queue] {
            let f = Fixture::new();
            let empty = f.store().admission("one", &f.root("one")).unwrap();
            assert_eq!(empty.waiting_reason, "session_unverified");
            assert_eq!(empty.reservation, "absent");
            assert!(!empty.launch_permitted);
            assert!(!empty.runtime_enabled);
            assert!(!f.path.join("project-owners-v1.json").exists());
            claim(&f, owner.clone());
            let before = fs::read(f.path.join("project-owners-v1.json")).unwrap();
            let busy = f.store().admission("one", &f.root("one")).unwrap();
            assert_eq!(busy.waiting_reason, "project_busy");
            assert_eq!(busy.owner, Some(owner));
            assert!(busy.recovery_required);
            assert!(!busy.launch_permitted);
            assert!(!busy.runtime_enabled);
            // Different roots are independent, but runtime still cannot start.
            assert_eq!(
                f.store()
                    .admission("two", &f.root("two"))
                    .unwrap()
                    .reservation,
                "absent"
            );
            let alias = f.path.join("alias");
            symlink(f.root("one"), &alias).unwrap();
            let conflict = f.store().admission("two", &alias).unwrap();
            assert_eq!(conflict.waiting_reason, "project_busy");
            assert_eq!(conflict.reservation, "conflict");
            assert_eq!(
                fs::read(f.path.join("project-owners-v1.json")).unwrap(),
                before
            );
        }
    }

    #[test]
    fn admission_corrupt_or_changed_scope_is_unknown_and_preserved() {
        let f = Fixture::new();
        claim(&f, Owner::Manual);
        f.edit(|j| j.claims[0].boot_id = "other-boot".into());
        let path = f.path.join("project-owners-v1.json");
        for corrupt in [false, true] {
            if corrupt {
                fs::write(&path, b"incomplete").unwrap();
            }
            let before = fs::read(&path).unwrap();
            let view = f.store().admission("one", &f.root("one")).unwrap();
            assert_eq!(view.waiting_reason, "session_unverified");
            assert_eq!(view.reservation, "unknown");
            assert!(view.recovery_required);
            assert!(!view.launch_permitted);
            assert_eq!(fs::read(&path).unwrap(), before);
        }
    }

    #[test]
    fn concurrent_process_claim_child() {
        let Some(path) = std::env::var_os("G2C_SUPERVISOR_FIXTURE") else {
            return;
        };
        let path = PathBuf::from(path);
        let store = Supervisor::open(&path).unwrap();
        let owner = if std::env::var_os("G2C_SUPERVISOR_QUEUE_FIXTURE").is_some() {
            Owner::Queue
        } else {
            Owner::Manual
        };
        match store.claim("one", &path.join("one"), owner, issue()) {
            Ok(_) => std::process::exit(0),
            Err("project_busy") => std::process::exit(23),
            Err(_) => std::process::exit(24),
        }
    }

    #[test]
    fn two_real_processes_exactly_one_claim_survives_restart() {
        let f = Fixture::new();
        let spawn = || {
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "project_supervisor::tests::concurrent_process_claim_child",
                ])
                .env("G2C_SUPERVISOR_FIXTURE", &f.path)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap()
        };
        let mut a = spawn();
        let mut b = spawn();
        let mut codes = [
            a.wait().unwrap().code().unwrap(),
            b.wait().unwrap().code().unwrap(),
        ];
        codes.sort();
        assert_eq!(codes, [0, 23]);
        assert!(f.store().inspect("one", &f.root("one")).unwrap().is_some());
        assert_eq!(
            f.store()
                .claim("one", &f.root("one"), Owner::Queue, issue())
                .unwrap_err(),
            "project_busy"
        );
    }

    #[test]
    fn shared_instance_threads_are_excluded() {
        let f = Fixture::new();
        let store = Arc::new(f.store());
        let barrier = Arc::new(Barrier::new(3));
        let mut threads = vec![];
        for _ in 0..2 {
            let (store, barrier, root) = (store.clone(), barrier.clone(), f.root("one"));
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                store.claim("one", &root, Owner::Manual, None).is_ok()
            }));
        }
        barrier.wait();
        assert_eq!(
            threads
                .into_iter()
                .map(|t| t.join().unwrap() as u8)
                .sum::<u8>(),
            1
        );
    }

    #[test]
    fn manual_queue_and_queue_queue_process_races_share_one_lock() {
        for first_queue in [false, true] {
            let f = Fixture::new();
            let spawn = |queue: bool| {
                let mut command = Command::new(std::env::current_exe().unwrap());
                command
                    .args([
                        "--exact",
                        "project_supervisor::tests::concurrent_process_claim_child",
                    ])
                    .env("G2C_SUPERVISOR_FIXTURE", &f.path)
                    .env_remove("G2C_SUPERVISOR_QUEUE_FIXTURE")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());
                if queue {
                    command.env("G2C_SUPERVISOR_QUEUE_FIXTURE", "1");
                }
                command.spawn().unwrap()
            };
            let mut a = spawn(first_queue);
            let mut b = spawn(true);
            let mut codes = [
                a.wait().unwrap().code().unwrap(),
                b.wait().unwrap().code().unwrap(),
            ];
            codes.sort();
            assert_eq!(codes, [0, 23]);
            let reopened = f.store();
            let claim = reopened.inspect("one", &f.root("one")).unwrap().unwrap();
            assert_eq!(
                reopened.poll_verify(&claim).unwrap(),
                "reserved_runtime_disabled"
            );
            assert!(reopened.finish_and_release(&claim).is_err());
        }
    }

    #[test]
    fn distinct_roots_claim_independently() {
        let f = Fixture::new();
        claim(&f, Owner::Manual);
        assert!(f
            .store()
            .claim("two", &f.root("two"), Owner::Queue, issue())
            .is_ok());
    }

    #[test]
    fn both_owner_types_block_all_other_requests() {
        for first in [Owner::Manual, Owner::Queue] {
            let f = Fixture::new();
            claim(&f, first);
            // No task activity/sandbox switch exists in the exclusion API:
            // idle TUI, start/resume and read-only/write cannot bypass a claim.
            for next in [Owner::Manual, Owner::Queue] {
                assert_eq!(
                    f.store()
                        .claim("one", &f.root("one"), next, issue())
                        .unwrap_err(),
                    "project_busy"
                );
            }
        }
    }

    #[test]
    fn alias_nested_and_id_rebinding_are_busy() {
        let f = Fixture::new();
        claim(&f, Owner::Manual);
        symlink(f.root("one"), f.path.join("alias")).unwrap();
        fs::create_dir(f.root("one").join("nested")).unwrap();
        for (id, root) in [
            ("alias", f.path.join("alias")),
            ("nested", f.root("one").join("nested")),
            ("one", f.root("two")),
        ] {
            assert_eq!(
                f.store().claim(id, &root, Owner::Manual, None).unwrap_err(),
                "project_busy"
            );
        }
    }

    #[test]
    fn pending_claim_never_expires_or_releases() {
        let f = Fixture::new();
        let c = claim(&f, Owner::Queue);
        assert_eq!(
            f.store().poll_verify(&c).unwrap(),
            "reserved_runtime_disabled"
        );
        assert!(f.store().finish_and_release(&c).is_err());
        assert!(f
            .store()
            .start_in_project_screen(&c, &["never-execute-this".into()])
            .is_err());
        assert_eq!(f.store().inspect("one", &f.root("one")).unwrap(), Some(c));
    }

    #[test]
    fn nonce_and_owner_cannot_be_substituted() {
        let f = Fixture::new();
        let c = claim(&f, Owner::Queue);
        let mut wrong = c.clone();
        wrong.nonce = "b".repeat(64);
        assert!(f.store().poll_verify(&wrong).is_err());
        wrong = c.clone();
        wrong.owner = Owner::Manual;
        assert!(f.store().finish_and_release(&wrong).is_err());
        wrong = c;
        wrong.issue.as_mut().unwrap().sha = "c".repeat(64);
        assert!(f.store().poll_verify(&wrong).is_err());
    }

    #[test]
    fn host_boot_namespace_and_root_identity_fail_closed() {
        for change in 0..5 {
            let f = Fixture::new();
            claim(&f, Owner::Manual);
            f.edit(|j| match change {
                0 => j.claims[0].hostname.push_str("-other"),
                1 => j.claims[0].boot_id.push_str("-other"),
                2 => j.claims[0].pid_namespace += 1,
                3 => j.claims[0].root_inode += 1,
                _ => j.claims[0].root_device += 1,
            });
            assert_eq!(
                f.store().inspect("one", &f.root("one")).unwrap_err(),
                "session_unverified"
            );
            assert!(f
                .store()
                .claim("two", &f.root("two"), Owner::Manual, None)
                .is_err());
        }
    }

    #[test]
    fn fabricated_process_or_window_cannot_authorize_adoption() {
        for field in 0..3 {
            let f = Fixture::new();
            let c = claim(&f, Owner::Queue);
            f.edit(|j| {
                let process = ProcessIdentity::read(std::process::id()).unwrap();
                match field {
                    0 => j.claims[0].process = Some(process),
                    1 => {
                        j.claims[0].window = Some(WindowIdentity {
                            session_id: "fixture.g2c-one".into(),
                            server: process,
                            socket_device: 1,
                            socket_inode: 1,
                            number: 7,
                            generation: "fixture".into(),
                        })
                    }
                    _ => j.claims[0].thread_uuid = Some("fixture".into()),
                }
            });
            assert!(f.store().poll_verify(&c).is_err());
            assert!(f.store().finish_and_release(&c).is_err());
        }
    }

    #[test]
    fn process_live_gone_and_reused_generation() {
        let mut child = Command::new("/bin/sleep").arg("0.2").spawn().unwrap();
        let identity = ProcessIdentity::read(child.id()).unwrap();
        assert_eq!(identity.verify(), ProcessState::Live);
        let mut wrong = identity.clone();
        wrong.start_ticks += 1;
        assert_eq!(wrong.verify(), ProcessState::Unverified);
        wrong = identity.clone();
        wrong.boot_id.push('x');
        assert_eq!(wrong.verify(), ProcessState::Unverified);
        wrong = identity.clone();
        wrong.pid_namespace += 1;
        assert_eq!(wrong.verify(), ProcessState::Unverified);
        child.wait().unwrap();
        assert_eq!(identity.verify(), ProcessState::Gone);
    }

    #[test]
    fn corrupted_journal_and_unsafe_links_are_not_idle() {
        let f = Fixture::new();
        fs::write(f.path.join("foreign"), b"{}").unwrap();
        symlink(
            f.path.join("foreign"),
            f.path.join("project-owners-v1.json"),
        )
        .unwrap();
        assert!(f.store().inspect("one", &f.root("one")).is_err());
        fs::remove_file(f.path.join("project-owners-v1.json")).unwrap();
        fs::write(f.path.join("project-owners-v1.json"), b"partial-crash").unwrap();
        fs::set_permissions(
            f.path.join("project-owners-v1.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        assert!(f
            .store()
            .claim("one", &f.root("one"), Owner::Manual, None)
            .is_err());
        assert!(Supervisor::open(Path::new("relative")).is_err());
        symlink(&f.path, f.path.join("self-link")).unwrap();
        assert!(Supervisor::open(&f.path.join("self-link")).is_err());
    }

    #[test]
    fn issue_binding_required_and_complete() {
        let f = Fixture::new();
        assert!(f
            .store()
            .claim("one", &f.root("one"), Owner::Queue, None)
            .is_err());
        let mut bad = issue().unwrap();
        bad.sha = "a".repeat(12);
        assert!(f
            .store()
            .claim("one", &f.root("one"), Owner::Queue, Some(bad))
            .is_err());
        let c = claim(&f, Owner::Queue);
        assert_eq!(c.issue, issue());
        assert!(c.thread_uuid.is_none());
    }
    #[test]
    fn cli_inspect_and_reservation_only_contract() {
        let f = Fixture::new();
        fs::create_dir(f.path.join(".ai")).unwrap();
        fs::create_dir(f.path.join("cfg")).unwrap();
        fs::create_dir(f.path.join("cfg/projects")).unwrap();
        assert!(std::process::Command::new("git").args(["init", "-q"]).arg(f.root("one")).status().unwrap().success());
        let config = f.path.join("cfg/config.toml");
        fs::write(
            &config,
            format!(
                "[workspace]\nproject_dir = {:?}\nrules_dir = '.ai'\nglobal_context_files = []\n",
                f.path
            ),
        )
        .unwrap();
        fs::write(
            f.path.join("cfg/projects/one.toml"),
            "[project]\nid = 'one'\ndirectory = 'one'\n",
        )
        .unwrap();
        let args = |operation: &str| {
            vec![
                operation.to_string(),
                "--config".into(),
                config.to_str().unwrap().into(),
                "--directory".into(),
                f.path.to_str().unwrap().into(),
                "--id".into(),
                "one".into(),
            ]
        };
        assert!(cli(args("inspect").into_iter()).is_ok());
        assert!(cli(args("admission").into_iter()).is_ok());
        assert!(!f.path.join("project-owners-v1.json").exists());
        let mut request = args("claim");
        request.extend(["--owner".into(), "manual".into()]);
        assert!(cli(request.clone().into_iter()).is_err());
        assert!(!f.path.join("project-owners-v1.json").exists());
        request.push("--reservation-only".into());
        assert!(cli(request.clone().into_iter()).is_ok());
        assert_eq!(cli(request.into_iter()).unwrap_err(), "project_busy");
        assert!(cli(args("start").into_iter()).is_err());
        assert!(cli(args("finish").into_iter()).is_err());
    }

    #[test]
    fn manual_gate_child() {
        if std::env::var_os("G2C_SUPERVISOR_GATE_TEST").is_none() {
            return;
        }
        let missing = Path::new("/nonexistent-supervisor-fixture-config");
        let target = crate::screen::Target::Project("one");
        for error in [
            crate::screen::start(missing, target, "fixture")
                .err()
                .unwrap(),
            crate::session::start(
                missing,
                crate::multiplexer::Multiplexer::Screen,
                target,
                "fixture",
            )
            .err()
            .unwrap(),
            crate::session::start(
                missing,
                crate::multiplexer::Multiplexer::Tmux,
                target,
                "fixture",
            )
            .err()
            .unwrap(),
            crate::attention_marker::run(missing, target, crate::multiplexer::Multiplexer::Screen)
                .err()
                .unwrap(),
        ] {
            assert!(error.0.contains("runtime_disabled"));
        }
        // Read-only CLI/MCP app-server discovery is also a Codex process.
        // Refuse before PATH lookup or child creation under the opt-in gate.
        assert!(crate::codex::CodexClient::connect()
            .err()
            .unwrap()
            .to_string()
            .contains("runtime_disabled"));
    }

    #[test]
    fn opt_in_gate_blocks_all_rust_start_paths_before_discovery() {
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "project_supervisor::tests::manual_gate_child"])
            .env("G2C_SUPERVISOR_GATE_TEST", "1")
            .env("G2C_PROJECT_SUPERVISOR", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
    }
}
