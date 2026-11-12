"""Tree walking and copying between the local filesystem and an SFTP server.

Both directions share the same shape: walk the source into a list of
`Entry` (paths relative to the source root, always '/'-separated), filter
with `select`, pair each picked file with its full source and target path
(`Item`), then copy, creating directories as needed. Events are sent to a
`Reporter`. Functions take a paramiko SFTPClient (or anything API-compatible).
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import stat
from dataclasses import dataclass, field
from typing import Iterable, Iterator


class TransferError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    rel: str  # '/'-separated path relative to the source root
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


@dataclass
class Result:
    copied: int = 0
    bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


# ---------- walking ----------

def walk_local(root: str) -> Iterator[Entry]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for name in dirnames:
            yield Entry(prefix + name, True)
        for name in sorted(filenames):
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue  # broken symlink or vanished file
            if stat.S_ISREG(st.st_mode):
                yield Entry(prefix + name, False, st.st_size, st.st_mtime)


def walk_remote(sftp, root: str) -> Iterator[Entry]:
    def recurse(path: str, prefix: str) -> Iterator[Entry]:
        for attr in sorted(sftp.listdir_attr(path), key=lambda a: a.filename):
            full = posixpath.join(path, attr.filename)
            rel = prefix + attr.filename
            mode = attr.st_mode or 0
            if stat.S_ISLNK(mode):
                # Follow links to files; skip links to directories to avoid loops.
                try:
                    attr = sftp.stat(full)
                except OSError:
                    continue
                mode = attr.st_mode or 0
                if not stat.S_ISREG(mode):
                    continue
            if stat.S_ISDIR(mode):
                yield Entry(rel, True)
                yield from recurse(full, rel + "/")
            elif stat.S_ISREG(mode):
                yield Entry(rel, False, attr.st_size or 0, float(attr.st_mtime or 0))

    yield from recurse(root, "")


# ---------- filtering ----------

def _excluded(rel: str, patterns: Iterable[str]) -> bool:
    parts = rel.split("/")
    return any(
        fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts)
        for p in patterns
    )


def select(entries: Iterable[Entry], since: float | None = None,
           excludes: Iterable[str] = ()) -> tuple[list[str], list[Entry]]:
    """Return (directories to create, files to copy).

    With `since`, only files modified strictly after that timestamp are kept
    and only their parent directories are created (no empty directories).
    """
    excludes = list(excludes)
    dirs: list[str] = []
    files: list[Entry] = []
    for e in entries:
        if _excluded(e.rel, excludes):
            continue
        if e.is_dir:
            if since is None:
                dirs.append(e.rel)
        elif since is None or e.mtime > since:
            files.append(e)
    return dirs, files


# ---------- remote helpers ----------

def _remote_isdir(sftp, path: str) -> bool:
    try:
        return stat.S_ISDIR(sftp.stat(path).st_mode or 0)
    except OSError:
        return False


class _RemoteDirs:
    """mkdir -p for SFTP, remembering what already exists."""

    def __init__(self, sftp):
        self.sftp = sftp
        self.known: set[str] = set()

    def ensure(self, path: str) -> None:
        if path in ("", "/", ".") or path in self.known:
            return
        if not _remote_isdir(self.sftp, path):
            self.ensure(posixpath.dirname(path))
            self.sftp.mkdir(path)
        self.known.add(path)


# ---------- copying ----------

@dataclass(frozen=True)
class Item:
    """A file picked for transfer, with its full source and target paths."""
    entry: Entry
    src: str
    dst: str


class Reporter:
    """Receives transfer events. This base class ignores them all."""

    def picked(self, items: list[Item]) -> None:
        """Called once, before any copying, with every file selected."""

    def progress(self, item: Item, done: int) -> None:
        """Called repeatedly while a file transfers; `done` is bytes so far."""

    def done(self, item: Item) -> None:
        """Called when a file has been transferred."""

    def failed(self, item: Item, error: str) -> None:
        """Called when a file could not be transferred."""


def _run(items: list[Item], copy_one, dry_run: bool, reporter: Reporter) -> Result:
    result = Result()
    reporter.picked(items)
    for item in items:
        if not dry_run:
            try:
                copy_one(item, lambda done, total, item=item: reporter.progress(item, done))
            except OSError as err:
                result.failed.append((item.entry.rel, str(err)))
                reporter.failed(item, str(err))
                continue
            reporter.done(item)
        result.copied += 1
        result.bytes += item.entry.size
    return result


def upload(sftp, local: str, remote: str, *, since: float | None = None,
           excludes: Iterable[str] = (), dry_run: bool = False,
           reporter: Reporter | None = None) -> Result:
    """Copy a local file, or the contents of a local directory, to `remote`."""
    local = os.path.expanduser(local)
    if not os.path.exists(local):
        raise TransferError(f"local path not found: {local}")
    dirs = _RemoteDirs(sftp)

    if os.path.isfile(local):
        st = os.stat(local)
        name = os.path.basename(local)
        if remote.endswith("/") or _remote_isdir(sftp, remote):
            remote = posixpath.join(remote, name)
        files = select([Entry(name, False, st.st_size, st.st_mtime)], since, excludes)[1]
        items = [Item(e, local, remote) for e in files]
        new_dirs: list[str] = []
    else:
        new_dirs, files = select(walk_local(local), since, excludes)
        items = [
            Item(e, os.path.join(local, *e.rel.split("/")), posixpath.join(remote, e.rel))
            for e in files
        ]

    def copy_one(item: Item, callback) -> None:
        dirs.ensure(posixpath.dirname(item.dst))
        sftp.put(item.src, item.dst, callback=callback)
        try:
            sftp.utime(item.dst, (item.entry.mtime, item.entry.mtime))
        except OSError:
            pass  # some servers refuse setstat; the file itself is copied

    if not dry_run:
        for d in new_dirs:
            dirs.ensure(posixpath.join(remote, d))
    return _run(items, copy_one, dry_run, reporter or Reporter())


def download(sftp, remote: str, local: str, *, since: float | None = None,
             excludes: Iterable[str] = (), dry_run: bool = False,
             reporter: Reporter | None = None) -> Result:
    """Copy a remote file, or the contents of a remote directory, to `local`."""
    local = os.path.expanduser(local)
    try:
        st = sftp.stat(remote)
    except OSError:
        raise TransferError(f"remote path not found: {remote}") from None

    if not stat.S_ISDIR(st.st_mode or 0):
        name = posixpath.basename(remote)
        if local.endswith(os.sep) or os.path.isdir(local):
            local = os.path.join(local, name)
        files = select([Entry(name, False, st.st_size or 0, float(st.st_mtime or 0))],
                       since, excludes)[1]
        items = [Item(e, remote, local) for e in files]
        new_dirs: list[str] = []
    else:
        new_dirs, files = select(walk_remote(sftp, remote), since, excludes)
        items = [
            Item(e, posixpath.join(remote, e.rel), os.path.join(local, *e.rel.split("/")))
            for e in files
        ]

    def copy_one(item: Item, callback) -> None:
        os.makedirs(os.path.dirname(item.dst) or ".", exist_ok=True)
        sftp.get(item.src, item.dst, callback=callback)
        os.utime(item.dst, (item.entry.mtime, item.entry.mtime))

    if not dry_run:
        for d in new_dirs:
            os.makedirs(os.path.join(local, *d.split("/")), exist_ok=True)
    return _run(items, copy_one, dry_run, reporter or Reporter())
