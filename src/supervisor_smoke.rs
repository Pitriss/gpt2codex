//! Operator-run, fixed dummy lifecycle using the actual reservation kernel.
//! Deliberately separate from production start/release: no arbitrary command,
//! adoption, recovery, Codex, registry discovery, or runtime enable switch.
use super::*;
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

fn io<T>(value: std::io::Result<T>) -> Result<T> {
    value.map_err(|_| "synthetic_io_failure: inspect retained scratch manually")
}
fn write_new(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut f = io(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path))?;
    io(f.write_all(&serde_json::to_vec(value).map_err(|_| "synthetic_json")?))?;
    io(f.sync_all())
}
fn wait_until(mut predicate: impl FnMut() -> Result<bool>) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(8);
    loop {
        if predicate()? {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("synthetic_timeout: inspect retained scratch manually");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Dummy {
    process: ProcessIdentity,
    window: String,
    session: String,
    token: String,
    group: u32,
    parent: u32,
    tty: i64,
}
fn process_fields(pid: u32) -> Result<(u32, u32, i64)> {
    let stat = io(fs::read_to_string(format!("/proc/{pid}/stat")))?;
    let f: Vec<_> = stat
        .rsplit_once(')')
        .ok_or("synthetic_stat")?
        .1
        .split_whitespace()
        .collect();
    Ok((
        f[1].parse().map_err(|_| "synthetic_stat")?,
        f[2].parse().map_err(|_| "synthetic_stat")?,
        f[4].parse().map_err(|_| "synthetic_stat")?,
    ))
}
fn children(pid: u32) -> Result<Vec<u32>> {
    io(fs::read_to_string(format!(
        "/proc/{pid}/task/{pid}/children"
    )))?
    .split_whitespace()
    .map(|s| s.parse().map_err(|_| "synthetic_children"))
    .collect()
}
fn dummy(base: &Path, role: &str, token: &str) -> Result<()> {
    if !matches!(role, "anchor" | "job")
        || token.len() != 64
        || !token.bytes().all(|b| b.is_ascii_hexdigit())
        || !base.is_absolute()
        || base.canonicalize().ok().as_deref() != Some(base)
        || base
            != io(Path::new(env!("CARGO_MANIFEST_DIR"))
                .join(".tmp")
                .canonicalize())?
            .join(format!("s23-{}", &token[..16]))
    {
        return Err("synthetic_dummy_invalid");
    }
    let me = ProcessIdentity::read(std::process::id())?;
    let (parent, group, tty) = process_fields(me.pid)?;
    let ready = Dummy {
        process: me,
        window: std::env::var("WINDOW").map_err(|_| "synthetic_window")?,
        session: std::env::var("STY").map_err(|_| "synthetic_session")?,
        token: token.into(),
        group,
        parent,
        tty,
    };
    write_new(&base.join(format!("{role}.ready")), &ready)?;
    // Fixed, single-process dummy; no fork/exec, network, or real task. A crash
    // leaves the reservation intact. TTL bounds orphan dummy lifetime only.
    let deadline = Instant::now() + Duration::from_secs(120);
    while !base.join(format!("{role}.exit")).exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Session {
    root: PathBuf,
    server: ProcessIdentity,
    full: String,
    socket: PathBuf,
    device: u64,
    inode: u64,
    directory_device: u64,
    directory_inode: u64,
}
impl Session {
    fn verify(&self) -> Result<()> {
        if self.server.verify() != ProcessState::Live {
            return Err("synthetic_server_identity_mismatch");
        }
        if io(self.root.canonicalize())? != self.root {
            return Err("synthetic_root_identity_mismatch");
        }
        let exe = io(fs::metadata(format!("/proc/{}/exe", self.server.pid)))?;
        let screen = io(fs::metadata("/usr/bin/screen"))?;
        if (exe.dev(), exe.ino()) != (screen.dev(), screen.ino()) {
            return Err("synthetic_server_executable");
        }
        let directory = self.socket.parent().ok_or("synthetic_socket_path")?;
        let dir = io(fs::symlink_metadata(directory))?;
        if !dir.is_dir()
            || dir.uid() != self.server.uid
            || dir.mode() & 0o077 != 0
            || (dir.dev(), dir.ino()) != (self.directory_device, self.directory_inode)
            || io(directory.canonicalize())? != directory
        {
            return Err("synthetic_socket_directory_mismatch");
        }
        let m = io(fs::symlink_metadata(&self.socket))?;
        if !m.file_type().is_socket()
            || m.uid() != self.server.uid
            || (m.dev(), m.ino()) != (self.device, self.inode)
        {
            return Err("synthetic_socket_identity_mismatch");
        }
        // Private fresh socket, nonblocking connect; peer must be our direct
        // foreground Child generation. No scan or connection to user sockets.
        verify_peer(&self.socket, &self.server)?;
        if self.server.verify() != ProcessState::Live {
            return Err("synthetic_server_identity_mismatch");
        }
        let after = io(fs::symlink_metadata(&self.socket))?;
        if (after.dev(), after.ino()) != (self.device, self.inode) {
            return Err("synthetic_socket_identity_mismatch");
        }
        Ok(())
    }
}
fn verify_peer(path: &Path, server: &ProcessIdentity) -> Result<()> {
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;
    let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    addr.sun_family = libc::AF_UNIX as _;
    let bytes = path.as_os_str().as_bytes();
    if bytes.len() >= addr.sun_path.len() || bytes.contains(&0) {
        return Err("synthetic_socket_path");
    }
    for (to, from) in addr.sun_path.iter_mut().zip(bytes) {
        *to = *from as _;
    }
    let fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err("NOT RUN / BLOCKED: AF_UNIX unavailable");
    }
    let stream = unsafe { UnixStream::from_raw_fd(fd) };
    if unsafe {
        libc::connect(
            fd,
            (&addr as *const libc::sockaddr_un).cast(),
            std::mem::size_of_val(&addr) as _,
        )
    } != 0
    {
        return Err("synthetic_socket_connect");
    }
    let mut peer: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of_val(&peer) as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut peer as *mut libc::ucred).cast(),
            &mut len,
        )
    } != 0
        || len as usize != std::mem::size_of_val(&peer)
        || peer.pid as u32 != server.pid
        || peer.uid != server.uid
    {
        return Err("synthetic_socket_peer_mismatch");
    }
    Ok(())
}

fn verify_record(path: &Path, expected: &Session) -> Result<()> {
    let m = io(fs::symlink_metadata(path))?;
    if !m.is_file()
        || m.uid() != unsafe { libc::geteuid() }
        || m.nlink() != 1
        || m.mode() & 0o077 != 0
        || m.len() > 16384
    {
        return Err("synthetic_session_record_unsafe");
    }
    let value: Session = serde_json::from_slice(&io(fs::read(path))?)
        .map_err(|_| "synthetic_session_record_invalid")?;
    if value != *expected {
        return Err("synthetic_session_record_changed");
    }
    Ok(())
}
/// Capability is constructed only around a newly created synthetic Child. It
/// cannot authorize any production start, supplied command, or existing screen.
struct SyntheticLifecycle {
    base: PathBuf,
    root: PathBuf,
    store: Supervisor,
    server: Child,
    session: Session,
    anchor: Dummy,
    token: String,
}
impl SyntheticLifecycle {
    fn verify_scope(&self) -> Result<()> {
        let expected = format!("{}.s23-{}", self.server.id(), &self.token[..16]);
        if self.session.server.pid != self.server.id()
            || self.session.full != expected
            || self.session.socket != self.base.join("s").join(expected)
            || self.root != self.base.join("project")
        {
            return Err("synthetic_control_scope_mismatch");
        }
        Ok(())
    }
    fn job_title(&self) -> String {
        format!("j23-{}", &self.token[..12])
    }
    fn verify_record(&self) -> Result<()> {
        if self.session.root != self.root {
            return Err("synthetic_root_identity_mismatch");
        }
        verify_record(&self.root.join(".ai/session.json"), &self.session)
    }
    fn screen(&self, args: &[&str]) -> Result<String> {
        self.verify_scope()?;
        self.verify_record()?;
        self.session.verify()?; // Revalidate immediately before every command.
        let mut command = isolated_command(&self.base);
        command.args(["-S", &self.session.full]).args(args);
        let mut child = io(command.stdout(Stdio::piped()).spawn())?;
        let status = wait_child(&mut child)?;
        if !status.success() {
            return Err("synthetic_screen_command_failed");
        }
        let mut out = String::new();
        if let Some(mut stdout) = child.stdout.take() {
            io(stdout.read_to_string(&mut out))?;
        }
        self.session.verify()?;
        Ok(out.trim().to_string())
    }
    fn observe(&self, role: &str, window: &str) -> Result<Dummy> {
        let data = io(fs::read(self.base.join(format!("{role}.ready"))))?;
        let d: Dummy = serde_json::from_slice(&data).map_err(|_| "synthetic_handshake")?;
        verify_dummy(&d, &self.session, &self.token, window, &self.root)?;
        let title = if window == "0" {
            "synthetic-anchor".into()
        } else {
            self.job_title()
        };
        if !self.inventory()?.contains(&format!("{window}:{title}")) {
            return Err("synthetic_window_mismatch");
        }
        Ok(d)
    }
    fn inventory(&self) -> Result<Vec<String>> {
        // Explicit format removes status flags and avoids truncated default output.
        let output = self.screen(&["-Q", "windows", "%n:%t "])?;
        Ok(output.split_whitespace().map(String::from).collect())
    }
    fn verify_tree(&self, job: Option<&Dummy>) -> Result<()> {
        self.session.verify()?;
        verify_dummy(&self.anchor, &self.session, &self.token, "0", &self.root)?;
        let mut expected = vec![self.anchor.process.pid];
        if let Some(j) = job {
            verify_dummy(j, &self.session, &self.token, "1", &self.root)?;
            if j.tty == self.anchor.tty {
                return Err("synthetic_tty_mismatch");
            }
            expected.push(j.process.pid);
        }
        expected.sort();
        let mut actual = children(self.session.server.pid)?;
        actual.sort();
        let mut expected_windows = vec!["0:synthetic-anchor".to_string()];
        if job.is_some() {
            expected_windows.push(format!("1:{}", self.job_title()));
        }
        if self.inventory()? != expected_windows {
            return Err("synthetic_window_inventory_mismatch");
        }
        if actual != expected {
            return Err("synthetic_unexpected_process_tree");
        }
        Ok(())
    }
    fn start(&self, claim: &Claim) -> Result<Dummy> {
        self.store.poll_verify(claim)?;
        self.verify_tree(None)?;
        if self.observe("anchor", "0")? != self.anchor {
            return Err("synthetic_anchor_changed");
        }
        let exe = io(std::env::current_exe())?;
        let exe = exe.to_str().ok_or("synthetic_executable_path")?;
        self.screen(&[
            "-X",
            "screen",
            "-t",
            &self.job_title(),
            "1",
            exe,
            "supervisor",
            "synthetic-dummy",
            self.base.to_str().ok_or("synthetic_path")?,
            "job",
            &self.token,
        ])?;
        wait_until(|| Ok(self.base.join("job.ready").exists()))?;
        let job = self.observe("job", "1")?;
        self.verify_tree(Some(&job))?;
        write_new(
            &self.root.join(".ai/job-binding.json"),
            &(claim, &self.session, &job),
        )?;
        println!("PASS start_verify: project/root + claim nonce + issue/SHA + server/socket + window + child/group bound; synthetic UUID NOT APPLICABLE");
        Ok(job)
    }
    fn finish_release(&self, claim: &Claim, job: &Dummy) -> Result<()> {
        self.store.poll_verify(claim)?;
        let binding: (Claim, Session, Dummy) =
            serde_json::from_slice(&io(fs::read(self.root.join(".ai/job-binding.json")))?)
                .map_err(|_| "synthetic_job_binding_invalid")?;
        if binding != (claim.clone(), self.session.clone(), job.clone()) {
            return Err("synthetic_job_binding_changed");
        }
        self.verify_tree(Some(job))?;
        if self.observe("job", "1")? != *job {
            return Err("synthetic_job_changed");
        }
        // Exercise precise window control, then let the verified single-process
        // dummy exit cooperatively. Never kill a server or arbitrary process.
        self.screen(&["-p", "1", "-X", "title", &self.job_title()])?;
        write_new(&self.base.join("job.exit"), &self.token)?;
        wait_until(|| Ok(job.process.verify() == ProcessState::Gone))?;
        self.verify_tree(None)?;
        if self.observe("anchor", "0")? != self.anchor {
            return Err("synthetic_anchor_changed");
        }
        write_new(
            &self.root.join(".ai/completed.json"),
            &(claim, &self.session, job, "gone_anchor_retained"),
        )?;
        // Private fixture-only positive release. Production finish_and_release
        // remains deny-only. Recheck full claim and all exit evidence under flock.
        self.store.locked(true, || {
            let mut j = self.store.load()?;
            if !j.claims.iter().any(|c| c == claim) || job.process.verify() != ProcessState::Gone {
                return Err("synthetic_release_refused");
            }
            self.verify_tree(None)?;
            j.claims.retain(|c| c != claim);
            self.store.persist(&j)
        })?;
        if self.store.inspect("synthetic", &self.root)?.is_some() {
            return Err("synthetic_release_failed");
        }
        println!("PASS finish_release: own job gone, anchor/session retained, reservation released after exit proof");
        Ok(())
    }
    fn run(&mut self) -> Result<()> {
        if self.store.inspect("synthetic", &self.root)?.is_some() {
            return Err("synthetic_initial_busy");
        }
        self.verify_tree(None)?;
        let claim = self.store.claim(
            "synthetic",
            &self.root,
            Owner::Queue,
            Some(IssueBinding {
                repository: "synthetic/fixture".into(),
                issue_id: 23,
                number: 23,
                sha: "0".repeat(64),
            }),
        )?;
        let job = self.start(&claim)?;
        // Independent descriptor/instance, exactly the common claim API.
        for owner in [Owner::Manual, Owner::Queue] {
            let second = Supervisor::open(&self.root.join(".ai/state"))?;
            if second
                .claim("synthetic", &self.root, owner, claim.issue.clone())
                .err()
                != Some("project_busy")
            {
                return Err("synthetic_second_claim_not_blocked");
            }
        }
        println!("PASS project_exclusion: second manual and queue claims project_busy while job live; modes share this API");
        self.finish_release(&claim, &job)?;
        self.verify_tree(None)?;
        // Cleanup consists only of asking the verified own anchor to exit.
        // No screen quit, kill, directory deletion, or fallback cleanup on error.
        write_new(&self.base.join("anchor.exit"), &self.token)?;
        if !wait_child(&mut self.server)?.success() {
            return Err("synthetic_server_exit_failed");
        }
        if self.anchor.process.verify() != ProcessState::Gone
            || self.session.server.verify() != ProcessState::Gone
        {
            return Err("synthetic_cleanup_identity_unverified");
        }
        match fs::symlink_metadata(&self.session.socket) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            _ => return Err("synthetic_socket_absence_unverified"),
        }
        println!("PASS own_cleanup: direct Child reaped; owned anchor/server/socket gone; scratch retained");
        self.verify_scope()?;
        println!("PASS confinement: exact own Child/session/socket scope; env_clear/private SCREENDIR; no foreign enumeration or control");
        Ok(())
    }
}
fn verify_dummy(d: &Dummy, s: &Session, token: &str, window: &str, root: &Path) -> Result<()> {
    if d.token != token
        || d.window != window
        || d.session != s.full
        || d.parent != s.server.pid
        || d.tty == 0
        || d.group != d.process.pid
        || d.process.verify() != ProcessState::Live
        || !children(d.process.pid)?.is_empty()
        || process_fields(d.process.pid)? != (d.parent, d.group, d.tty)
        || io(fs::read_link(format!("/proc/{}/cwd", d.process.pid)))? != root
    {
        return Err("synthetic_dummy_identity_mismatch");
    }
    let actual = io(fs::metadata(format!("/proc/{}/exe", d.process.pid)))?;
    let expected = io(fs::metadata(io(std::env::current_exe())?))?;
    if (actual.dev(), actual.ino()) != (expected.dev(), expected.ino())
        || d.process.verify() != ProcessState::Live
    {
        return Err("synthetic_dummy_executable_changed");
    }
    Ok(())
}
fn isolated_command(base: &Path) -> Command {
    let mut c = Command::new("/usr/bin/screen");
    c.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("TERM", "xterm")
        .env("LC_ALL", "C")
        .env("HOME", base)
        .env("SCREENDIR", base.join("s"))
        .current_dir(base.join("project"))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    c
}
fn wait_child(child: &mut Child) -> Result<std::process::ExitStatus> {
    let mut result = None;
    wait_until(|| {
        result = io(child.try_wait())?;
        Ok(result.is_some())
    })?;
    result.ok_or("synthetic_wait")
}
fn run() -> Result<()> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"));
    let tmp = io(repo.join(".tmp").canonicalize())?;
    let token = nonce()?;
    let base = tmp.join(format!("s23-{}", &token[..16]));
    io(fs::create_dir(&base))?;
    io(fs::set_permissions(
        &base,
        fs::Permissions::from_mode(0o700),
    ))?;
    println!(
        "SCRATCH={} (retained on success and failure)",
        base.display()
    );
    for dir in ["s", "project", "project/.ai", "project/.ai/state"] {
        io(fs::create_dir(base.join(dir)))?;
        io(fs::set_permissions(
            base.join(dir),
            fs::Permissions::from_mode(0o700),
        ))?;
    }
    // Only --run performs capability test. EPERM is BLOCKED, never PASS.
    let capability = UnixListener::bind(base.join("af-unix-probe")).map_err(|e| {
        if matches!(
            e.raw_os_error(),
            Some(libc::EPERM | libc::EACCES | libc::EAFNOSUPPORT)
        ) {
            "NOT RUN / BLOCKED: AF_UNIX bind denied; no screen launched"
        } else {
            "FAIL: AF_UNIX probe failed; no screen launched"
        }
    })?;
    drop(capability);
    println!("PASS host_AF_UNIX");
    let exe = io(std::env::current_exe())?;
    let name = format!("s23-{}", &token[..16]);
    let mut command = isolated_command(&base);
    command
        .args([
            "-c",
            "/dev/null",
            "-D",
            "-m",
            "-S",
            &name,
            "-t",
            "synthetic-anchor",
        ])
        .arg(exe)
        .args(["supervisor", "synthetic-dummy"])
        .arg(&base)
        .args(["anchor", &token]);
    let stderr = io(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(base.join("screen.stderr")))?;
    command.stderr(Stdio::from(stderr));
    let mut server = io(command.spawn())?;
    println!(
        "OWN_SERVER_PID={} (pending until full identity handshake)",
        server.id()
    );
    let identity = ProcessIdentity::read(server.id())?;
    let full = format!("{}.{}", server.id(), name);
    let socket = base.join("s").join(&full);
    write_new(
        &base.join("project/.ai/server-pending.json"),
        &(&identity, &full, &socket),
    )?;
    wait_until(|| {
        if io(server.try_wait())?.is_some() {
            return Err("synthetic_screen_start_failed");
        }
        Ok(socket.exists() && base.join("anchor.ready").exists())
    })?;
    let meta = io(fs::symlink_metadata(&socket))?;
    let directory = io(fs::metadata(base.join("s")))?;
    let session = Session {
        root: base.join("project"),
        server: identity,
        full,
        socket,
        device: meta.dev(),
        inode: meta.ino(),
        directory_device: directory.dev(),
        directory_inode: directory.ino(),
    };
    session.verify()?;
    write_new(&base.join("project/.ai/session.json"), &session)?;
    let anchor: Dummy = serde_json::from_slice(&io(fs::read(base.join("anchor.ready")))?)
        .map_err(|_| "synthetic_handshake")?;
    let root = base.join("project");
    let store = Supervisor::open(&root.join(".ai/state"))?;
    let mut runtime = SyntheticLifecycle {
        base,
        root,
        store,
        server,
        session,
        anchor,
        token,
    };
    runtime.run()
}

pub(super) fn cli(action: &str, args: Vec<String>) -> Result<()> {
    if action == "synthetic-dummy" && args.len() == 3 {
        return dummy(Path::new(&args[0]), &args[1], &args[2]);
    }
    if action != "synthetic-smoke" || args != ["--run"] {
        return Err("synthetic_explicit_run_required");
    }
    let result = run();
    if result.is_err() {
        eprintln!("FAIL/BLOCKED: no fallback cleanup. Review retained SCRATCH session.json, ready/binding and procfs identities manually. Do not delete claims or signal a PID by name. Dummies self-exit within 120 seconds; this is not release proof.");
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn session() -> Session {
        Session {
            root: PathBuf::from("/"),
            server: ProcessIdentity::read(std::process::id()).unwrap(),
            full: "synthetic-only".into(),
            socket: PathBuf::from("/no-synthetic-socket"),
            device: 1,
            inode: 2,
            directory_device: 3,
            directory_inode: 4,
        }
    }
    #[test]
    fn no_implicit_run_or_arbitrary_command_surface() {
        for args in [
            vec![],
            vec!["--check".into()],
            vec!["--run".into(), "codex".into()],
        ] {
            assert_eq!(
                cli("synthetic-smoke", args),
                Err("synthetic_explicit_run_required")
            );
        }
        assert!(dummy(Path::new("/tmp"), "job", &"a".repeat(64)).is_err());
        assert!(dummy(Path::new("/tmp"), "job", "short").is_err());
        assert!(dummy(Path::new("/tmp"), "arbitrary", &"a".repeat(64)).is_err());
    }
    #[test]
    fn generation_mismatch_refuses_before_socket_or_command() {
        let mut s = session();
        s.server.start_ticks += 1;
        assert_eq!(s.verify(), Err("synthetic_server_identity_mismatch"));
    }
    #[test]
    fn window_session_nonce_parent_group_and_pid_reuse_refuse() {
        let s = session();
        let token = "a".repeat(64);
        let original = Dummy {
            process: s.server.clone(),
            window: "1".into(),
            session: s.full.clone(),
            token: token.clone(),
            parent: s.server.pid,
            group: s.server.pid,
            tty: 1,
        };
        for change in 0..7 {
            let mut d = original.clone();
            match change {
                0 => d.window = "2".into(),
                1 => d.session.push('x'),
                2 => d.token.push('x'),
                3 => d.parent += 1,
                4 => d.group += 1,
                5 => d.process.start_ticks += 1,
                _ => d.tty = 0,
            }
            assert_eq!(
                verify_dummy(&d, &s, &token, "1", Path::new("/")),
                Err("synthetic_dummy_identity_mismatch")
            );
        }
    }
    #[test]
    fn changed_or_linked_session_record_refuses_before_control() {
        let base = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(".tmp")
            .canonicalize()
            .unwrap()
            .join(format!("supervisor23-offline-{}", nonce().unwrap()));
        fs::create_dir(&base).unwrap();
        let path = base.join("session.json");
        let s = session();
        write_new(&path, &s).unwrap();
        assert!(verify_record(&path, &s).is_ok());
        let mut changed = s.clone();
        changed.inode += 1;
        assert_eq!(
            verify_record(&path, &changed),
            Err("synthetic_session_record_changed")
        );
        changed = s.clone();
        changed.full.push('x');
        assert_eq!(
            verify_record(&path, &changed),
            Err("synthetic_session_record_changed")
        );
        let link = base.join("linked.json");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert_eq!(
            verify_record(&link, &s),
            Err("synthetic_session_record_unsafe")
        );
        // Only this offline test's newly created files, never runtime evidence.
        fs::remove_dir_all(base).unwrap();
    }
}
