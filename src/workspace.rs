//! Explicit local bootstrap and maintenance for bridge-managed project workspaces.
//!
//! This module intentionally has no MCP write surface. CLI invocation is the
//! human-authorized write boundary in P0-E; managed remote approvals come later.

use crate::registry::{validate_id, validate_relative, ProjectRegistry, ProjectType};
use serde::Deserialize;
use std::error::Error;
use std::fmt;
use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{symlink, DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const FILE_LIMIT: u64 = 1024 * 1024;
const IGNORE_LINES: [&str; 6] = ["/.tmp", "/.log", "/.ai/.project_codex.md", "/.ai/CODEX_RESULTS.md", "/CODEX_RESULTS.md", "/.bash_history"];
const SHARED_FILES: [(&str, &str); 4] = [
    ("CODEX_GLOBAL.md", include_str!("../.ai/templates/CODEX_GLOBAL.md")),
    ("BOOTSTRAP.md", include_str!("../.ai/templates/BOOTSTRAP.md")),
    ("PROJECT_DEFAULTS.md", include_str!("../.ai/templates/PROJECT_DEFAULTS.md")),
    ("VERSION", include_str!("../.ai/templates/VERSION")),
];
const PROJECT_OVERRIDE: &str = include_str!("../.ai/templates/.project_codex.md");
const POLICY_START: &str = "<!-- chatgpt2codex-managed-policy-v1 -->";
const POLICY_END: &str = "<!-- /chatgpt2codex-managed-policy-v1 -->";
// An explicit, additive update for existing user-maintained shared rules.
// Future installations receive the same text from CODEX_GLOBAL.md's template.
const POLICY_ADDENDUM: &str = include_str!("../.ai/templates/POLICY_ADDENDUM.md");
// Historical v1 block, accepted only byte-for-byte to support older installations.
// The newer approved policy clarifies that user-controlled history metadata is advisory.
const POLICY_ADDENDUM_V1_LEGACY: &str = include_str!("../tests/fixtures/policy_addendum_v1_legacy.md");
const POLICY_V2_START: &str = "<!-- gpt2codex-approved-policy-v2 -->";
const POLICY_V2_END: &str = "<!-- /gpt2codex-approved-policy-v2 -->";
const POLICY_APPROVAL_V2: &str = include_str!("../.ai/templates/POLICY_APPROVAL_V2.md");


#[derive(Debug)]
pub struct WorkspaceError(pub String);
impl fmt::Display for WorkspaceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str(&self.0) }
}
impl Error for WorkspaceError {}
type Result<T> = std::result::Result<T, WorkspaceError>;

fn error(s: impl Into<String>) -> WorkspaceError { WorkspaceError(s.into()) }
fn io_error(label: &'static str, cause: std::io::Error) -> WorkspaceError {
    error(format!("{label}: {cause}"))
}

#[derive(Debug)]
pub struct AdoptRequest {
    pub project_type: ProjectType,
    pub config_path: PathBuf,
    pub id: String,
    pub directory: String,
}

#[derive(Debug)]
pub struct BootstrapRequest {
    pub project_dir: PathBuf,
    pub config_path: PathBuf,
    pub id: String,
    pub directory: String,
    pub git_name: String,
    pub git_email: String,
    pub codex_name: String,
}

#[derive(Debug)]
pub struct Health {
    pub project_id: String,
    pub root: PathBuf,
    pub checks: Vec<(&'static str, &'static str)>,
    // User-controlled shell history settings are advisory, never a readiness gate.
    pub history_notices: Vec<&'static str>,
}
impl Health {
    pub fn ready(&self) -> bool { self.checks.iter().all(|(_, status)| *status == "ok") }
}

fn euid() -> u32 { unsafe { libc::geteuid() } }

fn lstat(path: &Path) -> Result<Option<fs::Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(io_error("cannot inspect path", err)),
    }
}

fn check_directory(path: &Path, private: bool) -> Result<bool> {
    match lstat(path)? {
        None => Ok(false),
        Some(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(error(format!("directory is missing or replaced by non-directory: {}", path.display())));
            }
            if metadata.uid() != euid() {
                return Err(error(format!("directory is not owned by current user: {}", path.display())));
            }
            if private && (metadata.mode() & 0o777) != 0o700 {
                return Err(error(format!("directory must have mode 0700: {}", path.display())));
            }
            Ok(true)
        }
    }
}

fn create_private_dir(path: &Path) -> Result<()> {
    if check_directory(path, true)? { return Ok(()); }
    match DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => (),
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(err) => return Err(io_error("cannot create private directory", err)),
    }
    if !check_directory(path, true)? { return Err(error("private directory was not created")); }
    Ok(())
}

fn safe_regular(path: &Path) -> Result<bool> {
    match lstat(path)? {
        None => Ok(false),
        Some(meta) if meta.is_file() && !meta.file_type().is_symlink() && meta.uid() == euid() => Ok(true),
        _ => Err(error(format!("unsafe or foreign existing file: {}", path.display()))),
    }
}

// Shell history is user-owned policy. The bridge does not inspect its contents,
// resolve user-supplied symlinks or enforce permissions/ownership on existing paths.
fn history_status(root: &Path) -> Result<bool> {
    Ok(lstat(&root.join(".bash_history"))?.is_some())
}

// Only create an absent history path. Never chmod, truncate or replace a user path.
fn ensure_history(root: &Path) -> Result<()> {
    let path = root.join(".bash_history");
    if history_status(root)? { return Ok(()); }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true).mode(0o600);
    options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    match options.open(&path) {
        Ok(_) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(err) => Err(io_error("cannot create missing shell history", err)),
    }
}

// Notes describe metadata only. They never downgrade workspace readiness.
fn history_notices(root: &Path, standard: bool) -> Vec<&'static str> {
    let path = root.join(".bash_history");
    let mut notes = Vec::new();
    match lstat(&path) {
        Ok(Some(metadata)) => {
            if metadata.file_type().is_symlink() {
                notes.push("symlink_preserved");
            } else {
                if !metadata.is_file() { notes.push("non_file_preserved"); }
                if metadata.mode() & 0o077 != 0 { notes.push("permissions_preserved"); }
            }
        }
        Ok(None) => (),
        Err(_) => notes.push("metadata_unavailable"),
    }
    if standard {
        match git_call(root, &["ls-files", "--", ".bash_history"]) {
            Ok(result) if result.status.success() && !result.stdout.is_empty() => {
                notes.push("git_tracked_not_untracked");
            }
            Ok(result) if !result.status.success() => notes.push("git_tracking_unknown"),
            Err(_) => notes.push("git_tracking_unknown"),
            _ => (),
        }
    }
    notes
}

fn read_bounded(path: &Path) -> Result<String> {
    if !safe_regular(path)? { return Err(error("required file is missing")); }
    let mut content = Vec::new();
    File::open(path).map_err(|e| io_error("cannot open file", e))?
        .take(FILE_LIMIT + 1).read_to_end(&mut content)
        .map_err(|e| io_error("cannot read file", e))?;
    if content.len() as u64 > FILE_LIMIT { return Err(error("existing file is too large")); }
    String::from_utf8(content).map_err(|_| error("existing file is not UTF-8"))
}

fn quote(value: &str) -> Result<String> {
    if value.is_empty() || value.chars().any(char::is_control) {
        return Err(error("invalid configuration text"));
    }
    Ok(toml::Value::String(value.to_owned()).to_string())
}

fn check_text(value: &str, max: usize) -> Result<()> {
    if value.trim().is_empty() || value.chars().count() > max || value.chars().any(char::is_control) {
        return Err(error("invalid text field"));
    }
    Ok(())
}

fn ensure_new_file(path: &Path, text: &str, private: bool) -> Result<()> {
    if safe_regular(path)? {
        if private && (fs::symlink_metadata(path).map_err(|e| io_error("cannot stat private file", e))?.mode() & 0o077) != 0 {
            return Err(error("existing private file has overly broad permissions"));
        }
        return Ok(());
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true).mode(if private { 0o600 } else { 0o644 });
    // Linux O_NOFOLLOW protects the file creation against final-component races.
    options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
            if safe_regular(path)? {
                if private && (fs::symlink_metadata(path).map_err(|e| io_error("cannot stat private file", e))?.mode() & 0o077) != 0 {
                    return Err(error("existing private file has overly broad permissions"));
                }
                return Ok(());
            }
            return Err(error("file appeared as an unsafe object"));
        }
        Err(err) => return Err(io_error("cannot create new file", err)),
    };
    file.write_all(text.as_bytes()).map_err(|e| io_error("cannot write file", e))?;
    file.sync_all().map_err(|e| io_error("cannot sync file", e))?;
    Ok(())
}

fn expected_link_status(root: &Path, id: &str, temp_base: &Path, kind: &str) -> Result<bool> {
    let link = root.join(format!(".{kind}"));
    let expected = temp_base.join(id).join(kind);
    match lstat(&link)? {
        None => Ok(false),
        Some(meta) if meta.file_type().is_symlink() => {
            if fs::read_link(&link).map_err(|e| io_error("cannot read link", e))? == expected { Ok(true) }
            else { Err(error(format!("symlink points to wrong destination: {}", link.display()))) }
        }
        Some(_) => Err(error(format!("reserved link name already exists: {}", link.display()))),
    }
}

fn check_tmp_base(temp_base: &Path) -> Result<()> {
    if !temp_base.is_absolute() { return Err(error("temporary base must be absolute")); }
    let meta = fs::symlink_metadata(temp_base).map_err(|e| io_error("temporary base unavailable", e))?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Err(error("temporary base must be a real directory"));
    }
    // Keep the historical check for public /tmp only. Other locations are
    // operator-managed: never enforce their owner or permissions.
    if temp_base == Path::new("/tmp") && (meta.mode() & 0o1000) == 0 {
        return Err(error("the default /tmp must have its sticky bit"));
    }
    Ok(())
}

fn storage_kinds(temp_base: &Path) -> &'static [&'static str] {
    if temp_base == Path::new("/tmp") { &["tmp", "log"] }
    else { &["tmp", "log", "rel"] }
}

fn storage_directory_state(path: &Path, temp_base: &Path) -> Result<bool> {
    if temp_base == Path::new("/tmp") { return check_directory(path, true); }
    match lstat(path)? {
        None => Ok(false),
        Some(meta) if meta.is_dir() && !meta.file_type().is_symlink() => Ok(true),
        Some(_) => Err(error(format!("workspace storage is not a directory: {}", path.display()))),
    }
}

fn create_storage_directory(path: &Path, temp_base: &Path) -> Result<()> {
    if temp_base == Path::new("/tmp") { return create_private_dir(path); }
    if storage_directory_state(path, temp_base)? { return Ok(()); }
    // Use the OS umask for new directories, do not chmod/chown existing ones.
    match fs::create_dir(path) {
        Ok(()) => (),
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(err) => return Err(io_error("cannot create workspace storage directory", err)),
    }
    if !storage_directory_state(path, temp_base)? {
        return Err(error("workspace storage directory was not created"));
    }
    Ok(())
}

fn tmp_directory_state(temp_base: &Path, id: &str) -> Result<bool> {
    let base = temp_base.join(id);
    if !storage_directory_state(&base, temp_base)? { return Ok(false); }
    for kind in storage_kinds(temp_base) {
        if !storage_directory_state(&base.join(kind), temp_base)? { return Ok(false); }
    }
    Ok(true)
}

fn ensure_tmp_directory(temp_base: &Path, id: &str) -> Result<()> {
    check_tmp_base(temp_base)?;
    let base = temp_base.join(id);
    create_storage_directory(&base, temp_base)?;
    for kind in storage_kinds(temp_base) {
        create_storage_directory(&base.join(kind), temp_base)?;
    }
    Ok(())
}

fn legacy_link(root: &Path, id: &str, temp_base: &Path, kind: &str) -> Result<bool> {
    if temp_base == Path::new("/tmp") { return Ok(false); }
    let link = root.join(format!(".{kind}"));
    Ok(lstat(&link)?.is_some_and(|m| m.file_type().is_symlink())
        && fs::read_link(&link).map_err(|e| io_error("cannot read workspace link", e))?
            == Path::new("/tmp").join(id).join(kind))
}

fn preflight_link(root: &Path, id: &str, temp_base: &Path, kind: &str) -> Result<()> {
    match expected_link_status(root, id, temp_base, kind) {
        Ok(_) => Ok(()),
        Err(_) if legacy_link(root, id, temp_base, kind)? => Ok(()),
        Err(err) => Err(err),
    }
}

// Copy missing legacy files only. Existing new files are retained and old
// originals are never removed; collisions remain accessible in the old tree.
fn copy_missing_tree(src: &Path, dst: &Path, copied: &mut u64, existing: &mut u64, transient: &mut u64) -> Result<()> {
    let src_meta = fs::symlink_metadata(src).map_err(|e| io_error("cannot inspect legacy data", e))?;
    if !src_meta.is_dir() || src_meta.file_type().is_symlink() {
        return Err(error(format!("legacy source is not a directory: {}", src.display())));
    }
    for entry in fs::read_dir(src).map_err(|e| io_error("cannot list legacy data", e))? {
        let entry = entry.map_err(|e| io_error("cannot read legacy entry", e))?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        let from_meta = fs::symlink_metadata(&from).map_err(|e| io_error("cannot inspect legacy entry", e))?;
        let destination = lstat(&to)?;
        if from_meta.is_dir() && !from_meta.file_type().is_symlink() {
            if destination.is_none() {
                DirBuilder::new().mode(from_meta.mode() & 0o777).create(&to)
                    .map_err(|e| io_error("cannot create migrated directory", e))?;
            } else if !destination.as_ref().is_some_and(|m| m.is_dir() && !m.file_type().is_symlink()) {
                *existing += 1;
                continue;
            }
            copy_missing_tree(&from, &to, copied, existing, transient)?;
        } else if destination.is_some() {
            *existing += 1;
        } else if from_meta.file_type().is_symlink() {
            symlink(fs::read_link(&from).map_err(|e| io_error("cannot read legacy symlink", e))?, &to)
                .map_err(|e| io_error("cannot copy legacy symlink", e))?;
            *copied += 1;
        } else if from_meta.is_file() {
            fs::copy(&from, &to).map_err(|e| io_error("cannot copy legacy file", e))?;
            *copied += 1;
        } else {
            *transient += 1;
        }
    }
    Ok(())
}

fn migrate_legacy_data(root: &Path, id: &str, temp_base: &Path) -> Result<()> {
    if temp_base == Path::new("/tmp") { return Ok(()); }
    let mut copied = 0;
    let mut existing = 0;
    let mut transient = 0;
    for kind in ["tmp", "log"] {
        if !legacy_link(root, id, temp_base, kind)? { continue; }
        let old = Path::new("/tmp").join(id).join(kind);
        if lstat(&old)?.is_some() {
            copy_missing_tree(&old, &temp_base.join(id).join(kind), &mut copied, &mut existing, &mut transient)?;
        }
    }
    if copied > 0 || existing > 0 || transient > 0 {
        eprintln!("workspace_migration={id} copied={copied} already_present={existing} transient_skipped={transient} old_data_retained=true");
    }
    Ok(())
}

fn ensure_link(root: &Path, id: &str, temp_base: &Path, kind: &str) -> Result<()> {
    if expected_link_status(root, id, temp_base, kind).unwrap_or(false) { return Ok(()); }
    preflight_link(root, id, temp_base, kind)?;
    let link = root.join(format!(".{kind}"));
    let target = temp_base.join(id).join(kind);
    if legacy_link(root, id, temp_base, kind)? {
        // Atomic replacement of exactly the old bridge-managed link, not a
        // general-purpose fixer of user-owned paths.
        let staging = root.join(format!(".{kind}.g2c-migrate-{}", std::process::id()));
        if lstat(&staging)?.is_some() { return Err(error("staging link already exists")); }
        symlink(&target, &staging).map_err(|e| io_error("cannot stage workspace link", e))?;
        let result = (|| {
            if !legacy_link(root, id, temp_base, kind)? {
                return Err(error("legacy workspace link changed during migration"));
            }
            fs::rename(&staging, &link).map_err(|e| io_error("cannot replace legacy link", e))
        })();
        if result.is_err() { let _ = fs::remove_file(&staging); }
        return result;
    }
    match symlink(&target, &link) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
            if expected_link_status(root, id, temp_base, kind)? { Ok(()) }
            else { Err(error("expected workspace symlink disappeared")) }
        }
        Err(err) => Err(io_error("cannot create symlink", err)),
    }
}

#[cfg(test)]
thread_local! { static GIT_FORBIDDEN: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }

fn git_call(root: &Path, params: &[&str]) -> Result<Output> {
    #[cfg(test)]
    GIT_FORBIDDEN.with(|blocked| assert!(!blocked.get(), "unexpected Git subprocess"));
    let mut command = Command::new("git");
    // Ignore inherited repository selectors: the registered root is authority.
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") { command.env_remove(key); }
    }
    let output = command.arg("-C").arg(root).args(params)
        .output().map_err(|e| io_error("cannot run git", e))?;
    Ok(output)
}

fn git_local(root: &Path, key: &str) -> Result<Option<String>> {
    let output = git_call(root, &["config", "--local", "--get", key])?;
    if output.status.success() {
        let value = String::from_utf8(output.stdout).map_err(|_| error("invalid Git config encoding"))?;
        Ok(Some(value.trim_end_matches('\n').to_owned()))
    } else if output.status.code() == Some(1) {
        Ok(None)
    } else {
        Err(error("git config read failed"))
    }
}

fn preflight_git_reserved(root: &Path) -> Result<()> {
    let tracked = git_call(root, &["ls-files", "--", ".tmp", ".log"])?;
    if !tracked.status.success() || !tracked.stdout.is_empty() {
        return Err(error("reserved .tmp or .log is tracked by Git (or Git check failed)"));
    }
    // .bash_history is user-managed: tracked history is reported, not blocked.
    Ok(())
}

fn preflight_git(root: &Path, name: &str, email: &str) -> Result<()> {
    preflight_git_reserved(root)?;
    for (key, target) in [("user.name", name), ("user.email", email)] {
        if let Some(existing) = git_local(root, key)? {
            if existing != target { return Err(error(format!("conflicting local Git identity ({key})"))); }
        }
    }
    Ok(())
}

fn ensure_git_identity(root: &Path, name: &str, email: &str) -> Result<()> {
    preflight_git(root, name, email)?;
    for (key, target) in [("user.name", name), ("user.email", email)] {
        if git_local(root, key)?.is_none() {
            if !git_call(root, &["config", "--local", key, target])?.status.success() {
                return Err(error(format!("cannot configure Git {key}")));
            }
        }
    }
    Ok(())
}

fn git_exclude_path(root: &Path) -> Result<PathBuf> {
    let git_path = root.join(".git");
    let meta = lstat(&git_path)?.ok_or_else(|| error("Git metadata not found"))?;
    if meta.file_type().is_symlink() || !meta.is_dir() { return Err(error("P0-E supports regular Git checkouts, not linked worktrees")); }
    if !check_directory(&git_path, false)? { return Err(error("unsafe Git metadata directory")); }
    if !safe_regular(&git_path.join("config"))? { return Err(error("Git config missing or unsafe")); }
    let info = git_path.join("info");
    if !check_directory(&info, false)? { return Err(error("Git info directory missing or unsafe")); }
    Ok(info.join("exclude"))
}

pub(crate) fn validate_registered_checkout(root: &Path, run_git: bool) -> Result<()> {
    let git = root.join(".git");
    let meta = lstat(&git)?.ok_or_else(|| error("Git metadata missing"))?;
    if meta.file_type().is_symlink() || meta.uid() != euid() {
        return Err(error("unsafe Git metadata"));
    }
    if meta.is_dir() {
        check_directory(&git, false)?;
        if !safe_regular(&git.join("config"))? || !safe_regular(&git.join("HEAD"))? {
            return Err(error("Git config or HEAD missing"));
        }
    } else if meta.is_file() {
        // Preserve read-only linked-worktree registration. Managed writes still
        // require a regular checkout through git_exclude_path.
        let text = read_bounded(&git)?;
        let target = text.trim_end().strip_prefix("gitdir: ").ok_or_else(|| error("invalid worktree Git file"))?;
        let directory = root.join(target);
        if !check_directory(&directory, false)? || !safe_regular(&directory.join("HEAD"))? {
            return Err(error("unsafe worktree Git directory"));
        }
    } else { return Err(error("invalid Git metadata")); }
    if !run_git { return Ok(()); }
    let out = git_call(root, &["rev-parse", "--show-toplevel"])?;
    if !out.status.success() || Path::new(std::str::from_utf8(&out.stdout)
        .map_err(|_| error("invalid Git root"))?.trim_end()) != root {
        return Err(error("Git checkout root mismatch"));
    }
    Ok(())
}

pub(crate) fn validate_git_checkout(root: &Path) -> Result<()> {
    git_exclude_path(root)?;
    validate_registered_checkout(root, true)
}

fn missing_excludes(root: &Path) -> Result<Vec<&'static str>> {
    let file = git_exclude_path(root)?;
    let data = if safe_regular(&file)? { read_bounded(&file)? } else { String::new() };
    let lines: Vec<_> = data.lines().map(str::trim).collect();
    Ok(IGNORE_LINES.into_iter().filter(|line| !lines.contains(line)).collect())
}

fn ensure_excludes(root: &Path) -> Result<()> {
    let missing = missing_excludes(root)?;
    if missing.is_empty() { return Ok(()); }
    let path = git_exclude_path(root)?;
    let mut options = OpenOptions::new();
    options.append(true).create(true).mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut file = options.open(&path).map_err(|e| io_error("cannot open Git exclude", e))?;
    let meta = file.metadata().map_err(|e| io_error("cannot stat Git exclude", e))?;
    if !meta.is_file() || meta.uid() != euid() { return Err(error("unsafe Git exclude")); }
    let length = meta.len();
    if length > FILE_LIMIT { return Err(error("Git exclude too large")); }
    if length > 0 {
        let text = read_bounded(&path)?;
        if !text.ends_with('\n') { file.write_all(b"\n").map_err(|e| io_error("cannot append newline", e))?; }
    }
    for line in missing {
        writeln!(file, "{line}").map_err(|e| io_error("cannot append Git exclude", e))?;
    }
    file.sync_all().map_err(|e| io_error("cannot sync Git exclude", e))?;
    Ok(())
}

fn validate_root(project_dir: &Path, directory: &str) -> Result<(PathBuf, PathBuf)> {
    if !project_dir.is_absolute() { return Err(error("project_dir must be absolute")); }
    if !check_directory(project_dir, false)? { return Err(error("project_dir does not exist")); }
    let project_dir = project_dir.canonicalize().map_err(|e| io_error("cannot resolve project_dir", e))?;
    let relative = Path::new(directory);
    validate_relative(relative, true).map_err(|e| error(e.to_string()))?;
    let root = project_dir.join(relative);
    if !check_directory(&root, false)? { return Err(error("project checkout directory does not exist")); }
    let root = root.canonicalize().map_err(|e| io_error("cannot resolve checkout", e))?;
    if root.parent() != Some(project_dir.as_path()) { return Err(error("checkout escapes project_dir")); }
    Ok((project_dir, root))
}

fn validate_input(req: &BootstrapRequest) -> Result<(PathBuf, PathBuf)> {
    validate_id(&req.id).map_err(|e| error(e.to_string()))?;
    check_text(&req.git_name, 100)?;
    check_text(&req.git_email, 200)?;
    if !req.git_email.contains('@') || req.git_email.contains(' ') { return Err(error("invalid Git email")); }
    check_text(&req.codex_name, 80)?;
    if !req.config_path.is_absolute() { return Err(error("config path must be absolute")); }
    let paths = validate_root(&req.project_dir, &req.directory)?;
    validate_git_checkout(&paths.1)?;
    Ok(paths)
}

fn config_text(req: &BootstrapRequest, project_dir: &Path) -> Result<String> {
    Ok(format!(
        "[workspace]\nproject_dir = {}\nrules_dir = \".ai\"\nglobal_context_files = [\"CODEX_GLOBAL.md\", \"BOOTSTRAP.md\", \"PROJECT_DEFAULTS.md\"]\n\n[codex]\nname = {}\n\n[sessions]\nname_template = \"{{project_id}} | {{codex_name}} | {{task}}\"\n\n[git]\nuser_name = {}\nuser_email = {}\n",
        quote(&project_dir.to_string_lossy())?, quote(&req.codex_name)?,
        quote(&req.git_name)?, quote(&req.git_email)?
    ))
}

fn project_text(req: &BootstrapRequest) -> Result<String> {
    Ok(format!("[project]\nid = {}\ndirectory = {}\n\n[contexts]\nfiles = [\"AGENTS.md\", \".ai/.project_codex.md\", \".ai/PROJECT_CONTEXT.md\", \".ai/CODEX_RESULTS.md\"]\n", quote(&req.id)?, quote(&req.directory)?))
}

#[derive(Deserialize)]
struct StoredWorkspace { workspace: StoredWorkspaceSection, git: Option<StoredGitSection> }
#[derive(Deserialize)]
struct StoredWorkspaceSection { project_dir: PathBuf, rules_dir: Option<String> }
#[derive(Deserialize)]
struct StoredGitSection { user_name: Option<String>, user_email: Option<String> }
#[derive(Deserialize)]
struct StoredProject { project: StoredProjectSection }
#[derive(Deserialize)]
struct StoredProjectSection { id: String, directory: String, #[serde(default, rename = "type")] project_type: ProjectType }

fn validate_existing_global(path: &Path, dir: &Path, req: &BootstrapRequest) -> Result<()> {
    if !safe_regular(path)? { return Ok(()); }
    let data: StoredWorkspace = toml::from_str(&read_bounded(path)?).map_err(|_| error("existing global config is invalid"))?;
    if data.workspace.project_dir != dir || data.workspace.rules_dir.as_deref().unwrap_or(".ai") != ".ai" {
        return Err(error("existing global workspace configuration conflicts with bootstrap"));
    }
    if let Some(git) = data.git {
        if git.user_name.as_deref().is_some_and(|s| s != req.git_name.as_str())
            || git.user_email.as_deref().is_some_and(|s| s != req.git_email.as_str()) {
            return Err(error("existing global Git defaults conflict with bootstrap"));
        }
    }
    Ok(())
}

fn validate_existing_project(path: &Path, req: &BootstrapRequest) -> Result<()> {
    if !safe_regular(path)? { return Ok(()); }
    let data: StoredProject = toml::from_str(&read_bounded(path)?).map_err(|_| error("existing project config is invalid"))?;
    if data.project.id != req.id || data.project.directory != req.directory || data.project.project_type != ProjectType::Standard {
        return Err(error("existing project ID or directory conflicts with bootstrap"));
    }
    Ok(())
}

fn safe_config_home(config_path: &Path) -> Result<(PathBuf, PathBuf)> {
    let parent = config_path.parent().ok_or_else(|| error("config has no parent"))?;
    let projects = parent.join("projects");
    if let Some(ancestor) = parent.parent() {
        if lstat(ancestor)?.is_some() && !check_directory(ancestor, false)? {
            return Err(error("unsafe configuration ancestor"));
        }
    }
    if lstat(parent)?.is_some() { check_directory(parent, true)?; }
    if lstat(&projects)?.is_some() { check_directory(&projects, true)?; }
    Ok((parent.to_path_buf(), projects))
}

fn create_config_dirs(parent: &Path, projects: &Path) -> Result<()> {
    if lstat(parent)?.is_none() {
        let base = parent.parent().ok_or_else(|| error("missing config parent"))?;
        if lstat(base)?.is_none() {
            create_private_dir(base)?;
        }
    }
    create_private_dir(parent)?;
    create_private_dir(projects)?;
    Ok(())
}

fn preflight_shared(root: &Path, rules: &Path) -> Result<()> {
    if lstat(rules)?.is_some() { check_directory(rules, false)?; }
    for (name, _) in SHARED_FILES {
        safe_regular(&rules.join(name))?;
    }
    let project_ai = root.join(".ai");
    if lstat(&project_ai)?.is_some() { check_directory(&project_ai, false)?; }
    safe_regular(&root.join("AGENTS.md"))?;
    history_status(root)?;
    safe_regular(&project_ai.join(".project_codex.md"))?;
    safe_regular(&project_ai.join("PROJECT_CONTEXT.md"))?;
    safe_regular(&root.join("PROJECT_CONTEXT.md"))?;
    safe_regular(&root.join("CODEX_RESULTS.md"))?;
    safe_regular(&project_ai.join("CODEX_RESULTS.md"))?;
    Ok(())
}

fn install_shared(root: &Path, rules: &Path, id: &str) -> Result<()> {
    if lstat(rules)?.is_none() { create_private_dir(rules)?; }
    for (name, text) in SHARED_FILES {
        ensure_new_file(&rules.join(name), text, false)?;
    }
    let project_ai = root.join(".ai");
    if lstat(&project_ai)?.is_none() { create_private_dir(&project_ai)?; }
    ensure_new_file(&project_ai.join(".project_codex.md"), PROJECT_OVERRIDE, false)?;
    let project_context = format!("# Project context\n\nProject ID: `{id}`.\n\nDescribe the project's goal and accepted design here.\n");
    let root_context = safe_regular(&root.join("PROJECT_CONTEXT.md"))?;
    if !root_context {
        ensure_new_file(&project_ai.join("PROJECT_CONTEXT.md"), &project_context, false)?;
    }
    let context_link = if root_context { "PROJECT_CONTEXT.md" } else { ".ai/PROJECT_CONTEXT.md" };
    let agent = format!("# Codex project entry point\n\nRead [shared bridge rules](../.ai/CODEX_GLOBAL.md), [local project additions](.ai/.project_codex.md), and [project context]({context_link}) before working. The bridge, not these Markdown files, enforces permissions and workspace preparation.\n");
    ensure_new_file(&root.join("AGENTS.md"), &agent, false)?;
    Ok(())
}

fn preflight_ensure(root: &Path, rules: &Path, id: &str, temp_base: &Path, git_name: Option<&str>, git_email: Option<&str>, standard: bool) -> Result<()> {
    check_tmp_base(temp_base)?;
    // Detect all common conflicts before changing any file.
    preflight_shared(root, rules)?;
    preflight_link(root, id, temp_base, "tmp")?;
    preflight_link(root, id, temp_base, "log")?;
    if lstat(&temp_base.join(id))?.is_some() {
        storage_directory_state(&temp_base.join(id), temp_base)?;
        for kind in storage_kinds(temp_base) {
            if lstat(&temp_base.join(id).join(kind))?.is_some() {
                storage_directory_state(&temp_base.join(id).join(kind), temp_base)?;
            }
        }
    }
    if standard { validate_git_checkout(root)?; missing_excludes(root)?; preflight_git_reserved(root)?; }
    history_status(root)?;
    if let (Some(name), Some(email)) = (git_name, git_email) { preflight_git(root, name, email)?; }
    Ok(())
}

fn do_ensure(root: &Path, rules: &Path, id: &str, temp_base: &Path, git_name: Option<&str>, git_email: Option<&str>, standard: bool) -> Result<()> {
    preflight_ensure(root, rules, id, temp_base, git_name, git_email, standard)?;
    // All of these are idempotent, create-only operations or validated additions.
    ensure_tmp_directory(temp_base, id)?;
    install_shared(root, rules, id)?;
    if standard { ensure_excludes(root)?; }
    ensure_history(root)?;
    migrate_legacy_data(root, id, temp_base)?;
    ensure_link(root, id, temp_base, "tmp")?;
    ensure_link(root, id, temp_base, "log")?;
    if let (Some(name), Some(email)) = (git_name, git_email) { ensure_git_identity(root, name, email)?; }
    Ok(())
}

/// Local-only, explicit first-time registration. No git clone, network or task execution.
pub fn bootstrap(req: &BootstrapRequest) -> Result<()> {
    let base = crate::registry::configured_temp_base(&req.config_path).map_err(|e| error(e.to_string()))?;
    bootstrap_with_tmp(req, &base)
}

fn bootstrap_with_tmp(req: &BootstrapRequest, temp_base: &Path) -> Result<()> {
    let (dir, root) = validate_input(req)?;
    check_tmp_base(temp_base)?;
    let (cfg_dir, projects) = safe_config_home(&req.config_path)?;
    let project_config = projects.join(format!("{}.toml", req.id));
    validate_existing_global(&req.config_path, &dir, req)?;
    validate_existing_project(&project_config, req)?;
    let global = config_text(req, &dir)?;
    let project = project_text(req)?;
    preflight_shared(&root, &dir.join(".ai"))?;
    preflight_link(&root, &req.id, temp_base, "tmp")?;
    preflight_link(&root, &req.id, temp_base, "log")?;
    missing_excludes(&root)?;
    preflight_git(&root, &req.git_name, &req.git_email)?;
    if lstat(&temp_base.join(&req.id))?.is_some() {
        storage_directory_state(&temp_base.join(&req.id), temp_base)?;
        for kind in storage_kinds(temp_base) {
            if lstat(&temp_base.join(&req.id).join(kind))?.is_some() {
                storage_directory_state(&temp_base.join(&req.id).join(kind), temp_base)?;
            }
        }
    }
    create_config_dirs(&cfg_dir, &projects)?;
    ensure_new_file(&req.config_path, &global, true)?;
    ensure_new_file(&project_config, &project, true)?;
    // Re-parse all configurations: catches conflicts with other registered projects.
    let registry = ProjectRegistry::load(&req.config_path).map_err(|e| error(e.to_string()))?;
    let registered = registry.project(&req.id).map_err(|e| error(e.to_string()))?;
    if registered.root != root || registered.project_type != ProjectType::Standard { return Err(error("registered project does not match requested checkout")); }
    do_ensure(&root, &registry.rules_dir, &req.id, temp_base, Some(&req.git_name), Some(&req.git_email), true)
}

/// Adopt an existing checkout without changing its Git author, tracked files or existing AI docs.
/// This is a local CLI-only write capability and must never be callable via read-only MCP.
#[derive(Debug)]
pub struct AdoptReport {
    pub root: PathBuf,
    pub had_agents: bool,
    pub had_context: bool,
    pub already_registered: bool,
}

pub fn adopt(req: &AdoptRequest, dry_run: bool) -> Result<AdoptReport> {
    let base = crate::registry::configured_temp_base(&req.config_path).map_err(|e| error(e.to_string()))?;
    adopt_with_tmp(req, &base, dry_run)
}

fn adopt_with_tmp(req: &AdoptRequest, temp_base: &Path, dry_run: bool) -> Result<AdoptReport> {
    validate_id(&req.id).map_err(|e| error(e.to_string()))?;
    if !req.config_path.is_absolute() { return Err(error("config path must be absolute")); }
    check_tmp_base(temp_base)?;
    let registry = ProjectRegistry::load_for(&req.config_path, &req.id).map_err(|e| error(e.to_string()))?;
    let (project_dir, root) = validate_root(&registry.project_dir, &req.directory)?;
    let (cfg_dir, projects) = safe_config_home(&req.config_path)?;
    let config_file = projects.join(format!("{}.toml", req.id));
    let registered = registry.projects().find(|entry| entry.id.as_str() == req.id.as_str() || entry.root.as_path() == root.as_path());
    if let Some(existing) = registered {
        if existing.id != req.id || existing.root != root || existing.manage_git_identity || existing.project_type != req.project_type {
            return Err(error("project ID or checkout is already registered with different settings"));
        }
    }
    // An invalid or mismatched TOML is never overwritten or auto-healed.
    let already_registered = safe_regular(&config_file)?;
    if already_registered && registered.is_none() { return Err(error("unrecognized existing project config")); }
    if !already_registered && registered.is_some() { return Err(error("project config disappeared")); }
    let had_agents = safe_regular(&root.join("AGENTS.md"))?;
    history_status(&root)?;
    let had_context = safe_regular(&root.join("PROJECT_CONTEXT.md"))?
        || safe_regular(&root.join(".ai/PROJECT_CONTEXT.md"))?;
    let rules = project_dir.join(".ai");
    preflight_ensure(&root, &rules, &req.id, temp_base, None, None, req.project_type == ProjectType::Standard)?;
    if !already_registered {
        let configured = quote(&req.id)?;
        let directory = quote(&req.directory)?;
        let contents = format!(
            "[project]\nid = {configured}\ndirectory = {directory}\ntype = {}\n\n[git]\nidentity = \"preserve\"\n\n[contexts]\nfiles = [\n  \"AGENTS.md\",\n  \".ai/.project_codex.md\",\n  \".ai/PROJECT_CONTEXT.md\",\n  \".ai/CODEX_RESULTS.md\",\n  \"PROJECT_CONTEXT.md\",\n  \"CODEX_RESULTS.md\",\n]\n", quote(req.project_type.as_str())?
        );
        // Validate syntax before touching any project files.
        toml::from_str::<toml::Value>(&contents).map_err(|_| error("invalid generated TOML"))?;
        if !dry_run {
            do_ensure(&root, &rules, &req.id, temp_base, None, None, req.project_type == ProjectType::Standard)?;
            create_config_dirs(&cfg_dir, &projects)?;
            ensure_new_file(&config_file, &contents, true)?;
        }
    } else if !dry_run {
        do_ensure(&root, &rules, &req.id, temp_base, None, None, req.project_type == ProjectType::Standard)?;
    }
    if !dry_run {
        let updated = ProjectRegistry::load_for(&req.config_path, &req.id).map_err(|e| error(e.to_string()))?;
        let project = updated.project(&req.id).map_err(|e| error(e.to_string()))?;
        if project.root != root || project.manage_git_identity || project.project_type != req.project_type { return Err(error("registration postcondition failed")); }
    }
    Ok(AdoptReport { root, had_agents, had_context, already_registered })
}

pub fn ensure(config_path: &Path, project_id: &str) -> Result<()> {
    let base = crate::registry::configured_temp_base(config_path).map_err(|e| error(e.to_string()))?;
    ensure_with_tmp(config_path, project_id, &base)
}

fn ensure_with_tmp(config_path: &Path, id: &str, temp_base: &Path) -> Result<()> {
    let registry = ProjectRegistry::load_for(config_path, id).map_err(|e| error(e.to_string()))?;
    let project = registry.project(id).map_err(|e| error(e.to_string()))?;
    let standard = project.project_type == ProjectType::Standard;
    if standard { validate_git_checkout(&project.root)?; }
    let name = registry.git_user_name.as_deref().filter(|_| standard && project.manage_git_identity);
    let email = registry.git_user_email.as_deref().filter(|_| standard && project.manage_git_identity);
    do_ensure(&project.root, &registry.rules_dir, id, temp_base, name, email, standard)
}

/// Explicit one-way metadata transaction. The operator prepares Git and its
/// excludes first; this operation never initializes Git or edits project data.
pub fn upgrade(config: &Path, id: &str, dry_run: bool) -> Result<()> {
    let base = crate::registry::configured_temp_base(config).map_err(|e| error(e.to_string()))?;
    upgrade_with_tmp(config, id, dry_run, &base)
}

fn upgrade_with_tmp(config: &Path, id: &str, dry_run: bool, temp: &Path) -> Result<()> {
    let validate = || -> crate::screen::Result<()> {
        let registry = ProjectRegistry::load_for(config, id).map_err(|e| crate::screen::err(e.to_string()))?;
        let project = registry.project(id).map_err(|e| crate::screen::err(e.to_string()))?;
        if project.project_type != ProjectType::NonGit {
            return Err(crate::screen::err("upgrade requires a non-git project"));
        }
        let check = || -> Result<()> {
            let name = registry.git_user_name.as_deref().filter(|_| project.manage_git_identity);
            let email = registry.git_user_email.as_deref().filter(|_| project.manage_git_identity);
            preflight_ensure(&project.root, &registry.rules_dir, id, temp, name, email, true)?;
            if !missing_excludes(&project.root)?.is_empty() {
                return Err(error("prepare managed Git excludes before upgrade"));
            }
            Ok(())
        };
        check().map_err(|e| crate::screen::err(e.to_string()))
    };
    crate::project_preference::replace(config, id, dry_run, |raw| {
        let mut doc = raw.parse::<toml_edit::DocumentMut>()
            .map_err(|_| crate::screen::err("invalid project TOML"))?;
        doc["project"]["type"] = toml_edit::value("standard");
        Ok(doc.to_string())
    }, validate).map_err(|e| error(e.to_string()))
}

/// Verify that a versioned managed block is either absent, or has exactly the
/// shipped text. Never silently overwrite locally customized managed text.
fn policy_block_present(
    contents: &str,
    start_marker: &str,
    end_marker: &str,
    expected: &str,
    known_legacy: Option<&str>,
) -> Result<bool> {
    let has_start = contents.contains(start_marker);
    let has_end = contents.contains(end_marker);
    if !has_start && !has_end { return Ok(false); }
    let start = contents.find(start_marker).ok_or_else(|| error("policy markers are inconsistent"))?;
    let end = contents.find(end_marker).ok_or_else(|| error("policy markers are inconsistent"))? + end_marker.len();
    if contents.matches(start_marker).count() != 1 || contents.matches(end_marker).count() != 1 || start >= end {
        return Err(error("policy markers are inconsistent"));
    }
    let actual = &contents[start..end];
    if actual != expected.trim() && !known_legacy.is_some_and(|v| actual == v.trim()) {
        return Err(error("managed policy block has been modified; manual review required"));
    }
    Ok(true)
}

/// Explicit local-only, append-only policy sync. Both recognized versioned
/// blocks are validated. User-authored text outside them is never rewritten.
/// Existing, exactly-known v1 blocks are preserved; v2 adds the approved rules.
/// Returns false only when all current managed blocks are present and valid.
pub fn sync_policy(config_path: &Path) -> Result<bool> {
    let registry = ProjectRegistry::load(config_path).map_err(|e| error(e.to_string()))?;
    if !check_directory(&registry.rules_dir, false)? { return Err(error("shared rules directory missing")); }
    let path = registry.rules_dir.join("CODEX_GLOBAL.md");
    let old = read_bounded(&path)?;
    if !old.starts_with("# Common Codex guidance for bridge-managed projects") {
        return Err(error("shared policy has an unexpected header; manual review required"));
    }
    let has_v1 = policy_block_present(
        &old, POLICY_START, POLICY_END, POLICY_ADDENDUM, Some(POLICY_ADDENDUM_V1_LEGACY),
    )?;
    let has_v2 = policy_block_present(&old, POLICY_V2_START, POLICY_V2_END, POLICY_APPROVAL_V2, None)?;
    let mut additions = Vec::new();
    if !has_v1 { additions.push(POLICY_ADDENDUM.trim()); }
    if !has_v2 { additions.push(POLICY_APPROVAL_V2.trim()); }
    if additions.is_empty() { return Ok(false); }

    let separator = if old.ends_with('\n') { "\n" } else { "\n\n" };
    let append = format!("{separator}{}\n", additions.join("\n\n"));
    if old.len() + append.len() > FILE_LIMIT as usize {
        return Err(error("shared policy would exceed size limit"));
    }
    let mut options = OpenOptions::new();
    options.read(true).append(true).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut file = options.open(&path).map_err(|e| io_error("cannot open shared rules", e))?;
    let meta = file.metadata().map_err(|e| io_error("cannot stat shared rules", e))?;
    let current = fs::symlink_metadata(&path).map_err(|e| io_error("cannot stat shared rules", e))?;
    if !meta.is_file() || meta.uid() != euid() || meta.ino() != current.ino() || meta.dev() != current.dev()
        || meta.len() != old.len() as u64
    {
        return Err(error("shared rules changed during preflight"));
    }
    let mut observed = String::new();
    file.read_to_string(&mut observed).map_err(|e| io_error("cannot re-read shared rules", e))?;
    if observed != old { return Err(error("shared rules changed during preflight")); }
    file.write_all(append.as_bytes()).map_err(|e| io_error("cannot append approved rules", e))?;
    file.sync_all().map_err(|e| io_error("cannot sync shared rules", e))?;
    Ok(true)
}

pub fn health(config_path: &Path, project_id: &str) -> Result<Health> {
    let base = crate::registry::configured_temp_base(config_path).map_err(|e| error(e.to_string()))?;
    health_with_tmp(config_path, project_id, &base)
}

fn health_with_tmp(config_path: &Path, id: &str, temp_base: &Path) -> Result<Health> {
    let registry = ProjectRegistry::load_for(config_path, id).map_err(|e| error(e.to_string()))?;
    let project = registry.project(id).map_err(|e| error(e.to_string()))?;
    check_tmp_base(temp_base)?;
    preflight_shared(&project.root, &registry.rules_dir)?;
    let mut checks = Vec::new();
    let rule_files_ok = SHARED_FILES.iter().all(|(name, _)| safe_regular(&registry.rules_dir.join(name)).unwrap_or(false));
    checks.push(("global_rules", if rule_files_ok { "ok" } else { "missing_or_unsafe" }));
    let ai = project.root.join(".ai");
    let context_ok = safe_regular(&project.root.join("AGENTS.md")).unwrap_or(false)
        && safe_regular(&ai.join(".project_codex.md")).unwrap_or(false)
        && (safe_regular(&ai.join("PROJECT_CONTEXT.md")).unwrap_or(false)
            || safe_regular(&project.root.join("PROJECT_CONTEXT.md")).unwrap_or(false));
    checks.push(("project_ai", if context_ok { "ok" } else { "missing_or_unsafe" }));
    checks.push(("temp_directories", if tmp_directory_state(temp_base, id).unwrap_or(false) { "ok" } else { "missing_or_unsafe" }));
    for kind in ["tmp", "log"] {
        checks.push((if kind == "tmp" { "tmp_link" } else { "log_link" },
            if expected_link_status(&project.root, id, temp_base, kind).unwrap_or(false) { "ok" } else { "missing_or_conflict" }));
    }
    if project.project_type == ProjectType::Standard {
    checks.push(("git_exclude", if missing_excludes(&project.root).is_ok_and(|entries| entries.is_empty()) { "ok" } else { "missing_or_unsafe" }));
    if project.manage_git_identity {
        if let (Some(name), Some(email)) = (registry.git_user_name.as_deref(), registry.git_user_email.as_deref()) {
            let valid = git_local(&project.root, "user.name")?.as_deref() == Some(name)
                && git_local(&project.root, "user.email")?.as_deref() == Some(email);
            checks.push(("git_identity", if valid { "ok" } else { "mismatch" }));
        }
    } else {
        checks.push(("git_identity", "ok")); // Git author settings deliberately untouched.
    }
    }
    checks.push(("bash_history", if history_status(&project.root).unwrap_or(false) { "ok" } else { "missing" }));
    let history_notices = history_notices(&project.root, project.project_type == ProjectType::Standard);
    Ok(Health { project_id: id.to_owned(), root: project.root.clone(), checks, history_notices })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};
    use std::os::unix::fs::PermissionsExt;
    static UNIQUE: AtomicUsize = AtomicUsize::new(0);

    struct Fixture { base: PathBuf, request: BootstrapRequest, tmp: PathBuf }
    impl Fixture {
        fn new() -> Self {
            let unique = UNIQUE.fetch_add(1, Ordering::SeqCst);
            let ticks = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
            let base = std::env::temp_dir().join(format!("chatgpt2codex-workspace-test-{}-{ticks}-{unique}", std::process::id()));
            fs::create_dir(&base).unwrap();
            let project_dir = base.join("mycode");
            fs::create_dir(&project_dir).unwrap();
            let repo = project_dir.join("BridgeRepo");
            fs::create_dir(&repo).unwrap();
            assert!(Command::new("git").args(["init", "-q"]).arg(&repo).status().unwrap().success());
            let tmp = base.join("scratch");
            fs::create_dir(&tmp).unwrap();
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o1777)).unwrap();
            Self { base: base.clone(), tmp, request: BootstrapRequest {
                project_dir, config_path: base.join("config/chatgpt2codex/config.toml"), id: "gpt2codex".into(),
                directory: "BridgeRepo".into(), git_name: "pitriss".into(),
                git_email: "5106460+Pitriss@users.noreply.github.com".into(), codex_name: "Koda".into(),
            }}
        }
        fn repo(&self) -> PathBuf { self.request.project_dir.join(&self.request.directory) }
    }
    impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.base); } }

    #[test]
    fn non_git_lifecycle_dry_run_types_conflicts_and_upgrade() {
        let f = Fixture::new();
        let cfg = f.request.config_path.parent().unwrap();
        fs::create_dir_all(cfg.join("projects")).unwrap();
        fs::set_permissions(cfg, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(cfg.join("projects"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(&f.request.config_path, config_text(&f.request, &f.request.project_dir).unwrap()).unwrap();
        let root = f.request.project_dir.join("plain");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("AGENTS.md"), "keep agents").unwrap();
        fs::write(root.join("PROJECT_CONTEXT.md"), "keep context").unwrap();
        fs::write(root.join(".bash_history"), "keep history").unwrap();
        fs::write(cfg.join("projects/gpt2codex.toml"), project_text(&f.request).unwrap()).unwrap();
        let mut req = adopt_request(&f, "plain", "plain");
        req.project_type = ProjectType::NonGit;
        GIT_FORBIDDEN.with(|b| b.set(true));
        assert!(adopt_with_tmp(&req, &f.tmp, true).is_ok());
        assert!(!root.join(".ai").exists());
        assert!(!root.join(".tmp").exists());
        assert!(!cfg.join("projects/plain.toml").exists());
        adopt_with_tmp(&req, &f.tmp, false).unwrap();
        ensure_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap();
        assert!(health_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap().ready());
        fs::remove_dir_all(f.tmp.join("plain")).unwrap();
        assert!(!health_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap().ready());
        ensure_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap();
        assert!(health_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap().ready());
        assert!(!root.join(".git").exists());
        assert_eq!(fs::read_to_string(root.join(".bash_history")).unwrap(), "keep history");
        assert_eq!(fs::read_to_string(root.join("PROJECT_CONTEXT.md")).unwrap(), "keep context");
        assert_eq!(fs::read_to_string(root.join("AGENTS.md")).unwrap(), "keep agents");
        let mut duplicate = adopt_request(&f, "other", "plain");
        duplicate.project_type = ProjectType::NonGit;
        assert!(adopt_with_tmp(&duplicate, &f.tmp, false).is_err());
        GIT_FORBIDDEN.with(|b| b.set(false));
        let path = cfg.join("projects/plain.toml");
        let original = fs::read(&path).unwrap();
        for dry in [true, false] {
            assert!(upgrade_with_tmp(&f.request.config_path, "plain", dry, &f.tmp).is_err());
            assert_eq!(fs::read(&path).unwrap(), original);
        }
        assert!(Command::new("git").args(["init", "-q"]).arg(&root).status().unwrap().success());
        assert!(upgrade_with_tmp(&f.request.config_path, "plain", false, &f.tmp).is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        GIT_FORBIDDEN.with(|b| b.set(true));
        ensure_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap();
        assert!(health_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap().ready());
        GIT_FORBIDDEN.with(|b| b.set(false));
        assert!(!missing_excludes(&root).unwrap().is_empty());
        ensure_excludes(&root).unwrap();
        upgrade_with_tmp(&f.request.config_path, "plain", true, &f.tmp).unwrap();
        assert_eq!(fs::read(&path).unwrap(), original);
        let pending = cfg.join(format!("projects/.plain-mux-{}.pending", std::process::id()));
        fs::write(&pending, "collision").unwrap();
        assert!(upgrade_with_tmp(&f.request.config_path, "plain", false, &f.tmp).is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        fs::remove_file(pending).unwrap();
        let validations = std::cell::Cell::new(0);
        assert!(crate::project_preference::replace(&f.request.config_path, "plain", false,
            |raw| Ok(raw.replace("non-git", "standard")), || {
                validations.set(validations.get() + 1);
                if validations.get() == 2 { Err(crate::screen::err("injected late preflight failure")) } else { Ok(()) }
            }).is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        assert!(!cfg.join(format!("projects/.plain-mux-{}.pending", std::process::id())).exists());
        crate::project_preference::FAIL_DIRECTORY_SYNC.with(|flag| flag.set(true));
        assert!(upgrade_with_tmp(&f.request.config_path, "plain", false, &f.tmp).unwrap_err().to_string().contains("original project config restored"));
        assert_eq!(fs::read(&path).unwrap(), original);
        upgrade_with_tmp(&f.request.config_path, "plain", false, &f.tmp).unwrap();
        assert_eq!(ProjectRegistry::load(&f.request.config_path).unwrap().project("plain").unwrap().project_type, ProjectType::Standard);
        assert!(health_with_tmp(&f.request.config_path, "plain", &f.tmp).unwrap().ready());
        assert!(upgrade_with_tmp(&f.request.config_path, "plain", false, &f.tmp).is_err());
        assert_eq!(fs::read_to_string(root.join(".bash_history")).unwrap(), "keep history");
    }

    #[test]
    fn bootstrap_self_repo_creates_expected_paths_and_git_identity() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = f.repo();
        assert_eq!(fs::read_link(repo.join(".tmp")).unwrap(), f.tmp.join("gpt2codex/tmp"));
        assert_eq!(fs::read_link(repo.join(".log")).unwrap(), f.tmp.join("gpt2codex/log"));
        let storage = f.tmp.join("gpt2codex");
        assert!(fs::symlink_metadata(&storage).unwrap().is_dir());
        for kind in ["tmp", "log", "rel"] {
            assert!(fs::symlink_metadata(storage.join(kind)).unwrap().is_dir());
        }
        assert!(f.request.project_dir.join(".ai/CODEX_GLOBAL.md").is_file());
        assert!(repo.join(".ai/.project_codex.md").is_file());
        assert!(repo.join("AGENTS.md").is_file());
        assert!(missing_excludes(&repo).unwrap().is_empty());
        let registry = ProjectRegistry::load(&f.request.config_path).unwrap();
        assert!(registry.project("gpt2codex").is_ok());
        assert!(registry.project("chatgpt2codex-debian").is_err());
        assert_eq!(git_local(&repo, "user.name").unwrap().as_deref(), Some("pitriss"));
        assert!(health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap().ready());
    }

    #[test]
    fn bootstrap_is_idempotent_preserves_user_files_and_repairs_cleaned_tmp() {
        let f = Fixture::new();
        let context = f.repo().join(".ai/PROJECT_CONTEXT.md");
        fs::create_dir(f.repo().join(".ai")).unwrap();
        fs::write(&context, "my own project context").unwrap();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        assert_eq!(fs::read_to_string(&context).unwrap(), "my own project context");
        fs::remove_dir_all(f.tmp.join("gpt2codex")).unwrap();
        assert!(!health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap().ready());
        ensure_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert!(health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap().ready());
    }

    #[test]
    fn wrong_symlink_is_rejected_without_changing_existing_link() {
        let f = Fixture::new();
        symlink("/etc", f.repo().join(".tmp")).unwrap();
        assert!(bootstrap_with_tmp(&f.request, &f.tmp).is_err());
        assert_eq!(fs::read_link(f.repo().join(".tmp")).unwrap(), PathBuf::from("/etc"));
        assert!(!f.request.config_path.exists());
    }

    #[test]
    fn operator_managed_storage_preserves_existing_permissions() {
        let f = Fixture::new();
        let storage = f.tmp.join("gpt2codex");
        fs::create_dir(&storage).unwrap();
        fs::set_permissions(&storage, fs::Permissions::from_mode(0o755)).unwrap();
        // The default /tmp remains strict; an operator-managed base is not chmod'ed.
        assert!(storage_directory_state(&storage, Path::new("/tmp")).is_err());
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        assert_eq!(fs::symlink_metadata(&storage).unwrap().mode() & 0o777, 0o755);
        assert!(f.request.config_path.exists());
    }

    #[test]
    fn conflicting_git_identity_is_not_overwritten() {
        let f = Fixture::new();
        let root = f.repo();
        assert!(git_call(&root, &["config", "--local", "user.name", "Other"]).unwrap().status.success());
        assert!(bootstrap_with_tmp(&f.request, &f.tmp).is_err());
        assert_eq!(git_local(&root, "user.name").unwrap().as_deref(), Some("Other"));
        assert!(!f.request.config_path.exists());
    }

    #[test]
    fn rejects_unsafe_project_id_and_symlinked_project_dir() {
        let mut f = Fixture::new();
        f.request.id = "../../evil".into();
        assert!(bootstrap_with_tmp(&f.request, &f.tmp).is_err());
        f.request.id = "gpt2codex".into();
        let fake = f.base.join("alias");
        symlink(&f.request.project_dir, &fake).unwrap();
        f.request.project_dir = fake;
        assert!(bootstrap_with_tmp(&f.request, &f.tmp).is_err());
    }

    #[test]
    fn reserved_tracked_path_is_rejected() {
        let f = Fixture::new();
        fs::write(f.repo().join(".tmp"), "tracked").unwrap();
        assert!(git_call(&f.repo(), &["add", ".tmp"]).unwrap().status.success());
        assert!(bootstrap_with_tmp(&f.request, &f.tmp).is_err());
    }

    fn sibling(f: &Fixture, directory: &str) -> PathBuf {
        let root = f.request.project_dir.join(directory);
        fs::create_dir(&root).unwrap();
        assert!(Command::new("git").args(["init", "-q"]).arg(&root).status().unwrap().success());
        root
    }

    fn adopt_request(f: &Fixture, id: &str, directory: &str) -> AdoptRequest {
        AdoptRequest { project_type: ProjectType::Standard, config_path: f.request.config_path.clone(), id: id.into(), directory: directory.into() }
    }

    #[test]
    fn adopt_preserves_legacy_context_git_author_and_agents() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = sibling(&f, "k2-smb");
        fs::create_dir(repo.join(".ai")).unwrap();
        fs::write(repo.join("AGENTS.md"), "existing instructions\n").unwrap();
        fs::write(repo.join("PROJECT_CONTEXT.md"), "original legacy context\n").unwrap();
        fs::write(repo.join(".ai/NOTES.md"), "project-owned notes\n").unwrap();
        assert!(git_call(&repo, &["config", "--local", "user.name", "Legacy Developer"]).unwrap().status.success());
        assert!(git_call(&repo, &["config", "--local", "user.email", "legacy@example.invalid"]).unwrap().status.success());
        let before = read_bounded(&repo.join("AGENTS.md")).unwrap();
        let request = adopt_request(&f, "k2-smb", "k2-smb");
        let preview = adopt_with_tmp(&request, &f.tmp, true).unwrap();
        assert!(preview.had_agents && preview.had_context && !preview.already_registered);
        assert!(!repo.join(".tmp").exists());
        assert!(!f.request.config_path.parent().unwrap().join("projects/k2-smb.toml").exists());
        assert!(!f.tmp.join("k2-smb").exists());
        let created = adopt_with_tmp(&request, &f.tmp, false).unwrap();
        assert!(!created.already_registered);
        assert_eq!(read_bounded(&repo.join("AGENTS.md")).unwrap(), before);
        assert_eq!(read_bounded(&repo.join("PROJECT_CONTEXT.md")).unwrap(), "original legacy context\n");
        assert!(!repo.join(".ai/PROJECT_CONTEXT.md").exists());
        assert_eq!(read_bounded(&repo.join(".ai/NOTES.md")).unwrap(), "project-owned notes\n");
        assert_eq!(git_local(&repo, "user.name").unwrap().as_deref(), Some("Legacy Developer"));
        assert_eq!(git_local(&repo, "user.email").unwrap().as_deref(), Some("legacy@example.invalid"));
        assert_eq!(fs::read_link(repo.join(".tmp")).unwrap(), f.tmp.join("k2-smb/tmp"));
        assert_eq!(fs::read_link(repo.join(".log")).unwrap(), f.tmp.join("k2-smb/log"));
        let registry = ProjectRegistry::load(&f.request.config_path).unwrap();
        let project = registry.project("k2-smb").unwrap();
        assert!(!project.manage_git_identity);
        assert!(registry.contexts("k2-smb").unwrap().iter().any(|entry| entry.path == PathBuf::from("PROJECT_CONTEXT.md") && entry.status == "available"));
        assert!(health_with_tmp(&f.request.config_path, "k2-smb", &f.tmp).unwrap().ready());
        ensure_with_tmp(&f.request.config_path, "k2-smb", &f.tmp).unwrap();
        assert!(adopt_with_tmp(&request, &f.tmp, false).unwrap().already_registered);
    }

    #[test]
    fn adopt_creates_missing_context_without_touching_other_repos() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = sibling(&f, "reddit2tg");
        let request = adopt_request(&f, "reddit2tg", "reddit2tg");
        adopt_with_tmp(&request, &f.tmp, false).unwrap();
        let new_agent = read_bounded(&repo.join("AGENTS.md")).unwrap();
        assert!(new_agent.contains(".ai/PROJECT_CONTEXT.md"));
        assert!(repo.join(".ai/PROJECT_CONTEXT.md").is_file());
        let registry = ProjectRegistry::load(&f.request.config_path).unwrap();
        assert_eq!(registry.projects().count(), 2);
        assert!(registry.project("gpt2codex").unwrap().manage_git_identity);
        assert!(health_with_tmp(&f.request.config_path, "reddit2tg", &f.tmp).unwrap().ready());
    }

    #[test]
    fn adopt_detects_conflicting_symlink_before_any_project_writes() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = sibling(&f, "m365-tui");
        symlink("/etc", repo.join(".log")).unwrap();
        let request = adopt_request(&f, "m365-tui", "m365-tui");
        assert!(adopt_with_tmp(&request, &f.tmp, true).is_err());
        assert!(adopt_with_tmp(&request, &f.tmp, false).is_err());
        assert_eq!(fs::read_link(repo.join(".log")).unwrap(), PathBuf::from("/etc"));
        assert!(!repo.join("AGENTS.md").exists());
        assert!(!f.tmp.join("m365-tui").exists());
        assert!(!f.request.config_path.parent().unwrap().join("projects/m365-tui.toml").exists());
    }

    #[test]
    fn adopt_rejects_duplicate_registration_and_unsafe_legacy_context() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = sibling(&f, "k2-smb");
        adopt_with_tmp(&adopt_request(&f, "k2-smb", "k2-smb"), &f.tmp, false).unwrap();
        assert!(adopt_with_tmp(&adopt_request(&f, "another-id", "k2-smb"), &f.tmp, false).is_err());
        assert!(!f.request.config_path.parent().unwrap().join("projects/another-id.toml").exists());
        let other = sibling(&f, "m365-tui");
        symlink("/etc/passwd", other.join("PROJECT_CONTEXT.md")).unwrap();
        assert!(adopt_with_tmp(&adopt_request(&f, "m365-tui", "m365-tui"), &f.tmp, false).is_err());
        assert!(!other.join(".ai").exists());
        assert!(!other.join("AGENTS.md").exists());
        assert!(repo.join("AGENTS.md").is_file());
    }

    #[test]
    fn adopted_project_ensure_does_not_force_global_git_identity() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let repo = sibling(&f, "m365-tui");
        assert!(git_call(&repo, &["config", "--local", "user.name", "Different Person"]).unwrap().status.success());
        adopt_with_tmp(&adopt_request(&f, "m365-tui", "m365-tui"), &f.tmp, false).unwrap();
        fs::remove_dir_all(f.tmp.join("m365-tui")).unwrap();
        ensure_with_tmp(&f.request.config_path, "m365-tui", &f.tmp).unwrap();
        assert_eq!(git_local(&repo, "user.name").unwrap().as_deref(), Some("Different Person"));
        assert!(health_with_tmp(&f.request.config_path, "m365-tui", &f.tmp).unwrap().ready());
    }

    #[test]
    fn managed_bash_history_is_private_ignored_and_preserved() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let history = f.repo().join(".bash_history");
        assert!(history.is_file());
        assert_eq!(fs::symlink_metadata(&history).unwrap().mode() & 0o777, 0o600);
        assert!(git_call(&f.repo(), &["check-ignore", "-q", ".bash_history"]).unwrap().status.success());
        fs::write(&history, "private shell entry\n").unwrap();
        ensure_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert_eq!(read_bounded(&history).unwrap(), "private shell entry\n");
        assert!(health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap().ready());
    }

    #[test]
    fn user_owned_history_symlink_is_preserved_and_not_blocking() {
        let f = Fixture::new();
        let history = f.repo().join(".bash_history");
        let target = f.repo().join("external-history");
        fs::write(&target, "original history\n").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o664)).unwrap();
        symlink(&target, &history).unwrap();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        ensure_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert_eq!(fs::read_link(&history).unwrap(), target);
        assert_eq!(fs::read_to_string(&target).unwrap(), "original history\n");
        assert_eq!(fs::symlink_metadata(&target).unwrap().mode() & 0o777, 0o664);
        let health = health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert!(health.ready());
        assert!(health.history_notices.contains(&"symlink_preserved"));
    }

    #[test]
    fn broad_permissions_and_git_tracked_history_are_only_notices() {
        let f = Fixture::new();
        let history = f.repo().join(".bash_history");
        fs::write(&history, "personal history\n").unwrap();
        fs::set_permissions(&history, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(git_call(&f.repo(), &["add", "-f", ".bash_history"]).unwrap().status.success());
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        ensure_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert_eq!(fs::read_to_string(&history).unwrap(), "personal history\n");
        assert_eq!(fs::symlink_metadata(&history).unwrap().mode() & 0o777, 0o644);
        let health = health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert!(health.ready());
        assert!(health.history_notices.contains(&"permissions_preserved"));
        assert!(health.history_notices.contains(&"git_tracked_not_untracked"));
    }

    #[test]
    fn dangling_user_symlink_is_not_followed_or_replaced() {
        let f = Fixture::new();
        let history = f.repo().join(".bash_history");
        symlink("../not-created-yet", &history).unwrap();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        ensure_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap();
        assert_eq!(fs::read_link(&history).unwrap(), PathBuf::from("../not-created-yet"));
        assert!(health_with_tmp(&f.request.config_path, "gpt2codex", &f.tmp).unwrap().ready());
    }

    #[test]
    fn additive_global_policy_sync_is_idempotent_and_detects_drift() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let global = f.request.project_dir.join(".ai/CODEX_GLOBAL.md");
        let installed = read_bounded(&global).unwrap();
        // Fresh installations include both versioned managed blocks.
        assert!(installed.contains(POLICY_V2_START));
        assert!(installed.contains("Před zahájením dosud neschválených změn"));
        assert!(!sync_policy(&f.request.config_path).unwrap());
        assert_eq!(read_bounded(&global).unwrap(), installed);

        // Old installations with neither managed block receive both once.
        let old = installed.split(POLICY_START).next().unwrap().trim_end().to_owned() + "\n";
        fs::write(&global, &old).unwrap();
        assert!(sync_policy(&f.request.config_path).unwrap());
        let merged = read_bounded(&global).unwrap();
        assert!(merged.starts_with(old.as_str()));
        assert!(merged.contains("Mandatory context preflight"));
        assert!(merged.contains("Preserve established workflows"));
        assert_eq!(merged.matches(POLICY_START).count(), 1);
        assert_eq!(merged.matches(POLICY_V2_START).count(), 1);
        assert!(!sync_policy(&f.request.config_path).unwrap());
        assert_eq!(read_bounded(&global).unwrap(), merged);

        // A local edit inside either managed block must stop the sync.
        let altered = merged.replace("Communicate with the user in Czech", "Communicate with the user in English");
        assert_ne!(altered, merged);
        fs::write(&global, altered).unwrap();
        assert!(sync_policy(&f.request.config_path).is_err());
        fs::write(&global, merged.replace("Neobvykle široká oprávnění", "Změněná oprávnění")).unwrap();
        assert!(sync_policy(&f.request.config_path).is_err());
    }

    #[test]
    fn policy_sync_preserves_approved_v1_and_user_notes() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let global = f.request.project_dir.join(".ai/CODEX_GLOBAL.md");
        let installed = read_bounded(&global).unwrap();
        let v1_only = installed.split(POLICY_V2_START).next().unwrap().trim_end().to_owned();
        let prior = format!("{v1_only}\n\n# Own notes\nLeave this paragraph unchanged.\n");
        fs::write(&global, &prior).unwrap();
        assert!(sync_policy(&f.request.config_path).unwrap());
        let updated = read_bounded(&global).unwrap();
        assert!(updated.starts_with(&prior));
        assert_eq!(updated.matches(POLICY_V2_START).count(), 1);
        assert!(!sync_policy(&f.request.config_path).unwrap());
        assert_eq!(updated, read_bounded(&global).unwrap());
    }

    #[test]
    fn policy_sync_accepts_exact_legacy_v1_without_overwriting_it() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let global = f.request.project_dir.join(".ai/CODEX_GLOBAL.md");
        let installed = read_bounded(&global).unwrap();
        let prefix = installed.split(POLICY_START).next().unwrap();
        let prior = format!("{prefix}{}\n\n# Additional user instructions\nKeep this text.\n", POLICY_ADDENDUM_V1_LEGACY.trim());
        fs::write(&global, &prior).unwrap();
        assert!(sync_policy(&f.request.config_path).unwrap());
        let updated = read_bounded(&global).unwrap();
        assert!(updated.starts_with(&prior));
        assert!(updated.contains("Před zahájením dosud neschválených změn"));
        assert!(!sync_policy(&f.request.config_path).unwrap());
    }

    #[test]
    fn policy_sync_rejects_partial_markers_before_writing() {
        let f = Fixture::new();
        bootstrap_with_tmp(&f.request, &f.tmp).unwrap();
        let global = f.request.project_dir.join(".ai/CODEX_GLOBAL.md");
        let installed = read_bounded(&global).unwrap();
        for marker in [POLICY_END, POLICY_V2_END] {
            let altered = installed.replace(marker, "");
            fs::write(&global, &altered).unwrap();
            assert!(sync_policy(&f.request.config_path).is_err());
            assert_eq!(altered, read_bounded(&global).unwrap());
        }
    }


}
