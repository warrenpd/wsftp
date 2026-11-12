"""Opening an authenticated SFTP session with host-key verification."""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import paramiko

from .config import Settings

# Host-key modes for hosts missing from known_hosts
HOSTKEY_ASK = "ask"  # prompt on a terminal, otherwise refuse
HOSTKEY_ACCEPT_NEW = "accept-new"  # trust and record on first connection
HOSTKEY_IGNORE = "ignore"  # trust without recording (insecure)


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _HostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, mode: str, known_hosts: Path):
        self.mode = mode
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        if self.mode == HOSTKEY_IGNORE:
            return
        fp = fingerprint(key)
        if self.mode == HOSTKEY_ASK:
            if not sys.stdin.isatty():
                raise paramiko.SSHException(
                    f"unknown host key for {hostname} ({key.get_name()} {fp}); "
                    "re-run with --accept-new-host-key to trust it"
                )
            print(f"The authenticity of host '{hostname}' can't be established.", file=sys.stderr)
            print(f"{key.get_name()} key fingerprint is {fp}.", file=sys.stderr)
            answer = input("Trust this host and add it to known_hosts? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                raise paramiko.SSHException("host key rejected")
        # Append rather than rewrite, so existing known_hosts content is untouched.
        self.known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.known_hosts, "a") as f:
            f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
        print(f"Added {hostname} ({fp}) to {self.known_hosts}", file=sys.stderr)


def _connect(client: paramiko.SSHClient, s: Settings, timeout: float) -> None:
    key_file = os.path.expanduser(s.key_file) if s.key_file else None
    password, passphrase = s.password, s.key_passphrase
    interactive = sys.stdin.isatty()

    for _ in range(3):
        try:
            client.connect(
                s.host,
                port=s.port,
                username=s.username,
                password=password,
                key_filename=key_file,
                passphrase=passphrase,
                timeout=timeout,
            )
            return
        except paramiko.PasswordRequiredException:
            if not interactive or passphrase is not None:
                raise
            passphrase = getpass.getpass(f"Passphrase for key {key_file or '(default key)'}: ")
        except paramiko.AuthenticationException:
            if not interactive or password is not None:
                raise
            password = getpass.getpass(f"Password for {s.username}@{s.host}: ")
    raise paramiko.AuthenticationException("authentication failed")


@contextmanager
def open_sftp(settings: Settings, hostkey_mode: str = HOSTKEY_ASK, timeout: float = 30.0):
    """Yield a paramiko SFTPClient connected according to `settings`."""
    known_hosts = Path(settings.known_hosts or "~/.ssh/known_hosts").expanduser()
    client = paramiko.SSHClient()
    if known_hosts.is_file():
        client.load_system_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(_HostKeyPolicy(hostkey_mode, known_hosts))
    try:
        _connect(client, settings, timeout)
        sftp = client.open_sftp()
        try:
            yield sftp
        finally:
            sftp.close()
    finally:
        client.close()
