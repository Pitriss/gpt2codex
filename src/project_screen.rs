//! Read-only diagnostic probe, NOT permission to start/close/release a job.
//! Legacy records have no window generation or persistent anchor evidence.
use crate::project_supervisor::{ProcessIdentity, ProcessState};
use crate::{multiplexer::Multiplexer, registry::ProjectRegistry, screen};
use serde::Serialize;
use std::fs;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};

type Result<T> = std::result::Result<T, &'static str>;

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ProcessObservation {
    pub identity: ProcessIdentity,
    pub parent_pid: u32,
    pub process_group: u32,
    pub session: u32,
    pub tty: i64,
    pub cwd_matches_root: bool,
}

impl ProcessObservation {
    fn read(pid: u32, root: &Path) -> Result<Self> {
        let identity = ProcessIdentity::read(pid)?;
        if identity.verify() != ProcessState::Live {
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
        let number = |index: usize| -> Result<u32> {
            fields
                .get(index)
                .ok_or("session_unverified")?
                .parse()
                .map_err(|_| "session_unverified")
        };
        let cwd = fs::read_link(proc.join("cwd")).map_err(|_| "session_unverified")?;
        let value = Self {
            identity: identity.clone(),
            parent_pid: number(1)?,
            process_group: number(2)?,
            session: number(3)?,
            tty: fields
                .get(4)
                .ok_or("session_unverified")?
                .parse()
                .map_err(|_| "session_unverified")?,
            cwd_matches_root: cwd == root,
        };
        if fields
            .get(19)
            .ok_or("session_unverified")?
            .parse::<u64>()
            .map_err(|_| "session_unverified")?
            != identity.start_ticks
            || identity.verify() != ProcessState::Live
            || fs::read_to_string(proc.join("stat"))
                .map_err(|_| "session_unverified")?
                .rsplit_once(')')
                .ok_or("session_unverified")?
                .1
                .split_whitespace()
                .skip(1)
                .take(4)
                .collect::<Vec<_>>()
                != fields[1..5]
            || fs::read_link(proc.join("cwd")).map_err(|_| "session_unverified")? != cwd
        {
            return Err("session_unverified");
        }
        Ok(value)
    }
}

#[derive(Debug, Serialize)]
pub struct ScreenInspection {
    pub version: u32,
    pub runtime_enabled: bool,
    pub launch_permitted: bool,
    pub availability: &'static str,
    pub session_id: String,
    pub server: ProcessIdentity,
    pub socket_device: u64,
    pub socket_inode: u64,
    pub descendants: Vec<ProcessObservation>,
    pub window_identity_verified: bool,
}

/// Opens only the registered full socket ID in the explicitly supplied private
/// directory. SO_PEERCRED independently binds the socket to the recorded server.
/// Sends no screen protocol bytes and never enumerates other screen sockets.
pub fn inspect(
    config: &Path,
    project_id: &str,
    socket_directory: &Path,
) -> Result<ScreenInspection> {
    let registry = ProjectRegistry::load(config).map_err(|_| "session_unverified")?;
    let project = registry
        .project(project_id)
        .map_err(|_| "session_unverified")?;
    if project.multiplexer != Multiplexer::Screen {
        return Err("session_unverified");
    }
    let target = screen::Target::Project(project_id);
    let name = target.screen_name().map_err(|_| "session_unverified")?;
    let state = screen::state_dir(false).map_err(|_| "session_unverified")?;
    let record_path = state.join(format!("{project_id}.json"));
    let (record, bytes, _) = screen::read_record_snapshot(&record_path)
        .map_err(|_| "session_unverified")?
        .ok_or("session_unverified")?;
    let full = record.full_id.as_ref().ok_or("session_unverified")?;
    let pid = record.pid.ok_or("session_unverified")?;
    if record.multiplexer != Multiplexer::Screen
        || record.tmux.is_some()
        || record.tmux_lifecycle.is_some()
        || record.project_id != project_id
        || record.root != project.root
        || record.screen_name != name
        || *full != format!("{pid}.{name}")
    {
        return Err("session_unverified");
    }
    let server = ProcessIdentity::read(pid)?;
    if Some(server.start_ticks) != record.process_start_ticks
        || server.verify() != ProcessState::Live
    {
        return Err("session_unverified");
    }
    // No process-name heuristic can stand in for executable identity.
    let exe = fs::metadata(format!("/proc/{pid}/exe")).map_err(|_| "session_unverified")?;
    let installed = fs::metadata("/usr/bin/screen").map_err(|_| "session_unverified")?;
    if (exe.dev(), exe.ino()) != (installed.dev(), installed.ino()) {
        return Err("session_unverified");
    }
    if !socket_directory.is_absolute()
        || socket_directory
            .canonicalize()
            .map_err(|_| "session_unverified")?
            != socket_directory
    {
        return Err("session_unverified");
    }
    let dir = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(socket_directory)
        .map_err(|_| "session_unverified")?;
    let meta = dir.metadata().map_err(|_| "session_unverified")?;
    if meta.uid() != server.uid || meta.mode() & 0o077 != 0 {
        return Err("session_unverified");
    }
    let socket = PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd())).join(full);
    let before = fs::symlink_metadata(&socket).map_err(|_| "session_unverified")?;
    if !before.file_type().is_socket() || before.uid() != server.uid {
        return Err("session_unverified");
    }
    // Never wait on a full/unresponsive screen listener backlog.
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    let path_bytes = socket.as_os_str().as_bytes();
    if path_bytes.len() >= address.sun_path.len() || path_bytes.contains(&0) {
        return Err("session_unverified");
    }
    for (slot, byte) in address.sun_path.iter_mut().zip(path_bytes) {
        *slot = *byte as libc::c_char;
    }
    let fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err("session_unverified");
    }
    let stream = unsafe { UnixStream::from_raw_fd(fd) };
    if unsafe {
        libc::connect(
            fd,
            (&address as *const libc::sockaddr_un).cast(),
            std::mem::size_of_val(&address) as libc::socklen_t,
        )
    } != 0
    {
        return Err("session_unverified");
    }
    let mut peer: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut peer as *mut libc::ucred).cast(),
            &mut len,
        )
    } != 0
        || len as usize != std::mem::size_of::<libc::ucred>()
        || peer.pid as u32 != pid
        || peer.uid != server.uid
    {
        return Err("session_unverified");
    }
    drop(stream);
    let mut descendants = Vec::new();
    let mut parents = vec![server.clone()];
    while let Some(parent) = parents.pop() {
        if parent.verify() != ProcessState::Live || descendants.len() > 256 {
            return Err("session_unverified");
        }
        let children_path = format!("/proc/{}/task/{}/children", parent.pid, parent.pid);
        let children = fs::read_to_string(&children_path).map_err(|_| "session_unverified")?;
        for child in children.split_whitespace() {
            if descendants.len() >= 256 {
                return Err("session_unverified");
            }
            let child = ProcessObservation::read(
                child.parse().map_err(|_| "session_unverified")?,
                &project.root,
            )?;
            if child.parent_pid != parent.pid
                || child.identity.start_ticks < parent.start_ticks
                || descendants
                    .iter()
                    .any(|p: &ProcessObservation| p.identity.pid == child.identity.pid)
            {
                return Err("session_unverified");
            }
            parents.push(child.identity.clone());
            descendants.push(child);
        }
        if parent.verify() != ProcessState::Live
            || fs::read_to_string(&children_path).map_err(|_| "session_unverified")? != children
        {
            return Err("session_unverified");
        }
    }
    let after = fs::symlink_metadata(&socket).map_err(|_| "session_unverified")?;
    let (_, latest, _) = screen::read_record_snapshot(&record_path)
        .map_err(|_| "session_unverified")?
        .ok_or("session_unverified")?;
    if (before.dev(), before.ino()) != (after.dev(), after.ino())
        || bytes != latest
        || server.verify() != ProcessState::Live
        || descendants.iter().any(|p| {
            ProcessObservation::read(p.identity.pid, &project.root)
                .map(|current| current != *p)
                .unwrap_or(true)
        })
    {
        return Err("session_unverified");
    }
    Ok(ScreenInspection {
        version: 1,
        runtime_enabled: false,
        launch_permitted: false,
        availability: if descendants.is_empty() {
            "session_unverified"
        } else {
            "project_busy"
        },
        session_id: full.clone(),
        server,
        socket_device: before.dev(),
        socket_inode: before.ino(),
        descendants,
        window_identity_verified: false,
    })
}

use std::os::unix::fs::OpenOptionsExt;
