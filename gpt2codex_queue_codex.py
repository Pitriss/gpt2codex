"""Codex options for locally approved, allowlisted queue roots only."""
import os
from pathlib import Path
import stat


def non_git_root(cwd):
    """Only ENOENT for .git means non-Git; never follow metadata symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = os.open(cwd, flags | os.O_DIRECTORY)
    try:
        try:
            metadata = os.stat('.git', dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise RuntimeError('Ambiguous Git metadata; no trust bypass')
        fd = os.open('.git', flags | os.O_NONBLOCK, dir_fd=root)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino, current.st_mode) != (
                    metadata.st_dev, metadata.st_ino, metadata.st_mode):
                raise RuntimeError('Git metadata changed; no trust bypass')
        finally:
            os.close(fd)
        return False
    finally:
        os.close(root)


def git_check_options(cwd, approved_project_root=None):
    # This binding is supplied only after Queue allowlist + local approval
    # validation, then persisted in its private, single-use screen slot.
    if approved_project_root is not None:
        if (approved_project_root != cwd or not Path(cwd).is_absolute()
                or str(Path(cwd).resolve(strict=True)) != cwd):
            raise RuntimeError('Approved project root mismatch')
        if non_git_root(cwd):
            return ['--skip-git-repo-check']
    return []


def codex_options(mode, cwd, approved_project_root=None):
    if mode not in ('read-only', 'workspace-write'):
        raise RuntimeError('Invalid sandbox')
    return ['-c', 'model_provider="openai"', '-c', 'approval_policy="never"',
            '--sandbox', mode, '-C', cwd,
            *git_check_options(cwd, approved_project_root)]
