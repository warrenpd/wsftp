import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import pytest

from wsftp import cli, transfer
from wsftp.config import ConfigError, parse_since, resolve

SFTP_SERVER = next(
    (p for p in ("/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server",
                 "/usr/libexec/openssh/sftp-server", "/usr/lib/ssh/sftp-server")
     if os.path.exists(p)),
    None,
)


class _NamedSocket(socket.socket):
    """paramiko's SFTPClient logs via sock.get_name(), which Channels have."""

    def get_name(self):
        return "test"


@pytest.fixture
def sftp():
    """A real SFTP session against the system sftp-server binary, no sshd needed."""
    if SFTP_SERVER is None:
        pytest.skip("sftp-server binary not found")
    ours, theirs = socket.socketpair()
    proc = subprocess.Popen([SFTP_SERVER], stdin=theirs, stdout=theirs)
    theirs.close()
    client = paramiko.SFTPClient(_NamedSocket(fileno=ours.detach()))
    yield client
    client.close()
    proc.wait(timeout=5)


def make_tree(root: Path, old: float, new: float) -> None:
    files = {
        "top.txt": new,
        "old.txt": old,
        "a/b/deep.txt": new,
        "a/stale.txt": old,
        "a/skip.log": new,
    }
    for rel, mtime in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)
        os.utime(p, (mtime, mtime))
    (root / "empty").mkdir()


def tree(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)).replace(os.sep, "/") + ("/" if p.is_dir() else "")
        for p in root.rglob("*")
    }


OLD, CUT, NEW = 1_700_000_000, 1_750_000_000, 1_780_000_000


# ---------- transfers ----------

def test_upload_full_tree(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, OLD, NEW)
    result = transfer.upload(sftp, str(src), str(dst))
    assert result.copied == 5 and not result.failed
    assert tree(dst) == tree(src)
    assert (dst / "a/b/deep.txt").read_text() == "a/b/deep.txt"
    assert (dst / "old.txt").stat().st_mtime == OLD


def test_upload_since_keeps_structure(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, OLD, NEW)
    result = transfer.upload(sftp, str(src), str(dst), since=CUT,
                             excludes=["*.log"])
    assert result.copied == 2
    assert tree(dst) == {"top.txt", "a/", "a/b/", "a/b/deep.txt"}


def test_upload_dry_run_writes_nothing(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, OLD, NEW)
    result = transfer.upload(sftp, str(src), str(dst), dry_run=True)
    assert result.copied == 5
    assert not dst.exists()


def test_upload_single_file(sftp, tmp_path):
    f = tmp_path / "one.txt"
    f.write_text("x")
    (tmp_path / "dir").mkdir()
    transfer.upload(sftp, str(f), str(tmp_path / "dir"))
    transfer.upload(sftp, str(f), str(tmp_path / "new/renamed.txt"))
    assert (tmp_path / "dir/one.txt").read_text() == "x"
    assert (tmp_path / "new/renamed.txt").read_text() == "x"


def test_download_since_keeps_structure(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, OLD, NEW)
    result = transfer.download(sftp, str(src), str(dst), since=CUT)
    assert result.copied == 3
    assert tree(dst) == {"top.txt", "a/", "a/b/", "a/b/deep.txt", "a/skip.log"}
    assert (dst / "top.txt").stat().st_mtime == NEW


def test_download_full_tree(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, OLD, NEW)
    transfer.download(sftp, str(src), str(dst))
    assert tree(dst) == tree(src)


class Recorder(transfer.Reporter):
    def __init__(self):
        self.events = []

    def picked(self, items):
        self.events.append(("picked", [i.entry.rel for i in items]))

    def progress(self, item, done):
        self.events.append(("progress", item.entry.rel, done))

    def done(self, item):
        self.events.append(("done", item.src, item.dst))

    def failed(self, item, error):
        self.events.append(("failed", item.entry.rel))


def test_reporter_sees_picked_files_progress_and_paths(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "sub/big.bin").write_bytes(b"x" * 200_000)
    (src / "empty.txt").write_text("")
    rec = Recorder()
    transfer.upload(sftp, str(src), str(dst), reporter=rec)
    assert rec.events[0] == ("picked", ["empty.txt", "sub/big.bin"])
    big = [e[2] for e in rec.events if e[:2] == ("progress", "sub/big.bin")]
    assert len(big) > 2 and big == sorted(big) and big[-1] == 200_000
    done = [e[1:] for e in rec.events if e[0] == "done"]
    assert done == [(str(src / "empty.txt"), str(dst / "empty.txt")),
                    (str(src / "sub/big.bin"), str(dst / "sub/big.bin"))]


def test_reporter_dry_run_and_failure(sftp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_text("a")
    rec = Recorder()
    transfer.upload(sftp, str(src), str(dst), dry_run=True, reporter=rec)
    assert rec.events == [("picked", ["a.txt"])]

    (src / "a.txt").chmod(0)  # unreadable: put() fails
    rec = Recorder()
    result = transfer.upload(sftp, str(src), str(dst), reporter=rec)
    assert rec.events[-1] == ("failed", "a.txt") and len(result.failed) == 1


def test_console_reporter_output():
    import io
    out = io.StringIO()
    rep = cli.ConsoleReporter(out, bar=True)
    item = transfer.Item(transfer.Entry("a/b.txt", False, 1000, 0.0), "/src/a/b.txt", "/dst/a/b.txt")
    bad = transfer.Item(transfer.Entry("c.txt", False, 5, 0.0), "/src/c.txt", "/dst/c.txt")
    rep.picked([item, bad])
    rep.progress(item, 500)
    rep.done(item)
    rep.failed(bad, "denied")
    text = out.getvalue()
    assert "2 file(s) picked for transfer" in text
    assert "  ○ /src/a/b.txt → /dst/a/b.txt  (1000 B)\n" in text
    assert "◐ a/b.txt [" + "#" * 15 + " " * 15 + "]  50% 1000 B" in text
    assert "\r\033[K  ● /src/a/b.txt → /dst/a/b.txt\n" in text
    assert text.endswith("  ✗ /src/c.txt → /dst/c.txt: denied\n")


def test_missing_source(sftp, tmp_path):
    with pytest.raises(transfer.TransferError):
        transfer.upload(sftp, str(tmp_path / "nope"), "/tmp/x")
    with pytest.raises(transfer.TransferError):
        transfer.download(sftp, str(tmp_path / "nope"), str(tmp_path / "x"))


# ---------- config ----------

def test_parse_since():
    assert parse_since("2026-11-01T14:30:00Z") == datetime(
        2026, 11, 1, 14, 30, tzinfo=timezone.utc).timestamp()
    assert parse_since("2026-11-01 14:30") == datetime(2026, 11, 1, 14, 30).timestamp()
    assert parse_since("2026-11-01") == datetime(2026, 11, 1).timestamp()
    with pytest.raises(ConfigError):
        parse_since("last tuesday")


def parse(*argv):
    return cli.build_parser().parse_args(argv)


def test_config_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("WSFTP_PASSWORD", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[default]\nhost = "a.example"\nusername = "u"\npassword = "filepw"\n'
        'local_path = "./l"\nremote_path = "/r"\n'
        '[other]\nhost = "b.example"\nport = 2222\n'
    )
    s = resolve(parse("upload", "-c", str(cfg)))
    assert (s.host, s.port, s.username, s.password) == ("a.example", 22, "u", "filepw")
    assert (s.local_path, s.remote_path) == ("./l", "/r")

    monkeypatch.setenv("WSFTP_PASSWORD", "envpw")
    s = resolve(parse("upload", "-c", str(cfg), "-p", "other", "-u", "cli", "src", "/dst"))
    assert (s.host, s.port, s.username, s.password) == ("b.example", 2222, "cli", "envpw")
    assert (s.local_path, s.remote_path) == ("src", "/dst")

    with pytest.raises(ConfigError, match="not found"):
        resolve(parse("check", "-c", str(cfg), "-p", "missing"))


def test_config_rejects_unknown_keys_and_missing_host(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[default]\nhostname = "typo"\n')
    with pytest.raises(ConfigError, match="hostname"):
        resolve(parse("check", "-c", str(cfg)))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="no host"):
        resolve(parse("check"))
