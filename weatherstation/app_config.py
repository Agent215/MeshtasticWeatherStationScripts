from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_PATH_OVERRIDE_VAR = "WEATHERSTATION_ENV_PATH"
FALLBACK_ENV_PATHS = (
    Path("/etc/weatherstation-home.env"),
)

_LOADED_ENV_PATHS: set[Path] = set()
_ACTIVE_ENV_PATH: Path | None = None
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def load_dotenv_file(env_path: Path = ENV_PATH) -> Path:
    global _ACTIVE_ENV_PATH

    env_path = env_path.expanduser()

    if env_path in _LOADED_ENV_PATHS:
        _ACTIVE_ENV_PATH = env_path
        return env_path

    if env_path.is_file():
        for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            key, separator, value = line.partition("=")
            if not separator:
                raise RuntimeError(f"Invalid .env entry at line {line_number}: expected KEY=VALUE")

            key = key.removeprefix("export ").strip()
            if not key:
                raise RuntimeError(f"Invalid .env entry at line {line_number}: missing key")

            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]

            os.environ.setdefault(key, value)

    if env_path.is_file():
        _LOADED_ENV_PATHS.add(env_path)
    _ACTIVE_ENV_PATH = env_path

    return env_path


def resolve_app_env_path() -> Path:
    override = os.environ.get(ENV_PATH_OVERRIDE_VAR, "").strip()
    if override:
        return Path(override).expanduser()

    if ENV_PATH.is_file():
        return ENV_PATH

    for env_path in FALLBACK_ENV_PATHS:
        if env_path.is_file():
            return env_path

    return ENV_PATH


def load_app_env() -> Path:
    return load_dotenv_file(resolve_app_env_path())


def get_active_env_path() -> Path:
    if _ACTIVE_ENV_PATH is not None:
        return _ACTIVE_ENV_PATH
    return resolve_app_env_path()


def get_optional_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default

    value = value.strip()
    if value:
        return value
    return default


def get_required_env(name: str) -> str:
    value = get_optional_env(name)
    if value is not None:
        return value
    raise RuntimeError(f"Missing required setting: {name}")


def get_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = get_optional_env(name)
    if raw_value is None:
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid integer setting for {name}: {raw_value!r}") from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(f"Invalid setting for {name}: expected >= {minimum}, got {value}")

    return value


def get_bool_env(name: str, default: bool) -> bool:
    raw_value = get_optional_env(name)
    if raw_value is None:
        return default

    normalized = raw_value.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    raise RuntimeError(f"Invalid boolean setting for {name}: {raw_value!r}")
