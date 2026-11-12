"""Connection settings: merged from CLI flags, environment and a TOML config file.

Precedence (highest first): CLI flag, environment variable, config-file profile.
"""

from __future__ import annotations

import getpass
import os
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG_PATHS = (
    Path("wsftp.toml"),
    Path.home() / ".config" / "wsftp" / "config.toml",
)
DEFAULT_PROFILE = "default"


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    host: str | None = None
    port: int = 22
    username: str | None = None
    password: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None
    known_hosts: str | None = None
    local_path: str | None = None
    remote_path: str | None = None


def load_profile(config_path: str | None, profile: str | None) -> dict:
    """Return the profile table from the config file ({} if no config file is in use)."""
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        path = next((p for p in DEFAULT_CONFIG_PATHS if p.is_file()), None)
        if path is None:
            if profile:
                raise ConfigError(
                    f"profile '{profile}' requested but no config file found "
                    f"(looked for {', '.join(str(p) for p in DEFAULT_CONFIG_PATHS)})"
                )
            return {}

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    name = profile or DEFAULT_PROFILE
    table = data.get(name)
    if table is None:
        if profile:
            available = ", ".join(k for k, v in data.items() if isinstance(v, dict)) or "none"
            raise ConfigError(f"profile '{name}' not found in {path} (available: {available})")
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: '{name}' must be a table, e.g. [{name}]")

    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) in [{name}]: {', '.join(unknown)}")
    return table


def resolve(args) -> Settings:
    """Build Settings from parsed CLI args, the environment and the config file."""
    merged = dict(load_profile(args.config, args.profile))

    if os.environ.get("WSFTP_PASSWORD"):
        merged["password"] = os.environ["WSFTP_PASSWORD"]
    if os.environ.get("WSFTP_KEY_PASSPHRASE"):
        merged["key_passphrase"] = os.environ["WSFTP_KEY_PASSPHRASE"]

    for f in fields(Settings):
        value = getattr(args, f.name, None)
        if value is not None:
            merged[f.name] = value

    settings = Settings(**merged)
    if not settings.host:
        raise ConfigError("no host given: use --host or set 'host' in a config profile")
    try:
        settings.port = int(settings.port)
    except (TypeError, ValueError):
        raise ConfigError(f"invalid port: {settings.port!r}") from None
    if not settings.username:
        settings.username = getpass.getuser()
    return settings


def parse_since(text: str) -> float:
    """Parse a date/time into a POSIX timestamp.

    Accepts ISO-8601 forms such as '2026-11-01', '2026-11-01 14:30',
    '2026-11-01T14:30:00', '2026-11-01T14:30:00Z' or with a '+05:30' offset.
    Values without a timezone are interpreted as local time.
    """
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ConfigError(
            f"invalid date/time {text!r}: expected e.g. '2026-11-01', "
            "'2026-11-01 14:30' or '2026-11-01T14:30:00Z'"
        ) from None
    return dt.astimezone().timestamp()
