# wsftp

A command-line SFTP client. Copies files and whole directory trees between
your machine and an SFTP server, optionally only the files changed after a
given date/time, keeping the directory structure intact.

## Install

```sh
uv sync                 # development: then run as `uv run wsftp ...`
uv tool install .       # or install the `wsftp` command globally
```

Requires Python 3.11+.

## Usage

```sh
# Everything on the command line
wsftp upload ./build /var/www/site -H sftp.example.com -u deploy -i ~/.ssh/id_ed25519

# Connection and paths from a config file profile
wsftp upload                       # [default] profile
wsftp upload --profile staging
wsftp upload -c ./other.toml ./dist /srv/app   # CLI paths override the config

# Only files changed after a date/time (directory structure is preserved)
wsftp upload --since "2026-11-01 14:30"
wsftp upload --since 2026-11-01T14:30:00Z --dry-run

# The other direction, and helpers
wsftp download /var/log/app ./logs --since 2026-11-10
wsftp ls /var/www
wsftp check                        # test connection and authentication
```

| Command | Purpose |
| --- | --- |
| `upload [LOCAL] [REMOTE]` | Copy a local file or tree to the server |
| `download [REMOTE] [LOCAL]` | Copy a remote file or tree to this machine |
| `ls [REMOTE]` | List a remote directory |
| `check` | Connect, authenticate, exit |

Transfer options: `--since DATETIME`, `--exclude GLOB` (repeatable),
`--dry-run`, `--quiet`, `--no-progress`. Run `wsftp <command> --help` for everything.

Output lists every file picked for transfer with its source and target
path, then marks each one as it completes:

```
Source: ./build
Target: deploy@sftp.example.com:/var/www/site
2 file(s) picked for transfer   [○ pending   ● transferred   ✗ failed]
  ○ ./build/index.html → /var/www/site/index.html  (4.1 KB)
  ○ ./build/media/video.mp4 → /var/www/site/media/video.mp4  (26.3 MB)

  ● ./build/index.html → /var/www/site/index.html
  ◐ media/video.mp4 [##############                ]  47% 26.3 MB
```

The `#` bar is drawn only on a terminal and is replaced by the `●` line when
the file finishes (`✗` with the reason if it fails). `--no-progress` turns
the bar off, `--dry-run` prints only the picked list, and `--quiet` prints
only the summary.

### Copy semantics

- A directory source copies its **contents** into the target: `SRC/a/b.txt`
  becomes `DEST/a/b.txt`. Missing target directories are created.
- A file source is copied into the target if it is an existing directory or
  ends with `/`; otherwise the target is the new file name.
- Existing files are overwritten. Modification times are preserved.
- `--since` keeps files whose modification time is strictly after the given
  time. Times without a timezone are local time; add `Z` or an offset such
  as `+05:30` to be explicit. With `--since`, only the directories needed for
  the copied files are created (empty directories are not).
- If a file fails, the rest still copy and the exit code is 1. Exit code 2
  means a configuration or connection error.

## Config file

TOML, looked up at `--config PATH`, else `./wsftp.toml`, else
`~/.config/wsftp/config.toml`. Each table is a profile; see
[`wsftp.example.toml`](wsftp.example.toml).

```toml
[default]
host = "sftp.example.com"
port = 22
username = "deploy"
key_file = "~/.ssh/id_ed25519"
local_path = "./build"
remote_path = "/var/www/site"
```

Keys: `host`, `port`, `username`, `password`, `key_file`, `key_passphrase`,
`known_hosts`, `local_path`, `remote_path`. Precedence: command-line flag,
then environment (`WSFTP_PASSWORD`, `WSFTP_KEY_PASSPHRASE`), then the
config file.

## Authentication and host keys

Tried in order: the given key file, keys in your SSH agent, default keys in
`~/.ssh`, then the password. On a terminal you are prompted for a missing
password or key passphrase.

Host keys are checked against `~/.ssh/known_hosts`. For an unknown host you
are asked to confirm the fingerprint on a terminal; in scripts the
connection is refused unless you pass `--accept-new-host-key` (trust and
record on first use). A host whose key has *changed* is always refused.

## Tests

```sh
uv run pytest
```
