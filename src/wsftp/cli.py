"""wsftp command-line interface."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from datetime import datetime

import paramiko

from . import __version__, connection, transfer
from .config import ConfigError, parse_since, resolve


def _connection_options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("connection")
    g.add_argument("-c", "--config", help="TOML config file (default: ./wsftp.toml, "
                   "then ~/.config/wsftp/config.toml)")
    g.add_argument("-p", "--profile", help="profile (table) in the config file (default: 'default')")
    g.add_argument("-H", "--host")
    g.add_argument("-P", "--port", type=int)
    g.add_argument("-u", "--username")
    g.add_argument("--password", help="prefer WSFTP_PASSWORD or the interactive prompt")
    g.add_argument("-i", "--key-file", help="private key file")
    g.add_argument("--key-passphrase", help="prefer WSFTP_KEY_PASSPHRASE or the prompt")
    g.add_argument("--known-hosts", help="known_hosts file (default: ~/.ssh/known_hosts)")
    g.add_argument("--accept-new-host-key", dest="hostkey_mode", action="store_const",
                   const=connection.HOSTKEY_ACCEPT_NEW, default=connection.HOSTKEY_ASK,
                   help="trust and record the key of a host not yet in known_hosts")
    g.add_argument("--insecure-ignore-host-key", dest="hostkey_mode", action="store_const",
                   const=connection.HOSTKEY_IGNORE,
                   help="do not verify unknown host keys (vulnerable to interception)")
    g.add_argument("--timeout", type=float, default=30.0, help="connect timeout in seconds")
    return p


def _transfer_options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-s", "--since", metavar="DATETIME",
                   help="only copy files modified after this date/time, e.g. '2026-11-01' or "
                        "'2026-11-01 14:30' (local time unless an offset or Z is given)")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                   help="skip paths or names matching this glob (repeatable)")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be copied")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    p.add_argument("--no-progress", action="store_true",
                   help="do not draw the # progress bar while a file transfers")
    return p


def build_parser() -> argparse.ArgumentParser:
    conn, xfer = _connection_options(), _transfer_options()
    parser = argparse.ArgumentParser(
        prog="wsftp",
        description="Copy files and directory trees over SFTP.",
    )
    parser.add_argument("--version", action="version", version=f"wsftp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", parents=[conn, xfer],
                        help="copy a local file or directory tree to the server")
    up.add_argument("local_path", nargs="?", help="local source (default: local_path from config)")
    up.add_argument("remote_path", nargs="?", help="remote target (default: remote_path from config)")

    down = sub.add_parser("download", parents=[conn, xfer],
                          help="copy a remote file or directory tree from the server")
    down.add_argument("remote_path", nargs="?", help="remote source (default: remote_path from config)")
    down.add_argument("local_path", nargs="?", help="local target (default: local_path from config)")

    ls = sub.add_parser("ls", parents=[conn], help="list a remote directory")
    ls.add_argument("remote_path", nargs="?", help="remote directory (default: remote_path from config, else '.')")

    sub.add_parser("check", parents=[conn], help="connect, authenticate and exit")
    return parser


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class ConsoleReporter(transfer.Reporter):
    """Prints the files picked for transfer and their status.

    Each file is listed as `○ source → target` when picked, shows a bar of
    '#' while it transfers (terminals only), and ends as `● source → target`
    once transferred or `✗ source → target: reason` if it failed.
    """

    WIDTH = 30

    def __init__(self, out=None, bar: bool = True, color: bool = False):
        self.out = out or sys.stdout
        self.bar = bar
        try:
            "○◐●✗→".encode(getattr(self.out, "encoding", None) or "utf-8")
            self.pending, self.active, self.ok, self.bad, self.arrow = "○", "◐", "●", "✗", "→"
        except (UnicodeEncodeError, LookupError):
            self.pending, self.active, self.ok, self.bad, self.arrow = "( )", "(~)", "(*)", "(x)", "->"
        self.legend = f"{self.pending} pending   {self.ok} transferred   {self.bad} failed"
        if color:
            self.ok = f"\033[32m{self.ok}\033[0m"
            self.bad = f"\033[31m{self.bad}\033[0m"
        self._bar_drawn = False
        self._last = None

    def _line(self, mark: str, item, suffix: str = "") -> None:
        if self._bar_drawn:
            self.out.write("\r\033[K")  # erase the in-progress bar line
            self._bar_drawn = False
        self.out.write(f"  {mark} {item.src} {self.arrow} {item.dst}{suffix}\n")
        self.out.flush()

    def picked(self, items) -> None:
        if not items:
            return
        self.out.write(f"{len(items)} file(s) picked for transfer   [{self.legend}]\n")
        for item in items:
            self._line(self.pending, item, f"  ({_human(item.entry.size)})")
        self.out.write("\n")

    def progress(self, item, done: int) -> None:
        if not self.bar:
            return
        size = item.entry.size
        frac = 1.0 if size <= 0 else min(done / size, 1.0)
        filled, pct = int(frac * self.WIDTH), int(frac * 100)
        if (item, filled, pct) == self._last:
            return
        self._last = (item, filled, pct)
        tail = f" [{'#' * filled}{' ' * (self.WIDTH - filled)}] {pct:3d}% {_human(size)}"
        room = max(shutil.get_terminal_size().columns - len(tail) - 5, 8)
        rel = item.entry.rel
        name = rel if len(rel) <= room else "..." + rel[-(room - 3):]
        self.out.write(f"\r  {self.active} {name}{tail}")
        self.out.flush()
        self._bar_drawn = True

    def done(self, item) -> None:
        self._line(self.ok, item)

    def failed(self, item, error: str) -> None:
        self._line(self.bad, item, f": {error}")


def _list(sftp, path: str) -> None:
    for a in sorted(sftp.listdir_attr(path), key=lambda a: a.filename):
        mode = a.st_mode or 0
        kind = "d" if stat.S_ISDIR(mode) else "l" if stat.S_ISLNK(mode) else "-"
        when = datetime.fromtimestamp(a.st_mtime or 0).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{kind} {a.st_size or 0:>12} {when} {a.filename}")


def _run(args) -> int:
    settings = resolve(args)

    if args.command in ("upload", "download"):
        since = parse_since(args.since) if args.since else None
        if not settings.local_path or not settings.remote_path:
            raise ConfigError(
                "both a local and a remote path are required "
                "(as arguments, or local_path/remote_path in the config profile)"
            )

    with connection.open_sftp(settings, args.hostkey_mode, args.timeout) as sftp:
        if args.command == "check":
            print(f"OK: connected to {settings.username}@{settings.host}:{settings.port} "
                  f"(remote dir: {sftp.normalize('.')})")
            return 0
        if args.command == "ls":
            _list(sftp, settings.remote_path or ".")
            return 0

        reporter = None
        if not args.quiet:
            tty = sys.stdout.isatty()
            reporter = ConsoleReporter(bar=tty and not args.no_progress,
                                       color=tty and "NO_COLOR" not in os.environ)
            there = f"{settings.username}@{settings.host}:{settings.remote_path}"
            here = settings.local_path
            src, dst = (here, there) if args.command == "upload" else (there, here)
            print(f"Source: {src}\nTarget: {dst}")
        if args.command == "upload":
            src, dst, func = settings.local_path, settings.remote_path, transfer.upload
        else:
            src, dst, func = settings.remote_path, settings.local_path, transfer.download
        result = func(sftp, src, dst, since=since, excludes=args.exclude,
                      dry_run=args.dry_run, reporter=reporter)

    for rel, err in result.failed if args.quiet else ():
        print(f"FAILED {rel}: {err}", file=sys.stderr)
    prefix = "dry run: would copy" if args.dry_run else "copied"
    summary = f"{prefix} {result.copied} file(s), {_human(result.bytes)}"
    if result.failed:
        summary += f"; {len(result.failed)} failed"
    print(summary)
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ConfigError, transfer.TransferError, paramiko.SSHException, OSError) as e:
        print(f"wsftp: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
