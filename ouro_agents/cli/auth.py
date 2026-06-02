from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, TypeVar

from platformdirs import user_config_dir


APP_NAME = "ouro-agents"
USER_API_KEY_ENV = "OURO_USER_API_KEY"
AGENT_API_KEY_ENV = "OURO_API_KEY"

# Validating a key constructs an Ouro client, which performs blocking HTTP with
# a long default read timeout. Bound it so the CLI fails fast and stays
# responsive instead of hanging if the backend is slow or unreachable.
DEFAULT_LOGIN_TIMEOUT = 20.0

T = TypeVar("T")


class LoginTimeout(RuntimeError):
    """Raised when contacting the Ouro backend exceeds the allotted time."""


@dataclass(frozen=True)
class OuroIdentity:
    user_id: str
    username: str
    email: str
    actor_type: str
    api_key_name: str = ""

    @property
    def display_name(self) -> str:
        return self.username or self.email or self.user_id


def credentials_path() -> Path:
    return Path(user_config_dir(APP_NAME)) / "credentials.json"


def load_credentials() -> dict[str, Any]:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_user_credentials(*, api_key: str, base_url: str | None = None) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"user_api_key": api_key}
    if base_url:
        payload["base_url"] = base_url
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)
    return path


def clear_user_credentials() -> bool:
    path = credentials_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _base_url_from_env_or_credentials(base_url: str | None = None) -> str | None:
    if base_url:
        return base_url
    return (
        os.getenv("OURO_BASE_URL")
        or os.getenv("OURO_BACKEND_URL")
        or load_credentials().get("base_url")
    )


def _new_ouro_client(api_key: str, *, base_url: str | None = None):
    from ouro import Ouro

    return Ouro(api_key=api_key, base_url=_base_url_from_env_or_credentials(base_url))


def get_user_api_key() -> str | None:
    return os.getenv(USER_API_KEY_ENV) or load_credentials().get("user_api_key")


def get_agent_api_key() -> str | None:
    return os.getenv(AGENT_API_KEY_ENV)


def get_user_client(*, base_url: str | None = None):
    api_key = get_user_api_key()
    if not api_key:
        raise RuntimeError(
            f"Not logged in. Run `ouro-agents login`, or set {USER_API_KEY_ENV}."
        )
    return _new_ouro_client(api_key, base_url=base_url)


def get_agent_client(*, base_url: str | None = None):
    api_key = get_agent_api_key()
    if not api_key:
        raise RuntimeError(f"{AGENT_API_KEY_ENV} is not set for the agent account.")
    return _new_ouro_client(api_key, base_url=base_url)


def validate_user_api_key(api_key: str, *, base_url: str | None = None):
    return _new_ouro_client(api_key, base_url=base_url)


def _as_mapping(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, SimpleNamespace):
        return vars(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {
        key: getattr(obj, key)
        for key in ("id", "user_id", "username", "email", "actor_type")
        if hasattr(obj, key)
    }


def resolved_base_url(base_url: str | None = None) -> str:
    """Best-effort label for the backend a login attempt will contact."""
    return _base_url_from_env_or_credentials(base_url) or "the Ouro backend"


def _run_with_timeout(work: Callable[[], T], timeout: float) -> T:
    """Run ``work`` on a daemon thread, giving up after ``timeout`` seconds.

    The Ouro SDK performs blocking network I/O during client construction with
    a long default read timeout, so we isolate it on a daemon thread. The main
    thread blocks on an interruptible ``join`` (so Ctrl+C still works) and, on
    timeout, abandons the daemon thread, which is reaped when the process exits.
    """
    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = work()
        except BaseException as exc:  # surfaced to the caller below
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise LoginTimeout(f"timed out after {timeout:.0f}s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def login_and_identify(
    api_key: str,
    *,
    base_url: str | None = None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
) -> OuroIdentity:
    """Validate ``api_key`` and read its identity, bounded by ``timeout``."""

    def _work() -> OuroIdentity:
        client = validate_user_api_key(api_key, base_url=base_url)
        return read_identity(client)

    return _run_with_timeout(_work, timeout)


def identify_account(
    factory: Callable[[], Any],
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
) -> OuroIdentity:
    """Build a client via ``factory`` and read its identity, bounded by ``timeout``."""

    def _work() -> OuroIdentity:
        return read_identity(factory())

    return _run_with_timeout(_work, timeout)


def read_identity(client) -> OuroIdentity:
    user_data = _as_mapping(getattr(client, "user", None))
    profile: dict[str, Any] = {}
    try:
        profile = client.users.me() or {}
    except Exception:
        profile = {}

    user_id = str(
        profile.get("user_id")
        or profile.get("id")
        or user_data.get("user_id")
        or user_data.get("id")
        or ""
    )
    username = str(profile.get("username") or user_data.get("username") or "")
    email = str(user_data.get("email") or profile.get("email") or "")
    actor_type = str(profile.get("actor_type") or user_data.get("actor_type") or "unknown")
    api_key_name = str(getattr(client, "api_key_name", "") or "")
    return OuroIdentity(
        user_id=user_id,
        username=username,
        email=email,
        actor_type=actor_type,
        api_key_name=api_key_name,
    )

