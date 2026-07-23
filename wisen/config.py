"""
Optional user config at ~/.wisen/config.toml. Environment variables always win
over the file; the file wins over built-in defaults. See config.example.toml
in the repo root for all keys.
"""
import os
import sys
import tomllib
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path.home() / ".wisen" / "config.toml"


@lru_cache(maxsize=1)
def _config() -> dict:
    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as e:
        print(f"wisen: ignoring invalid {CONFIG_PATH}: {e}", file=sys.stderr)
        return {}


def get(section: str, key: str, env_var: str, default: str = "") -> str:
    env = os.getenv(env_var, "").strip()
    if env:
        return env
    return str(_config().get(section, {}).get(key, "")).strip() or default


def root() -> Path:
    """Base dir containing the agents' dot-dirs (.claude, .codex, …).

    Defaults to the home dir; point it at a synced copy of another machine's
    dotfiles to run wisen against that machine's history.
    """
    return Path(get("paths", "root_dir", "WISEN_ROOT_DIR", "~")).expanduser()
