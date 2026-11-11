//! Opt-in ordinary-question markers. No conversation data or session control.
use crate::multiplexer::Multiplexer;
use crate::registry::ProjectRegistry;
use crate::screen::{self, err, Result, Target};
use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::MetadataExt;
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

const LIMIT: u64 = 4096;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub version: u32,
    pub project_id: String,
    pub multiplexer: String,
    pub session_id: String,
    pub server_pid: String,
    pub server_start_ticks: String,
    pub run_nonce: String,
    pub codex_pid: String,
    pub codex_start_ticks: String,
    pub boot_id: String,
    pub pid_namespace: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Marker {
    pub version: u32,
    pub binding: Binding,
    pub wait_nonce: String,
    pub status: String,
}

pub fn nonce_valid(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn nonce() -> Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|_| err("attention random source unavailable"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn number(value: &str) -> Option<u64> {
    if value.is_empty()
        || value.len() > 20
        || value.starts_with('0')
        || !value.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    value.parse().ok()
}

fn project(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
}

fn boot_id() -> Option<String> {
    Some(
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()?
            .trim()
            .to_owned(),
    )
}

fn pid_namespace(pid: u32) -> Option<u64> {
    Some(fs::metadata(format!("/proc/{pid}/ns/pid")).ok()?.ino())
}

// Only numeric provenance, never cmdline, environment or conversation files.
pub(crate) fn process(pid: u32) -> Option<(u32, u64)> {
    if fs::metadata(format!("/proc/{pid}")).ok()?.uid() != unsafe { libc::geteuid() } {
        return None;
    }
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields = text
        .rsplit_once(')')?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    if matches!(*fields.first()?, "Z" | "X" | "x") {
        return None;
    }
    Some((fields.get(1)?.parse().ok()?, fields.get(19)?.parse().ok()?))
}

fn descendant(mut pid: u32, ancestor: u32, ticks: u64) -> bool {
    for _ in 0..128 {
        let Some((parent, started)) = process(pid) else {
            return false;
        };
        if pid == ancestor {
            return started == ticks;
        }
        if parent <= 1 || parent == pid {
            return false;
        }
        pid = parent;
    }
    false
}

// Fixed metadata-only categories; never include procfs contents, paths or env.
// The order below is the original fail-closed live/caller validation order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum IdentityRejection {
    InvalidNumbers,
    InvalidBinding,
    SessionMismatch,
    ProcessCollision,
    BootUnavailable,
    BootMismatch,
    ServerNamespaceUnavailable,
    ServerNamespaceMismatch,
    CodexNamespaceUnavailable,
    CodexNamespaceMismatch,
    ServerProcessUnconfirmed,
    ServerGenerationMismatch,
    RunAncestryUnconfirmed,
    CodexProcessUnconfirmed,
    CodexGenerationMismatch,
    CallerAncestryUnconfirmed,
    RunNonceMismatch,
}

impl IdentityRejection {
    fn code(self) -> &'static str {
        match self {
            Self::InvalidNumbers => "invalid_numeric_identity",
            Self::InvalidBinding => "invalid_binding",
            Self::SessionMismatch => "session_identity_mismatch",
            Self::ProcessCollision => "process_identity_collision",
            Self::BootUnavailable => "boot_unavailable",
            Self::BootMismatch => "boot_mismatch",
            Self::ServerNamespaceUnavailable => "server_namespace_unavailable",
            Self::ServerNamespaceMismatch => "server_namespace_mismatch",
            Self::CodexNamespaceUnavailable => "codex_namespace_unavailable",
            Self::CodexNamespaceMismatch => "codex_namespace_mismatch",
            Self::ServerProcessUnconfirmed => "server_process_unconfirmed",
            Self::ServerGenerationMismatch => "server_generation_mismatch",
            Self::RunAncestryUnconfirmed => "run_ancestry_unconfirmed",
            Self::CodexProcessUnconfirmed => "codex_process_unconfirmed",
            Self::CodexGenerationMismatch => "codex_generation_mismatch",
            Self::CallerAncestryUnconfirmed => "caller_ancestry_unconfirmed",
            Self::RunNonceMismatch => "run_nonce_mismatch",
        }
    }
    fn error(self) -> screen::ScreenError {
        err(format!(
            "attention run identity unconfirmed; no write; reason={}",
            self.code()
        ))
    }
}

impl Binding {
    pub fn live(&self) -> bool {
        self.check_live().is_ok()
    }
    fn check_live(&self) -> std::result::Result<(), IdentityRejection> {
        let (Some(server), Some(server_ticks), Some(codex), Some(codex_ticks), Some(namespace)) = (
            number(&self.server_pid).and_then(|n| u32::try_from(n).ok()),
            number(&self.server_start_ticks),
            number(&self.codex_pid).and_then(|n| u32::try_from(n).ok()),
            number(&self.codex_start_ticks),
            number(&self.pid_namespace),
        ) else {
            return Err(IdentityRejection::InvalidNumbers);
        };
        let name = format!("g2c-{}", self.project_id);
        let identity_ok = match self.multiplexer.as_str() {
            "screen" => self.session_id == format!("{server}.{name}"),
            "tmux" => {
                let p: Vec<_> = self.session_id.split('.').collect();
                p.len() == 3
                    && p[0] == self.server_pid
                    && p[1]
                        .strip_prefix('$')
                        .is_some_and(|id| !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit()))
                    && number(p[2]).is_some()
            }
            _ => false,
        };
        use IdentityRejection::*;
        if self.version != 1 || !project(&self.project_id) || !nonce_valid(&self.run_nonce) {
            return Err(InvalidBinding);
        }
        if !identity_ok {
            return Err(SessionMismatch);
        }
        if server == codex {
            return Err(ProcessCollision);
        }
        if boot_id().ok_or(BootUnavailable)? != self.boot_id {
            return Err(BootMismatch);
        }
        if pid_namespace(server).ok_or(ServerNamespaceUnavailable)? != namespace {
            return Err(ServerNamespaceMismatch);
        }
        if pid_namespace(codex).ok_or(CodexNamespaceUnavailable)? != namespace {
            return Err(CodexNamespaceMismatch);
        }
        if process(server).ok_or(ServerProcessUnconfirmed)?.1 != server_ticks {
            return Err(ServerGenerationMismatch);
        }
        if !descendant(codex, server, server_ticks) {
            return Err(RunAncestryUnconfirmed);
        }
        if process(codex).ok_or(CodexProcessUnconfirmed)?.1 != codex_ticks {
            return Err(CodexGenerationMismatch);
        }
        Ok(())
    }
    fn check_caller(&self) -> std::result::Result<(), IdentityRejection> {
        self.check_live()?;
        if !descendant(
            std::process::id(),
            number(&self.codex_pid).ok_or(IdentityRejection::InvalidNumbers)? as u32,
            number(&self.codex_start_ticks).ok_or(IdentityRejection::InvalidNumbers)?,
        ) {
            return Err(IdentityRejection::CallerAncestryUnconfirmed);
        }
        Ok(())
    }
}

// Descriptor-relative access. Existing objects are never chmodded or followed
// through symlinks; runtime metadata is a private command/identity boundary.
struct Directory(File);

// Closing the parent's fd alone does not unlock flock when a concurrent fork
// inherited its open file description. Explicitly end our critical section;
// O_CLOEXEC still prevents descriptors surviving the child's exec.
struct DirectoryLock<'a>(&'a Directory);
impl Drop for DirectoryLock<'_> {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0 .0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}
impl Directory {
    fn path(path: &Path) -> Result<Self> {
        if !path.is_absolute() {
            return Err(err("attention runtime path must be absolute"));
        }
        let mut dir = Self(File::open("/").map_err(|_| err("cannot open filesystem root"))?);
        for component in path.components() {
            if let Component::Normal(name) = component {
                dir = dir.open(
                    name.to_str().ok_or_else(|| err("invalid runtime path"))?,
                    false,
                )?;
            } else if !matches!(component, Component::RootDir) {
                return Err(err("noncanonical runtime path"));
            }
        }
        dir.private()?;
        Ok(dir)
    }
    fn open(&self, name: &str, create: bool) -> Result<Self> {
        let name = std::ffi::CString::new(name).map_err(|_| err("invalid attention name"))?;
        if create {
            let rc = unsafe { libc::mkdirat(self.0.as_raw_fd(), name.as_ptr(), 0o700) };
            if rc != 0
                && std::io::Error::last_os_error().kind() != std::io::ErrorKind::AlreadyExists
            {
                return Err(err("cannot create attention directory"));
            }
        }
        let fd = unsafe {
            libc::openat(
                self.0.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(err("unsafe or missing attention directory"));
        }
        Ok(Self(unsafe { File::from_raw_fd(fd) }))
    }
    fn private(&self) -> Result<()> {
        let meta = self
            .0
            .metadata()
            .map_err(|_| err("cannot inspect attention directory"))?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
            return Err(err("attention directory must be owned and private"));
        }
        Ok(())
    }
    fn lock(&self) -> Result<DirectoryLock<'_>> {
        if unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(err("attention metadata busy"));
        }
        Ok(DirectoryLock(self))
    }
    fn read<T: for<'de> Deserialize<'de>>(&self, name: &str) -> Result<Option<T>> {
        let name = std::ffi::CString::new(name).map_err(|_| err("invalid metadata name"))?;
        let fd = unsafe {
            libc::openat(
                self.0.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound {
                return Ok(None);
            }
            return Err(err("cannot safely read attention metadata"));
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let before = file.metadata().map_err(|_| err("metadata unavailable"))?;
        if !before.is_file()
            || before.uid() != unsafe { libc::geteuid() }
            || before.mode() & 0o077 != 0
            || before.nlink() != 1
            || before.len() == 0
            || before.len() > LIMIT
        {
            return Err(err("unsafe attention metadata"));
        }
        let mut data = Vec::new();
        (&file)
            .take(LIMIT + 1)
            .read_to_end(&mut data)
            .map_err(|_| err("cannot read attention metadata"))?;
        let after = file.metadata().map_err(|_| err("metadata disappeared"))?;
        if before.len() != data.len() as u64
            || before.len() != after.len()
            || before.mtime_nsec() != after.mtime_nsec()
            || before.mtime() != after.mtime()
        {
            return Err(err("attention metadata changed during read"));
        }
        let value: T =
            serde_json::from_slice(&data).map_err(|_| err("invalid attention metadata schema"))?;
        let canonical: serde_json::Value =
            serde_json::from_slice(&data).map_err(|_| err("invalid metadata JSON"))?;
        if serde_json::to_vec(&canonical).map_err(|_| err("metadata encoding failed"))? != data {
            return Err(err("noncanonical attention metadata"));
        }
        Ok(Some(value))
    }
    fn write_new<T: Serialize>(&self, name: &str, value: &T) -> Result<()> {
        // Value objects have sorted keys: the GUI can reject duplicate keys or
        // noncanonical JSON without accepting an ambiguous alternate parser view.
        let bytes = serde_json::to_vec(
            &serde_json::to_value(value).map_err(|_| err("metadata encoding failed"))?,
        )
        .map_err(|_| err("metadata encoding failed"))?;
        self.write_bytes(name, &bytes)
    }
    fn write_bytes(&self, name: &str, bytes: &[u8]) -> Result<()> {
        if bytes.len() as u64 > LIMIT {
            return Err(err("attention metadata too large"));
        }
        let temp = std::ffi::CString::new(format!(".tmp-{}", nonce()?)).unwrap();
        let name = std::ffi::CString::new(name).unwrap();
        let fd = unsafe {
            libc::openat(
                self.0.as_raw_fd(),
                temp.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err(err("cannot create atomic metadata"));
        }
        let mut file = unsafe { File::from_raw_fd(fd) };
        file.write_all(bytes)
            .and_then(|_| file.sync_all())
            .map_err(|_| err("cannot write attention metadata"))?;
        let rc = unsafe {
            libc::renameat2(
                self.0.as_raw_fd(),
                temp.as_ptr(),
                self.0.as_raw_fd(),
                name.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        if rc != 0 {
            unsafe {
                libc::unlinkat(self.0.as_raw_fd(), temp.as_ptr(), 0);
            }
            return Err(err("attention metadata exists; no overwrite"));
        }
        self.0
            .sync_all()
            .map_err(|_| err("cannot sync attention directory"))
    }
}

fn runtime(create: bool) -> Result<(PathBuf, Directory)> {
    let base = PathBuf::from(
        std::env::var_os("XDG_RUNTIME_DIR")
            .ok_or_else(|| err("XDG_RUNTIME_DIR required for attention"))?,
    );
    let runtime = Directory::path(&base)?;
    let app = runtime.open("gpt2codex", create)?;
    app.private()?;
    let attention = app.open("attention", create)?;
    attention.private()?;
    Ok((base.join("gpt2codex/attention"), attention))
}

fn run_dir(run: &str) -> Result<Directory> {
    if !nonce_valid(run) {
        return Err(err("invalid attention run nonce"));
    }
    let (_, parent) = runtime(false)?;
    let dir = parent.open(run, false)?;
    dir.private()?;
    Ok(dir)
}

fn checked_binding(dir: &Directory, run: &str) -> Result<Binding> {
    let binding: Binding = dir
        .read("binding.json")?
        .ok_or_else(|| err("attention binding missing"))?;
    if binding.run_nonce != run {
        return Err(IdentityRejection::RunNonceMismatch.error());
    }
    binding.check_caller().map_err(IdentityRejection::error)?;
    Ok(binding)
}

pub fn mark(run: &str) -> Result<String> {
    let dir = run_dir(run)?;
    let _lock = dir.lock()?;
    let binding = checked_binding(&dir, run)?;
    if let Some(marker) = dir.read::<Marker>("waiting.json")? {
        if marker.version == 1
            && marker.binding == binding
            && marker.status == "waiting"
            && nonce_valid(&marker.wait_nonce)
        {
            return Ok(marker.wait_nonce);
        }
        return Err(err("invalid existing waiting marker; no overwrite"));
    }
    let marker = Marker {
        version: 1,
        binding,
        wait_nonce: nonce()?,
        status: "waiting".into(),
    };
    dir.write_new("waiting.json", &marker)?;
    Ok(marker.wait_nonce)
}

pub fn clear(run: &str, wait: &str) -> Result<bool> {
    if !nonce_valid(wait) {
        return Err(err("exact wait nonce required"));
    }
    let dir = run_dir(run)?;
    let _lock = dir.lock()?;
    let binding = checked_binding(&dir, run)?;
    let Some(marker) = dir.read::<Marker>("waiting.json")? else {
        return Ok(false);
    };
    if marker.version != 1
        || marker.binding != binding
        || marker.status != "waiting"
        || marker.wait_nonce != wait
    {
        return Err(err("stale or mismatched waiting marker; clear refused"));
    }
    let name = std::ffi::CString::new("waiting.json").unwrap();
    if unsafe { libc::unlinkat(dir.0.as_raw_fd(), name.as_ptr(), 0) } != 0 {
        return Err(err("cannot clear waiting marker"));
    }
    dir.0
        .sync_all()
        .map_err(|_| err("cannot sync marker clear"))?;
    Ok(true)
}

/// Used only by the explicitly opted-in session launcher. Default argv stay
/// unchanged. No generic executable or shell-string parameter is accepted.
pub fn launch_arguments(
    config: &Path,
    target: Target<'_>,
    mux: Multiplexer,
) -> Result<Vec<String>> {
    if target == Target::Bridge {
        return Err(err(
            "--attention requires a project Codex start; Bridge remains a shell",
        ));
    }
    Ok(vec![
        std::env::current_exe()
            .map_err(|_| err("cannot find attention launcher"))?
            .to_string_lossy()
            .into_owned(),
        "attention".into(),
        "run".into(),
        "--id".into(),
        target.id().into(),
        "--multiplexer".into(),
        mux.as_str().into(),
        "--config".into(),
        config.to_string_lossy().into_owned(),
        "--approve-run".into(),
    ])
}

pub fn run(config: &Path, target: Target<'_>, mux: Multiplexer) -> Result<()> {
    crate::project_supervisor::manual_project_start_gate(config, target)?;
    let registry =
        ProjectRegistry::load(config).map_err(|_| err("cannot load attention project registry"))?;
    let root = screen::target_root(&registry, target)?;
    if std::env::current_dir()
        .ok()
        .and_then(|p| p.canonicalize().ok())
        .as_ref()
        != Some(&root)
    {
        return Err(err(
            "attention run must start in its registered project root",
        ));
    }
    // Resolve before creating runtime records; exec preserves this PID/start time.
    let program = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|p| p.join("codex"))
        .find(|p| screen::executable(p))
        .and_then(|p| p.canonicalize().ok())
        .ok_or_else(|| err("Codex executable unavailable"))?;
    let state = screen::state_dir(false)?;
    let path = match mux {
        Multiplexer::Screen => state.join(format!("{}.json", target.id())),
        Multiplexer::Tmux => crate::session::record_path(&state, target)?,
    };
    let deadline = Instant::now() + Duration::from_secs(4);
    let record = loop {
        let record =
            screen::read_record(&path)?.ok_or_else(|| err("registered attention slot missing"))?;
        if record.full_id.is_some() {
            break record;
        }
        if Instant::now() >= deadline {
            return Err(err("new attention slot did not finish registration"));
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    if record.root != root
        || record.project_id != target.id()
        || record.multiplexer != mux
        || record.screen_name != target.screen_name()?
    {
        return Err(err("attention record differs from registered project"));
    }
    if mux == Multiplexer::Tmux {
        let original = record
            .tmux
            .as_ref()
            .ok_or_else(|| err("tmux provenance missing"))?;
        let socket = crate::session::socket_path(&state, target, false)?;
        if record.full_id.as_deref()
            != Some(
                format!(
                    "{}.{}.{}",
                    original.server_pid, original.session_id, original.created
                )
                .as_str(),
            )
            || crate::session::socket_identity(&socket)?
                != (original.socket_dev, original.socket_ino)
        {
            return Err(err("attention tmux socket/session identity changed"));
        }
    }
    let pid = std::process::id();
    let binding = Binding {
        version: 1,
        project_id: target.id().into(),
        multiplexer: mux.as_str().into(),
        session_id: record
            .full_id
            .ok_or_else(|| err("full session identity missing"))?,
        server_pid: record
            .pid
            .ok_or_else(|| err("server PID missing"))?
            .to_string(),
        server_start_ticks: record
            .process_start_ticks
            .ok_or_else(|| err("server generation missing"))?
            .to_string(),
        run_nonce: nonce()?,
        codex_pid: pid.to_string(),
        codex_start_ticks: process(pid)
            .ok_or_else(|| err("Codex process identity missing"))?
            .1
            .to_string(),
        boot_id: boot_id().ok_or_else(|| err("boot provenance unavailable"))?,
        pid_namespace: pid_namespace(pid)
            .ok_or_else(|| err("PID namespace unavailable"))?
            .to_string(),
    };
    if !binding.live() {
        return Err(err(
            "launcher is not inside the registered persistent session",
        ));
    }
    let (runtime_path, attention) = runtime(true)?;
    let creation_lock = attention.lock()?;
    // Directory enumeration is confined to the held private root; no registry
    // entries are deleted or migrated, and a live instrumented run is not adopted.
    let entries = fs::read_dir(format!("/proc/self/fd/{}", attention.0.as_raw_fd()))
        .map_err(|_| err("cannot list attention bindings"))?;
    let mut count = 0;
    for item in entries {
        count += 1;
        if count > 256 {
            return Err(err("too many attention runs; explicit review required"));
        }
        let name = item
            .map_err(|_| err("attention entry unavailable"))?
            .file_name()
            .to_string_lossy()
            .into_owned();
        if !nonce_valid(&name) {
            continue;
        }
        if let Ok(dir) = attention.open(&name, false) {
            dir.private()?;
            if let Some(old) = dir.read::<Binding>("binding.json")? {
                if old.live()
                    && old.project_id == binding.project_id
                    && old.multiplexer == binding.multiplexer
                    && old.session_id == binding.session_id
                    && old.server_start_ticks == binding.server_start_ticks
                {
                    return Err(err("persistent slot already has a live attention run"));
                }
            }
        }
    }
    let dir = attention.open(&binding.run_nonce, true)?;
    dir.private()?;
    dir.write_new("binding.json", &binding)?;
    dir.write_bytes(
        "instructions.md",
        include_bytes!("../.ai/ATTENTION_OPT_IN.md"),
    )?;
    drop(creation_lock);
    drop(attention); // Release the creation lock before exec into the foreground CLI.
    let helper =
        std::env::current_exe().map_err(|_| err("attention helper executable unavailable"))?;
    eprintln!("ATTENTION_OPT_IN=ready; ask Codex to read $G2C_ATTENTION_INSTRUCTIONS; native approvals unsupported");
    let result = Command::new(program)
        .env("G2C_ATTENTION_RUN", &binding.run_nonce)
        .env("G2C_ATTENTION_HELPER", helper)
        .env(
            "G2C_ATTENTION_INSTRUCTIONS",
            runtime_path
                .join(&binding.run_nonce)
                .join("instructions.md"),
        )
        .arg("--add-dir")
        .arg(runtime_path.join(&binding.run_nonce))
        .current_dir(root)
        .exec();
    let _ = result; // Never print a user-controlled executable/error payload.
    Err(err("opt-in Codex exec failed; no automatic retry"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, DirBuilderExt, PermissionsExt};
    use std::sync::Mutex;
    static ENV: Mutex<()> = Mutex::new(());

    struct Fixture {
        root: PathBuf,
        old: Option<std::ffi::OsString>,
    }
    impl Fixture {
        fn new() -> Self {
            let root = PathBuf::from(std::env::var_os("TMPDIR").expect("isolated TMPDIR required"))
                .join(format!("marker-unit-{}", nonce().unwrap()));
            fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
            let old = std::env::var_os("XDG_RUNTIME_DIR");
            std::env::set_var("XDG_RUNTIME_DIR", &root);
            Self { root, old }
        }
        fn binding(&self, mux: &str) -> Binding {
            let pid = std::process::id();
            let parent = process(pid).unwrap().0;
            Binding {
                version: 1,
                project_id: "alpha".into(),
                multiplexer: mux.into(),
                session_id: if mux == "screen" {
                    format!("{parent}.g2c-alpha")
                } else {
                    format!("{parent}.$0.12345")
                },
                server_pid: parent.to_string(),
                server_start_ticks: process(parent).unwrap().1.to_string(),
                run_nonce: nonce().unwrap(),
                codex_pid: pid.to_string(),
                codex_start_ticks: process(pid).unwrap().1.to_string(),
                boot_id: boot_id().unwrap(),
                pid_namespace: pid_namespace(pid).unwrap().to_string(),
            }
        }
        fn install(&self, binding: &Binding) -> Directory {
            let (_, root) = runtime(true).unwrap();
            let dir = root.open(&binding.run_nonce, true).unwrap();
            dir.write_new("binding.json", binding).unwrap();
            dir
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            match &self.old {
                Some(old) => std::env::set_var("XDG_RUNTIME_DIR", old),
                None => std::env::remove_var("XDG_RUNTIME_DIR"),
            }
            fs::remove_dir_all(&self.root).unwrap(); // Only this owned synthetic fixture.
        }
    }

    #[test]
    fn mark_clear_both_multiplexers_and_delayed_clear() {
        let _guard = ENV.lock().unwrap();
        let fixture = Fixture::new();
        for mux in ["screen", "tmux"] {
            let b = fixture.binding(mux);
            assert!(b.live());
            let dir = fixture.install(&b);
            let a = mark(&b.run_nonce).unwrap();
            assert_eq!(mark(&b.run_nonce).unwrap(), a);
            assert!(clear(&b.run_nonce, &a).unwrap());
            let next = mark(&b.run_nonce).unwrap();
            assert_ne!(a, next);
            assert!(clear(&b.run_nonce, &a).is_err());
            assert_eq!(
                dir.read::<Marker>("waiting.json")
                    .unwrap()
                    .unwrap()
                    .wait_nonce,
                next
            );
            assert!(clear(&b.run_nonce, &next).unwrap());
            assert!(!clear(&b.run_nonce, &next).unwrap());
        }
    }
    #[test]
    fn strict_identity_and_process_generation() {
        let _guard = ENV.lock().unwrap();
        let f = Fixture::new();
        let b = f.binding("screen");
        for index in 0..7 {
            let mut wrong = b.clone();
            wrong.run_nonce = nonce().unwrap();
            match index {
                0 => wrong.codex_start_ticks = "1".into(),
                1 => wrong.server_start_ticks = "1".into(),
                2 => wrong.boot_id = "other-boot".into(),
                3 => wrong.pid_namespace = "1".into(),
                4 => wrong.project_id = "../alpha".into(),
                5 => wrong.session_id = "wrong.g2c-alpha".into(),
                _ => wrong.multiplexer = "other".into(),
            }
            let _dir = f.install(&wrong);
            assert!(!wrong.live());
            assert!(mark(&wrong.run_nonce).is_err());
        }
    }
    #[test]
    fn rejection_categories_preserve_fail_closed_identity() {
        let _guard = ENV.lock().unwrap();
        let f = Fixture::new();
        let original = f.binding("screen");
        use IdentityRejection::*;
        for (index, expected) in [
            InvalidNumbers,
            InvalidBinding,
            SessionMismatch,
            ProcessCollision,
            BootMismatch,
            ServerNamespaceMismatch,
            ServerGenerationMismatch,
            CodexGenerationMismatch,
            ServerNamespaceUnavailable,
            CodexNamespaceUnavailable,
            RunAncestryUnconfirmed,
        ]
        .into_iter()
        .enumerate()
        {
            let mut b = original.clone();
            b.run_nonce = nonce().unwrap();
            match index {
                0 => b.codex_start_ticks = "0".into(),
                1 => b.version = 2,
                2 => b.session_id = "other".into(),
                3 => b.codex_pid = b.server_pid.clone(),
                4 => b.boot_id = "other".into(),
                5 => b.pid_namespace = "1".into(),
                6 => b.server_start_ticks = "1".into(),
                7 => b.codex_start_ticks = "1".into(),
                8 => {
                    b.server_pid = u32::MAX.to_string();
                    b.session_id = format!("{}.g2c-alpha", b.server_pid);
                }
                9 => b.codex_pid = u32::MAX.to_string(),
                _ => {
                    // A real owned process, but the parent is not its descendant.
                    b.server_pid = std::process::id().to_string();
                    b.server_start_ticks = process(std::process::id()).unwrap().1.to_string();
                    b.codex_pid = original.server_pid.clone();
                    b.session_id = format!("{}.g2c-alpha", b.server_pid);
                }
            }
            assert_eq!(b.check_live(), Err(expected));
            assert!(!b.live());
            let dir = f.install(&b);
            assert_eq!(
                mark(&b.run_nonce).unwrap_err().to_string(),
                expected.error().to_string()
            );
            assert!(dir.read::<Marker>("waiting.json").unwrap().is_none());
        }
        let dir = f.install(&original);
        assert_eq!(
            checked_binding(&dir, &nonce().unwrap())
                .unwrap_err()
                .to_string(),
            RunNonceMismatch.error().to_string()
        );
        assert!(dir.read::<Marker>("waiting.json").unwrap().is_none());
    }
    #[test]
    fn dead_program_with_forgotten_marker_is_not_live() {
        let _guard = ENV.lock().unwrap();
        let f = Fixture::new();
        let mut child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut b = f.binding("screen");
        b.codex_pid = child.id().to_string();
        b.codex_start_ticks = process(child.id()).unwrap().1.to_string();
        assert!(b.live());
        let dir = f.install(&b);
        // A live run is not enough: its parent/sibling helper cannot mark it.
        assert_eq!(
            mark(&b.run_nonce).unwrap_err().to_string(),
            IdentityRejection::CallerAncestryUnconfirmed
                .error()
                .to_string()
        );
        assert!(dir.read::<Marker>("waiting.json").unwrap().is_none());
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(!b.live());
    }
    #[test]
    fn private_paths_symlinks_permissions_schema_and_atomic_no_overwrite() {
        let _guard = ENV.lock().unwrap();
        let f = Fixture::new();
        let b = f.binding("screen");
        let dir = f.install(&b);
        let base = f.root.join("gpt2codex/attention").join(&b.run_nonce);
        let bytes = fs::read(base.join("binding.json")).unwrap();
        assert!(dir.write_new("binding.json", &b).is_err());
        assert_eq!(fs::read(base.join("binding.json")).unwrap(), bytes);
        fs::set_permissions(base.join("binding.json"), fs::Permissions::from_mode(0o644)).unwrap();
        assert!(mark(&b.run_nonce).is_err());
        fs::set_permissions(base.join("binding.json"), fs::Permissions::from_mode(0o600)).unwrap();
        symlink(base.join("binding.json"), base.join("waiting.json")).unwrap();
        assert!(mark(&b.run_nonce).is_err());
        fs::remove_file(base.join("waiting.json")).unwrap();
        fs::write(
            base.join("binding.json"),
            b"{\"prompt\":\"synthetic-private\"}",
        )
        .unwrap();
        assert!(mark(&b.run_nonce).is_err());
        fs::write(base.join("binding.json"), vec![b'x'; 4097]).unwrap();
        assert!(mark(&b.run_nonce).is_err());
        fs::write(base.join("binding.json"), &bytes).unwrap();
        fs::set_permissions(&base, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(mark(&b.run_nonce).is_err());
        fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
        let _lock = dir.lock().unwrap();
        assert!(mark(&b.run_nonce).is_err());
        let alias = f.root.join("alias");
        symlink(&f.root, &alias).unwrap();
        assert!(Directory::path(&alias).is_err());
    }
    #[test]
    fn inherited_descriptor_does_not_extend_completed_lock() {
        // Isolate fork from the multithreaded test runner. The child performs
        // only async-signal-safe libc calls until _exit.
        const KEY: &str = "G2C_ATTENTION_FORK_FIXTURE";
        if std::env::var_os(KEY).is_none() {
            assert!(Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "attention_marker::tests::inherited_descriptor_does_not_extend_completed_lock"
                ])
                .env(KEY, "1")
                .status()
                .unwrap()
                .success());
            return;
        }
        let _guard = ENV.lock().unwrap();
        let f = Fixture::new();
        let dir = Directory::path(&f.root).unwrap();
        let lock = dir.lock().unwrap();
        let mut pipe = [0; 2];
        assert_eq!(
            unsafe { libc::pipe2(pipe.as_mut_ptr(), libc::O_CLOEXEC) },
            0
        );
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe {
                libc::close(pipe[1]);
                let mut byte = 0u8;
                libc::read(pipe[0], (&mut byte as *mut u8).cast(), 1);
                libc::_exit(0);
            }
        }
        unsafe {
            libc::close(pipe[0]);
        }
        drop(lock);
        drop(dir);
        let other = Directory::path(&f.root).unwrap();
        let acquired = other.lock();
        unsafe {
            libc::close(pipe[1]);
            libc::waitpid(pid, std::ptr::null_mut(), 0);
        }
        assert!(
            acquired.is_ok(),
            "fork child retained a completed metadata lock"
        );
    }

    #[test]
    fn launcher_argv_is_direct_and_bridge_start_is_not_reinterpreted() {
        let args = launch_arguments(
            Path::new("/registered/config with ; punctuation.toml"),
            Target::Project("alpha"),
            Multiplexer::Tmux,
        )
        .unwrap();
        assert_eq!(args[1..5], ["attention", "run", "--id", "alpha"]);
        assert!(args.contains(&"/registered/config with ; punctuation.toml".into()));
        assert_eq!(args.last().unwrap(), "--approve-run");
        assert!(launch_arguments(
            Path::new("/registered/config"),
            Target::Bridge,
            Multiplexer::Screen
        )
        .is_err());
    }
}
