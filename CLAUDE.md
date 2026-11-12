# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv sync                                   # create .venv, install deps + dev group
uv run pytest                             # all tests
uv run pytest tests/test_wsftp.py::test_upload_since_keeps_structure   # one test
uv run pytest -k since                    # by keyword
uv run wsftp <upload|download|ls|check> --help   # run the CLI from source
```

No linter or formatter is configured.

## Architecture

`wsftp` is a Python (3.11+) CLI over paramiko, in `src/wsftp/`. User-facing behaviour (copy semantics, config keys, host-key handling, exit codes) is specified in `README.md`; keep it in sync when changing behaviour.

The flow is `cli.main` → `config.resolve` → `connection.open_sftp` → `transfer.upload` / `transfer.download`:

- **`config.py`** — `Settings` dataclass is the single list of connection/path settings. `resolve(args)` merges config-file profile < environment < CLI flags by matching `Settings` field names against argparse `dest` names, so adding a setting means adding a `Settings` field plus a same-named CLI option; it is then automatically a valid config key (unknown config keys are rejected). The positional `local_path` / `remote_path` arguments override the profile's paths through this same mechanism. `parse_since` also lives here.
- **`connection.py`** — the only module that does SSH auth and host-key verification. Unknown hosts go through `_HostKeyPolicy` (ask / accept-new / ignore); it appends to known_hosts itself rather than letting paramiko rewrite the file.
- **`transfer.py`** — direction-agnostic core. Each side is walked into `Entry` objects whose `rel` path is always `/`-separated and relative to the source root; `select()` applies `--since` and `--exclude` and is the one place filtering rules live; each picked file becomes an `Item` (entry + full source and target path); `_run()` does the per-file loop (dry-run, collect failures without aborting) and emits events to a `Reporter` (`picked`, `progress`, `done`, `failed`). `upload`/`download` only differ in the walker, path joining (`os.path` locally, `posixpath` remotely) and the copy primitive. `transfer.py` never prints: all output (picked list with ○/●/✗ markers, `#` bar) is `ConsoleReporter` in `cli.py`. These functions take any paramiko-`SFTPClient`-compatible object and know nothing about connections.

## Testing

Transfer tests run the real SFTP protocol with no sshd: the `sftp` fixture spawns the system `sftp-server` binary on a socketpair and wraps it in `paramiko.SFTPClient`, so "remote" paths are local temp paths. Tests skip if the binary is absent. `connection.py` (auth, host keys) is not covered by automated tests; to exercise it, run a throwaway `sshd -f <config>` on a high port with its own host key and `AuthorizedKeysFile`, and point a profile at it with a scratch `known_hosts`.
